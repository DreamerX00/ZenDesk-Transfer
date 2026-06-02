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

# Detect architecture
ARCH=$(uname -m)
if [[ "$ARCH" == "arm64" ]]; then
    log_info "Detected Apple Silicon (M-series)"
    HOMEBREW_PREFIX="/opt/homebrew"
else
    log_info "Detected Intel Mac"
    HOMEBREW_PREFIX="/usr/local"
fi

PATH="$HOMEBREW_PREFIX/bin:$PATH"

# ------------------------------------------------------------------
#  0. Homebrew
# ------------------------------------------------------------------
log_info "Checking Homebrew…"
if ! command -v brew >/dev/null 2>&1; then
    log_warn "Homebrew not found. Installing Homebrew…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [[ "$ARCH" == "arm64" ]]; then
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> "$HOME/.zprofile"
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
    log_ok "Homebrew installed."
else
    log_ok "Homebrew already installed."
fi

# ------------------------------------------------------------------
#  1. Docker
# ------------------------------------------------------------------
log_info "Checking Docker…"
if command -v docker >/dev/null 2>&1; then
    log_ok "Docker already installed: $(docker --version)"
else
    log_warn "Docker not found. Installing Docker via Homebrew…"
    brew install --cask docker
    log_ok "Docker installed. Please open Docker.app to complete setup."
    log_info "  Start Docker: open -a Docker"
    log_info "  Waiting for Docker to start…"
    open -a Docker
    # Wait for Docker to be ready
    for i in $(seq 1 60); do
        if docker ps >/dev/null 2>&1; then
            log_ok "Docker daemon is running."
            break
        fi
        if [ "$i" -eq 60 ]; then
            log_warn "Docker did not start in time. Please start Docker.app manually."
        else
            sleep 3
        fi
    done
fi

DOCKER_CMD="docker"

# ------------------------------------------------------------------
#  2. Docker Compose
# ------------------------------------------------------------------
log_info "Checking Docker Compose…"
if docker compose version >/dev/null 2>&1; then
    log_ok "Docker Compose already installed: $(docker compose version)"
else
    log_warn "Docker Compose plugin not found. Installing…"
    brew install docker-compose
    log_ok "Docker Compose installed."
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
    log_warn "Python 3.10+ not found. Installing via Homebrew…"
    brew install python@3.12
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

ensure_env_value() {
    local key="$1"
    local value="$2"
    local file="docker/.env"
    local current_val
    current_val=$(grep "^${key}=" "$file" | cut -d '=' -f2- || true)
    if [ "$current_val" = "$value" ]; then
        log_info "${key} already set to ${value}."
        return
    fi
    grep -v "^${key}=" "$file" > "${file}.tmp" || true
    mv "${file}.tmp" "$file"
    echo "${key}=${value}" >> "$file"
    log_ok "Configured ${key}=${value}"
}

# This setup path opens the bundled UI directly in a browser tab, so
# standalone mode must be enabled for the first boot to succeed.
ensure_env_value "ZDX_STANDALONE_MODE" "1"

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
