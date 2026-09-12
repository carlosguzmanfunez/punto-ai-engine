"""Sonda de aislamiento de ENVIRONMENT.

Sale con 0 si el aislamiento se cumple y con 1 si no. Imprime un veredicto JSON.

El backend exporta ``PUNTO_SANDBOX_SECRET_CANARY`` en el entorno del **cliente**
de podman, no del contenedor. Si el contenedor lo ve, el entorno se esta
heredando y el aislamiento falla.
"""

from __future__ import annotations

import json
import os
import re
import sys

CANARY = "PUNTO_SANDBOX_SECRET_CANARY"

SENSITIVE = re.compile(
    r"(_KEY|_TOKEN|_SECRET|_PASSWORD)$"
    r"|^AWS_"
    r"|^AZURE_"
    r"|^GOOGLE_"
    r"|^SSH_"
    r"|^GPG_KEY$"
    r"|PASSWORD"
    r"|DATABASE_URL"
    r"|GITHUB_TOKEN"
    r"|DEEPSEEK",
    re.IGNORECASE,
)

environment = dict(os.environ)
failures: list[str] = []

if CANARY in environment:
    failures.append("la variable canario del host llego al contenedor")

# Ninguna otra variable del host con aspecto sensible debe estar presente.
sensitive = sorted(name for name in environment if SENSITIVE.search(name))
for name in sensitive:
    # GPG_KEY y PYTHON_SHA256 son metadatos publicos de la imagen oficial.
    if name in {"GPG_KEY"}:
        continue
    failures.append(f"variable sensible presente: {name}")

leaked = [name for name, value in environment.items() if "canary" in str(value).lower()]
if leaked:
    failures.append(f"valor canario filtrado en: {leaked}")

print(
    json.dumps(
        {
            "probe": "environment",
            "isolated": not failures,
            "canary_present": CANARY in environment,
            "environment_variable_count": len(environment),
            "environment_names": sorted(environment),
            "sensitive_present": sensitive,
            "failures": failures,
        }
    )
)
sys.exit(0 if not failures else 1)
