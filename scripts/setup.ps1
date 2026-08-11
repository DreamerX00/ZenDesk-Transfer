# =============================================================================
#  zd-transfer — Truly Automated Setup for Windows (PowerShell)
#  =============================================================================
#  This script installs everything needed to run the Zendesk Configuration
#  Transfer tool, including Docker Desktop, Python, and the Docker Compose
#  stack. Run this in PowerShell as Administrator once after cloning the repo.
#
#  Usage:
#    Right-click → "Run with PowerShell" (as Administrator)
#    OR:
#    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ROOT

function Write-Info  { Write-Host "[INFO]  $args" -ForegroundColor Cyan }
function Write-Ok    { Write-Host "[OK]    $args" -ForegroundColor Green }
function Write-Warn  { Write-Host "[WARN]  $args" -ForegroundColor Yellow }
function Write-Crit  { Write-Host "[ERROR] $args" -ForegroundColor Red }

# Determine the docker command (account for possible alias or PATH issues)
$DOCKER_CMD = "docker"

# ------------------------------------------------------------------
#  1. Docker Desktop
# ------------------------------------------------------------------
Write-Info "Checking Docker Desktop..."
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    $ver = docker --version
    Write-Ok "Docker already installed: $ver"
} else {
    Write-Info "Docker Desktop not found. Downloading..."
    $installer = "$env:TEMP\DockerDesktopInstaller.exe"
    Invoke-WebRequest -Uri "https://desktop.docker.com/win/stable/Docker%20Desktop%20Installer.exe" -OutFile $installer
    Write-Info "Running Docker Desktop installer..."
    Start-Process -Wait -FilePath $installer -ArgumentList "install", "--accept-license", "--quiet"
    Write-Ok "Docker Desktop installed. You may need to restart your computer."
    Write-Info "After restart, ensure Docker Desktop is running before proceeding."
}

# ------------------------------------------------------------------
#  2. Docker Compose
# ------------------------------------------------------------------
Write-Info "Checking Docker Compose..."
try {
    $composeVer = & $DOCKER_CMD compose version
    Write-Ok "Docker Compose available: $composeVer"
} catch {
    Write-Warn "Docker Compose not found as Docker plugin."
    Write-Warn "Please install Docker Desktop 4.0+ which includes Compose v2."
}

# ------------------------------------------------------------------
#  3. Python 3.10+
# ------------------------------------------------------------------
Write-Info "Checking Python..."

function Test-PythonOk {
    # True only if `python` exists AND is >= 3.10. Also rejects the Windows
    # Store `python` alias stub (it prints nothing / opens the Store, so the
    # version parse fails and we fall through to a real install).
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { return $false }
    try {
        $raw = (python --version 2>&1) -replace '^Python\s+', ''
        $v = [version]($raw -replace '(\d+\.\d+(\.\d+)?).*', '$1')
        return ($v.Major -gt 3) -or ($v.Major -eq 3 -and $v.Minor -ge 10)
    } catch {
        return $false
    }
}

if (-not (Test-PythonOk)) {
    Write-Info "Python 3.10+ not found. Downloading Python 3.12..."
    $installer = "$env:TEMP\python-installer.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $installer
    Start-Process -Wait -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1"
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    Write-Ok "Python 3.12 installed"
} else {
    $ver = python --version
    Write-Ok "Python $ver — OK"
}

# ------------------------------------------------------------------
#  4. Python virtual environment
# ------------------------------------------------------------------
Write-Info "Setting up Python virtual environment..."
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .\.venv\Scripts\Activate.ps1
Write-Info "Installing Python dependencies..."
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt
Write-Ok "Python dependencies installed"

# ------------------------------------------------------------------
#  5. Config .env files
# ------------------------------------------------------------------
Write-Info "Setting up configuration files..."
if (-not (Test-Path "config\source.env")) {
    Copy-Item config\source.env.example config\source.env
    Write-Info "  Created config\source.env from template"
} else {
    Write-Info "  config\source.env already exists — keeping existing"
}

if (-not (Test-Path "config\target.env")) {
    Copy-Item config\target.env.example config\target.env
    Write-Info "  Created config\target.env from template"
} else {
    Write-Info "  config\target.env already exists — keeping existing"
}

# ------------------------------------------------------------------
#  6. Generate Docker secrets
# ------------------------------------------------------------------
Write-Info "Generating cryptographic secrets..."
# Generate HMAC secret using Python
$hmacSecret = python -c "import secrets; print(secrets.token_hex(32))"
$fernetKey = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

if (-not (Test-Path "docker\.env")) {
    Copy-Item docker\.env.example docker\.env
    Write-Info "  Created docker\.env from template"
}

function Set-EnvValue {
    param(
        [string]$Content,
        [string]$Key,
        [string]$Value
    )

    if ($Content -match "(?m)^$Key=") {
        return [System.Text.RegularExpressions.Regex]::Replace(
            $Content,
            "(?m)^$Key=.*$",
            "$Key=$Value"
        )
    }

    if (-not $Content.EndsWith("`n")) {
        $Content += "`r`n"
    }
    return $Content + "$Key=$Value`r`n"
}

$dockerEnv = Get-Content "docker\.env" -Raw
$currentHmac = [System.Text.RegularExpressions.Regex]::Match($dockerEnv, "(?m)^ZDX_HMAC_SECRET=(.*)$").Groups[1].Value.Trim()
$currentFernet = [System.Text.RegularExpressions.Regex]::Match($dockerEnv, "(?m)^ZDX_FERNET_KEY=(.*)$").Groups[1].Value.Trim()
$updatedSecrets = $false

if ([string]::IsNullOrWhiteSpace($currentHmac) -or $currentHmac -eq "change_me") {
    $dockerEnv = Set-EnvValue -Content $dockerEnv -Key "ZDX_HMAC_SECRET" -Value $hmacSecret
    $updatedSecrets = $true
}
if ([string]::IsNullOrWhiteSpace($currentFernet) -or $currentFernet -eq "change_me") {
    $dockerEnv = Set-EnvValue -Content $dockerEnv -Key "ZDX_FERNET_KEY" -Value $fernetKey
    $updatedSecrets = $true
}

if ($updatedSecrets) {
    Set-Content "docker\.env" -Value $dockerEnv
    Write-Ok "Docker secrets generated and written to docker\.env"
} else {
    Write-Info "  docker\.env already has secrets — keeping existing"
}

$dockerEnv = Get-Content "docker\.env" -Raw
$dockerEnv = Set-EnvValue -Content $dockerEnv -Key "ZDX_STANDALONE_MODE" -Value "1"
# Dev mode allows the standalone session endpoint to accept requests from the
# Docker bridge IP (172.20.0.x) in addition to loopback. Without this the
# browser gets a 403 because Docker routes host→container traffic through the
# bridge interface, not 127.0.0.1.
$dockerEnv = Set-EnvValue -Content $dockerEnv -Key "ZDX_DEV_MODE" -Value "1"
Set-Content "docker\.env" -Value $dockerEnv
Write-Ok "Configured ZDX_STANDALONE_MODE=1 and ZDX_DEV_MODE=1 for local browser access"

# ------------------------------------------------------------------
#  7. Create required directories
# ------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path state, exports, backups | Out-Null

# ------------------------------------------------------------------
#  8. Build and start Docker stack
# ------------------------------------------------------------------
Write-Info "Building and starting Docker containers..."
Write-Info "  (This may take a few minutes the first time)..."
& $DOCKER_CMD compose -f docker\docker-compose.yml --env-file docker\.env up -d --build
Write-Ok "Docker stack is running"

# ------------------------------------------------------------------
#  9. Verify health
# ------------------------------------------------------------------
Write-Info "Waiting for backend to become healthy..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8080/api/v1/health" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Ok "Backend is healthy at http://localhost:8080"
            $healthy = $true
            break
        }
    } catch {
        # Not ready yet
    }
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Write-Warn "Backend health check timed out. Check logs: $DOCKER_CMD compose -f docker\docker-compose.yml logs backend"
}

# ------------------------------------------------------------------
#  Summary
# ------------------------------------------------------------------
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║  zd-transfer — Setup Complete!                          ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  CLI usage:" -ForegroundColor Cyan
Write-Host "    .\.venv\Scripts\Activate.ps1"
Write-Host "    python main.py pre-flight"
Write-Host ""
Write-Host "  Web UI:" -ForegroundColor Cyan
Write-Host "    Open http://localhost:8080/ in your browser"
Write-Host ""
Write-Host "  Docker commands:" -ForegroundColor Cyan
Write-Host "    $DOCKER_CMD compose -f docker\docker-compose.yml logs -f backend"
Write-Host "    $DOCKER_CMD compose -f docker\docker-compose.yml down"
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Yellow
Write-Host "    1. Edit config\source.env with your source Zendesk credentials"
Write-Host "    2. Edit config\target.env with your target Zendesk credentials"
Write-Host "    3. Run: python main.py pre-flight"
Write-Host "    4. Run: python main.py migrate"
Write-Host ""
