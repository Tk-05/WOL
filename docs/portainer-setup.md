# Deploying via Portainer

The simplest route needs no configuration in Portainer at all: deploy the
stack without any `WOL_*` variables, open the web UI (`http://<host>:9090`)
and add your machines there. The daemon starts with an empty configuration
and writes `config.yaml` into the `wol-config` volume as soon as you add the
first machine. This works for any number of machines, Proxmox and SSH alike.

Optionally, one Proxmox machine can instead be defined through `WOL_*`
environment variables (step 3). On every start the daemon rebuilds that
machine's settings from the variables, so changing a variable and
redeploying (e.g. after rotating the Proxmox token) takes effect immediately.
Its **schedule**, and any further machines you add in the web UI, are kept
from `config.yaml` and never overwritten. As long as the variables are set,
that machine can't be deleted permanently in the UI: it comes back on the
next start.

## 1. Complete Phase 0 first

You need the Proxmox host/node, a `Sys.PowerMgmt`-scoped API token, and the
target MAC/IP before any of this works — see
[phase0-setup.md](phase0-setup.md).

## 2. Create the stack

In Portainer: **Stacks → Add stack**, then either:

- **Repository**: URL `https://github.com/Tk-05/WOL`, Compose path
  `docker-compose.yml`, or
- **Web editor**: paste the contents of
  [`docker-compose.yml`](../docker-compose.yml).

Both work the same way, because the compose file doesn't build anything. It
pulls the prebuilt image `ghcr.io/tk-05/wol:latest`, which GitHub Actions
builds for amd64 and arm64 on every push to `main`
([`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)).

**Don't add `build:` back to the compose file.** On nodes connected through a
Portainer Edge Agent, Portainer can't build images at all: the agent proxies
Docker API calls to the node, and that proxy can't pass through the HTTP/2
protocol upgrade BuildKit needs. The deploy fails with
`listing workers for Build: ... http2: frame too large, note that the frame
header looked like an HTTP/1.1 header`, and the agent logs
`Error copying response body from proxied request`. Pulling a finished image
is a normal API call and works fine through the agent.

**Updating to a new version:** after a push to `main`, wait for the workflow
run to finish, then redeploy the stack in Portainer. `pull_policy: always`
makes every deploy fetch the newest `:latest`. For a **Repository** stack,
use **"Pull and redeploy"** rather than the plain update button: Portainer
only clones the repo once, so without it, changes to the compose file itself
(e.g. new environment variables) wouldn't be picked up.

**One-time step when publishing for the first time:** GitHub creates new
container packages as private, even for a public repo. After the first
successful workflow run, open the package on GitHub (your profile →
**Packages → wol → Package settings**) and change its visibility to
**Public**. Otherwise every node needs registry credentials to pull it.

## 3. Optional: fill in the environment variables

Skip this step if you'd rather add all machines in the web UI.

**Important:** Portainer's stack-level "Environment variables" section only
fills in `${VAR}` placeholders during compose interpolation — it does not
override values that are already hardcoded in the compose file. The
`environment:` block in `docker-compose.yml` uses `${WOL_PROXMOX_HOST:-}`
style placeholders for exactly this reason; if you rewrite one of those lines
to a literal value while editing the stack, Portainer's variable table will
silently stop having any effect on it and the container will start with an
empty value instead.

In the stack's **Environment variables** section, set all of these:

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

## 4. Deploy, add machines and schedules

Deploy the stack. Portainer creates the `wol-config` named volume
automatically — no host filesystem access needed.

Open `http://<pi-ip>:9090`. Without environment variables you'll see "No
machines configured yet"; use **Add your first machine**. Then add schedule
rules on each machine's page (the "New rule" form at the bottom). This is the
same UI regardless of how you deployed, and it's the only place the schedule
is ever edited.

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

## Multiple machines, and SSH-based shutdown

Machines added in the web UI are stored under `machines:` in `config.yaml`
(see the two worked examples, Proxmox and SSH, in `config.yaml.example`). Each
gets its own page (`http://<host>:9090/machines/<key>`); the root page becomes
an overview listing all of them once there's more than one.

**Edit machine** and **Edit cluster** on those pages change everything except
the key, and take effect immediately, including for already scheduled and
running actions; no restart needed. When editing a Proxmox machine, leave the
token secret empty to keep the current one. The machine defined by `WOL_*`
environment variables can't be edited in the UI while they're set.

For a plain PC shut down over SSH, the private key file needs to be readable
inside the container. Put it in the same volume as `config.yaml` — e.g. bind
mount a host directory to `/config` and place the key at
`/config/ssh/desktop_key`, matching `private_key_path` in the example — rather
than trying to bake it into an environment variable.

## Clusters

A cluster groups machines so they can be woken and shut down together
(**Add cluster** in the top navigation). Each member gets a position: members
are woken in that order and shut down in reverse, with a configurable pause in
between, e.g. the NAS first on and last off so the Proxmox nodes find their
storage. A cluster has its own schedule, which runs in addition to each
member's own rules. Deleting a machine removes it from its clusters; deleting a
cluster keeps its machines.

## Export and import

**Import / export** in the top navigation downloads the complete configuration
as YAML, or replaces it with an uploaded file. An import is validated first and
changes nothing if the file has any error; the previous `config.yaml` is kept
in `backups/`. The export contains Proxmox token secrets in plain text, so
store it like a password. SSH private keys are not part of it, only their
paths, so copy the key files to the new node separately.
