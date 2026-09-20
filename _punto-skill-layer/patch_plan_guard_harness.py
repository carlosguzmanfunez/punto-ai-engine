"""Parche ronda 2: guardián de secretos en el texto del plan y evidencia completa del arnés.

- Motor: un plan con forma de credencial en su **texto** (resumen, riesgos, mapeo de aceptación o
  descripción de un eslabón) se rechaza. El handoff transporta ahora esos campos al BUILDER, así que
  la frontera de secretos se extiende a donde el texto viaja.
- Arnés: persistir plan, eventos de auditoría y handoff causal, para poder atribuir el resultado sin
  volver a gastar una ejecución de proveedor.
"""

from __future__ import annotations

import pathlib

CYCLE = pathlib.Path(__file__).resolve().parent.parent / "src/punto/orchestrator/dev_cycle.py"
HARNESS = pathlib.Path(__file__).resolve().parent / "run_baseline.py"

PLAN_GUARD_ANCHOR = """        if request.acceptance_criteria and not plan.acceptance_mapping:
"""

PLAN_GUARD_NEW = '''        plan_text = (
            plan.summary,
            *plan.risks,
            *plan.acceptance_mapping,
            *(step.description for step in plan.functional_chain),
        )
        if any(
            pattern.search(value)
            for value in plan_text
            for _name, pattern, _severity in SECRET_PATTERNS
        ):
            issues.append(
                BuildValidationIssue(
                    code="PLAN_SECRET_TEXT",
                    detail=(
                        "el texto del plan contiene algo con forma de credencial: el plan viaja al "
                        "BUILDER en el handoff causal, así que no cruza esta frontera"
                    ),
                )
            )
        if request.acceptance_criteria and not plan.acceptance_mapping:
'''

IMPORT_ANCHOR = "from punto.policy.config_loader import ConfigLoader\n"
IMPORT_NEW = (
    "from punto.policy.config_loader import ConfigLoader\n"
    "from punto.security.deterministic import SECRET_PATTERNS\n"
)

HARNESS_RETURN_ANCHOR = """    return {
        "record": record,
        "status": result.status.value,
"""
HARNESS_RETURN_NEW = """    from punto.orchestrator.dev_cycle import causal_handoff

    handoff = causal_handoff(result.plan) if result.plan is not None else ""
    return {
        "record": record,
        "status": result.status.value,
        "plan": result.plan.model_dump(mode="json") if result.plan is not None else None,
        "causal_handoff": handoff,
        "causal_handoff_chars": len(handoff),
        "audit_events": [
            {
                "event_type": event.event_type.value,
                "result": event.result.value,
                "metadata": dict(event.metadata),
            }
            for event in events
        ],
"""

HARNESS_WRITE_ANCHOR = """                    "overhead_ms": round(item["overhead_ms"], 3),
"""
HARNESS_WRITE_NEW = """                    "overhead_ms": round(item["overhead_ms"], 3),
                    "plan": item.get("plan"),
                    "causal_handoff": item.get("causal_handoff", ""),
                    "causal_handoff_chars": item.get("causal_handoff_chars", 0),
                    "audit_events": item.get("audit_events", []),
"""

HARNESS_TELEMETRY_ANCHOR = """        skill_id=str(activation.get("skill_id", "")),
"""
HARNESS_TELEMETRY_NEW = """        causal_handoff_present=bool(
            next(
                (
                    dict(e.metadata).get("present")
                    for e in events
                    if e.event_type.value == "DEV_CAUSAL_HANDOFF"
                ),
                False,
            )
        ),
        causal_handoff_chars=int(
            next(
                (
                    dict(e.metadata).get("chars", 0)
                    for e in events
                    if e.event_type.value == "DEV_CAUSAL_HANDOFF"
                ),
                0,
            )
        ),
        skill_id=str(activation.get("skill_id", "")),
"""


def _patch(path: pathlib.Path, pairs: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"no encontrado en {path.name}: {old[:60]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    """Aplica los cambios al motor y al arnés."""
    _patch(CYCLE, ((IMPORT_ANCHOR, IMPORT_NEW), (PLAN_GUARD_ANCHOR, PLAN_GUARD_NEW)))
    _patch(
        HARNESS,
        (
            (HARNESS_RETURN_ANCHOR, HARNESS_RETURN_NEW),
            (HARNESS_WRITE_ANCHOR, HARNESS_WRITE_NEW),
            (HARNESS_TELEMETRY_ANCHOR, HARNESS_TELEMETRY_NEW),
        ),
    )
    print("parcheado")


if __name__ == "__main__":
    main()
