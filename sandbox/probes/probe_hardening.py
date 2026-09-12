"""Sonda de HARDENING OCI.

Sale con 0 si el endurecimiento se cumple y con 1 si no. Imprime un veredicto JSON.

No es uno de los cuatro aislamientos: verifica que los parametros de ejecucion
realmente se aplican (capacidades, no-new-privileges, limites de cgroups,
usuario no-root, raiz de solo lectura y tmpfs escribible).
"""

from __future__ import annotations

import json
import os
import sys

failures: list[str] = []


def read_first(*paths: str) -> str:
    """Devuelve el contenido del primer archivo legible de la lista."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except Exception:
            continue
    return "no-disponible"


status: dict[str, str] = {}
try:
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if ":" in line:
                key, _, value = line.partition(":")
                status[key.strip()] = value.strip()
except Exception as exc:
    failures.append(f"no se pudo leer /proc/self/status: {type(exc).__name__}")

cap_eff = status.get("CapEff", "?")
no_new_privs = status.get("NoNewPrivs", "?")
uid = os.getuid()

if uid == 0:
    failures.append("el proceso corre como root")
if cap_eff not in {"0000000000000000", "0"}:
    failures.append(f"capacidades efectivas no vacias: {cap_eff}")
if no_new_privs != "1":
    failures.append(f"NoNewPrivs={no_new_privs}")

pids_max = read_first("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max")
memory_max = read_first("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
cpu_max = read_first("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us")

if pids_max == "max":
    failures.append("pids.max sin limite")
if memory_max == "max":
    failures.append("memory.max sin limite")
if cpu_max in {"max", "-1"}:
    failures.append("cpu.max sin limite")

root_read_only = False
try:
    with open("/punto_probe_root_write", "w", encoding="utf-8") as handle:
        handle.write("x")
except Exception:
    root_read_only = True
if not root_read_only:
    failures.append("la raiz del sistema de archivos es escribible")

tmpfs_writable = False
try:
    with open("/tmp/punto_probe_tmpfs", "w", encoding="utf-8") as handle:
        handle.write("x")
    tmpfs_writable = True
except Exception as exc:
    failures.append(f"/tmp no escribible: {type(exc).__name__}")

print(
    json.dumps(
        {
            "probe": "hardening",
            "hardened": not failures,
            "uid": uid,
            "cap_effective": cap_eff,
            "no_new_privs": no_new_privs,
            "seccomp": status.get("Seccomp", "?"),
            "pids_max": pids_max,
            "memory_max": memory_max,
            "cpu_max": cpu_max,
            "root_read_only": root_read_only,
            "tmpfs_writable": tmpfs_writable,
            "failures": failures,
        }
    )
)
sys.exit(0 if not failures else 1)
