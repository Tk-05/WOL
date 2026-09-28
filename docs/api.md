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

Like the web UI, the API has no authentication and is meant for the home network
only. It never contains Proxmox token secrets, but it does contain IP and MAC
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
- `last_action`: `null` until something happened since the daemon started.
  `source` is `schedule`, `manual`, `watchdog` or `cluster:<key>`; `result` is `ok`,
  `skipped` (e.g. already online), `error` (reason in `detail`) or, for clusters,
  `started`.
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

## Examples

**curl**

```sh
curl http://<pi-ip>:9090/api/machines/pve
```

**Home Assistant** (`configuration.yaml`)

```yaml
rest:
  - resource: http://<pi-ip>:9090/api/machines/pve
    scan_interval: 60
    binary_sensor:
      - name: "Proxmox online"
        value_template: "{{ value_json.online }}"
        device_class: connectivity
  - resource: http://<pi-ip>:9090/api/clusters/homelab
    scan_interval: 60
    sensor:
      - name: "Homelab state"
        value_template: "{{ value_json.state }}"
```

**Uptime Kuma**: monitor type "HTTP(s) - Json Query", URL
`http://<pi-ip>:9090/api/machines/pve`, JSON query `online`, expected value `true`.

**Browser-based dashboards**: the API sends no CORS headers, so a dashboard that
fetches the status from the browser (instead of from its own server) can't read it.
