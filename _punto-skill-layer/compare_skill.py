"""Compara el baseline congelado con la ejecución con skill (experimento 01).

No modifica el baseline: lee ``baseline-real.json`` (control) y ``baseline-real-skill.json``
(experimento) y calcula el delta por dimensión, con la lista de métricas del encargo.
"""

from __future__ import annotations

import json
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent

METRICS: tuple[tuple[str, str], ...] = (
    ("success", "success"),
    ("functional_chain_pass", "functional_chain_pass"),
    ("elapsed_ms", "elapsed_ms"),
    ("provider_calls", "provider_calls"),
    ("repair_rounds", "repair_rounds"),
    ("scope_expansions", "scope_expansions"),
    ("tool_calls", "tool_calls"),
    ("prompt_chars", "prompt_chars"),
    ("context_chars", "context_chars"),
    ("files_read", "files_read"),
    ("verification_count", "verification_count"),
    ("human_gates", "human_gates"),
)


def _load(name: str) -> dict[str, dict]:
    """Índice de casos por identificador."""
    path = EVIDENCE / name
    if not path.exists():
        return {}
    return {item["case"]: item for item in json.loads(path.read_text(encoding="utf-8"))}


def _tokens(item: dict) -> int | None:
    """Tokens reales de una ejecución, si el transporte los expuso."""
    tokens = item["record"]["tokens"]
    return tokens["total_tokens"] if tokens["source"] == "REAL" else None


def main() -> None:
    """Imprime el delta por caso y lo guarda como evidencia."""
    control = _load("baseline-real.json")
    experiment = _load("baseline-real-skill.json")
    if not experiment:
        raise SystemExit("no hay ejecución con skill todavía")

    deltas: dict[str, dict] = {}
    for case_id, item in experiment.items():
        base = control.get(case_id)
        if base is None:
            continue
        record, base_record = item["record"], base["record"]
        print(f"\n=== {case_id} · {base['status']} -> {item['status']} ===")
        print(f"skill: {record['skill_id']}@{record['skill_version']} activada={record['skill_activated']}")
        print(f"llamadas por rol: {base_record['provider_calls_by_role']} -> {record['provider_calls_by_role']}")
        print(f"{'métrica':22} {'baseline':>12} {'skill':>12} {'delta':>10}")
        print(f"{'status':22} {base['status']:>12} {item['status']:>12} {'':>10}")
        row: dict[str, object] = {
            "status_baseline": base["status"],
            "status_skill": item["status"],
            "skill_reference": f"{record['skill_id']}@{record['skill_version']}",
        }
        for label, key in METRICS:
            before, after = base_record[key], record[key]
            delta = (
                int(after) - int(before)
                if isinstance(before, (int, float)) and isinstance(after, (int, float))
                else ""
            )
            print(f"{label:22} {str(before):>12} {str(after):>12} {str(delta):>10}")
            row[label] = {"baseline": before, "skill": after, "delta": delta}
        tokens_before, tokens_after = _tokens(base), _tokens(item)
        print(f"{'tokens reales':22} {str(tokens_before):>12} {str(tokens_after):>12} "
              f"{str((tokens_after - tokens_before) if tokens_before and tokens_after else ''):>10}")
        row["tokens_reales"] = {"baseline": tokens_before, "skill": tokens_after}
        row["provider_calls_by_role"] = {
            "baseline": base_record["provider_calls_by_role"],
            "skill": record["provider_calls_by_role"],
        }
        row["skill_chars"] = record.get("skill_chars", 0)
        deltas[case_id] = row

    (EVIDENCE / "experiment-01-delta.json").write_text(
        json.dumps(deltas, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\ndelta guardado: {EVIDENCE / 'experiment-01-delta.json'}")


if __name__ == "__main__":
    main()
