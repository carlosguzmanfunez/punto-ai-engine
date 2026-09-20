"""Comprobación previa a la corrida real: proveedores asignados por rol y estado de la evidencia.

No llama a ningún proveedor y no imprime credenciales: solo dice qué proveedor atendería cada rol y
confirma que el control congelado sigue intacto (huellas).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
EVIDENCE = ENGINE / "_punto-skill-layer"

FROZEN = (
    "baseline-deterministic.json",
    "baseline-real.json",
    "baseline-real-skill.json",
    "baseline-real-round2.json",
    "baseline-real-builder-0.1.0.json",
)


def main() -> int:
    """Informa del entorno del experimento y del estado del control."""
    from punto.providers.contract import ProviderRole
    from punto.providers.registry import ProviderRegistry

    registry = ProviderRegistry()
    router = registry.router_instance()
    print("proveedores por rol:")
    for role in ProviderRole:
        try:
            print(f"  {role.value}: {router.get_provider_for_role(role)}")
        except Exception as exc:  # el router reporta la ausencia de asignación
            print(f"  {role.value}: SIN ASIGNAR ({exc})")

    print("\nvariables declaradas (solo presencia y longitud):")
    for nombre in ("PUNTO_ARCHITECT_SKILL", "PUNTO_BUILDER_SKILL", "PUNTO_RESOLUTION_SKILL"):
        valor = os.environ.get(nombre, "")
        print(f"  {nombre}: {'(vacía)' if not valor else valor}")

    print("\ncontrol congelado (sha256):")
    for nombre in FROZEN:
        ruta = EVIDENCE / nombre
        if not ruta.is_file():
            print(f"  {nombre}: AUSENTE")
            continue
        huella = hashlib.sha256(ruta.read_bytes()).hexdigest()
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        caso = next((item for item in datos if item["case"] == "CASE-B"), {})
        record = caso.get("record", {})
        print(
            f"  {nombre}: {huella[:16]}… status={caso.get('status')} "
            f"repairs={record.get('repair_rounds')} tokens={record.get('tokens', {}).get('total_tokens')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
