"""
Windows service wrapper bundling the whole NSE momentum trading app -- the
Streamlit dashboard (always-on, auto-restarted if it crashes) and the daily
rebalance scan (run internally on a schedule) -- behind one service,
controlled with a single command:

    trading-app install     # registers the service, sets auto-start on boot
    trading-app start       # or: net start NSEMomentumTradingApp
    trading-app stop        # or: net stop NSEMomentumTradingApp
    trading-app restart
    trading-app status      # custom: service state + dashboard port + last scan
    trading-app remove      # unregisters the service

Needs an elevated (Administrator) command prompt for install/remove/start/
stop, same as any Windows service. Once installed, the service starts
automatically on machine reboot -- no need to run `trading-app start`
yourself after a restart.

Supersedes the standalone "NSE-Momentum-DailyRebalanceScan" Task Scheduler
entry -- the scheduler loop below runs the exact same command
(`python.exe live_rebalance.py`, at the same 16:00 IST weekday trigger) that
task used, now folded into this one service. That old task isn't removed
automatically (see README) -- both would otherwise run the scan twice a day.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys
import threading
import time

import servicemanager
import win32event
import win32service
import win32serviceutil

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# NOT sys.executable -- inside a running service, pywin32 hosts the process
# via its own pythonservice.exe (it only understands a `-debug servicename`
# argument, not `-m streamlit run ...`), so sys.executable resolves to THAT,
# not the real interpreter. Subprocess launches silently failed (exit code 2,
# crash-looping every 5s) until this was caught by reading
# cache/service_dashboard_stdout.txt after `trading-app status` reported the
# dashboard port never came up. sys.exec_prefix is still correct either way,
# since Python derives it from the standard-library location, not the hosting
# executable's name.
PYTHON_EXE = os.path.join(sys.exec_prefix, "python.exe")
CACHE_DIR = os.path.join(PROJECT_DIR, "cache")
LOG_PATH = os.path.join(CACHE_DIR, "service_log.txt")

REBALANCE_HOUR, REBALANCE_MINUTE = 16, 0  # matches the Task Scheduler entry this replaces
DASHBOARD_PORT = 8501


def _log(msg: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    try:
        servicemanager.LogInfoMsg(line)
    except Exception:
        pass  # the Windows event log API isn't available outside the SCM context (e.g. `debug` mode)


class TradingAppService(win32serviceutil.ServiceFramework):
    _svc_name_ = "NSEMomentumTradingApp"
    _svc_display_name_ = "NSE Momentum Trading App"
    _svc_description_ = ("Runs the NSE momentum Streamlit dashboard and the "
                         "daily 4 PM rebalance scan on a schedule.")

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 1, 0, None)
        self.running = True
        self.dashboard_proc: subprocess.Popen | None = None
        self.rebalance_proc: subprocess.Popen | None = None
        self._last_rebalance_date: dt.date | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        _log("Stop requested.")
        self.running = False
        win32event.SetEvent(self.stop_event)
        for proc in (self.dashboard_proc, self.rebalance_proc):
            if proc and proc.poll() is None:
                proc.terminate()

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        _log("Service starting.")
        self._start_dashboard()
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        self._supervise_loop()
        _log("Service stopped.")

    def _start_dashboard(self) -> None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        stdout_log = open(os.path.join(CACHE_DIR, "service_dashboard_stdout.txt"), "a")
        self.dashboard_proc = subprocess.Popen(
            [PYTHON_EXE, "-m", "streamlit", "run", "dashboard.py"],
            cwd=PROJECT_DIR, stdout=stdout_log, stderr=subprocess.STDOUT)
        _log(f"Dashboard started (pid {self.dashboard_proc.pid}).")

    def _supervise_loop(self) -> None:
        """Restarts the dashboard if it crashes; returns once SvcStop signals."""
        while self.running:
            rc = win32event.WaitForSingleObject(self.stop_event, 5000)
            if rc == win32event.WAIT_OBJECT_0:
                break
            if self.dashboard_proc and self.dashboard_proc.poll() is not None:
                _log(f"Dashboard exited (code {self.dashboard_proc.returncode}) -- restarting.")
                time.sleep(5)
                if self.running:
                    self._start_dashboard()

    def _scheduler_loop(self) -> None:
        """Runs live_rebalance.py once a day at REBALANCE_HOUR:REBALANCE_MINUTE
        on weekdays -- the same command/schedule the standalone
        NSE-Momentum-DailyRebalanceScan Task Scheduler entry used to run."""
        while self.running:
            now = dt.datetime.now()
            due = (now.weekday() < 5 and now.hour == REBALANCE_HOUR
                  and now.minute >= REBALANCE_MINUTE
                  and self._last_rebalance_date != now.date())
            if due:
                _log("Running scheduled rebalance scan.")
                stdout_log = open(os.path.join(CACHE_DIR, "service_rebalance_stdout.txt"), "a")
                self.rebalance_proc = subprocess.Popen(
                    [PYTHON_EXE, "live_rebalance.py"], cwd=PROJECT_DIR,
                    stdout=stdout_log, stderr=subprocess.STDOUT)
                self.rebalance_proc.wait()
                self._last_rebalance_date = now.date()
                _log(f"Rebalance scan finished (code {self.rebalance_proc.returncode}).")
            if win32event.WaitForSingleObject(self.stop_event, 30_000) == win32event.WAIT_OBJECT_0:
                break


_STATE_NAMES = {
    win32service.SERVICE_RUNNING: "RUNNING",
    win32service.SERVICE_STOPPED: "STOPPED",
    win32service.SERVICE_START_PENDING: "START_PENDING",
    win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
    win32service.SERVICE_PAUSED: "PAUSED",
}


def _print_status() -> None:
    try:
        state = win32serviceutil.QueryServiceStatus(TradingAppService._svc_name_)[1]
        print(f"Service state: {_STATE_NAMES.get(state, state)}")
    except Exception as e:
        print(f"Service not installed or not queryable ({e}).")
        print("Run 'trading-app install' first (from an Administrator prompt).")
        return

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        reachable = s.connect_ex(("127.0.0.1", DASHBOARD_PORT)) == 0
    print(f"Dashboard port {DASHBOARD_PORT}: {'listening' if reachable else 'not reachable'}")

    try:
        sys.path.insert(0, PROJECT_DIR)
        import state_db
        # state_db.DB_PATH is relative ("cache/state.db") -- fine for
        # dashboard.py/live_rebalance.py, which always run with cwd=
        # PROJECT_DIR (see _start_dashboard/_scheduler_loop above), but
        # `trading-app status` runs from wherever the CALLER's shell happens
        # to be sitting, so a bare relative path resolved a DIFFERENT (and
        # non-existent) database there -- silently creating a stray empty
        # cache/state.db in the caller's cwd and reporting "none recorded
        # yet" regardless of what the real database actually has. Pointing
        # DB_PATH at the real absolute location fixes both.
        state_db.DB_PATH = os.path.join(PROJECT_DIR, "cache", "state.db")
        last = state_db.get_last_rebalance_run()
        print(f"Last rebalance scan: {last['run_time']:%d %b %Y %H:%M}" if last
             else "Last rebalance scan: none recorded yet")
    except Exception as e:
        print(f"Could not read last rebalance run: {e}")


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "status":
        _print_status()
        return
    # pywin32's arg parser (getopt, non-GNU) requires options like --startup
    # before the verb -- inject it there automatically so a plain
    # `trading-app install` sets auto-start-on-boot without the caller
    # needing to know/remember the flag order. Explicitly pass --startup
    # yourself (before "install") to override, e.g. `trading-app --startup
    # manual install` to opt out of auto-start.
    if argv and argv[-1] == "install" and not any(a.startswith("--startup") for a in argv):
        argv = ["--startup", "auto"] + argv
    win32serviceutil.HandleCommandLine(TradingAppService, argv=[sys.argv[0]] + argv)


if __name__ == "__main__":
    main()
