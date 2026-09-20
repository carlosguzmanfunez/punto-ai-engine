"""Tabla del experimento 02: baseline vs skill del BUILDER, con la calidad del primer intento."""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent

FUENTES = (
    ("baseline", "baseline-real.json"),
    ("builder 0.1.0", "baseline-real-builder-0.1.0.json"),
)

CAMPOS = (
    ("status", None),
    ("success", "success"),
    ("functional_chain_pass", "functional_chain_pass"),
    ("provider_calls", "provider_calls"),
    ("builder_calls", "_builder_calls"),
    ("repair_rounds", "repair_rounds"),
    ("verification_failures", "verification_failures"),
    ("scope_expansions", "scope_expansions"),
    ("tool_calls", "tool_calls"),
    ("files_read", "files_read"),
    ("prompt_chars", "prompt_chars"),
    ("builder_prompt_chars", "_builder_prompt_chars"),
    ("repair_prompt_chars", "_repair_prompt_chars"),
    ("causal_handoff_chars", "causal_handoff_chars"),
    ("builder_skill_chars", "builder_skill_chars"),
    ("input_tokens", "_input_tokens"),
    ("output_tokens", "_output_tokens"),
    ("total_tokens", "_total_tokens"),
    ("provider_elapsed_ms", "provider_elapsed_ms"),
    ("elapsed_ms", "elapsed_ms"),
    ("human_gates", "human_gates"),
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
    detalle = item.get("calls_detail", [])
    if key == "_builder_calls":
        return len([call for call in detalle if call["role"] == "BUILDER"])
    if key == "_builder_prompt_chars":
        return sum(call["prompt_chars"] for call in detalle if call["role"] == "BUILDER")
    if key == "_repair_prompt_chars":
        return sum(
            call["prompt_chars"]
            for call in detalle
            if call["role"] == "BUILDER" and call.get("phase") == "repair"
        )
    if key in {"_input_tokens", "_output_tokens", "_total_tokens"}:
        return record.get("tokens", {}).get(key.strip("_"))
    return record.get(key, "")


def main() -> None:
    """Imprime la tabla, el primer intento y guarda el delta."""
    corridas = [(label, _cargar(name)) for label, name in FUENTES]
    print(f"{'métrica':24}" + "".join(f"{label:>18}" for label, _ in corridas))
    for titulo, key in CAMPOS:
        fila = "".join(f"{str(_valor(item, key)):>18}" for _, item in corridas)
        print(f"{titulo:24}{fila}")
    print()
    for label, item in corridas:
        first = item.get("first_attempt") or {}
        record = item.get("record", {})
        print(
            f"{label:16} first_attempt_pass={first.get('first_attempt_pass')} "
            f"builder_calls={first.get('builder_calls')} "
            f"missing_acceptance={first.get('missing_acceptance')} "
            f"missing_resources={first.get('missing_resources')} "
            f"failed_verifications={first.get('failed_verifications')} "
            f"skill={record.get('builder_skill_id') or '(ninguna)'}"
            f"@{record.get('builder_skill_version') or '-'}"
        )
    delta = {
        label: {
            "status": item.get("status", ""),
            "metrics": {titulo: _valor(item, key) for titulo, key in CAMPOS},
            "first_attempt": item.get("first_attempt", {}),
            "record": item.get("record", {}),
            "plan": item.get("plan"),
            "causal_handoff": item.get("causal_handoff", ""),
            "calls_detail": item.get("calls_detail", []),
        }
        for label, item in corridas
    }
    (EVIDENCE / "experiment-02-delta.json").write_text(
        json.dumps(delta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\ndelta: {EVIDENCE / 'experiment-02-delta.json'}")


if __name__ == "__main__":
    main()
