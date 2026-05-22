#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

export DEBIAN_FRONTEND=noninteractive

# Detect package manager
if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
    PKG_INSTALL="sudo dnf install -y"
    PKG_GROUP="sudo dnf groupinstall -y"
    PYTHON_PKG="python3 python3-pip python3-virtualenv"
elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
    PKG_INSTALL="sudo yum install -y"
    PKG_GROUP="sudo yum groupinstall -y"
    PYTHON_PKG="python3 python3-pip python3-virtualenv"
elif command -v zypper >/dev/null 2>&1; then
    PKG_MGR="zypper"
    PKG_INSTALL="sudo zypper install -y"
    PKG_GROUP=""
    PYTHON_PKG="python3 python3-pip python3-virtualenv"
else
    log_error "No supported package manager found (dnf, yum, zypper)."
    log_error "This script supports RHEL, CentOS, Fedora, and openSUSE."
    exit 1
fi

log_info "Detected package manager: $PKG_MGR"

# ------------------------------------------------------------------
#  Pre-flight: curl
# ------------------------------------------------------------------
log_info "Checking for curl…"
if ! command -v curl >/dev/null 2>&1; then
    log_warn "curl not found. Installing curl…"
    $PKG_INSTALL curl
    log_ok "curl installed."
else
    log_ok "curl is already installed."
fi

# ------------------------------------------------------------------
#  1. Docker
# ------------------------------------------------------------------
log_info "Checking Docker…"
if command -v docker >/dev/null 2>&1; then
    log_ok "Docker already installed: $(docker --version)"
else
    log_warn "Docker not found. Installing Docker Engine…"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    sudo systemctl enable docker 2>/dev/null || true
    sudo systemctl start docker 2>/dev/null || true
    log_ok "Docker installed."
fi

DOCKER_CMD="docker"
if ! docker ps >/dev/null 2>&1; then
    if sudo docker ps >/dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
        log_info "Using 'sudo docker' for this session (group changes pending logout/login)."
    else
        log_error "Docker is installed but the daemon is not running."
        exit 1
    fi
fi

# ------------------------------------------------------------------
#  2. Docker Compose plugin
# ------------------------------------------------------------------
log_info "Checking Docker Compose…"
if docker compose version >/dev/null 2>&1; then
    log_ok "Docker Compose already installed: $(docker compose version)"
else
    log_warn "Docker Compose plugin not found. Installing…"
    $PKG_INSTALL docker-compose-plugin
    log_ok "Docker Compose plugin installed."
fi

# ------------------------------------------------------------------
#  3. Python 3.10+
# ------------------------------------------------------------------
log_info "Checking Python…"
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" --version 2>&1 | awk '{print $2}')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -n "$PYTHON" ]; then
    log_ok "Python $("$PYTHON" --version) — OK"
else
    log_warn "Python 3.10+ not found. Installing…"
    $PKG_INSTALL $PYTHON_PKG
    PYTHON="python3"
    log_ok "Python $($PYTHON --version) installed"
fi

# ------------------------------------------------------------------
#  4. Python virtual environment + dependencies
# ------------------------------------------------------------------
log_info "Setting up Python virtual environment…"
if [ ! -d ".venv" ]; then
    $PYTHON -m venv .venv
fi
source .venv/bin/activate
log_info "Installing Python dependencies…"
pip install -q --upgrade pip
pip install -q -r requirements.txt
log_ok "Python dependencies installed"

# ------------------------------------------------------------------
#  5. Config .env files
# ------------------------------------------------------------------
log_info "Setting up configuration files…"

mkdir -p config
for env_file in source target; do
    if [ ! -f "config/${env_file}.env" ]; then
        if [ -f "config/${env_file}.env.example" ]; then
            cp "config/${env_file}.env.example" "config/${env_file}.env"
            log_info "  Created config/${env_file}.env from template"
        else
            touch "config/${env_file}.env"
            log_info "  Created empty config/${env_file}.env"
        fi
    else
        log_info "  config/${env_file}.env already exists — keeping existing"
    fi
done

# ------------------------------------------------------------------
#  6. Generate Docker secrets
# ------------------------------------------------------------------
log_info "Ensuring cryptographic secrets exist in docker/.env…"

mkdir -p docker
if [ ! -f "docker/.env" ]; then
    if [ -f "docker/.env.example" ]; then
        cp docker/.env.example docker/.env
    else
        touch docker/.env
    fi
fi

ensure_secret() {
    local key="$1"
    local generate_cmd="$2"
    local file="docker/.env"

    local current_val
    current_val=$(grep "^${key}=" "$file" | cut -d '=' -f2- || true)

    if [ -z "$current_val" ] || [ "$current_val" = "change_me" ]; then
        local new_val
        new_val=$(eval "$generate_cmd")
        grep -v "^${key}=" "$file" > "${file}.tmp" || true
        mv "${file}.tmp" "$file"
        echo "${key}=${new_val}" >> "$file"
        log_ok "Generated missing secret: ${key}"
    else
        log_info "Secret ${key} already configured."
    fi
}

ensure_secret "ZDX_HMAC_SECRET" "openssl rand -hex 32"
ensure_secret "ZDX_FERNET_KEY" "$PYTHON -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""

# ------------------------------------------------------------------
#  7. Create required directories
# ------------------------------------------------------------------
mkdir -p state exports backups

# ------------------------------------------------------------------
#  8. Build and start Docker stack
# ------------------------------------------------------------------
log_info "Building and starting Docker containers…"
log_info "  (This may take a few minutes the first time)…"
$DOCKER_CMD compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
log_ok "Docker stack is running"

# ------------------------------------------------------------------
#  9. Verify health
# ------------------------------------------------------------------
log_info "Waiting for backend to become healthy…"
for i in $(seq 1 30); do
    if curl -sf http://localhost:8080/api/v1/health >/dev/null 2>&1; then
        log_ok "Backend is healthy at http://localhost:8080"
        break
    fi
    if [ "$i" -eq 30 ]; then
        log_warn "Backend health check timed out. Check logs: $DOCKER_CMD compose -f docker/docker-compose.yml logs backend"
    else
        sleep 2
    fi
done

# ------------------------------------------------------------------
#  Summary
# ------------------------------------------------------------------
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  zd-transfer — Setup Complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${CYAN}CLI usage:${NC}"
echo -e "    source .venv/bin/activate"
echo -e "    python main.py pre-flight"
echo ""
echo -e "  ${CYAN}Web UI:${NC}"
echo -e "    Open http://localhost:8080/ in your browser"
echo ""
echo -e "  ${CYAN}Docker commands:${NC}"
echo -e "    $DOCKER_CMD compose -f docker/docker-compose.yml logs -f backend"
echo -e "    $DOCKER_CMD compose -f docker/docker-compose.yml down"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "    1. Edit config/source.env with your source Zendesk credentials"
echo -e "    2. Edit config/target.env with your target Zendesk credentials"
echo -e "    3. Run: python main.py pre-flight"
echo -e "    4. Run: python main.py migrate"
echo ""
