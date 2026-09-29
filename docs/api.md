# Status API

Read-only JSON endpoints so other services (Home Assistant, Uptime Kuma, dashboards,
scripts) can query the status of machines and clusters.

| Request | Returns |
| --- | --- |
| `GET /api/machines` | `{"machines": [...]}`, every machine |
| `GET /api/machines/<key>` | one machine |
| `GET /api/clusters` | `{"clusters": [...]}`, every cluster |
| `GET /api/clusters/<key>` | one cluster |

An unknown key returns HTTP 404 with `{"error": "..."}`.

Every request pings the machines, so the status is always live. An offline machine
takes up to `status_check.timeout_seconds` to answer (5 seconds by default); list
requests ping all machines in parallel. A polling interval of 30–60 seconds is plenty.

## Authentication

Every request needs an API key: create one under **Settings → API key** in the web
UI (it's shown once, then only its hash is stored) and send it as a header:

```
Authorization: Bearer <key>
```

Without a valid key the API answers HTTP 401. A browser that's logged in to the
web UI can open the API URLs directly, without a key. Generating a new key
replaces the old one; revoking it switches the API off for everything but
logged-in browsers.

The API never contains Proxmox token secrets, but it does contain IP and MAC
addresses.

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
  `source` is `schedule`, `manual`, `watchdog` or `cluster:<key>`; `result` is `ok`,
  `skipped` (e.g. already online) or `error` (reason in `detail`). For clusters it's
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

**Home Assistant** (`configuration.yaml`, with the key in `secrets.yaml` as
`wol_api_auth: "Bearer <key>"`)

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
