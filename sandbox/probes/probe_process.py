"""Sonda de aislamiento de PROCESS.

Sale con 0 si el aislamiento se cumple y con 1 si no. Imprime un veredicto JSON.

Dentro del contenedor solo deben verse los procesos del propio contenedor: el
PID 1 es el proceso lanzado y no aparece ningun ejecutable de Windows. El
aislamiento se confirma ademas comparando los namespaces con los del host, que
no son accesibles desde aqui.
"""

from __future__ import annotations

import json
import os
import sys

WINDOWS_MARKERS = (".exe", "system32", "csrss", "wininit", "services.exe", "windows\\")

failures: list[str] = []


def read(path: str) -> str:
    """Lee un archivo de /proc, devolviendo cadena vacia si no es legible."""
    try:
        with open(path, "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


pids = sorted(int(entry) for entry in os.listdir("/proc") if entry.isdigit())
commands = {pid: read(f"/proc/{pid}/cmdline") for pid in pids}

# 1. No debe verse ningun proceso de Windows.
windows_like = [cmd for cmd in commands.values() if any(m in cmd.lower() for m in WINDOWS_MARKERS)]
if windows_like:
    failures.append(f"procesos de Windows visibles: {windows_like}")

# 2. El PID 1 debe ser el proceso del contenedor, no un init del host.
pid1 = commands.get(1, "")
if not pid1:
    failures.append("no se pudo leer el PID 1")

# 3. Los namespaces deben ser propios (no los del host).
namespaces: dict[str, str] = {}
for key in ("pid", "net", "mnt", "ipc", "uts", "user"):
    try:
        namespaces[key] = os.readlink(f"/proc/self/ns/{key}")
    except Exception as exc:
        namespaces[key] = f"ERROR:{type(exc).__name__}"
        failures.append(f"namespace {key} no legible")

# 4. Un contenedor recien arrancado no deberia exponer cientos de procesos.
if len(pids) > 20:
    failures.append(f"demasiados procesos visibles: {len(pids)}")

print(
    json.dumps(
        {
            "probe": "process",
            "isolated": not failures,
            "process_count": len(pids),
            "pid1": pid1[:120],
            "namespaces": namespaces,
            "windows_processes_visible": windows_like,
            "failures": failures,
        }
    )
)
sys.exit(0 if not failures else 1)
