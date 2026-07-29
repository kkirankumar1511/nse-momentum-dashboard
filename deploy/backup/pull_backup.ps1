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
# Setup:
#   1. Fill in $VpsHost / $VpsUser below once the VPS is active.
#   2. Set up SSH key-based auth to the VPS (ssh-copy-id equivalent, or
#      manually append your public key to the VPS's ~/.ssh/authorized_keys)
#      so this can run unattended without a password prompt.
#   3. Register as a daily Task Scheduler task, timed AFTER the VPS's own
#      backup_db.sh cron job (e.g. 30-60 min later):
#        schtasks /create /tn "NSE Backup Pull" /tr "powershell.exe -File \"E:\trading-workspace\nse-momentum-dashboard\deploy\backup\pull_backup.ps1\"" /sc daily /st 00:30

$VpsHost = "<vps-tailscale-ip-or-hostname>"
$VpsUser = "<ssh-username>"
$RemoteBackupDir = "/opt/nse-momentum-dashboard/backups"
$LocalBackupDir = "E:\trading-backups\nse-momentum-dashboard"
$RetentionDays = 60

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
    $output = & scp -p "${VpsUser}@${VpsHost}:${RemoteBackupDir}/state_*.db.gz.age" $LocalBackupDir 2>&1
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
Write-Log "Rotation complete -- keeping last $RetentionDays days locally."
