from __future__ import annotations

import platform
import subprocess


def is_host_up(ip_address: str, timeout_seconds: int = 5) -> bool:
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout_seconds * 1000)) if is_windows else str(timeout_seconds)
    cmd = ["ping", count_flag, "1", timeout_flag, timeout_value, ip_address]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0
