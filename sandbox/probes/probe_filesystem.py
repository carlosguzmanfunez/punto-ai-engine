"""Sonda de aislamiento de FILESYSTEM.

Sale con 0 si el aislamiento se cumple y con 1 si no. Imprime un veredicto JSON.

Comprueba:
  - ninguna ruta del host es visible dentro del contenedor;
  - el workspace montado es visible y escribible;
  - la raiz del sistema de archivos es de solo lectura.
"""

from __future__ import annotations

import json
import os
import sys

HOST_PATHS = (
    "/mnt/c",
    "/mnt/d",
    "/mnt/host/c",
    "/host",
    "/host_mnt",
    "/run/desktop/mnt/host/c",
    "/Users",
    "/Volumes",
)

failures: list[str] = []

for host_path in HOST_PATHS:
    if os.path.exists(host_path):
        failures.append(f"ruta del host visible: {host_path}")

# Los puntos de montaje tipicos deben estar vacios o no existir: si WSL hubiera
# montado las unidades del host, aparecerian aqui.
for mount_root in ("/mnt", "/media"):
    if os.path.isdir(mount_root):
        try:
            entries = os.listdir(mount_root)
        except OSError:
            continue
        if entries:
            failures.append(f"{mount_root} contiene montajes: {entries}")

workspace = os.environ.get("PUNTO_PROBE_WORKSPACE", "/workspace")
workspace_writable = False
if os.path.isdir(workspace):
    probe_file = os.path.join(workspace, ".punto_probe_write")
    try:
        with open(probe_file, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe_file)
        workspace_writable = True
    except Exception as exc:
        failures.append(f"workspace no escribible: {type(exc).__name__}")
else:
    failures.append(f"workspace no visible: {workspace}")

root_writable = False
try:
    with open("/punto_probe_root_write", "w", encoding="utf-8") as handle:
        handle.write("x")
    root_writable = True
    failures.append("la raiz del sistema de archivos es escribible")
except Exception:
    pass

print(
    json.dumps(
        {
            "probe": "filesystem",
            "isolated": not failures,
            "host_paths_visible": [p for p in HOST_PATHS if os.path.exists(p)],
            "workspace_writable": workspace_writable,
            "root_read_only": not root_writable,
            "failures": failures,
        }
    )
)
sys.exit(0 if not failures else 1)
