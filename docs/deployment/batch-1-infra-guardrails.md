# Batch 1 Infrastructure Guardrails
## VirusTotal Bulk Check Tool

| Field | Value |
|---|---|
| Date | 2026-05-24 |
| Tickets | QUOTA-05, SCOPE-05, SEC-02 |
| Branch | aipf/batch-1-infra-guardrails |
| Status | IN PROGRESS |
| Executed by | _(fill in)_ |

> **Template Notice:** All configuration templates in the `systemd/` subdirectory
> are examples. Verify all paths, service names, usernames, and ports against
> your actual deployment before copying any file to a production host.

---

## Summary of Changes

| Ticket | Change | Risk |
|---|---|---|
| QUOTA-05 | Move VT usage counter from `/tmp` to `/var/lib/dns-tool/vt_usage.json` | Low — adds directory and env var only |
| SCOPE-05 | Bind uvicorn to `127.0.0.1` instead of `0.0.0.0` | Low — nginx proxy already handles all external traffic |
| SEC-02 | Move API key from inline `Environment=` in override.conf to a restricted `EnvironmentFile` | Low — key value unchanged; access control tightened |

All three changes are systemd/OS configuration only. No application code changes.
A single service restart applies all three.

---

## Files Changed on the Production Host

| Path | Action |
|---|---|
| `/etc/systemd/system/dns-tool-backend.service` | Modified — `--host 127.0.0.1` |
| `/etc/systemd/system/dns-tool-backend.service.d/override.conf` | Replaced — uses `EnvironmentFile` and `VT_USAGE_FILE` |
| `/etc/dns-tool/secrets.env` | New — contains API key (mode 640, root:www-data) |
| `/var/lib/dns-tool/` | New directory — stable usage counter location |

## Files NOT in This Repository

The following production files must never be committed:

- `/etc/dns-tool/secrets.env` — contains real API key
- `/var/lib/dns-tool/vt_usage.json` — runtime counter data
- Any `*.bak-*` backup files created during execution

---

## Sanitized Templates

See the `systemd/` subdirectory:

- `dns-tool-backend.service.example` — shows `--host 127.0.0.1`
- `override.conf.example` — shows `EnvironmentFile=` and `VT_USAGE_FILE=`
- `secrets.env.example` — placeholder only, safe to commit

---

## Pre-Flight Checks (Run Before Any Production Changes)

```bash
# Confirm service is currently running
systemctl is-active dns-tool-backend.service

# Confirm current bind address (should show 0.0.0.0:8000 before change)
ss -tlnp | grep 8000

# Confirm API key is currently inline in override (do not copy output anywhere)
sudo systemctl show dns-tool-backend.service | grep -i "VT_API_KEY" | wc -c
# Expected: non-zero number of characters (key is currently exposed)

# Confirm tool is reachable
curl -s http://127.0.0.1:8000/api/vt-bulk-check/usage | head -c 100

# Confirm current usage file location
ls -la /tmp/vt_bulk_check_usage.json
```

---

## Backup Steps (Run Before Any Changes)

```bash
STAMP=$(date +%Y%m%d-%H%M%S)

sudo cp /etc/systemd/system/dns-tool-backend.service \
    /etc/systemd/system/dns-tool-backend.service.bak-${STAMP}

sudo cp /etc/systemd/system/dns-tool-backend.service.d/override.conf \
    /etc/systemd/system/dns-tool-backend.service.d/override.conf.bak-${STAMP}

echo "Backups created with timestamp: ${STAMP}"
ls -la /etc/systemd/system/dns-tool-backend.service.bak-*
ls -la /etc/systemd/system/dns-tool-backend.service.d/override.conf.bak-*
```

---

## Implementation Steps

### QUOTA-05 — Stable Usage File Path

```bash
# Create the stable data directory
sudo mkdir -p /var/lib/dns-tool
sudo chown www-data:www-data /var/lib/dns-tool
sudo chmod 750 /var/lib/dns-tool

# Verify
ls -ld /var/lib/dns-tool
# Expected: drwxr-x--- www-data www-data ...

# Migrate existing usage data (if present)
if [ -f /tmp/vt_bulk_check_usage.json ]; then
    sudo cp /tmp/vt_bulk_check_usage.json /var/lib/dns-tool/vt_usage.json
    sudo chown www-data:www-data /var/lib/dns-tool/vt_usage.json
    echo "Usage data migrated"
else
    echo "No existing usage file in /tmp — new file will be created on first run"
fi
```

### SEC-02 — Restricted EnvironmentFile for API Key

```bash
# Create the secrets directory
sudo mkdir -p /etc/dns-tool

# Create the secrets file — populate with real key from credential store
sudo bash -c 'echo "VT_API_KEY=replace_with_real_key" > /etc/dns-tool/secrets.env'
sudo nano /etc/dns-tool/secrets.env
# Replace "replace_with_real_key" with the actual value from the credential store.
# Do not paste the key into any terminal command — use the editor only.

# Set restrictive permissions
sudo chown root:www-data /etc/dns-tool/secrets.env
sudo chmod 640 /etc/dns-tool/secrets.env

# Verify permissions (should show -rw-r----- root www-data)
ls -la /etc/dns-tool/secrets.env

# Rewrite override.conf — remove inline key, add EnvironmentFile
sudo tee /etc/systemd/system/dns-tool-backend.service.d/override.conf > /dev/null << 'EOF'
[Service]
EnvironmentFile=/etc/dns-tool/secrets.env
Environment="VT_USAGE_FILE=/var/lib/dns-tool/vt_usage.json"
EOF

# Verify the override no longer contains the inline key
sudo grep "VT_API_KEY" /etc/systemd/system/dns-tool-backend.service.d/override.conf
# Expected: no output (key is now in secrets.env, not here)

sudo cat /etc/systemd/system/dns-tool-backend.service.d/override.conf
# Expected: only EnvironmentFile= and VT_USAGE_FILE= lines
```

### SCOPE-05 — Bind to 127.0.0.1

```bash
# Edit the service file to change --host 0.0.0.0 to --host 127.0.0.1
sudo sed -i 's/--host 0\.0\.0\.0/--host 127.0.0.1/' \
    /etc/systemd/system/dns-tool-backend.service

# Verify the change
grep "ExecStart" /etc/systemd/system/dns-tool-backend.service
# Expected: ... uvicorn main:app --host 127.0.0.1 --port 8000
```

---

## Service Restart

Apply all three changes in a single restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart dns-tool-backend.service

# Wait a moment for the process to start
sleep 3
systemctl is-active dns-tool-backend.service
```

---

## Verification Checklist

Run each check in order. Do not mark complete until all pass.

```bash
# V1 — Service is active
systemctl is-active dns-tool-backend.service
# Expected: active

# V2 — Port bound to 127.0.0.1 only (not 0.0.0.0)
ss -tlnp | grep 8000
# Expected: 127.0.0.1:8000

# V3 — API key not visible in process list or service env dump
ps aux | grep uvicorn | grep -i "VT_API\|api_key\|key="
# Expected: no output

# V4 — API responds on localhost
curl -s http://127.0.0.1:8000/api/vt-bulk-check/usage
# Expected: valid JSON with usage counters

# V5 — Usage file exists at new stable path
ls -la /var/lib/dns-tool/vt_usage.json
# Expected: file present, owned by www-data

# V6 — Usage counter is being written to new path (wait ~30s after a lookup or restart)
cat /var/lib/dns-tool/vt_usage.json
# Expected: valid JSON with date and count fields

# V7 — Tool is functional via browser
# Manually verify in the browser: run a short test domain list,
# confirm results appear and no errors surface in the UI.

# V8 — Override no longer exposes key inline
sudo systemctl show dns-tool-backend.service -p Environment
# Expected: output shows VT_USAGE_FILE but NOT VT_API_KEY value inline
```

- [ ] V1 — Service active
- [ ] V2 — Port bound to 127.0.0.1
- [ ] V3 — Key not in ps aux
- [ ] V4 — API responds on localhost
- [ ] V5 — Usage file at /var/lib/dns-tool/
- [ ] V6 — Usage file written correctly
- [ ] V7 — Browser test passes
- [ ] V8 — Key not exposed in systemctl show

---

## Rollback Steps

If any verification step fails or the service is unstable after restart:

```bash
# Restore backed-up service files (replace YYYYMMDD-HHMMSS with your backup timestamp)
sudo cp /etc/systemd/system/dns-tool-backend.service.bak-YYYYMMDD-HHMMSS \
    /etc/systemd/system/dns-tool-backend.service

sudo cp /etc/systemd/system/dns-tool-backend.service.d/override.conf.bak-YYYYMMDD-HHMMSS \
    /etc/systemd/system/dns-tool-backend.service.d/override.conf

# Reload and restart
sudo systemctl daemon-reload
sudo systemctl restart dns-tool-backend.service

# Confirm restored
sleep 3
systemctl is-active dns-tool-backend.service
ss -tlnp | grep 8000
# Expected: active; 0.0.0.0:8000 (reverted)

curl -s http://127.0.0.1:8000/api/vt-bulk-check/usage
# Expected: valid JSON response
```

After rollback is stable:

- The `/var/lib/dns-tool/` directory and `/etc/dns-tool/` directory may remain on disk safely.
  They will not be referenced by the reverted service configuration.
- Remove them only after confirming the reverted service is stable and you are not re-attempting the change.

---

## Completion Notes

_(Fill in after all verification steps pass.)_

| Field | Value |
|---|---|
| Date completed | |
| Executed by | |
| All verification steps passed | yes / no |
| Issues encountered | none / _(describe)_ |
| Rollback required | yes / no |
| Notes | |
