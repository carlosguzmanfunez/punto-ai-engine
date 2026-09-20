"""Parche de la ronda 2: handoff causal compacto plan → BUILDER (y su medida).

Tres cambios mínimos, ninguno toca autoridad, verificación ni proveedor:

- `DevelopmentConfig.causal_handoff` (configurable para poder reproducir el control);
- `causal_handoff(plan)`: función **pura** que serializa la parte operativa del plan ya validado;
- el prompt del BUILDER incluye ese handoff y el ciclo deja constancia en auditoría.
"""

from __future__ import annotations

import pathlib

CYCLE = pathlib.Path(__file__).resolve().parent.parent / "src/punto/orchestrator/dev_cycle.py"
TELEMETRY = pathlib.Path(__file__).resolve().parent.parent / "src/punto/telemetry/efficiency.py"

CONFIG_ANCHOR = """    #: Skill experimental del ARCHITECT (``id`` o ``id@version``), declarada por el operador.
"""

CONFIG_NEW = """    #: Handoff causal plan → BUILDER: transporta la parte operativa del plan ya validado
    #: (fuente, consumidores, cadena funcional y qué demuestra cada criterio). Configurable para
    #: poder reproducir el control sin él.
    causal_handoff: bool = True
    #: Skill experimental del ARCHITECT (``id`` o ``id@version``), declarada por el operador.
"""

PROMPT_ANCHOR = """            "PLAN FILES TO CREATE: " + (" | ".join(plan.files_to_create) or "(ninguno)"),
        ]
"""

PROMPT_NEW = """            "PLAN FILES TO CREATE: " + (" | ".join(plan.files_to_create) or "(ninguno)"),
        ]
        if self.config.causal_handoff:
            lines.append(CAUSAL_HANDOFF_LABEL + causal_handoff(plan))
"""

FUNCTION_ANCHOR = """def default_development_cycle(
"""

FUNCTION_NEW = '''#: Etiqueta del handoff causal en el prompt del BUILDER: dice de dónde sale y qué hacer con él.
CAUSAL_HANDOFF_LABEL: Final[str] = (
    "CAUSAL HANDOFF (del plan ya validado por PUNTO; úsalo, no lo rederives): "
)


def causal_handoff(plan: DevelopmentPlan) -> str:
    """Handoff causal compacto del plan al BUILDER, en una línea JSON determinista.

    Transporta **solo** la parte operativa que hoy se perdía: el objetivo, los recursos del plan, la
    cadena funcional con su verificación por eslabón, qué observación demuestra cada criterio y las
    verificaciones del catálogo. No incluye riesgos, razonamiento, autoridad, PELL ni auditoría: es
    información para implementar, no un plan paralelo ni un ensayo.
    """
    payload = {
        "goal": plan.summary,
        "resources": list(plan.touched_paths()),
        "chain": [
            {"step": step.step, "verification": step.verification}
            for step in plan.functional_chain
        ],
        "done": list(plan.acceptance_mapping),
        "verify": list(plan.verification_commands),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def default_development_cycle(
'''

AUDIT_ANCHOR = """        context_files: list[ContextFile] = list(inventory["selected"])
"""

AUDIT_NEW = """        if self.config.causal_handoff:
            handoff = causal_handoff(plan)
            self._log(
                AuditEventType.DEV_CAUSAL_HANDOFF,
                "dev_causal_handoff",
                request,
                {
                    "present": True,
                    "chars": len(handoff),
                    "sha256": hashlib.sha256(handoff.encode("utf-8")).hexdigest(),
                    "keys": sorted(json.loads(handoff)),
                },
            )

        context_files: list[ContextFile] = list(inventory["selected"])
"""

TELEMETRY_FIELD_ANCHOR = """    #: Skill activada en esta ejecución (SKILL-LAYER-0). Vacío y ``False`` significan el control.
"""
TELEMETRY_FIELD_NEW = """    #: Handoff causal plan → BUILDER: si viajó y cuánto ocupó (SKILL-LAYER-0, ronda 2).
    causal_handoff_present: bool = False
    causal_handoff_chars: int = Field(default=0, ge=0)

    #: Skill activada en esta ejecución (SKILL-LAYER-0). Vacío y ``False`` significan el control.
"""

TELEMETRY_PARAM_ANCHOR = """    skill_id: str = "",
"""
TELEMETRY_PARAM_NEW = """    causal_handoff_present: bool = False,
    causal_handoff_chars: int = 0,
    skill_id: str = "",
"""

TELEMETRY_CTOR_ANCHOR = """        skill_id=skill_id,
"""
TELEMETRY_CTOR_NEW = """        causal_handoff_present=causal_handoff_present,
        causal_handoff_chars=causal_handoff_chars,
        skill_id=skill_id,
"""


def _patch(path: pathlib.Path, pairs: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"no encontrado en {path.name}: {old[:60]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    """Aplica los cambios del handoff causal."""
    _patch(
        CYCLE,
        (
            (CONFIG_ANCHOR, CONFIG_NEW),
            (PROMPT_ANCHOR, PROMPT_NEW),
            (AUDIT_ANCHOR, AUDIT_NEW),
            (FUNCTION_ANCHOR, FUNCTION_NEW),
        ),
    )
    _patch(
        TELEMETRY,
        (
            (TELEMETRY_FIELD_ANCHOR, TELEMETRY_FIELD_NEW),
            (TELEMETRY_PARAM_ANCHOR, TELEMETRY_PARAM_NEW),
            (TELEMETRY_CTOR_ANCHOR, TELEMETRY_CTOR_NEW),
        ),
    )
    print("parcheado")


if __name__ == "__main__":
    main()
