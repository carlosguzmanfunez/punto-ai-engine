"""Sonda de aislamiento de NETWORK.

Sale con 0 si el aislamiento se cumple y con 1 si no. Imprime un veredicto JSON.

Con ``--network none`` ninguna conexion puede establecerse: ni TCP, ni UDP, ni
resolucion DNS. Comprobar solo la ausencia de ``curl`` no demostraria nada, asi
que se intentan conexiones reales desde Python.
"""

from __future__ import annotations

import json
import socket
import sys

TCP_TARGETS = (("1.1.1.1", 53), ("8.8.8.8", 53), ("9.9.9.9", 443))
DNS_NAMES = ("example.com", "punto-network-canary.invalid")

failures: list[str] = []
results: dict[str, str] = {}

for host, port in TCP_TARGETS:
    label = f"tcp:{host}:{port}"
    try:
        connection = socket.create_connection((host, port), timeout=4)
        connection.close()
        results[label] = "CONECTADO"
        failures.append(f"{label} conecto (no deberia)")
    except Exception as exc:
        results[label] = f"DENEGADO:{type(exc).__name__}"

for name in DNS_NAMES:
    label = f"dns:{name}"
    try:
        results[label] = socket.gethostbyname(name)
        failures.append(f"{label} resolvio (no deberia)")
    except Exception as exc:
        results[label] = f"DENEGADO:{type(exc).__name__}"

try:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.settimeout(4)
    udp.sendto(b"probe", ("8.8.8.8", 53))
    udp.close()
    results["udp:8.8.8.8:53"] = "ENVIADO"
    failures.append("udp:8.8.8.8:53 envio (no deberia)")
except Exception as exc:
    results["udp:8.8.8.8:53"] = f"DENEGADO:{type(exc).__name__}"

try:
    with open("/proc/net/dev", encoding="utf-8") as handle:
        interfaces = [line.split(":")[0].strip() for line in handle if ":" in line]
except Exception:
    interfaces = []
usable = [name for name in interfaces if name and name != "lo"]
if usable:
    failures.append(f"interfaces de red utilizables: {usable}")

print(
    json.dumps(
        {
            "probe": "network",
            "isolated": not failures,
            "results": results,
            "usable_interfaces": usable,
            "failures": failures,
        }
    )
)
sys.exit(0 if not failures else 1)
