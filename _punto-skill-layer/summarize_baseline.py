"""Resume el baseline y escribe ``baseline-efficiency.jsonl`` (una línea por ejecución).

Salida:

- tabla por caso (determinista y real) con las dimensiones del §24 del encargo;
- contexto repetido por invocación (§16): bloques que PUNTO envía en cada llamada;
- overhead medido de la instrumentación (§22);
- formato de comparación BASELINE vs SKILL preparado, con SKILL = N/A (§18).
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
ENGINE = EVIDENCE.parent


def _load(name: str) -> list[dict]:
    """Carga la evidencia de un modo, si existe."""
    path = EVIDENCE / name
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _records(items: list[dict]) -> list[dict]:
    """Registros de eficiencia de una lista de resultados."""
    return [item["record"] for item in items]


def _table(items: list[dict], title: str) -> None:
    """Imprime la tabla de un modo."""
    if not items:
        print(f"\n== {title}: sin datos ==")
        return
    print(f"\n== {title} ==")
    header = (
        "caso  tipo  estado                 llam  fases                              "
        "repar  expan  verif  fall  chain  prompt_c  ctx_c  elapsed_ms"
    )
    print(header)
    for item in items:
        record = item["record"]
        print(
            f"{item['case']}  {item['kind']}     {item['status']:20} "
            f"{record['provider_calls']:>4}  {str(record['provider_calls_by_phase']):32} "
            f"{record['repair_rounds']:>5}  {record['scope_expansions']:>5}  "
            f"{record['verification_count']:>5}  {record['verification_failures']:>4}  "
            f"{str(record['functional_chain_pass']):>5}  {record['prompt_chars']:>8}  "
            f"{record['context_chars']:>5}  {record['elapsed_ms']:>10}"
        )


def _aggregate(items: list[dict]) -> dict[str, float | int | str]:
    """Agregados del baseline (medias por ejecución, con la mediana de tiempo)."""
    records = _records(items)
    if not records:
        return {}
    return {
        "runs": len(records),
        "provider_calls_total": sum(item["provider_calls"] for item in records),
        "provider_calls_media": round(
            statistics.fmean(item["provider_calls"] for item in records), 2
        ),
        "prompt_chars_media": round(statistics.fmean(item["prompt_chars"] for item in records)),
        "context_chars_media": round(statistics.fmean(item["context_chars"] for item in records)),
        "elapsed_ms_mediana": round(statistics.median(item["elapsed_ms"] for item in records)),
        "verification_media": round(
            statistics.fmean(item["verification_count"] for item in records), 2
        ),
        "repair_rounds_total": sum(item["repair_rounds"] for item in records),
        "success": f"{sum(1 for item in records if item['success'])}/{len(records)}",
        "functional_chain_pass": (
            f"{sum(1 for item in records if item['functional_chain_pass'])}/{len(records)}"
        ),
    }


def _overhead(items: list[dict]) -> dict[str, float]:
    """Overhead medido de la instrumentación: construir y serializar un registro."""
    from punto.telemetry import EfficiencyRecord

    records = [EfficiencyRecord.model_validate(item["record"]) for item in items]
    if not records:
        return {}
    started = time.perf_counter()
    for _ in range(50):
        for record in records:
            record.as_dict()
    derive_ms = (time.perf_counter() - started) * 1000 / (50 * len(records))
    from punto.telemetry import record_line

    started = time.perf_counter()
    for _ in range(50):
        for record in records:
            record_line(record)
    serialize_ms = (time.perf_counter() - started) * 1000 / (50 * len(records))
    recorded = [item["overhead_ms"] for item in items if item.get("overhead_ms")]
    return {
        "derivar_registro_ms": round(derive_ms, 4),
        "serializar_linea_ms": round(serialize_ms, 4),
        "medido_durante_la_corrida_ms": (
            round(statistics.fmean(recorded), 4) if recorded else 0.0
        ),
    }


def _comparison(items: list[dict]) -> None:
    """Formato de comparación BASELINE vs SKILL (§18), con SKILL = N/A en esta etapa."""
    records = _records(items)
    if not records:
        return
    print("\n== formato de comparación (SKILL = N/A en esta etapa) ==")
    print(f"{'metric':24} {'baseline':>14} {'skill':>8}")
    dimensions = (
        ("success", sum(1 for item in records if item["success"])),
        ("elapsed_ms (mediana)", round(statistics.median(item["elapsed_ms"] for item in records))),
        ("provider_calls (media)", round(statistics.fmean(item["provider_calls"] for item in records), 2)),
        ("repair_rounds (total)", sum(item["repair_rounds"] for item in records)),
        ("tool_calls (total)", sum(item["tool_calls"] for item in records)),
        ("prompt_chars (media)", round(statistics.fmean(item["prompt_chars"] for item in records))),
        ("context_chars (media)", round(statistics.fmean(item["context_chars"] for item in records))),
        ("files_read (media)", round(statistics.fmean(item["files_read"] for item in records), 2)),
        ("verification_count (media)", round(statistics.fmean(item["verification_count"] for item in records), 2)),
        ("functional_chain_pass", sum(1 for item in records if item["functional_chain_pass"])),
    )
    for label, value in dimensions:
        print(f"{label:24} {value:>14} {'N/A':>8}")


def main() -> int:
    """Resume ambos modos y escribe el JSONL del baseline."""
    from punto.telemetry import EfficiencyRecord, write_jsonl

    deterministic = _load("baseline-deterministic.json")
    real = _load("baseline-real.json")

    _table(deterministic, "BASELINE DETERMINISTA (proveedores guionizados)")
    _table(real, "BASELINE REAL (proveedores configurados)")

    print("\n== agregados ==")
    print("determinista:", json.dumps(_aggregate(deterministic), ensure_ascii=False))
    print("real:        ", json.dumps(_aggregate(real), ensure_ascii=False))

    blocks = deterministic[0]["repeated_blocks"] if deterministic else {}
    print("\n== contexto repetido por invocación (§16) ==")
    for name, chars in blocks.items():
        print(f"  {name:22} {chars:>6} chars")
    print(f"  {'total por invocación':22} {sum(blocks.values()):>6} chars")

    print("\n== overhead de la instrumentación (§22) ==")
    print(json.dumps(_overhead(deterministic), ensure_ascii=False))

    _comparison(deterministic)

    records = [
        EfficiencyRecord.model_validate(item["record"]) for item in (*deterministic, *real)
    ]
    written = write_jsonl(
        EVIDENCE / "baseline-efficiency.jsonl", records
    )
    print(f"\nbaseline-efficiency.jsonl: {len(records)} registros, {written} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
