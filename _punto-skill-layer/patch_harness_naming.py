"""Corrige el arnés (nombre de evidencia por versión de skill) y escribe el delta de la ronda 2."""

from __future__ import annotations

import json
import pathlib

HARNESS = pathlib.Path(__file__).resolve().parent / "run_baseline.py"
TABLE = pathlib.Path(__file__).resolve().parent / "round2_table.py"
EVIDENCE = pathlib.Path(__file__).resolve().parent

OLD = '    suffix = "-skill" if skill else ""\n    evidence_name = f"baseline-{mode.lower()}{suffix}.json"\n'
NEW = (
    '    # El nombre lleva la versión de la skill: sin ella, una corrida nueva pisaría la evidencia\n'
    '    # de la anterior y se perdería el control (defecto detectado en la ronda 2).\n'
    '    suffix = ""\n'
    '    if skill:\n'
    '        version = skill.partition("@")[2] or "sin-version"\n'
    '        suffix = f"-skill-{version}"\n'
    '    evidence_name = f"baseline-{mode.lower()}{suffix}.json"\n'
)

TABLE_EXTRA = '''

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
'''

WRITE_ANCHOR = 'if __name__ == "__main__":\n    main()\n'
WRITE_NEW = '''if __name__ == "__main__":
    main()
    (EVIDENCE / "experiment-01-round2-delta.json").write_text(
        json.dumps(_delta(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"delta: {EVIDENCE / 'experiment-01-round2-delta.json'}")
'''


def _patch(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"no encontrado en {path.name}: {old[:60]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    """Aplica el arreglo del arnés y añade el volcado del delta."""
    _patch(HARNESS, OLD, NEW)
    _patch(TABLE, WRITE_ANCHOR, TABLE_EXTRA.strip("\n") + "\n\n\n" + WRITE_NEW)
    print("parcheado")


if __name__ == "__main__":
    main()
