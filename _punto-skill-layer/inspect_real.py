"""Detalle de las ejecuciones reales del baseline: quién llamó, cuánto consumió y por qué falló."""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent


def main() -> None:
    """Imprime el detalle de cada caso real."""
    items = json.loads((EVIDENCE / "baseline-real.json").read_text(encoding="utf-8"))
    for item in items:
        record = item["record"]
        print(f"\n=== {item['case']} ({item['kind']}) {item['status']} ===")
        print(f"error: {item['error'] or '(ninguno)'}")
        print(f"change_issues: {item['change_issues']}")
        print(f"applied: {item['applied']}")
        print(f"verificación: {item['verification']}")
        print(f"autoridad: {item['gates']}")
        print(f"llamadas por rol: {record['provider_calls_by_role']}")
        print(f"llamadas por fase: {record['provider_calls_by_phase']}")
        print(f"tokens: {record['tokens']}")
        print(f"prompt: {record['prompt_chars']} chars | contexto: {record['context_chars']} chars")
        print(f"reparaciones: {record['repair_rounds']} | expansiones: {record['scope_expansions']}")
        print(f"elapsed: {record['elapsed_ms']} ms | proveedor: {record['provider_elapsed_ms']} ms")
    if items:
        from punto.telemetry import EfficiencyRecord

        records = [EfficiencyRecord.model_validate(item["record"]) for item in items]
        total = sum(item.tokens.total_tokens or 0 for item in records)
        print(f"\ntokens reales totales del baseline real: {total}")
        for record in records:
            print(f"  {record.case_id}: {record.tokens.model_dump()}")


if __name__ == "__main__":
    main()
