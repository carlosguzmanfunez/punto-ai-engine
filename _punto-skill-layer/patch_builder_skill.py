"""Parche experimento 02: skill del BUILDER (activación por rol) y evidencia de propuestas.

- Motor: `DevelopmentConfig.builder_skill`, activación por rol (una copia por invocación) y
  auditoría con el rol; se conserva el handoff causal tal cual (no se rediseña).
- Telemetría: cuatro campos del skill del BUILDER.
- Arnés: `PUNTO_BUILDER_SKILL`, detalle por llamada (rol/fase/chars/tokens) y resumen estructurado de
  cada propuesta del BUILDER (sin contenido de ficheros), con el diagnóstico del primer intento.
"""

from __future__ import annotations

import pathlib

CYCLE = pathlib.Path(__file__).resolve().parent.parent / "src/punto/orchestrator/dev_cycle.py"
TELEMETRY = pathlib.Path(__file__).resolve().parent.parent / "src/punto/telemetry/efficiency.py"
HARNESS = pathlib.Path(__file__).resolve().parent / "run_baseline.py"

CONFIG_OLD = """    #: Skill experimental del ARCHITECT (``id`` o ``id@version``), declarada por el operador.
"""
CONFIG_NEW = """    #: Skill experimental del BUILDER (``id`` o ``id@version``), declarada por el operador.
    builder_skill: str = ""
    #: Skill experimental del ARCHITECT (``id`` o ``id@version``), declarada por el operador.
"""

STATE_OLD = """    _skill_activation: Any = field(default=None, init=False, repr=False)
"""
STATE_NEW = """    _skill_activations: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
"""

RESET_OLD = """        self._skill_activation = None
"""
RESET_NEW = """        self._skill_activations.clear()
"""

METHOD_OLD = '''        if role is not ProviderRole.ARCHITECT or not self.config.architect_skill.strip():
            return WORKER_INSTRUCTIONS
        if self._skill_activation is None:
'''
METHOD_NEW = '''        declared = {
            ProviderRole.ARCHITECT: self.config.architect_skill,
            ProviderRole.BUILDER: self.config.builder_skill,
        }.get(role, "")
        if not declared.strip():
            return WORKER_INSTRUCTIONS
        key = role.value
        if key not in self._skill_activations:
'''

METHOD_TAIL_OLD = """                activation = activate_skill(
                    self.config.architect_skill,
                    role=role.value,
                    base_instructions=WORKER_INSTRUCTIONS,
                )
"""
METHOD_TAIL_NEW = """                activation = activate_skill(
                    declared,
                    role=role.value,
                    base_instructions=WORKER_INSTRUCTIONS,
                )
"""

AUDIT_OLD = """                    {
                        "skill_reference": self.config.architect_skill,
                        "activated": False,
                        "detail": str(exc)[:300],
                    },
"""
AUDIT_NEW = """                    {
                        "role": role.value,
                        "skill_reference": declared,
                        "activated": False,
                        "detail": str(exc)[:300],
                    },
"""

AUDIT_OK_OLD = """            self._skill_activation = activation
            self._log(
                AuditEventType.DEV_SKILL_ACTIVATED,
                "dev_skill_activated",
                request,
                {
                    "skill_id": activation.skill_id,
"""
AUDIT_OK_NEW = """            self._skill_activations[key] = activation
            self._log(
                AuditEventType.DEV_SKILL_ACTIVATED,
                "dev_skill_activated",
                request,
                {
                    "role": role.value,
                    "skill_id": activation.skill_id,
"""

RETURN_OLD = """        return self._skill_activation.instructions or WORKER_INSTRUCTIONS
"""
RETURN_NEW = """        return self._skill_activations[key].instructions or WORKER_INSTRUCTIONS
"""

TELE_FIELD_OLD = """    #: Handoff causal plan → BUILDER: si viajó y cuánto ocupó (SKILL-LAYER-0, ronda 2).
"""
TELE_FIELD_NEW = """    #: Skill del BUILDER (SKILL-LAYER-0, experimento 02): identificador, versión y tamaño.
    builder_skill_id: str = Field(default="", max_length=80)
    builder_skill_version: str = Field(default="", max_length=20)
    builder_skill_activated: bool = False
    builder_skill_chars: int = Field(default=0, ge=0)

    #: Handoff causal plan → BUILDER: si viajó y cuánto ocupó (SKILL-LAYER-0, ronda 2).
"""

TELE_PARAM_OLD = """    causal_handoff_present: bool = False,
"""
TELE_PARAM_NEW = """    builder_skill_id: str = "",
    builder_skill_version: str = "",
    builder_skill_activated: bool = False,
    builder_skill_chars: int = 0,
    causal_handoff_present: bool = False,
"""

TELE_CTOR_OLD = """        causal_handoff_present=causal_handoff_present,
"""
TELE_CTOR_NEW = """        builder_skill_id=builder_skill_id,
        builder_skill_version=builder_skill_version,
        builder_skill_activated=builder_skill_activated,
        builder_skill_chars=builder_skill_chars,
        causal_handoff_present=causal_handoff_present,
"""

HARNESS_CONFIG_OLD = """    skill_reference = os.environ.get("PUNTO_ARCHITECT_SKILL", "").strip()
"""
HARNESS_CONFIG_NEW = """    skill_reference = os.environ.get("PUNTO_ARCHITECT_SKILL", "").strip()
    builder_skill_reference = os.environ.get("PUNTO_BUILDER_SKILL", "").strip()
"""

HARNESS_CONFIG_USE_OLD = """        config=DevelopmentConfig(max_repair_rounds=2, architect_skill=skill_reference),
"""
HARNESS_CONFIG_USE_NEW = """        config=DevelopmentConfig(
            max_repair_rounds=2,
            architect_skill=skill_reference,
            builder_skill=builder_skill_reference,
        ),
"""

HARNESS_ACTIVATION_OLD = """    activation = next(
        (dict(e.metadata) for e in events if e.event_type.value == "DEV_SKILL_ACTIVATED"), {}
    )
"""
HARNESS_ACTIVATION_NEW = """    activations = [
        dict(e.metadata) for e in events if e.event_type.value == "DEV_SKILL_ACTIVATED"
    ]
    activation = next(
        (item for item in activations if item.get("role", "ARCHITECT") == "ARCHITECT"), {}
    )
    builder_activation = next((item for item in activations if item.get("role") == "BUILDER"), {})
"""

HARNESS_CALLS_OLD = """    evidence = RunEvidence(
"""
HARNESS_CALLS_NEW = """    calls_detail = [
        {
            "role": item["role"],
            "phase": item["phase"],
            "status": item["status"],
            "prompt_chars": item["prompt_chars"],
            "total_tokens": item["total_tokens"],
            "duration_ms": item["duration_ms"],
            "proposal": _proposal_summary(item.get("content", "")),
        }
        for item in observed.calls
    ]
    evidence = RunEvidence(
"""

HARNESS_TELE_OLD = """        causal_handoff_present=bool(
"""
HARNESS_TELE_NEW = """        builder_skill_id=str(builder_activation.get("skill_id", "")),
        builder_skill_version=str(builder_activation.get("skill_version", "")),
        builder_skill_activated=bool(builder_activation.get("activated", False)),
        builder_skill_chars=int(builder_activation.get("chars", 0) or 0),
        causal_handoff_present=bool(
"""

HARNESS_RETURN_OLD = """        "audit_events": [
"""
HARNESS_RETURN_NEW = """        "calls_detail": calls_detail,
        "first_attempt": _first_attempt(result, calls_detail, handoff),
        "audit_events": [
"""

OBSERVER_OLD = """                "prompt_chars": len(prompt),
                "context_chars": len(context),
            }
"""
OBSERVER_NEW = """                "prompt_chars": len(prompt),
                "context_chars": len(context),
                "content": getattr(result, "content", "") or "",
            }
"""

HELPERS_ANCHOR = """def _run_case(case: dict[str, Any], *, mode: str, root: Path) -> dict[str, Any]:
"""
HELPERS = '''def _proposal_summary(content: str) -> dict[str, Any]:
    """Resumen **estructurado** de lo que propuso el BUILDER, sin contenido de ficheros.

    Es lo que permite diagnosticar qué criterio o recurso quedó sin cubrir sin volver a llamar al
    proveedor: rutas, operaciones, criterio de aceptación citado y si declaró causa raíz.
    """
    import json as _json

    try:
        payload = _json.loads(content)
    except (ValueError, TypeError):
        return {"parsed": False, "chars": len(content)}
    if not isinstance(payload, dict):
        return {"parsed": False, "chars": len(content)}
    changes = payload.get("changes")
    resumen = {
        "parsed": True,
        "chars": len(content),
        "changes": [
            {
                "path": str(item.get("path", "")),
                "operation": str(item.get("operation", "")),
                "acceptance_criterion": str(item.get("acceptance_criterion", "")),
                "reason": str(item.get("reason", ""))[:120],
            }
            for item in (changes if isinstance(changes, list) else [])
            if isinstance(item, dict)
        ],
        "root_cause": str(payload.get("root_cause", ""))[:200],
        "scope_expansion": bool(payload.get("scope_expansion")),
        "context_requests": len(payload.get("context_requests") or []),
    }
    return resumen


def _first_attempt(result: Any, calls_detail: list[dict[str, Any]], handoff: str) -> dict[str, Any]:
    """Calidad del **primer** intento del BUILDER, en hechos observables.

    ``first_attempt_pass`` es verdadero solo si el ciclo cerró sin ninguna ronda de reparación: eso
    significa que la primera propuesta, aplicada, pasó la verificación. Cuando no, se dice qué
    criterios del handoff no aparecen citados y qué recursos del plan no se tocaron.
    """
    import json as _json

    proposals = [item for item in calls_detail if item["role"] == "BUILDER"]
    primera = proposals[0]["proposal"] if proposals else {}
    try:
        handoff_payload = _json.loads(handoff) if handoff else {}
    except ValueError:
        handoff_payload = {}
    criterios = [str(item) for item in handoff_payload.get("done", [])]
    recursos = [str(item) for item in handoff_payload.get("resources", [])]
    citados = [str(change.get("acceptance_criterion", "")) for change in primera.get("changes", [])]
    tocados = [str(change.get("path", "")) for change in primera.get("changes", [])]
    return {
        "first_attempt_pass": result.repair_rounds == 0 and result.completed,
        "repair_rounds": result.repair_rounds,
        "builder_calls": len(proposals),
        "missing_acceptance": [item for item in criterios if item and item not in citados],
        "missing_resources": [item for item in recursos if item not in tocados],
        "declared_root_cause": bool(primera.get("root_cause")),
        "failed_verifications": [item.name for item in result.verification if not item.passed],
    }


def _run_case(case: dict[str, Any], *, mode: str, root: Path) -> dict[str, Any]:
'''

HARNESS_WRITE_OLD = """                    "audit_events": item.get("audit_events", []),
"""
HARNESS_WRITE_NEW = """                    "audit_events": item.get("audit_events", []),
                    "calls_detail": item.get("calls_detail", []),
                    "first_attempt": item.get("first_attempt", {}),
"""


def _patch(path: pathlib.Path, pairs: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"no encontrado en {path.name}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    """Aplica los cambios del experimento 02."""
    _patch(
        CYCLE,
        (
            (CONFIG_OLD, CONFIG_NEW),
            (STATE_OLD, STATE_NEW),
            (RESET_OLD, RESET_NEW),
            (METHOD_OLD, METHOD_NEW),
            (METHOD_TAIL_OLD, METHOD_TAIL_NEW),
            (AUDIT_OLD, AUDIT_NEW),
            (AUDIT_OK_OLD, AUDIT_OK_NEW),
            (RETURN_OLD, RETURN_NEW),
        ),
    )
    _patch(
        TELEMETRY,
        (
            (TELE_FIELD_OLD, TELE_FIELD_NEW),
            (TELE_PARAM_OLD, TELE_PARAM_NEW),
            (TELE_CTOR_OLD, TELE_CTOR_NEW),
        ),
    )
    _patch(
        HARNESS,
        (
            (HELPERS_ANCHOR, HELPERS),
            (OBSERVER_OLD, OBSERVER_NEW),
            (HARNESS_CONFIG_OLD, HARNESS_CONFIG_NEW),
            (HARNESS_CONFIG_USE_OLD, HARNESS_CONFIG_USE_NEW),
            (HARNESS_ACTIVATION_OLD, HARNESS_ACTIVATION_NEW),
            (HARNESS_CALLS_OLD, HARNESS_CALLS_NEW),
            (HARNESS_TELE_OLD, HARNESS_TELE_NEW),
            (HARNESS_RETURN_OLD, HARNESS_RETURN_NEW),
            (HARNESS_WRITE_OLD, HARNESS_WRITE_NEW),
        ),
    )
    print("parcheado")


if __name__ == "__main__":
    main()
