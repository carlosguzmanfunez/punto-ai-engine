"""Tabla de la ronda 2: baseline vs 0.1.0 vs 0.2.0 + handoff, sobre las tres corridas de CASE-B."""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent

FUENTES = (
    ("baseline", "baseline-real.json"),
    ("0.1.0", "baseline-real-skill.json"),
    ("0.2.0+handoff", "baseline-real-round2.json"),
)

CAMPOS = (
    ("status", None),
    ("provider_calls", "provider_calls"),
    ("architect_calls", "_architect_calls"),
    ("builder_calls", "_builder_calls"),
    ("repair_rounds", "repair_rounds"),
    ("scope_expansions", "scope_expansions"),
    ("tool_calls", "tool_calls"),
    ("files_read", "files_read"),
    ("verification_count", "verification_count"),
    ("verification_failures", "verification_failures"),
    ("prompt_chars", "prompt_chars"),
    ("causal_handoff_chars", "causal_handoff_chars"),
    ("input_tokens", "_input_tokens"),
    ("output_tokens", "_output_tokens"),
    ("total_tokens", "_total_tokens"),
    ("provider_elapsed_ms", "provider_elapsed_ms"),
    ("elapsed_ms", "elapsed_ms"),
    ("human_gates", "human_gates"),
    ("success", "success"),
    ("functional_chain_pass", "functional_chain_pass"),
)


def _cargar(name: str) -> dict:
    """Primer caso del fichero de evidencia."""
    path = EVIDENCE / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))[0]


def _valor(item: dict, key: str | None) -> object:
    """Valor de un campo, con los derivados que no viven en el registro."""
    if key is None:
        return item.get("status", "")
    record = item.get("record", {})
    if key == "_architect_calls":
        return record.get("provider_calls_by_role", {}).get("ARCHITECT", 0)
    if key == "_builder_calls":
        return record.get("provider_calls_by_role", {}).get("BUILDER", 0)
    if key in {"_input_tokens", "_output_tokens", "_total_tokens"}:
        tokens = record.get("tokens", {})
        return tokens.get(key.strip("_"))
    return record.get(key, "")


def main() -> None:
    """Imprime la tabla de tres columnas."""
    corridas = [(label, _cargar(name)) for label, name in FUENTES]
    print(f"{'métrica':24}" + "".join(f"{label:>16}" for label, _ in corridas))
    for titulo, key in CAMPOS:
        fila = "".join(f"{str(_valor(item, key)):>16}" for _, item in corridas)
        print(f"{titulo:24}{fila}")
    print()
    for label, item in corridas:
        record = item.get("record", {})
        print(
            f"{label:16} skill={record.get('skill_id', '') or '(ninguna)'}"
            f"@{record.get('skill_version', '') or '-'} "
            f"handoff={record.get('causal_handoff_present')}/"
            f"{record.get('causal_handoff_chars')} plan={bool(item.get('plan'))}"
        )


def _delta() -> dict:
    """Delta de la ronda 2, listo para el informe."""
    corridas = [(label, _cargar(name)) for label, name in FUENTES]
    columnas: dict[str, dict] = {}
    for label, item in corridas:
        record = item.get("record", {})
        columnas[label] = {
            campo: _valor(item, clave) for campo, clave in CAMPOS
        } | {
            "status": item.get("status", ""),
            "skill": f"{record.get('skill_id', '')}@{record.get('skill_version', '')}".strip("@"),
            "causal_handoff": item.get("causal_handoff", ""),
            "causal_handoff_chars": item.get("causal_handoff_chars", 0),
            "plan_present": bool(item.get("plan")),
            "audit_events": len(item.get("audit_events", [])),
        }
    return {"columns": [label for label, _ in corridas], "by_column": columnas}


if __name__ == "__main__":
    main()
    (EVIDENCE / "experiment-01-round2-delta.json").write_text(
        json.dumps(_delta(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"delta: {EVIDENCE / 'experiment-01-round2-delta.json'}")
