"""Diagnóstico de una corrida real de CASE-B: qué propuso cada invocación y qué pasó con ello.

Uso:
    python _punto-skill-layer/diagnose_run.py <evidencia.json> [CASE-ID]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    """Imprime el resumen diagnóstico de la corrida pedida."""
    ruta = Path(sys.argv[1])
    quiero = (sys.argv[2] if len(sys.argv) > 2 else "").upper()
    for caso in json.loads(ruta.read_text(encoding="utf-8")):
        if quiero and caso["case"] != quiero:
            continue
        record = caso["record"]
        print(f"===== {caso['case']} {caso['status']} =====")
        print(f"error: {caso['error'][:160]}")
        print(f"change_issues: {caso['change_issues']}")
        print(
            f"prompt={record['prompt_chars']} (inicial {record['initial_builder_prompt_chars']} / "
            f"resolución {record['resolution_prompt_chars']}) tokens={record['tokens']['total_tokens']} "
            f"elapsed={record['elapsed_ms']}ms provider={record['provider_elapsed_ms']}ms"
        )
        print(
            f"repairs={record['repair_rounds']} calls={record['provider_calls']} "
            f"{record['provider_calls_by_role']} fases={record['provider_calls_by_phase']} "
            f"fallos={record['verification_failures']} gates={record['human_gates']}"
        )
        reparacion = caso.get("first_repair", {})
        print("\n-- métricas de la primera reparación --")
        for clave in (
            "first_repair_attempted",
            "first_repair_strategy_changed",
            "first_repair_addressed_failure_resource",
            "first_repair_pass",
            "first_repair_causal_gap",
            "first_repair_scope_expansion_requested",
            "first_repair_scope_expansion_approved",
            "first_repair_change_same_resource",
            "first_repair_change_same_proposal",
            "first_repair_change_applied",
            "first_repair_focused_pass",
            "first_repair_chain_pass",
        ):
            print(f"   {clave} = {reparacion.get(clave)}")
        print("\n-- propuestas por invocación --")
        for indice, llamada in enumerate(caso.get("calls_detail", [])):
            propuesta: dict[str, Any] = llamada.get("proposal", {})
            cambios = [
                f"{item.get('path')}:{item.get('operation')}"
                for item in propuesta.get("changes", [])
            ]
            print(
                f"   [{indice}] {llamada['phase']}: cambios={cambios} "
                f"sin_cambio={propuesta.get('unchanged_resources')} "
                f"expansion={propuesta.get('scope_expansion')}"
            )
            print(f"        root_cause: {str(propuesta.get('root_cause', ''))[:140]}")
        print("\n-- eventos de decisión --")
        for evento in caso.get("audit_events", []):
            tipo = evento["event_type"]
            if tipo in (
                "DEV_CHANGE_VALIDATED",
                "DEV_CHANGE_REJECTED",
                "DEV_SCOPE_EXPANSION_REQUESTED",
                "DEV_SCOPE_EXPANSION_APPROVED",
                "DEV_SCOPE_EXPANSION_DENIED",
                "DEV_PLAN_REVISED",
                "DEV_REPAIR_EXHAUSTED",
                "DEV_ROLLBACK_COMPLETED",
                "DEV_CAUSAL_STAGNATION",
            ):
                print(f"   {tipo}: {json.dumps(evento['metadata'], ensure_ascii=False)[:260]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
