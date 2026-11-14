param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$appUrl = 'http://localhost:3000'

if (-not (Test-Path (Join-Path $projectDir '.env'))) {
    throw 'Missing .env. Create it from .env.example and set local secrets before running FACEMARK.'
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker is not installed or is not available on PATH.'
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $dockerDesktop)) {
        throw 'Docker Desktop is not running and its executable was not found.'
    }
    Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 2
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if (-not $ready) { throw 'Docker Desktop did not become ready within two minutes.' }
}

Push-Location $projectDir
try {
    # The PyCharm desktop run uses the browser origin that actually submits forms.
    # Keep .env unchanged; start-phone-app.ps1 remains the explicit phone mode.
    $env:APP_ORIGIN = $appUrl
    & docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose could not start FACEMARK.' }

    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "$appUrl/api/health" -TimeoutSec 5
            if ($response.StatusCode -eq 200 -and $response.Content -match '"status":"ok"') {
                $healthy = $true
                break
            }
        } catch {
            # The frontend or backend may still be starting.
        }
        Start-Sleep -Seconds 2
    }
    if (-not $healthy) {
        & docker compose ps
        throw 'FACEMARK did not become healthy at the same-origin /api/health endpoint.'
    }

    Write-Host "FACEMARK is ready at $appUrl"
    if (-not $NoBrowser) { Start-Process $appUrl }
} finally {
    Pop-Location
}
