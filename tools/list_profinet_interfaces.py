"""
tools/list_profinet_interfaces.py — List network adapters for PROFINET IO (Mode B).

Run from the project root:
    python tools/list_profinet_interfaces.py

Output shows the exact values to paste into settings.json:
    "profinet": {
        "interface":   "<name shown here>",
        "mac_address": "<MAC shown here>",
        ...
    }

Requires Npcap + scapy to be installed (run with profinet.enabled=true first,
or install manually: pip install scapy).
"""

import sys
import socket


def _via_scapy():
    from scapy.all import conf, get_if_hwaddr  # type: ignore

    ifaces = conf.ifaces
    print(f"{'#':<4} {'Interface name (use this in settings.json)':<48} {'MAC address':<20} {'IP address'}")
    print("-" * 110)
    for i, (name, iface) in enumerate(ifaces.items()):
        try:
            mac = get_if_hwaddr(name)
        except Exception:
            mac = "??:??:??:??:??:??"

        try:
            ip = iface.ip or ""
        except Exception:
            ip = ""

        desc = getattr(iface, "description", "") or getattr(iface, "name", name)
        # Scapy on Windows uses GUID names internally; the friendly description
        # is what operators recognise.  We show both.
        display = desc if desc != name else name
        print(f"[{i:<2}] {display:<48} {mac:<20} {ip}")

    print()
    print("Paste the exact interface name from the column above into settings.json.")
    print("The MAC address column gives the value for 'mac_address'.")
    print()
    print('Example:')
    print('  "profinet": {')
    print('    "enabled": true,')
    if ifaces:
        sample_name = next(iter(ifaces))
        sample_iface = ifaces[sample_name]
        try:
            sample_mac = get_if_hwaddr(sample_name)
        except Exception:
            sample_mac = "AA:BB:CC:DD:EE:FF"
        desc = getattr(sample_iface, "description", "") or sample_name
        print(f'    "interface":   "{desc}",')
        print(f'    "mac_address": "{sample_mac}",')
    print('    "ip_address":  "192.168.0.2",')
    print('    "subnet_mask": "255.255.255.0",')
    print('    "gateway":     "192.168.0.1"')
    print('  }')


def _via_ipconfig():
    """Fallback: parse ipconfig /all output when scapy is not installed."""
    import subprocess
    import re

    result = subprocess.run(
        ["ipconfig", "/all"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = result.stdout

    # Split into adapter blocks
    blocks = re.split(r"\r?\n(?=\S)", output)

    adapters = []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        header = lines[0].strip().rstrip(":")
        if not header or "Windows IP" in header:
            continue

        mac = ""
        ip  = ""
        for line in lines[1:]:
            m = re.search(r"Physical Address[^\:]*:\s*([0-9A-Fa-f-]{17})", line)
            if m:
                mac = m.group(1).replace("-", ":")
            m = re.search(r"IPv4 Address[^\:]*:\s*([\d\.]+)", line)
            if m:
                ip = m.group(1).rstrip("(Preferred)")

        if mac:
            adapters.append((header, mac, ip))

    if not adapters:
        print("No adapters with a MAC address found via ipconfig.")
        return

    print(f"{'Adapter name (use in settings.json)':<52} {'MAC address':<20} {'IP address'}")
    print("-" * 100)
    for name, mac, ip in adapters:
        print(f"{name:<52} {mac:<20} {ip}")

    print()
    print("NOTE: scapy is not installed — interface names above come from ipconfig.")
    print("After installing scapy (pip install scapy) re-run this script for the")
    print("exact Scapy interface name, which may differ on Windows.")


if __name__ == "__main__":
    print("=" * 110)
    print("  PROFINET IO — Available network interfaces")
    print("=" * 110)
    print()

    try:
        _via_scapy()
    except ImportError:
        print("scapy not installed — falling back to ipconfig output.\n")
        _via_ipconfig()
    except Exception as exc:
        print(f"scapy error: {exc}")
        print("Falling back to ipconfig output.\n")
        _via_ipconfig()
