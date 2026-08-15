$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$npx = (Get-Command npx.cmd -ErrorAction Stop).Source

# localtunnel sometimes closes a healthy-looking session with exit code 0.
# Keep the scheduled task alive so that both clean disconnects and failures
# reconnect without waiting for the next Windows logon.
while ($true) {
    & $npx --yes localtunnel --port 8788 --subdomain better-call-sol
    Start-Sleep -Seconds 2
}
