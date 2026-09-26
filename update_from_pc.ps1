# Daily WNBA data update, run from this PC by Windows Task Scheduler.
# stats.wnba.com doesn't answer GitHub's servers, so the update runs here instead
# and pushes data/wnba_stats.json to GitHub.
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$git = 'C:\Program Files\Git\cmd\git.exe'
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$log = Join-Path $repo 'update_from_pc.log'

Set-Location $repo
Start-Transcript -Path $log -Append | Out-Null
try {
    Write-Output "=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') ==="
    & $git pull --rebase --quiet origin main
    if ($LASTEXITCODE) { throw "git pull failed" }
    & $python fetch_wnba.py
    if ($LASTEXITCODE) { throw "fetch_wnba.py failed" }
    & $git add data/wnba_stats.json
    & $git diff --staged --quiet
    if ($LASTEXITCODE) {
        & $git commit --quiet -m "Update WNBA stats $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')) UTC"
        & $git push --quiet origin main
        if ($LASTEXITCODE) { throw "git push failed" }
        Write-Output "Pushed new data."
    } else {
        Write-Output "No data changes."
    }
} finally {
    Stop-Transcript | Out-Null
}
