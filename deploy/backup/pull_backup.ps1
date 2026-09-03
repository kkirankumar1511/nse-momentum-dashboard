# Pulls the latest encrypted SQLite backups from the VPS daily, over
# Tailscale, via the SSH SERVER the VPS already needs for admin access --
# this machine only needs an SSH CLIENT (ships with Windows 10/11 by
# default), so nothing new is exposed on this PC.
#
# Deliberately a PULL (this machine reaches out to the VPS), not a PUSH
# (VPS reaching into this PC) -- avoids needing an SSH server running on
# your personal Windows machine at all.
#
# This script only copies the encrypted .age files down -- it does NOT
# decrypt them. Decryption only matters if/when you actually need to
# restore a backup (rare), at which point install age
# (https://github.com/FiloSottile/age/releases) and run:
#   age -d -i <private-key-file> -o state_restored.db.gz state_TIMESTAMP.db.gz.age
#   gunzip state_restored.db.gz
#
# Setup (done 2026-07-29 -- keeping this for reference/re-setup on another PC):
#   1. $VpsHost/$VpsUser/$SshKeyPath below already point at the real VPS.
#   2. SSH key-based auth: ssh-keygen -t ed25519 -f ~/.ssh/nse_backup_key -N ""
#      then appended the .pub key to the VPS's root ~/.ssh/authorized_keys --
#      passwordless, so this can run unattended via Task Scheduler.
#   3. Registered as a Task Scheduler task with TWO triggers -- daily at
#      17:00 IST (the laptop is reliably on then; the VPS's own backup_db.sh
#      cron runs 16:45, just before), PLUS "at log on" as a catch-up in case
#      5 PM was missed (laptop off/asleep) -- the script is idempotent
#      (scp just re-pulls whatever's new), so an extra run costs nothing.
#      See the PowerShell Register-ScheduledTask block used for this (not
#      the simpler schtasks CLI, which can't cleanly express two triggers
#      on one task):
#        $t1 = New-ScheduledTaskTrigger -Daily -At 5:00PM
#        $t2 = New-ScheduledTaskTrigger -AtLogOn
#        $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-ExecutionPolicy Bypass -File "E:\trading-workspace\nse-momentum-dashboard\deploy\backup\pull_backup.ps1"'
#        Register-ScheduledTask -TaskName "NSE Backup Pull" -Trigger $t1,$t2 -Action $a -RunLevel Limited -Force
#
#      Two real bugs hit 2026-07-31, both causing a silent failure the
#      instant Task Scheduler fired the task (exit code 1, before this
#      script's own logging ever ran, so nothing showed in pull_backup.log
#      at all -- only visible via `schtasks /query /tn "NSE Backup Pull" /v`
#      showing Last Result 1, or by reproducing it live with `schtasks /run`):
#        - RunLevel was "Highest" (requests UAC elevation) -- a background-
#          triggered task can't answer a UAC prompt, so it died before
#          starting. This script never needed admin rights (just scp to a
#          data drive) -- fixed to RunLevel Limited above.
#        - Task Scheduler launches a FRESH powershell.exe with no execution
#          policy override, and LocalMachine's policy here is Undefined
#          (defaults to Restricted) -- an unsigned .ps1 gets refused outright.
#          An already-interactive shell can have Process-scope Bypass set,
#          which is why running the .ps1 directly always "worked" and masked
#          this. Fixed by adding -ExecutionPolicy Bypass to the action's
#          arguments (scoped to just this task, not a machine-wide policy
#          change) -- see the Argument string above.
#      If re-registering this task, both fixes are already baked into the
#      Register-ScheduledTask block above -- just run it as shown.

$VpsHost = "100.117.150.114"
$VpsUser = "root"
$SshKeyPath = "$env:USERPROFILE\.ssh\nse_backup_key"
$RemoteBackupDir = "/opt/nse-momentum-dashboard/backups"
$LocalBackupDir = "D:\trading-db-backup"
# D:\trading-db-backup is itself synced to Google Drive via Drive for
# Desktop's "back up this folder" feature (set up 2026-09-03) -- so this
# one retention number IS the Google Drive retention too, not just local.
# No separate copy step needed: whatever lands/gets pruned here, Drive
# mirrors automatically in the background.
$RetentionDays = 30

New-Item -ItemType Directory -Force -Path $LocalBackupDir | Out-Null
$LogFile = Join-Path $LocalBackupDir "pull_backup.log"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Output $line
    Add-Content -Path $LogFile -Value $line
}

if ($VpsHost -like "<*>") {
    Write-Log "FAILED -- placeholder VpsHost/VpsUser not filled in yet."
    exit 1
}

try {
    $output = & scp -i $SshKeyPath -p "${VpsUser}@${VpsHost}:${RemoteBackupDir}/state_*.db.gz.age" $LocalBackupDir 2>&1
    $output | ForEach-Object { Write-Log $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "FAILED -- scp exited with code $LASTEXITCODE"
        exit 1
    }
    Write-Log "OK -- pull complete."
} catch {
    Write-Log "FAILED -- $($_.Exception.Message)"
    exit 1
}

$cutoff = (Get-Date).AddDays(-$RetentionDays)
Get-ChildItem -Path $LocalBackupDir -Filter "state_*.db.gz.age" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Write-Log "Pruning old local backup: $($_.Name)"
        Remove-Item $_.FullName -Force
    }
Write-Log "Rotation complete -- keeping last $RetentionDays days locally (and on Google Drive, via Drive for Desktop's folder backup)."
