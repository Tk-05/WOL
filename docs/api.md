# API

JSON endpoints so other services (Home Assistant, Uptime Kuma, dashboards, scripts) can
query the status of machines and clusters, and, with a control key, switch them.

| Request | Returns |
| --- | --- |
| `GET /api/info` | this daemon's ID and the calling key's access, see [Info](#info) |
| `GET /api/machines` | `{"machines": [...]}`, every machine |
| `GET /api/machines/<key>` | one machine |
| `GET /api/clusters` | `{"clusters": [...]}`, every cluster |
| `GET /api/clusters/<key>` | one cluster |
| `POST /api/machines/<key>/action/<on\|off\|reboot>` | wakes, shuts down or reboots a machine, see [Actions](#actions) |
| `POST /api/clusters/<key>/action/<on\|off>` | starts a cluster's wake or shutdown sequence |

An unknown key returns HTTP 404 with `{"error": "..."}`.

Every status request pings the machines, so the status is always live. An offline
machine takes up to `status_check.timeout_seconds` to answer (5 seconds by default);
list requests ping all machines in parallel. A polling interval of 30–60 seconds is
plenty.

## Authentication

Every request needs an API key: create one under **Settings → API keys** in the web
UI (it's shown once, then only its hash is stored) and send it as a header:

```
Authorization: Bearer <key>
```

Each key has a name and an access level:

- **Read only**: the `GET` endpoints. Enough for Uptime Kuma and dashboards.
- **Control**: also the `POST` action endpoints. For Home Assistant and scripts that
  switch machines.

Give each service its own key, so revoking one doesn't break the others. Without a
valid key the API answers HTTP 401; a read-only key calling an action gets HTTP 403. A
browser that's logged in to the web UI can use the API directly, without a key.

The key that existed before named keys were introduced is kept as a read-only key
called "API key".

The API never contains Proxmox token secrets, but it does contain IP and MAC
addresses.

## Info

```json
{
  "instance_id": "3f1c0d9a6b6e4c4f9a1e2b7d5c8a9f01",
  "status_timeout_seconds": 5,
  "access": "control",
  "key_name": "Home Assistant"
}
```

- `instance_id`: random, created once and kept in the file `instance_id` next to
  `config.yaml`. It stays the same when the daemon's address changes or the login is
  reset, so clients can recognise it.
- `status_timeout_seconds`: how long one ping waits. Set client timeouts above it.
- `access`: `read` or `control`; `key_name` is `null` for a logged-in browser.

## Actions

Actions need a control key. In the event log they appear with the source
`api:<key name>`, e.g. `api:Home Assistant` (`manual` for a logged-in browser).

**Machines**: `POST /api/machines/<key>/action/<action>` with `on` (wake), `off` (shut
down) or `reboot`. It answers once the magic packet or the shutdown command has been
sent (a few seconds, since it pings first), with the recorded action, the same object
as a machine's `last_action`:

```json
{
  "time": "2026-10-11T18:04:12+02:00",
  "action": "on",
  "source": "api:Home Assistant",
  "result": "ok",
  "detail": ""
}
```

| Status | Meaning |
| --- | --- |
| 200 | `result` is `ok`, or `skipped` (e.g. already online, `detail` says why) |
| 400 | unknown action |
| 403 | the key is read-only |
| 404 | unknown machine |
| 409 | `off` or `reboot` on a machine without a shutdown method (wake-only) |
| 502 | Proxmox or SSH failed; the body has `result: "error"` and the reason in `detail` |

A `200` for `on` means the packet went out, not that the machine is up yet: watch
`online` (the daemon also verifies the wake-up on its own and retries, see
`wol.verify_after_seconds`).

**Clusters**: `POST /api/clusters/<key>/action/<action>` with `on` or `off`. A sequence
can take minutes, so it answers at once with HTTP 202 and the cluster's `last_action`
(`result: "started"`); how it ended shows up there later (`ok`, `error` or
`cancelled`). An unknown action is HTTP 400.

```sh
curl -X POST -H "Authorization: Bearer <key>" http://<pi-ip>:9090/api/machines/pve/action/on
```

## Machine

```json
{
  "key": "pve",
  "name": "Proxmox Node",
  "online": true,
  "ip_address": "192.168.1.10",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "shutdown_method": "proxmox",
  "clusters": ["homelab"],
  "last_action": {
    "time": "2026-09-28T07:00:02+02:00",
    "action": "on",
    "source": "schedule",
    "result": "ok",
    "detail": ""
  },
  "next_action": {
    "rule": "Weekdays off",
    "action": "off",
    "time": "2026-09-28T23:00:00+02:00",
    "will_be_skipped": false
  }
}
```

- `shutdown_method`: `proxmox`, `ssh` or `none` (wake-only).
- `last_action`: `null` until the machine was woken or shut down for the first
  time (it's saved, so it survives restarts).
  `source` is `schedule`, `manual`, `watchdog`, `cluster:<key>` or `api:<key name>`;
  `result` is `ok`, `skipped` (e.g. already online) or `error` (reason in `detail`).
  For clusters it's
  `started` while a sequence runs, then `ok`, `error` (a member didn't come up/go
  down in time, or failed) or `cancelled` (replaced by a newer action).
- `next_action`: `null` if the machine has no schedule rules.

## Cluster

```json
{
  "key": "homelab",
  "name": "Homelab",
  "state": "partial",
  "online": 1,
  "total": 2,
  "delay_seconds": 60,
  "max_wait_seconds": 300,
  "members": [
    {"key": "nas", "name": "NAS", "online": true},
    {"key": "pve", "name": "Proxmox Node", "online": false}
  ],
  "last_action": null,
  "next_action": null
}
```

- `state`: `on` (all members online), `off` (none) or `partial`.
- `members` are listed in wake order.
- `max_wait_seconds`: how long a wake/shutdown sequence waits for each member
  before moving on; `0` means it doesn't wait, only pauses `delay_seconds`.

## Examples

**curl**

```sh
curl -H "Authorization: Bearer <key>" http://<pi-ip>:9090/api/machines/pve
```

**Home Assistant**: the [WOL Daemon integration](https://github.com/Tk-05/ha-wol-daemon)
(installable through HACS) sets up every machine and cluster as a device, with status,
power switch and wake/shutdown/reboot buttons; give it a key with Control access.

Without it, plain REST sensors work too (`configuration.yaml`, with a read-only key in
`secrets.yaml` as `wol_api_auth: "Bearer <key>"`):

```yaml
rest:
  - resource: http://<pi-ip>:9090/api/machines/pve
    headers:
      Authorization: !secret wol_api_auth
    scan_interval: 60
    binary_sensor:
      - name: "Proxmox online"
        value_template: "{{ value_json.online }}"
        device_class: connectivity
  - resource: http://<pi-ip>:9090/api/clusters/homelab
    headers:
      Authorization: !secret wol_api_auth
    scan_interval: 60
    sensor:
      - name: "Homelab state"
        value_template: "{{ value_json.state }}"
```

**Uptime Kuma**: monitor type "HTTP(s) - Json Query", URL
`http://<pi-ip>:9090/api/machines/pve`, JSON query `online`, expected value `true`,
and under Headers `{"Authorization": "Bearer <key>"}`.

**Browser-based dashboards**: the API sends no CORS headers, so a dashboard that
fetches the status from the browser (instead of from its own server) can't read it.
