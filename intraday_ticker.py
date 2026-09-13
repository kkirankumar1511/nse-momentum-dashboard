"""
Live tick feed for the intraday engine -- wraps Kite Connect's WebSocket
ticker (KiteTicker) so entry-trigger/stop/target checks react to real
market ticks instead of polling the LTP REST endpoint every few seconds.
The engine previously polled kite_client.get_ltp() every POLL_SECONDS
(7s), meaning every trigger/stop/target event could be detected up to
7 seconds late; Spec.md §6.2 explicitly calls for these to be
tick-driven rather than candle- or poll-driven, for exactly this reason.

The KiteTicker connection runs in its own background thread (connect
(threaded=True)); on_ticks() only ever writes into a lock-protected
dict, so the engine's own decision loop never blocks on network I/O --
it just reads whatever the most recent tick left in the cache. Kite's
own client handles reconnection (reconnect=True by default).
"""
from __future__ import annotations

import datetime as dt
import threading

from kiteconnect import KiteTicker

import config


class LiveTicker:
    """Subscribes (in MODE_LTP -- last-traded-price only, the lightest
    subscription mode, plenty for trigger/stop/target checks) to a fixed
    set of instrument tokens for the life of the trading day and keeps
    the latest price for each in memory.

    symbol_by_token: {instrument_token: "SYMBOL"} -- subscription itself
    is by token (Kite's ticker protocol has no symbol-based API); this
    mapping is kept only for get_ltp_by_symbol()/logging convenience.
    """

    def __init__(self, symbol_by_token: dict[int, str]):
        self.symbol_by_token = dict(symbol_by_token)
        self.token_by_symbol = {sym: tok for tok, sym in symbol_by_token.items()}
        self.tokens = list(symbol_by_token)
        self._lock = threading.Lock()
        self._ltp: dict[int, float] = {}
        self._last_tick_at: dict[int, dt.datetime] = {}
        self._connected = threading.Event()
        self.kws = KiteTicker(config.KITE_API_KEY, config.KITE_ACCESS_TOKEN)
        self.kws.on_ticks = self._on_ticks
        self.kws.on_connect = self._on_connect
        self.kws.on_close = self._on_close
        self.kws.on_error = self._on_error
        self.kws.on_reconnect = self._on_reconnect

    def start(self, timeout: float = 15.0) -> None:
        """Connects in a background thread and blocks until the first
        on_connect callback fires (or `timeout` elapses -- the engine
        logs a warning and carries on rather than hanging forever, so a
        slow/failed WS handshake doesn't wedge the whole trading day)."""
        self.kws.connect(threaded=True)
        if not self._connected.wait(timeout=timeout):
            print(f"[intraday_ticker] WARNING: no connect callback within {timeout}s "
                 "-- ticks may not be arriving yet.")

    def stop(self) -> None:
        try:
            self.kws.close()
        except Exception:
            pass

    def _on_connect(self, ws, response) -> None:
        ws.subscribe(self.tokens)
        ws.set_mode(ws.MODE_LTP, self.tokens)
        self._connected.set()
        print(f"[intraday_ticker] connected, subscribed to {len(self.tokens)} tokens "
             f"({', '.join(self.symbol_by_token.values())})")

    def _on_close(self, ws, code, reason) -> None:
        print(f"[intraday_ticker] closed: {code} {reason}")

    def _on_error(self, ws, code, reason) -> None:
        print(f"[intraday_ticker] error: {code} {reason}")

    def _on_reconnect(self, ws, attempts_count) -> None:
        print(f"[intraday_ticker] reconnecting (attempt {attempts_count})...")

    def _on_ticks(self, ws, ticks) -> None:
        now = dt.datetime.now()
        with self._lock:
            for t in ticks:
                token = t["instrument_token"]
                self._ltp[token] = t["last_price"]
                self._last_tick_at[token] = now

    def get_ltp(self, token: int) -> float | None:
        with self._lock:
            return self._ltp.get(token)

    def get_ltp_by_symbol(self, symbol: str) -> float | None:
        token = self.token_by_symbol.get(symbol)
        return self.get_ltp(token) if token is not None else None

    def last_tick_age(self, token: int) -> float | None:
        """Seconds since the last tick for `token`, or None if none has
        arrived yet -- lets a caller fall back to a REST get_ltp() call
        if the feed has gone stale (e.g. mid-reconnect) instead of
        silently acting on a price that's minutes old."""
        with self._lock:
            ts = self._last_tick_at.get(token)
        return (dt.datetime.now() - ts).total_seconds() if ts else None
