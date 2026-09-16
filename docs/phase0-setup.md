# Phase 0 — Proxmox Host Setup

One-time preparation on the Proxmox host itself. Do this before running the daemon
(Phase 1) — the daemon assumes all of this is already in place.

## 1. Enable Wake-on-LAN in BIOS/UEFI

Reboot the host into BIOS/UEFI setup and enable Wake-on-LAN. Depending on the
vendor it may be labeled "Wake on LAN", "Power On by PCI-E/PCIe", or "PME Event
Wake Up". Save and boot back into Proxmox.

## 2. Identify the physical NIC

Proxmox bridges the physical NIC into `vmbr0`. Wake-on-LAN has to be enabled on
the physical interface underneath, not on the bridge itself.

```sh
cat /etc/network/interfaces
```

Look for the `bridge-ports` line of `vmbr0`, e.g. `bridge-ports enp2s0` — here
`enp2s0` is the physical NIC you need for the next steps.

## 3. Enable WOL on the NIC and make it persistent

Check current support and state:

```sh
ethtool enp2s0 | grep Wake-on
```

Enable it for this boot:

```sh
ethtool -s enp2s0 wol g
```

This setting is lost on reboot. Make it persistent by adding a `post-up` hook to
the interface's *existing* stanza in `/etc/network/interfaces` (don't create a
second `iface enp2s0` block — that will conflict with the one Proxmox already
generated for the bridge port):

```
iface enp2s0 inet manual
    post-up /sbin/ethtool -s enp2s0 wol g
```

Apply and verify:

```sh
ifreload -a
ethtool enp2s0 | grep Wake-on   # should show "Wake-on: g"
```

Reboot once to confirm the setting survives a restart.

**Alternative (non-Debian networking, e.g. systemd-networkd):** create a oneshot
unit instead:

```ini
# /etc/systemd/system/enable-wol.service
[Unit]
Description=Enable Wake-on-LAN on boot
After=network.target

[Service]
Type=oneshot
ExecStart=/sbin/ethtool -s enp2s0 wol g

[Install]
WantedBy=multi-user.target
```

```sh
systemctl enable --now enable-wol.service
```

## 4. Note the MAC address

```sh
cat /sys/class/net/enp2s0/address
```

This goes into `target.mac_address` in `config.yaml`.

## 5. Static IP / DHCP reservation

Both the Proxmox node and the Pi need a stable IP — otherwise the MAC↔IP mapping
in `config.yaml` breaks silently after a lease renewal. Easiest: a DHCP
reservation by MAC address on your router for both devices. Alternative: a
static IP directly in `/etc/network/interfaces` on the Proxmox side.

## 6. Create a minimal-privilege API token

Don't reuse a root token for this — a leaked or misused root token can do
anything, but the daemon only ever needs to power the node on/off. Create a
dedicated user with a role scoped to exactly that:

```sh
pveum role add WOLPowerMgmt -privs "Sys.PowerMgmt"
pveum user add wol-daemon@pve --comment "WOL daemon service account"
pveum user token add wol-daemon@pve wol-daemon --privsep 1
```

The token secret is printed once at creation — copy it immediately, it can't be
retrieved again later. Then grant the role on the node:

```sh
pveum acl modify /nodes/<node-name> --token 'wol-daemon@pve!wol-daemon' --roles WOLPowerMgmt
```

Fill the result into `config.yaml`:

```yaml
proxmox:
  host: "https://<proxmox-ip>:8006"
  node: "<node-name>"
  token_id: "wol-daemon@pve!wol-daemon"
  token_secret: "<the printed secret>"
  verify_ssl: false
```

## 7. Verify before running the daemon

Two independent checks, so a problem is easy to localize.

**a) Token works** (uses the read-only `status` command, safe to run anytime):

```sh
curl -k -X POST "https://<proxmox-ip>:8006/api2/json/nodes/<node-name>/status" \
  -H "Authorization: PVEAPIToken=wol-daemon@pve!wol-daemon=<secret>" \
  -d "command=status"
```

Should return a JSON status object, not `401`/`403`. Don't run this with
`command=shutdown` unless you actually intend to power the host off.

**b) Wake-on-LAN works.** Shut the host down once manually (via the Proxmox UI
or `shutdown -h now`), then from another machine on the same LAN:

```sh
pip install wakeonlan   # or: apt install wakeonlan
wakeonlan <mac-address>
```

Confirm the host powers back on.

## Checklist

- [ ] WOL enabled in BIOS/UEFI
- [ ] `ethtool ... wol g` set and confirmed to survive a reboot
- [ ] Physical NIC's MAC address noted
- [ ] Static IP / DHCP reservation set for both the Proxmox node and the Pi
- [ ] Dedicated API token created with a `Sys.PowerMgmt`-only role (not root)
- [ ] `curl` status check against the token succeeds
- [ ] Manual `wakeonlan` test successfully powers the host back on

Once every box is checked, `config.yaml` has everything Phase 1 needs.
