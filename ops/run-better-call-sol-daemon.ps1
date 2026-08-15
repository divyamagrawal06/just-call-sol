$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

$env:HOTLINE_TRANSPORT = "fake"
$env:HOTLINE_ALLOW_CODEX_WRITES = "true"
$env:HOTLINE_DEMO_AUTO_EXECUTE_ACTIONS = "true"
$env:HOTLINE_SHOW_SPAWNED_CODEX_TASKS = "false"
$env:HOTLINE_WORKSPACE_ROOTS = $repoRoot
$env:PUBLIC_BASE_URL = "https://typing-excerpt-kennedy-herein.trycloudflare.com"

foreach ($name in @("VAPI_WEBHOOK_TOKEN", "VAPI_ASSISTANT_ID", "VAPI_PHONE_NUMBER_ID")) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing required Windows user environment variable: $name"
    }
    Set-Item -Path "Env:$name" -Value $value
}

& uv run agent-hotline serve --host 127.0.0.1 --port 8788
exit $LASTEXITCODE
