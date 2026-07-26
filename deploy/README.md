# Running the backend

The AppImage is the console client. Choose one backend:

## Docker

```bash
export OMARAG_TOKEN="$(openssl rand -hex 32)"
docker compose -f deploy/compose.yaml up -d
curl http://127.0.0.1:8765/v1/health
```

Ollama stays on the host and is reached through `host.docker.internal`. The container is non-root,
the API binds to loopback, and the `oracle-data` volume survives upgrades.

## Native user service

The release `install.sh` creates the environment, private token and systemd units. It is the
supported native installation path; the `.in` files under `deploy/systemd` are templates used by
that installer.

```bash
systemctl --user status oracle-daedalus.service
journalctl --user -u oracle-daedalus.service -f
systemctl --user list-timers oracle-daedalus-update.timer
```
