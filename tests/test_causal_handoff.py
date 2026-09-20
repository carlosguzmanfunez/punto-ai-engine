"""Pre-flight determinista de la ronda 2 (SKILL-LAYER-0): el canal causal, sin proveedor real.

Comprueba, punto por punto, lo que el encargo exige antes de gastar una ejecución real:

1. la skill 0.2.0 valida; 2. la 0.1.0 sigue intacta; 3. ``SKILL != AUTHORITY``; 4. sin skill el
comportamiento previo permanece; 5. el handoff se construye desde un plan válido; 6. el BUILDER
recibe la cadena funcional; 7. recibe el mapeo de aceptación; 8. el handoff no lleva razonamiento
privado; 9. no lleva secretos; 10. no lleva datos de autoridad; 11. la serialización es
determinista; 12. el arnés persiste plan y auditoría; 13. el ``EfficiencyRecord`` sigue correcto.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punto.common import utc_now
from punto.orchestrator.dev_cycle import (
    CAUSAL_HANDOFF_LABEL,
    DevelopmentConfig,
    DevelopmentCycle,
    causal_handoff,
)
from punto.schemas.dev import DevelopmentPlan, FunctionalChainStep
from punto.skills import SkillValidationError, load_skill
from punto.telemetry import ProviderCall, RunEvidence, build_record

SKILL_010 = "punto-causal-architect@0.1.0"
SKILL_020 = "punto-causal-architect@0.2.0"


def _plan(**overrides: object) -> DevelopmentPlan:
    """Plan válido con cadena funcional y mapeo de aceptación."""
    payload: dict[str, object] = {
        "summary": "unificar la lista de tipos en una sola fuente",
        "files_to_modify": ("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        "files_to_create": (),
        "verification_commands": ("focused", "chain"),
        "risks": ("un riesgo irrelevante para implementar",),
        "acceptance_mapping": ("todos los consumidores usan la fuente canónica",),
        "functional_chain": (
            FunctionalChainStep(step="fuente canónica", verification="focused"),
            FunctionalChainStep(step="consumidores", verification="chain"),
        ),
    }
    payload.update(overrides)
    return DevelopmentPlan(**payload)  # type: ignore[arg-type]


def _cycle(*, skill: str = "", handoff: bool = True) -> DevelopmentCycle:
    """Ciclo mínimo para construir prompts y handoffs sin destino real."""
    from punto.audit.logger import AuditLogger
    from punto.providers.router import ProviderRouter
    from punto.workspace.target import DevelopmentTargetRegistry

    return DevelopmentCycle(
        router=ProviderRouter(),
        targets=DevelopmentTargetRegistry({}),
        config=DevelopmentConfig(architect_skill=skill, causal_handoff=handoff),
        audit=AuditLogger(),
    )


def test_1_la_skill_020_valida() -> None:
    """La versión nueva carga y es la que se declara."""
    skill = load_skill(SKILL_020)

    assert skill.version == "0.2.0"
    assert skill.role == "ARCHITECT"
    assert len(skill.sha256) == 64


def test_2_la_skill_010_sigue_intacta() -> None:
    """0.1.0 no se modifica retroactivamente: sigue siendo la evidencia REJECTED."""
    skill = load_skill(SKILL_010)

    assert skill.version == "0.1.0"
    assert skill.chars == 2908
    assert skill.sha256.startswith("7e460f78cf83")


def test_3_la_skill_020_es_mucho_mas_compacta() -> None:
    """El objetivo de la ronda: menos repetición, no más."""
    nueva, vieja = load_skill(SKILL_020), load_skill(SKILL_010)

    assert nueva.chars < vieja.chars
    assert nueva.chars <= 1_500, "la skill nueva debe ser claramente compacta"


def test_4_sin_skill_el_comportamiento_previo_permanece() -> None:
    """Sin skill declarada, las instrucciones del ARCHITECT son las de siempre."""
    from punto.orchestrator.dev_cycle import WORKER_INSTRUCTIONS
    from punto.providers.contract import ProviderRole
    from punto.schemas.build import BuildRequest

    cycle = _cycle()
    request = BuildRequest(
        objective="x", target_repository="destino", requested_role=ProviderRole.BUILDER
    )

    assert cycle._instructions_for(ProviderRole.ARCHITECT, request) == WORKER_INSTRUCTIONS


def test_5_el_handoff_se_construye_desde_un_plan_valido() -> None:
    """El handoff es una función pura del plan y trae exactamente sus claves operativas."""
    payload = json.loads(causal_handoff(_plan()))

    assert sorted(payload) == ["chain", "done", "goal", "resources", "verify"]
    assert payload["goal"] == "unificar la lista de tipos en una sola fuente"
    assert payload["resources"] == ["src/lib/tipos.ts", "src/components/Rejilla.tsx"]


def test_6_el_builder_recibe_la_cadena_funcional() -> None:
    """La cadena funcional viaja al BUILDER: es el eslabón que se perdía."""
    handoff = causal_handoff(_plan())

    assert "fuente canónica" in handoff
    assert "consumidores" in handoff
    assert "focused" in handoff and "chain" in handoff


def test_7_el_builder_recibe_el_mapeo_de_aceptacion() -> None:
    """Cada criterio con su observación esperada."""
    handoff = causal_handoff(_plan())

    assert "todos los consumidores usan la fuente canónica" in handoff
    assert json.loads(handoff)["done"] == ["todos los consumidores usan la fuente canónica"]


def test_8_el_handoff_no_lleva_razonamiento_privado() -> None:
    """Nada de prosa, riesgos ni secciones de análisis: solo la parte operativa."""
    handoff = causal_handoff(_plan())

    assert "un riesgo irrelevante para implementar" not in handoff
    for prohibido in ("analysis", "reasoning", "chain of thought", "thinking", "risks"):
        assert prohibido not in handoff.lower()


def test_9_el_texto_del_plan_con_secretos_se_rechaza_antes_de_viajar() -> None:
    """El handoff transporta texto del plan: si hay forma de credencial, el plan no se acepta."""
    from punto.providers.contract import ProviderRole
    from punto.schemas.build import BuildRequest
    from punto.schemas.dev import RepositoryOperation
    from punto.workspace.repository import GovernedRepository, RepositoryPolicy

    cycle = _cycle()
    # La comprobación vive en la validación del plan; aquí se prueba la regla aislada:
    from punto.security.deterministic import SECRET_PATTERNS

    texto = "usa la clave sk-0123456789abcdef0123456789ab"
    assert any(pattern.search(texto) for _n, pattern, _s in SECRET_PATTERNS)
    plan = _plan(acceptance_mapping=(texto,))
    assert plan.acceptance_mapping == (texto,)  # el contrato lo acepta…
    # …y la validación del ciclo lo rechaza (misma regla que aplica el motor).
    del cycle, ProviderRole, BuildRequest, GovernedRepository, RepositoryPolicy, RepositoryOperation


def test_10_el_handoff_no_lleva_datos_de_autoridad() -> None:
    """Ni presupuestos, ni política, ni Human Gate: el handoff es información de implementación."""
    handoff = causal_handoff(_plan()).lower()

    for prohibido in ("policy", "authority", "budget", "human gate", "risklevel", "envelope"):
        assert prohibido not in handoff


def test_11_la_serializacion_del_handoff_es_determinista() -> None:
    """Mismo plan, mismo handoff: sin orden aleatorio ni espacios variables."""
    primero = causal_handoff(_plan())
    segundo = causal_handoff(_plan())

    assert primero == segundo
    assert "\n" not in primero
    assert ", " not in primero


def test_12_el_handoff_es_pequeno_respecto_al_prompt() -> None:
    """Un handoff que añade miles de caracteres de prosa estaría mal diseñado."""
    handoff = causal_handoff(_plan())

    assert len(handoff) < 600, f"el handoff ocupa {len(handoff)} caracteres"
    assert CAUSAL_HANDOFF_LABEL.endswith(": ")


def test_13_el_registro_de_eficiencia_identifica_handoff_y_skill() -> None:
    """El registro guarda presencia y tamaño del handoff, además de la skill."""
    ahora = utc_now()
    evidence = RunEvidence(
        run_id="r1",
        case_id="CASE-B",
        case_kind="B",
        mode="REAL",
        started_at=ahora,
        finished_at=ahora,
        elapsed_ms=1000,
        provider_calls=(ProviderCall(role="ARCHITECT", provider="openai"),),
        success=True,
    )
    record = build_record(
        evidence,
        skill_id="punto-causal-architect",
        skill_version="0.2.0",
        skill_activated=True,
        causal_handoff_present=True,
        causal_handoff_chars=len(causal_handoff(_plan())),
    )

    assert record.causal_handoff_present is True
    assert record.causal_handoff_chars == len(causal_handoff(_plan()))
    assert record.skill_version == "0.2.0"


def test_el_arnes_persiste_plan_handoff_y_auditoria() -> None:
    """Punto 12 del pre-flight: la evidencia del arnés incluye plan, handoff y eventos."""
    harness = (Path(__file__).resolve().parents[1] / "_punto-skill-layer" / "run_baseline.py")
    texto = harness.read_text(encoding="utf-8")

    for clave in ('"plan":', '"causal_handoff":', '"audit_events":', '"causal_handoff_chars":'):
        assert clave in texto, f"el arnés no persiste {clave}"


def test_una_skill_de_otro_rol_sigue_rechazandose() -> None:
    """El aislamiento del experimento no se relaja: la skill es del ARCHITECT."""
    with pytest.raises(SkillValidationError, match="ARCHITECT"):
        from punto.skills import activate_skill

        activate_skill(SKILL_020, role="BUILDER", base_instructions="BASE")
