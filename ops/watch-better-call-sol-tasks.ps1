$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot
$Host.UI.RawUI.WindowTitle = "Better Call Sol - Live Codex Tasks"

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$watcher = Join-Path $repoRoot "ops\watch_better_call_sol_tasks.py"
$database = Join-Path $repoRoot ".hotline\hotline.db"

& $python $watcher --database $database
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Read-Host "Task feed stopped unexpectedly. Press Enter to close"
}
