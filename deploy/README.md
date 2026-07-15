# Terra production operations

This directory contains a macOS `launchd` LaunchAgent template for the single-host Terra deployment.
The application remains bound to `127.0.0.1:8787`; Cloudflare Tunnel is the only intended public ingress.

## 1. Prerequisites

- Use a dedicated, non-administrator macOS account for Terra when practical.
- Install the locked frontend/backend dependencies and the pinned image-generation tool before deployment.
- Keep the project checkout, `backend/data`, `backend/generated`, logs, and backups readable only by that account.
- Configure Gemini credentials through the existing trusted secret helper or a mode-`0600` `.env`.
  The plist must remain free of credentials and secret-manager session values.
- Configure Cloudflare Tunnel to reach `http://127.0.0.1:8787`; do not expose port 8787 on a public or LAN interface.

Run the full checks before the first install:

```sh
cd /absolute/path/to/terra
make test
npm --prefix frontend run lint
```

## 2. Install the LaunchAgent

Copy `com.jiun.terra.plist.template` to a temporary file and replace every placeholder:

| Placeholder | Value |
| --- | --- |
| `__TERRA_ROOT__` | Absolute project checkout path, without a trailing slash |
| `__HOME__` | Home directory of the account running Terra |
| `__PATH__` | Minimal executable path containing `uv`, `npm`, `node`, and the image CLI |
| `__LOG_DIR__` | Private existing log directory, such as `$HOME/Library/Logs/Terra` |

The template intentionally contains no secret placeholders. Do not add secrets to `EnvironmentVariables`; plist files
and process environments are routinely visible to local diagnostic tools.

Install it as a per-user LaunchAgent so the Apple/MLX process runs in the intended user session:

```sh
mkdir -p "$HOME/Library/Logs/Terra"
chmod 700 "$HOME/Library/Logs/Terra"
cp deploy/com.jiun.terra.plist.template "$HOME/Library/LaunchAgents/com.jiun.terra.plist"
# Replace the four placeholders in the copied file, then validate it.
plutil -lint "$HOME/Library/LaunchAgents/com.jiun.terra.plist"
chmod 600 "$HOME/Library/LaunchAgents/com.jiun.terra.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.jiun.terra.plist"
launchctl enable "gui/$(id -u)/com.jiun.terra"
launchctl kickstart -k "gui/$(id -u)/com.jiun.terra"
```

Do not install this as root. `KeepAlive` restarts unexpected exits, while a clean `launchctl bootout` remains stopped.
The template's `0077` umask prevents newly created DB, image, and log files from becoming group/world readable.

## 3. Verify service and ingress

```sh
launchctl print "gui/$(id -u)/com.jiun.terra"
curl --fail --silent --show-error http://127.0.0.1:8787/api/health
curl --fail --silent --show-error https://terra.jiun.dev/api/health
```

Also verify that the production API docs are unavailable, security headers are present, and the origin is loopback-only:

```sh
curl --fail --head https://terra.jiun.dev/
curl --silent --output /dev/null --write-out '%{http_code}\n' https://terra.jiun.dev/docs
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

Expected: `/docs` returns `404`; the listener is `127.0.0.1:8787`, not `*:8787`.

## 4. Backup

`scripts/backup.py` uses SQLite's online backup API, so the service may remain running. It creates one mode-`0600`
`.tar.gz` containing:

- an integrity-checked, standalone SQLite snapshot;
- only `/generated/*.png` files referenced by that snapshot;
- a manifest with SHA-256, byte size, planet/asset references, counts, and missing-file status.

Choose a private output directory outside `frontend`, `backend/generated`, and any web-served path:

```sh
mkdir -p "$HOME/TerraBackups"
chmod 700 "$HOME/TerraBackups"
python3 scripts/backup.py create --output-dir "$HOME/TerraBackups"
python3 scripts/backup.py verify "$HOME/TerraBackups/terra-backup-YYYYMMDDTHHMMSSZ.tar.gz"
```

The create command fails if a referenced PNG is absent or unsafe. `--allow-missing` is an explicit disaster-recovery
escape hatch; archives made with it list omissions in `manifest.json` and should not be treated as complete backups.
Copy verified archives to encrypted off-host storage and apply a documented retention policy. Periodically perform a
restore drill rather than assuming an archive is usable.

## 5. Restore drill

1. Verify the archive with `scripts/backup.py verify`.
2. Stop Terra so no process can write the DB or generated directory:

   ```sh
   launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.jiun.terra.plist"
   ```

3. Extract the trusted, verified archive into a new private staging directory. Inspect `manifest.json`, verify
   `database/terra.sqlite3` with `PRAGMA quick_check`, and compare the listed image count.
4. Make a separate safety copy of the current `backend/data` and referenced images. Replace the DB with the staged
   snapshot and copy the staged `generated/*.png` files into `backend/generated`. Do not delete unrelated generated
   files until the application and gallery have been verified.
5. Bootstrap the LaunchAgent again and verify local/public health plus several gallery records and images.

The archive contains edit-token hashes and potentially unpublished world descriptions. Treat it as confidential even
though it contains no plaintext API keys.

## 6. Upgrade and rollback

Before restarting a deployed checkout:

1. Create and verify a backup.
2. Fetch/update into a separate release directory or worktree.
3. Run `make test` and the frontend lint there.
4. Stop the LaunchAgent, update `__TERRA_ROOT__` to the tested release, validate the plist, and bootstrap it.
5. Verify local readiness, public headers, gallery reads, and image provider status.

Keep the previous release and its matching backup until verification succeeds. Rollback means stopping the new agent,
pointing the plist to the previous tested release, and bootstrapping it again; avoid editing a live checkout in place.

## 7. Logs and incident checks

The template separates stdout and stderr in `__LOG_DIR__`. Rotate these files with the host's normal log policy and alert
on repeated restarts, HTTP 5xx, image-job failures, queue saturation, low disk space, and failed backups. Never log API
keys, Vault sessions, request authorization capabilities, complete story text, or child-process environments.
