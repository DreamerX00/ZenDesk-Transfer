# Self-host the zd-transfer backend

This directory builds and runs the FastAPI backend + RQ worker + Redis
stack that powers the Zendesk-app UI. The CLI in `main.py` works
independently and doesn't require this stack — use this only if you
want the in-Zendesk wizard experience.

---

## 1. Prerequisites

- Docker Engine ≥ 20.10 and Docker Compose v2.
- A machine reachable by the agent's browser. For a local-only dev
  setup this is just `localhost`; for production it's a small VM/VPS
  with HTTPS (Caddy/Traefik/nginx) in front of port 8080.
- Two OAuth clients registered in Zendesk — one in the source tenant,
  one in the target. See **section 4** of the repo-root
  `INSTRUCTIONS.md` for the OAuth client creation walkthrough.
- The Zendesk CLI (`zcli`) installed and logged in if you want to push
  the private app via the script:
    ```
    npm install -g @zendesk/zcli
    zcli login -i
    ```

## 2. Generate secrets

The backend requires two cryptographic secrets. **Both must persist
across restarts** — losing either makes stored OAuth tokens
unreadable, and the iframe will reject the next session attempt.

```bash
# HMAC secret (32 random bytes, hex)
openssl rand -hex 32
# Fernet key (urlsafe-base64 of 32 random bytes)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the example env file and paste both values in:

```bash
cp docker/.env.example docker/.env
$EDITOR docker/.env
```

The `ZDX_BACKEND_URL` must be the URL the agent's browser will use to
reach this backend. For a local-only setup leave it as
`http://localhost:8080`. For production, fill in your HTTPS endpoint.
`ZDX_STANDALONE_MODE` now defaults to `0` in the example env because
direct browser access to the bundled UI is usually not what you want in
production.

If you do not want operational data living inside the repo checkout,
also set:

```bash
ZDX_STATE_DIR=/srv/zd-transfer/state
ZDX_EXPORTS_DIR=/srv/zd-transfer/exports
ZDX_BACKUPS_DIR=/srv/zd-transfer/backups
```

## 3. Boot the stack

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d --build
```

This brings up three containers:

| Service   | Purpose                                        | Port           |
|-----------|------------------------------------------------|----------------|
| `redis`   | Job queue (RQ) and SSE event bus               | (internal)     |
| `backend` | FastAPI HTTP API                               | `:8080`        |
| `worker`  | RQ consumer that runs `phase*.run()` jobs      | (internal)     |

The compose setup also applies a few production-friendly defaults:

- `backend` and `worker` run as a non-root user from a read-only root filesystem.
- The only writable paths are the mounted state, exports, backups, and `/tmp`.
- Container logs are rotated (`10m` x `3`) to avoid silent disk growth.
- Standalone mode is off unless you explicitly enable it.

Verify the health endpoint:

```bash
curl http://localhost:8080/api/v1/health
# → {"ok":true,"version":"0.1.0"}
```

Tail logs while you work:

```bash
docker compose -f docker/docker-compose.yml logs -f backend worker
```

## 4. Build and install the Zendesk app

From the repo root:

```bash
./scripts/build_app.sh                       # produces dist/zd-transfer-app-<ver>.zip
./scripts/install_private.sh <target-subdomain>  # pushes via zcli
```

`install_private.sh` wraps `zcli apps:push`. When prompted by `zcli`,
provide the two manifest parameters:

| Parameter         | Value                                            |
|-------------------|--------------------------------------------------|
| `backend_url`     | The same URL you set as `ZDX_BACKEND_URL` above. |
| `backend_secret`  | The same `ZDX_HMAC_SECRET` you set above.        |

If you prefer the manual route, the same .zip can be uploaded via
Admin Center → Apps and integrations → Zendesk Support apps → Upload
private app.

## 5. Open the wizard

Visit `https://<target>.zendesk.com/agent` and look for the
**ZD Config Transfer** icon in the left nav bar. Click it; the wizard
opens with Step 1 (pre-flight). The first thing it does is HMAC-sign
the agent's identity envelope and POST it to `/api/v1/session` —
that's the round-trip that gives the iframe its bearer token.

If the wizard shows a red "Setup required" panel, the most common
causes are:

- `backend_url` or `backend_secret` is unset in the app's settings.
- The browser can't reach `backend_url` (firewall, mixed-content from
  HTTPS Zendesk → HTTP backend — production needs HTTPS).
- `ZDX_HMAC_SECRET` on the backend doesn't match `backend_secret` in
  the manifest.

## 6. Upgrade

```bash
git pull
docker compose -f docker/docker-compose.yml --env-file docker/.env build
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
./scripts/build_app.sh
./scripts/install_private.sh <target-subdomain>   # re-push the new bundle
```

## 7. Where state lives

| Path on host                  | Contents                                        |
|-------------------------------|-------------------------------------------------|
| `state/<migration_id>/`       | `id_map.json`, `migration_log.jsonl` per run    |
| `state/connections.enc`       | Fernet-encrypted OAuth tokens                   |
| `exports/`                    | Extractor output (JSON files per resource)      |
| `backups/<timestamp>/`        | Pre-format/pre-migrate target snapshots         |

These are bind-mounted from the repo into both `backend` and `worker`,
so the CLI you run on the host sees identical state to what the UI
operates on. You can fall back to the CLI mid-migration if needed.

## 8. CORS in production

By default the dev config allows `localhost:4567` (the origin
`zcli apps:server` uses). For production, set
`ZDX_CORS_ORIGINS=https://<your-subdomain>.zendesk.com` in
`docker/.env` — this is the iframe's parent-page origin. Avoid leaving
this blank in production.

## 9. Backups before you migrate

`docker compose` mounts `../backups` into both backend and worker, but
the existing CLI is still the canonical way to take a full backup:

```bash
python main.py --target config/target.env migrate   # backs up then migrates
# or:
python main.py --target config/target.env restore --path backups/<timestamp>/
```

The UI calls the same backup code path — there is no separate
"UI-only" backup format.

## 10. Stopping / removing

```bash
docker compose -f docker/docker-compose.yml down          # stop, keep volumes
docker compose -f docker/docker-compose.yml down --volumes # also wipe redis-data
```

Stored Zendesk OAuth tokens live in `../state/connections.enc` on the
host filesystem — they survive a `down --volumes`. Delete that file
to revoke them locally (the Zendesk side still considers them valid
until they expire; revoke at the OAuth client management page).
