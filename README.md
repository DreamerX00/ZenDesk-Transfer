<div align="center">
  <h1>ZD Config Transfer</h1>
  <p><strong>Migrate Zendesk configuration between accounts — CLI + self-hosted Web UI</strong></p>
  <p>
    <a href="#-quick-start"><img src="https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black" alt="Linux"></a>
    <a href="#-quick-start"><img src="https://img.shields.io/badge/macOS-000000?logo=apple&logoColor=white" alt="macOS"></a>
    <a href="#-quick-start"><img src="https://img.shields.io/badge/Windows-0078D6?logo=windows&logoColor=white" alt="Windows"></a>
    <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python" alt="Python 3.10+"></a>
    <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/docker-required-2496ED?logo=docker" alt="Docker"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  </p>
</div>

---

## ✨ Features

- **Full configuration migration** — Groups, Brands, Ticket Fields, User Fields, Organizations, Views, Triggers, Automations, Macros, SLA Policies, Schedules, Webhooks, Help Center, and Users
- **Dual interface** — Feature-rich CLI for power users + self-hosted Web UI wizard
- **Safe by design** — Dry-run mode, automatic rollback, full cleanup, backup/restore
- **Smart rate limiting** — Token-bucket throttle calibrated to your Zendesk plan's RPM
- **Bulk user migration** — Chunked import with suspension-risk protections
- **OAuth 2.0 + API Token auth** — Supports both authentication methods with auto-refresh
- **Post-migration tools** — Cleanup (surgical undo via id_map), per-phase rollback, and restore from backup — all available in the Web UI
- **Live phase timer** — Elapsed/estimated duration per phase with a resource-count-based estimator
- **Source & Target baselines** — Pre-flight scans both accounts so you know what exists before any write

---

## 🚀 Quick Start

### One-click setup (recommended)

Choose your platform and run — the script installs everything (Docker, Python, dependencies, secrets, config):

<details>
<summary><b>🐧 Ubuntu / Debian</b></summary>

```bash
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
chmod +x scripts/setup.sh
./scripts/setup.sh
```

The script will:
1. Install Docker Engine if missing
2. Install Python 3.10+ if missing
3. Create a Python virtual environment with all dependencies
4. Generate cryptographic secrets (HMAC + Fernet)
5. Create configuration files from templates
6. Build and start the Docker stack (backend + worker + Redis)
7. Verify the backend is healthy

After setup, edit `config/source.env` and `config/target.env` with your Zendesk credentials, then:

```bash
source .venv/bin/activate
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

<details>
<summary><b>🐧 RHEL / Fedora / CentOS</b></summary>

```bash
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
chmod +x scripts/setup_linux_rhel.sh
./scripts/setup_linux_rhel.sh
```

The script will:
1. Install Docker Engine if missing
2. Install Python 3.10+ if missing
3. Create a Python virtual environment with all dependencies
4. Generate cryptographic secrets (HMAC + Fernet)
5. Create configuration files from templates
6. Build and start the Docker stack (backend + worker + Redis)
7. Verify the backend is healthy

After setup, edit `config/source.env` and `config/target.env` with your Zendesk credentials, then:

```bash
source .venv/bin/activate
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

<details>
<summary><b>📦 Arch Linux</b></summary>

```bash
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
chmod +x scripts/setup_arch.sh
./scripts/setup_arch.sh
```

The script will:
1. Install Docker and docker-compose if missing
2. Install Python 3.10+ if missing
3. Create a Python virtual environment with all dependencies
4. Generate cryptographic secrets (HMAC + Fernet)
5. Create configuration files from templates
6. Build and start the Docker stack (backend + worker + Redis)
7. Verify the backend is healthy

After setup, edit `config/source.env` and `config/target.env` with your Zendesk credentials, then:

```bash
source .venv/bin/activate
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

<details>
<summary><b>🏔️ Alpine Linux</b></summary>

```bash
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
chmod +x scripts/setup_alpine.sh
./scripts/setup_alpine.sh
```

The script will:
1. Install Docker, docker-compose, Python if missing
2. Create a Python virtual environment with all dependencies
3. Generate cryptographic secrets (HMAC + Fernet)
4. Create configuration files from templates
5. Build and start the Docker stack (backend + worker + Redis)
6. Verify the backend is healthy

After setup, edit `config/source.env` and `config/target.env` with your Zendesk credentials, then:

```bash
source .venv/bin/activate
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

<details>
<summary><b>🍏 macOS (Intel & Apple Silicon)</b></summary>

```bash
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
chmod +x scripts/setup_macos.sh
./scripts/setup_macos.sh
```

The script will:
1. Install Homebrew if missing
2. Install Docker Desktop if missing
3. Install Python 3.10+ if missing
4. Create a Python virtual environment with all dependencies
5. Generate cryptographic secrets (HMAC + Fernet)
6. Create configuration files from templates
7. Build and start the Docker stack (backend + worker + Redis)
8. Verify the backend is healthy

After setup, edit `config/source.env` and `config/target.env` with your Zendesk credentials, then:

```bash
source .venv/bin/activate
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

<details>
<summary><b>🪟 Windows (PowerShell)</b></summary>

```powershell
git clone https://github.com/DreamerX00/ZenDesk-Transfer.git
cd ZenDesk-Transfer
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

> Run PowerShell **as Administrator** for best results.

The script will:
1. Install Docker Desktop if missing
2. Install Python 3.12 if missing
3. Create a Python virtual environment with all dependencies
4. Generate cryptographic secrets (HMAC + Fernet)
5. Create configuration files from templates
6. Build and start the Docker stack
7. Verify the backend is healthy

After setup, edit `config\source.env` and `config\target.env` with your Zendesk credentials, then:

```powershell
.\.venv\Scripts\Activate.ps1
python main.py pre-flight
python main.py migrate
```

Open **http://localhost:8080/** for the Web UI wizard.
</details>

### Generating secrets

The backend requires two cryptographic keys. You can generate them with a single command:

```bash
# ZDX_HMAC_SECRET — 64-char hex string (any 32 random bytes)
openssl rand -hex 32

# ZDX_FERNET_KEY — url-safe base64-encoded 32-byte Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Note:** Both values must be **persistent** — if they change, existing sessions and encrypted credentials become invalid. Generate them once and keep them in `docker/.env`.

The one-click setup scripts (`scripts/setup.sh` / `scripts/setup.ps1`) handle this automatically. For manual setup see below.

### Manual setup

<details>
<summary><b>Click to expand</b></summary>

**Prerequisites:** Python 3.10+, Docker & Docker Compose v2

```bash
# 1. Install Python dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Generate secrets (see "Generating secrets" above)
HMAC_SECRET=$(openssl rand -hex 32)
FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 3. Configure
cp docker/.env.example docker/.env

# 4. Write the secrets into docker/.env
sed -i "s|ZDX_HMAC_SECRET=.*|ZDX_HMAC_SECRET=${HMAC_SECRET}|" docker/.env
sed -i "s|ZDX_FERNET_KEY=.*|ZDX_FERNET_KEY=${FERNET_KEY}|" docker/.env

# 5. Set up Zendesk credentials
cp config/source.env.example config/source.env
cp config/target.env.example config/target.env
# Edit config/source.env and config/target.env with your Zendesk credentials

# 6. Build & start
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build

# 7. Verify
curl http://localhost:8080/api/v1/health
```
</details>

---

## 📋 What Gets Migrated

| Phase | Resources | Description |
|-------|-----------|-------------|
| **1 — Foundation** | Groups, Brands, Ticket Fields, User Fields, Org Fields, Custom Roles, Ticket Forms, Organizations | Core account structure |
| **2 — Business Logic** | Views, Triggers, Automations, Macros, SLA Policies, Schedules, Routing, Dynamic Content, Webhooks | Operational rules |
| **3 — Help Center** | Categories, Sections, Articles, User Segments | Knowledge base |
| **4 — Verify** | Cross-account comparison | Reports & validation |
| **5 — Users** | User accounts (all roles) | Identity migration |

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Browser                            │
│  ┌───────────────────┐    ┌──────────────────────┐  │
│  │   Web UI (React)  │    │  Zendesk App (iframe)│  │
│  └────────┬──────────┘    └──────────┬───────────┘  │
└───────────┼──────────────────────────┼──────────────┘
            │ HTTP/SSE                 │ HMAC + Bearer
            ▼                          ▼
┌───────────────────────────────────────────────────────┐
│              FastAPI Backend (:8080)                    │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐   │
│  │ REST API │  │ SSE Bus  │  │ Connection Store    │   │
│  │          │  │ (events) │  │ (Fernet-encrypted)  │   │
│  └────┬─────┘  └────┬─────┘  └────────────────────┘   │
└───────┼──────────────┼─────────────────────────────────┘
        │              │
        ▼              ▼
┌──────────────┐  ┌──────────┐
│   RQ Worker   │  │   Redis   │
│ (phase*.run())│  │ (queue +  │
└──────────────┘  │  events)  │
                  └──────────┘
```

---

## 🌐 Web UI Walkthrough

The wizard guides you through every step of a migration:

| Step | Description |
|------|-------------|
| **Connections** | Add source / target Zendesk credentials (API token or OAuth). Each connection shows a **Refresh** button to renew OAuth tokens in-place. |
| **Pre-flight** | Validates both accounts and scans **source** and **target** baselines side-by-side showing resource counts. |
| **Phases** | Select which phases to run (Foundation, Business Logic, Help Center, Verify, Users). Toggle formatting to purge target resources before migration. |
| **Progress** | Live SSE stream of phase output with elapsed / estimated timer. At the end, a **Report** tab shows the full summary. On failure, a **Rollback** button undoes the phase; on completion, **Cleanup** removes created artifacts and **Restore** lets you pick a backup to revert to. |
| **Dashboard** | Lists all past migrations with their current status and one-click access to Cleanup / Rollback / Restore operations. |

### Operations after migration

- **Cleanup** — Deletes everything the tool created on the target (uses the `id_map` stored per migration). Results in a clean target account.
- **Rollback** — Per-phase undo. Only works on the most recent phase that ran.
- **Restore** — Reverts the target to a previous state from a `.zip` backup. The picker lists all available backups with timestamps.

---

## 🛠 CLI Reference

```bash
# Validate credentials + baseline scan
python main.py pre-flight

# Safe migration: backup → format → migrate
python main.py migrate

# Or run steps individually:
python main.py run                # Run all phases
python main.py run --phase 1      # Single phase
python main.py run --dry-run      # Preview only

# Rollback / Cleanup
python main.py cleanup            # Undo everything this tool created
python main.py rollback --phase 1 # Undo one phase

# Verify & Restore
python main.py verify
python main.py restore --path backups/2025-01-01_12-00-00
```

---

## 🔒 Security

| Concern | Mitigation |
|---------|------------|
| **Credential leakage** | `.env` files in `.gitignore`; secrets never logged |
| **XSS** | DOMPurify sanitizes all rendered HTML; HTML-escaped OAuth callback |
| **SSRF** | Next-page URLs validated against expected subdomain |
| **Path traversal** | Migration IDs validated by regex before filesystem use |
| **At-rest secrets** | Fernet-encrypted connection store (`state/connections.enc`) |
| **Auth bypass** | HMAC-signed iframe sessions; standalone mode disabled by default |
| **Rate limiting** | Token-bucket throttle prevents API abuse |

---

## 📚 Documentation

- [Full CLI Reference](documentation.md) — Detailed command docs and troubleshooting
- [Web UI Guide](documentation.md#web-ui) — Screenshots and walkthrough of every page
- [Docker Deployment Guide](docker/README.md) — Production deployment with TLS
- [OAuth Setup Walkthrough](https://github.com/DreamerX00/ZenDesk-Transfer/wiki) — Step-by-step OAuth client creation

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Commit your changes: `git commit -am 'feat: add my feature'`
4. Push: `git push origin feat/my-feature`
5. Open a Pull Request

Please ensure tests pass before submitting:

```bash
# Python tests
python -m pytest tests/

# UI tests
cd ui && npm test
```

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

<div align="center">
  <sub>Built with ❤️ for Zendesk administrators everywhere</sub>
</div>
