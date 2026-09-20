"""Comparación de la ronda 2: baseline vs skill 0.1.0 vs skill 0.2.0 + handoff causal.

Tres columnas, una por corrida real de CASE-B, con las métricas del encargo. Lee la evidencia
persistida; no modifica nada del baseline congelado.
"""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent

METRICS: tuple[tuple[str, str], ...] = (
    ("success", "success"),
    ("functional_chain_pass", "functional_chain_pass"),
    ("provider_calls", "provider_calls"),
    ("repair_rounds", "repair_rounds"),
    ("scope_expansions", "scope_expansions"),
    ("tool_calls", "tool_calls"),
    ("files_read", "files_read"),
    ("verification_count", "verification_count"),
    ("verification_failures", "verification_failures"),
    ("prompt_chars", "prompt_chars"),
    ("context_chars", "context_chars"),
    ("elapsed_ms", "elapsed_ms"),
    ("human_gates", "human_gates"),
)


def _case(name: str, case_id: str = "CASE-B") -> dict | None:
    """Resultado de un caso en un fichero de evidencia."""
    path = EVIDENCE / name
    if not path.exists():
        return None
    for item in json.loads(path.read_text(encoding="utf-8")):
        if item["case"] == case_id:
            return item
    return None


def _role_chars(item: dict, role: str) -> int:
    """Caracteres de prompt enviados a un rol."""
    calls = [call for call in item.get("_calls", []) if call["role"] == role]
    return sum(call["prompt_chars"] for call in calls)


def main() -> None:
    """Imprime la tabla comparativa de las tres corridas."""
    corridas = {
        "baseline": _case("baseline-real.json"),
        "0.1.0": _case("baseline-real-skill.json"),
        "0.2.0+handoff": _case("baseline-real-skill.json", "CASE-B"),
    }
    # La corrida de 0.2.0 vive en su propio fichero para no pisar la de 0.1.0.
    dos = _case("baseline-real-round2.json") or _case("baseline-real-skill.json")
    if dos is not None and dos["record"].get("skill_version") == "0.2.0":
        corridas["0.2.0+handoff"] = dos

    columnas = [nombre for nombre, item in corridas.items() if item is not None]
    print(f"{'métrica':24}" + "".join(f"{nombre:>16}" for nombre in columnas))
    print(f"{'status':24}" + "".join(f"{corridas[n]['status'][:14]:>16}" for n in columnas))
    tabla: dict[str, dict[str, object]] = {}
    for label, key in METRICS:
        fila = {nombre: corridas[nombre]["record"][key] for nombre in columnas}
        tabla[label] = fila
        print(f"{label:24}" + "".join(f"{str(fila[n]):>16}" for n in columnas))
    for nombre in columnas:
        record = corridas[nombre]["record"]
        tokens = record["tokens"]
        print(
            f"{nombre:24} tokens: total={tokens['total_tokens']} in={tokens['input_tokens']} "
            f"out={tokens['output_tokens']} source={tokens['source']} | "
            f"handoff={record.get('causal_handoff_present')}/{record.get('causal_handoff_chars')} | "
            f"skill={record.get('skill_id')}@{record.get('skill_version')}"
        )
    (EVIDENCE / "experiment-01-round2-delta.json").write_text(
        json.dumps(
            {
                "columns": columnas,
                "table": tabla,
                "runs": {
                    nombre: {
                        "status": corridas[nombre]["status"],
                        "plan": corridas[nombre].get("plan"),
                        "causal_handoff": corridas[nombre].get("causal_handoff", ""),
                        "causal_handoff_chars": corridas[nombre].get("causal_handoff_chars", 0),
                        "record": corridas[nombre]["record"],
                    }
                    for nombre in columnas
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\ndelta: {EVIDENCE / 'experiment-01-round2-delta.json'}")


if __name__ == "__main__":
    main()
