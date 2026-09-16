from __future__ import annotations

import socket


def send_magic_packet(mac_address: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> None:
    mac_bytes = _parse_mac(mac_address)
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip, port))


def _parse_mac(mac_address: str) -> bytes:
    cleaned = mac_address.replace(":", "").replace("-", "")
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac_address}")
    return bytes.fromhex(cleaned)
