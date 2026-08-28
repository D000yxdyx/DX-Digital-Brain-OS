Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"
$logRoot = Join-Path $projectRoot "runtime\logs"
$apiLog = Join-Path $logRoot "control-ui-api.out.log"
$apiErrorLog = Join-Path $logRoot "control-ui-api.err.log"
$uiLog = Join-Path $logRoot "control-ui-vite.out.log"
$uiErrorLog = Join-Path $logRoot "control-ui-vite.err.log"

function Read-DotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if (-not $line) {
        throw "Missing $name in $envPath"
    }
    return $line.Substring($name.Length + 1)
}

function Set-OptionalDotEnvValue([string]$name) {
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) {
        Set-Item -Path ("Env:" + $name) -Value $line.Substring($name.Length + 1)
    }
}

function Test-LocalPort([int]$port) {
    return $null -ne (Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python environment not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing local configuration: $envPath"
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    throw "npm.cmd was not found on PATH"
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

# Keep credentials in the local .env file; child processes inherit them in memory.
$env:DX_DATABASE_URL = Read-DotEnvValue "DX_DATABASE_URL_API"
$env:DX_ROOT_API_KEY = Read-DotEnvValue "DX_ROOT_API_KEY"
$env:PYTHONPATH = Join-Path $projectRoot "src"
foreach ($optionalName in @(
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
        "DX_LLM_PLANNER_PROVIDER", "DX_LLM_REVIEWER_PROVIDER", "DX_LLM_FALLBACK_PROVIDER",
        "DX_LLM_LIVE_ENABLED", "DX_LLM_TIMEOUT_SECONDS", "DX_LLM_MAX_RETRIES"
    )) {
    Set-OptionalDotEnvValue $optionalName
}

if (-not (Test-LocalPort 8000)) {
    Start-Process -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "dxbrain.api.app:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Minimized `
        -RedirectStandardOutput $apiLog `
        -RedirectStandardError $apiErrorLog | Out-Null
}

if (-not (Test-LocalPort 5173)) {
    Start-Process -FilePath $npmCommand.Source `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") `
        -WorkingDirectory (Join-Path $projectRoot "apps\control-ui") `
        -WindowStyle Minimized `
        -RedirectStandardOutput $uiLog `
        -RedirectStandardError $uiErrorLog | Out-Null
}

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $api = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 1
        $ui = Invoke-WebRequest -Uri "http://127.0.0.1:5173/" -UseBasicParsing -TimeoutSec 1
        if ($api.StatusCode -eq 200 -and $ui.StatusCode -eq 200) {
            Start-Process "http://127.0.0.1:5173/"
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

Start-Process "http://127.0.0.1:5173/"
Write-Warning "Services did not both report ready within 15 seconds. Check runtime\logs if the page is unavailable."
