# Deploying via Portainer

The daemon can be configured either from a mounted `config.yaml` (see
`config.yaml.example`) or from `WOL_*` environment variables. Portainer has a
first-class UI for environment variables but none for arbitrary host files, so
the env var route is the one that lets you do everything from the browser,
with no SSH/SCP step at all.

How this works: on every start, if the required `WOL_*` variables are set, the
daemon builds its Proxmox/target/notification settings from them and writes
the result to `config.yaml` inside the `wol-config` volume — so changing an
env var and redeploying the stack (e.g. after rotating the Proxmox token)
takes effect immediately. The **schedule** is the one exception: it's only
ever managed through the daemon's own web UI (`http://<host>:9090`) and is
preserved across redeploys, never overwritten by env vars.

## 1. Complete Phase 0 first

You need the Proxmox host/node, a `Sys.PowerMgmt`-scoped API token, and the
target MAC/IP before any of this works — see
[phase0-setup.md](phase0-setup.md).

## 2. Create the stack

In Portainer: **Stacks → Add stack → Web editor**, paste the contents of
[`docker-compose.yml`](../docker-compose.yml).

Since the web editor has no build context (no access to this repo's
`Dockerfile`), `build: .` won't work there. Build the image once beforehand,
either:

- on the Pi itself, from a checkout of this repo: `docker compose build`
  (creates `wol-daemon:latest` locally, which Portainer will then use as-is), or
- as a **Repository** stack instead of Web editor, if this project lives in a
  git repo — Portainer clones it and has the build context available.

## 3. Fill in the environment variables

**Important:** Portainer's stack-level "Environment variables" section only
fills in `${VAR}` placeholders during compose interpolation — it does not
override values that are already hardcoded in the compose file. The
`environment:` block in `docker-compose.yml` uses `${WOL_PROXMOX_HOST:-}`
style placeholders for exactly this reason; if you rewrite one of those lines
to a literal value while editing the stack, Portainer's variable table will
silently stop having any effect on it and the container will start with an
empty value instead.

In the stack's **Environment variables** section, set at minimum:

| Variable | Example | Notes |
| --- | --- | --- |
| `WOL_PROXMOX_HOST` | `https://192.168.1.10:8006` | |
| `WOL_PROXMOX_NODE` | `pve` | |
| `WOL_PROXMOX_TOKEN_ID` | `wol-daemon@pve!wol-daemon` | from Phase 0 |
| `WOL_PROXMOX_TOKEN_SECRET` | `xxxxxxxx-...` | from Phase 0, mark this as a "secret" value in Portainer if it offers to |
| `WOL_PROXMOX_VERIFY_SSL` | `false` | `false` unless you have a real cert on the Proxmox API |
| `WOL_TARGET_MAC` | `AA:BB:CC:DD:EE:FF` | |
| `WOL_TARGET_IP` | `192.168.1.10` | |

All other `WOL_*` variables (`WOL_STATUS_TIMEOUT_SECONDS`,
`WOL_VERIFY_AFTER_SECONDS`, `WOL_RETRY_INTERVAL_SECONDS`, `WOL_MAX_RETRIES`,
`WOL_NTFY_URL`, `WOL_TELEGRAM_BOT_TOKEN`, `WOL_TELEGRAM_CHAT_ID`) are optional
and fall back to the same defaults as `config.yaml.example`. `WOL_TELEGRAM_BOT_TOKEN`
and `WOL_TELEGRAM_CHAT_ID` must be set together or not at all.

If any of the six required variables above is missing while at least one is
set, the daemon refuses to start with a clear error naming the missing ones —
it won't silently fall back to a half-configured state.

## 4. Deploy and add the schedule

Deploy the stack. Portainer creates the `wol-config` named volume
automatically — no host filesystem access needed.

Open `http://<pi-ip>:9090` and add your schedule rules there (the "New rule"
form at the bottom of the page). This is the same UI regardless of how you
deployed — file-based or env-based — and it's the only place the schedule is
ever edited.

## Rotating a secret or changing the target

Update the relevant `WOL_*` variable in the stack's environment variables and
redeploy. The schedule is untouched; everything else is rebuilt from the
current environment on that next start.

## Switching back to a file

If you'd rather manage `config.yaml` directly (e.g. to keep the exact same
file across a bare-metal and a Docker deployment), just don't set any
`WOL_*` variables and bind-mount your own file over the volume instead:

```yaml
    volumes:
      - /srv/wol-daemon/config.yaml:/config/config.yaml
      - /srv/wol-daemon/backups:/config/backups
```

Env vars and a mounted file can coexist, but env vars always win for
everything except the schedule — see the precedence note in
`config.yaml.example`.
