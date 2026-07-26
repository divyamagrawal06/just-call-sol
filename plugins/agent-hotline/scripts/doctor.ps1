$ErrorActionPreference = "Stop"

if (-not (Get-Command agent-hotline -ErrorAction SilentlyContinue)) {
    throw "agent-hotline is not installed. Run: uv tool install --editable <repository-root>"
}

agent-hotline doctor
