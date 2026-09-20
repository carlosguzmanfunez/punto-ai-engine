"""Pre-flight determinista del EXPERIMENTO 03 (sin proveedor real).

Comprueba, **antes** de gastar una ejecución real de CASE-B, los veinte puntos que el encargo
exige: validación y tamaño de ``punto-focused-resolution@0.1.0``, aislamiento por fase (la skill no
viaja al ARCHITECT ni a la implementación inicial), mapeo determinista fallo → recurso, registro de
recursos tocados, distinción entre *cambiar el parche* y *cambiar la estrategia*, detección de
estancamiento causal, estados ``CHANGED`` / ``UNCHANGED_BY_EVIDENCE`` / ``BLOCKED_BY_SCOPE`` /
``UNEXPLAINED``, invariantes de autoridad y de límites, persistencia de la evidencia,
``EfficiencyRecord``, determinismo y ausencia de secretos.

Tres pruebas recorren el **ciclo real** con un proveedor guionizado sobre un repositorio Git real:
la resolución que aborda el recurso discriminante, la que repite el parche anterior y el recurso
relevante que el plan no autoriza.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from punto.audit.logger import AuditLogger
from punto.common import utc_now
from punto.orchestrator.dev_cycle import (
    BUILD_CONTRACT,
    BUILD_SCHEMA,
    CAUSAL_HANDOFF_LABEL,
    WORKER_INSTRUCTIONS,
    DevelopmentConfig,
    DevelopmentCycle,
)
from punto.orchestrator.focused_resolution import (
    BLOCKED_BY_SCOPE,
    CAUSAL_STAGNATION_LABEL,
    CHANGED,
    IMPLEMENTATION_PHASE,
    RELATION_PLAN_CHAIN,
    RELATION_VERIFICATION_ARGV,
    RESOLUTION_INPUT_LABEL,
    RESOLUTION_PHASE,
    UNCHANGED_BY_EVIDENCE,
    UNEXPLAINED,
    candidate_paths,
    causal_progress,
    declared_unchanged,
    duplicated_chars,
    escalation_resources,
    failure_map,
    resolution_block,
    resource_statuses,
)
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ModelCompletion
from punto.providers.contract import ModelUsage, ProviderRole
from punto.providers.router import ProviderRouter
from punto.schemas.build import BuildRequest
from punto.schemas.dev import (
    CommandEvidence,
    DevelopmentPlan,
    DevelopmentStatus,
    FunctionalChainStep,
    RepositoryOperation,
)
from punto.skills import SkillValidationError, activate_skill, load_skill
from punto.telemetry import ProviderCall, RunEvidence, build_record, record_line
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetRegistry,
    VerificationCommand,
)

SKILL = "punto-focused-resolution@0.1.0"
#: Versión de la ronda final de refinación (EXPERIMENTO 03 · D-5). La 0.1.0 se conserva intacta.
SKILL_V2 = "punto-focused-resolution@0.2.0"
SKILL_ID = "punto-focused-resolution"
TARGET_ID = "preflight-fixture"
WORK_BRANCH = "ai/skill-layer-baseline"

#: Verificación focalizada del montaje: mide **un** recurso, y lo dice en su propio ``argv``.
FOCUSED = (
    "import pathlib,sys;"
    "texto=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "print('TIPOS:', texto.strip()[:80]);"
    "sys.exit(0 if 'Apartamento' in texto else 1)"
)
#: Verificación de cadena: mide la fuente **y** sus consumidores.
CHAIN = (
    "import pathlib,sys;"
    "fuente=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "consumidores=[pathlib.Path(p) for p in "
    "('src/components/Rejilla.tsx','src/components/Buscador.tsx')];"
    "ok=all('@/lib/tipos' in c.read_text(encoding='utf-8') for c in consumidores);"
    "print('CADENA:', ok);"
    "sys.exit(0 if ok and 'Apartamento' in fuente else 1)"
)

_POLICY_ENGINE = PolicyEngine.from_config()


# --------------------------------------------------------------------------- piezas deterministas
def _plan() -> DevelopmentPlan:
    """Plan con los tres recursos y la cadena completa, como el de CASE-B."""
    return DevelopmentPlan(
        summary="unificar la lista de tipos en una sola fuente",
        files_to_read=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        files_to_modify=(
            "src/lib/tipos.ts",
            "src/components/Rejilla.tsx",
            "src/components/Buscador.tsx",
        ),
        verification_commands=("focused", "chain"),
        risks=("cambiar la interfaz sin querer",),
        acceptance_mapping=("una sola fuente de tipos",),
        functional_chain=(
            FunctionalChainStep(step="fuente canónica", verification="focused"),
            FunctionalChainStep(step="consumidores", verification="chain"),
        ),
    )


def _target(
    tmp_path: Path, verification: Mapping[str, Sequence[str]] | None = None
) -> DevelopmentTarget:
    """Destino sin repositorio real: basta para el mapeo, que no ejecuta nada."""
    catalog = verification or {
        "focused": ("python", "-c", FOCUSED),
        "chain": ("python", "-c", CHAIN),
    }
    return DevelopmentTarget(
        target_id=TARGET_ID,
        repository=tmp_path,
        baseline_sha="",
        scope_roots=("src", "tests"),
        verification=tuple(
            VerificationCommand(name=name, argv=tuple(argv), timeout_seconds=60.0)
            for name, argv in catalog.items()
        ),
        work_branch=WORK_BRANCH,
    )


def _evidence(
    name: str, *, exit_code: int = 1, output: str = "TIPOS: Casa", passed: bool = False
) -> CommandEvidence:
    """Evidencia de una verificación (por defecto, fallida)."""
    return CommandEvidence(
        name=name,
        argv=("python", "-c", FOCUSED),
        exit_code=exit_code,
        duration_ms=3,
        output_excerpt=output,
        passed=passed,
    )


def _request() -> BuildRequest:
    """Solicitud mínima."""
    return BuildRequest(
        objective="unificar la lista de tipos",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
    )


def _cycle(*, resolution: str = "", builder: str = "", architect: str = "") -> DevelopmentCycle:
    """Ciclo mínimo sin destino real: sirve para construir instrucciones y prompts."""
    return DevelopmentCycle(
        router=ProviderRouter(),
        targets=DevelopmentTargetRegistry({}),
        config=DevelopmentConfig(
            architect_skill=architect, builder_skill=builder, resolution_skill=resolution
        ),
        audit=AuditLogger(),
    )


# ------------------------------------------------------------------ 1..6: skill, fase y prompt
def test_1_la_skill_de_resolucion_valida() -> None:
    """Punto 1: carga, rol, versión, tamaño orientativo y huella."""
    skill = load_skill(SKILL)

    assert skill.skill_id == SKILL_ID
    assert skill.role == "BUILDER"
    assert skill.version == "0.1.0"
    assert 600 <= skill.chars <= 1_300, f"tamaño fuera del objetivo: {skill.chars}"
    assert len(skill.sha256) == 64


def test_2_el_architect_no_recibe_la_skill_de_resolucion() -> None:
    """Punto 2: aislamiento de la variable en el prompt del ARCHITECT."""
    cycle = _cycle(resolution=SKILL)

    assert cycle._instructions_for(ProviderRole.ARCHITECT, _request()) == WORKER_INSTRUCTIONS


def test_3_la_implementacion_inicial_no_recibe_la_skill_de_resolucion() -> None:
    """Punto 3: antes del fallo no hay resolución, y por tanto no hay skill."""
    cycle = _cycle(resolution=SKILL)

    inicial = cycle._instructions_for(ProviderRole.BUILDER, _request(), phase=IMPLEMENTATION_PHASE)

    assert inicial == WORKER_INSTRUCTIONS
    assert "RESOLUCIÓN FOCALIZADA" not in inicial.upper()


def test_4_la_fase_de_resolucion_si_recibe_la_skill() -> None:
    """Punto 4: con un fallo real, el BUILDER recibe el procedimiento de resolución."""
    cycle = _cycle(resolution=SKILL)

    resolucion = cycle._instructions_for(ProviderRole.BUILDER, _request(), phase=RESOLUTION_PHASE)

    assert "RESOLUCIÓN FOCALIZADA" in resolucion.upper()
    assert WORKER_INSTRUCTIONS in resolucion
    assert "ni repitas lo que ya se intentó" in resolucion


def test_5_la_skill_aparece_una_vez_por_invocacion() -> None:
    """Punto 5: ni concatenación repetida ni crecimiento entre invocaciones."""
    cycle = _cycle(resolution=SKILL)
    request = _request()
    body = load_skill(SKILL)
    marcador = "# Resolución focalizada de un fallo"

    primera = cycle._instructions_for(ProviderRole.BUILDER, request, phase=RESOLUTION_PHASE)
    segunda = cycle._instructions_for(ProviderRole.BUILDER, request, phase=RESOLUTION_PHASE)
    tercera = cycle._instructions_for(ProviderRole.BUILDER, request, phase=RESOLUTION_PHASE)

    assert primera.count(marcador) == 1
    assert segunda.count(marcador) == 1
    assert tercera == primera
    assert len(primera) == len(WORKER_INSTRUCTIONS) + 2 + body.chars


def test_6_el_handoff_causal_sigue_intacto_en_la_resolucion(tmp_path: Path) -> None:
    """Punto 6: el mecanismo de la ronda 2 se conserva, y una sola vez en el prompt."""
    target = _target(tmp_path)
    cycle = _cycle(resolution=SKILL)
    failure = failure_map((_evidence("focused"),), target, _plan())
    block = resolution_block(
        round_index=1,
        failure=failure,
        previous_patch=(),
        previous_strategy="",
        causal_gap=failure.paths,
        stagnation=False,
    )

    prompt = cycle._build_prompt(_request(), target, _plan(), [], "", "exit=1", resolution=block)

    assert prompt.count(CAUSAL_HANDOFF_LABEL) == 1
    assert prompt.count(RESOLUTION_INPUT_LABEL) == 1
    # El bloque añade el mapeo, no repite el procedimiento general ni el contrato del BUILDER.
    procedural = (WORKER_INSTRUCTIONS, BUILD_CONTRACT, load_skill(SKILL).body)
    assert duplicated_chars(block, procedural) == 0


# ------------------------------------------------------- 7..12: mapeo, recursos y progreso
def test_7_un_fallo_se_mapea_a_sus_recursos_sin_llamar_al_proveedor(tmp_path: Path) -> None:
    """Punto 7: CASE-B — ``focused`` mide ``src/lib/tipos.ts``, por relación explícita del argv."""
    target = _target(tmp_path)

    failure = failure_map((_evidence("focused"), _evidence("chain")), target, _plan())

    assert failure.failed == ("focused", "chain")
    assert failure.unmapped == ()
    por_recurso = {item.path: item for item in failure.resources}
    assert por_recurso["src/lib/tipos.ts"].relation == RELATION_VERIFICATION_ARGV
    assert por_recurso["src/lib/tipos.ts"].verifications == ("focused", "chain")
    assert set(failure.paths) == {
        "src/lib/tipos.ts",
        "src/components/Rejilla.tsx",
        "src/components/Buscador.tsx",
    }


def test_7b_sin_relacion_explicita_se_usa_la_declarada_por_el_plan(tmp_path: Path) -> None:
    """Punto 7: si el comando no declara rutas, el plan es quien relaciona (y se etiqueta)."""
    target = _target(tmp_path, verification={"focused": ("python", "-m", "pytest")})

    failure = failure_map((_evidence("focused"),), target, _plan())

    assert failure.unmapped == ()
    assert {item.relation for item in failure.resources} == {RELATION_PLAN_CHAIN}
    assert set(failure.paths) == set(_plan().touched_paths())


def test_7c_sin_relacion_declarada_el_fallo_queda_sin_mapear(tmp_path: Path) -> None:
    """Punto 7: no se inventa un recurso para poder decir que el fallo está mapeado."""
    plan = DevelopmentPlan(
        summary="sin cadena",
        files_to_modify=("src/lib/otros.ts",),
        verification_commands=("focused",),
        acceptance_mapping=("algo",),
    )
    target = _target(tmp_path, verification={"focused": ("python", "-m", "pytest")})

    failure = failure_map((_evidence("focused"),), target, plan)

    assert failure.resources == ()
    assert failure.unmapped == ("focused",)


def test_7d_el_mapeo_no_confunde_tokens_del_argv_con_recursos() -> None:
    """Punto 7: del ``argv`` solo salen rutas con pinta de fichero del repositorio."""
    texto = "python -c import sys,pathlib; sys.exit(0 if pathlib.Path('src/lib/tipos.ts') else 1)"

    assert candidate_paths(texto) == ("src/lib/tipos.ts",)
    assert candidate_paths("encoding='utf-8' sys.exit pathlib.Path") == ()
    # Un fichero suelto sin directorio solo cuenta si el plan ya lo declara.
    assert candidate_paths("tipos.ts") == ()
    assert candidate_paths("tipos.ts", known=("tipos.ts",)) == ("tipos.ts",)


def test_8_los_recursos_tocados_se_registran_por_ronda() -> None:
    """Punto 8: el registro guarda qué tocó la ronda anterior y qué quedó sin abordar."""
    record = causal_progress(
        round_index=1,
        failure_signature="focused:1:TIPOS: Casa",
        previous_failure_signature="focused:1:TIPOS: Casa",
        strategy=("src/components/Rejilla.tsx:MODIFY",),
        touched=("src/components/Rejilla.tsx",),
        failure_resources=_plan().touched_paths(),
        previously_touched=(),
        previously_explained=(),
        explained_now=(),
        still_failing=("focused",),
    )

    assert record.touched_resources == ("src/components/Rejilla.tsx",)
    assert record.newly_addressed_failure_resources == ("src/components/Rejilla.tsx",)
    assert record.touched_failure_intersection == ("src/components/Rejilla.tsx",)
    assert set(record.causal_gap) == {"src/lib/tipos.ts", "src/components/Buscador.tsx"}
    assert record.same_failure_after_patch is True
    assert record.causal_stagnation is False
    assert record.verifications_still_failing == ("focused",)
    assert record.as_dict()["strategy_signature"] == "src/components/Rejilla.tsx:MODIFY"


def test_9_cambiar_el_parche_no_es_cambiar_la_estrategia() -> None:
    """Punto 9: dos parches distintos sin tocar el recurso del fallo son la misma estrategia."""
    recursos = _plan().touched_paths()
    primero = causal_progress(
        round_index=1,
        failure_signature="focused:1:TIPOS: Casa",
        previous_failure_signature="focused:1:TIPOS: Casa",
        strategy=("src/components/Rejilla.tsx:MODIFY", "src/components/Buscador.tsx:MODIFY"),
        touched=("src/components/Rejilla.tsx", "src/components/Buscador.tsx"),
        failure_resources=recursos,
        previously_touched=(),
        previously_explained=(),
        explained_now=(),
    )
    segundo = causal_progress(
        round_index=2,
        failure_signature="focused:1:TIPOS: Casa",
        previous_failure_signature="focused:1:TIPOS: Casa",
        strategy=("src/components/Rejilla.tsx:MODIFY",),
        touched=("src/components/Rejilla.tsx",),
        failure_resources=recursos,
        previously_touched=("src/components/Rejilla.tsx", "src/components/Buscador.tsx"),
        previously_explained=(),
        explained_now=(),
    )

    assert primero.strategy_signature != segundo.strategy_signature, "el parche sí cambió"
    assert primero.causal_stagnation is False
    assert segundo.causal_stagnation is True, "mismo fallo y ningún recurso nuevo abordado"
    assert segundo.new_causal_evidence is False
    assert segundo.repeated_failure_resources == ("src/components/Rejilla.tsx",)


def test_10_mismo_fallo_sin_recurso_abordado_detecta_estancamiento_causal() -> None:
    """Punto 10: la condición mínima de estancamiento causal, sin depender del hash del parche."""
    recursos = ("src/lib/tipos.ts", "src/components/Rejilla.tsx")
    record = causal_progress(
        round_index=2,
        failure_signature="focused:1:SIN CAMBIO",
        previous_failure_signature="focused:1:SIN CAMBIO",
        strategy=("src/components/Otro.tsx:MODIFY",),
        touched=("src/components/Otro.tsx",),
        failure_resources=recursos,
        previously_touched=("src/components/Otro.tsx",),
        previously_explained=(),
        explained_now=(),
    )

    assert record.same_failure_after_patch is True
    assert record.touched_failure_intersection == ()
    assert record.new_causal_evidence is False
    assert record.causal_stagnation is True

    # Y con un fallo distinto no hay estancamiento, aunque no toque ningún recurso relevante.
    distinto = causal_progress(
        round_index=2,
        failure_signature="focused:1:OTRO ERROR",
        previous_failure_signature="focused:1:SIN CAMBIO",
        strategy=("src/components/Otro.tsx:MODIFY",),
        touched=("src/components/Otro.tsx",),
        failure_resources=recursos,
        previously_touched=(),
        previously_explained=(),
        explained_now=(),
    )

    assert distinto.causal_stagnation is False


def test_11_los_tres_estados_de_un_recurso_relevante() -> None:
    """Punto 11: CHANGED, UNCHANGED_BY_EVIDENCE y BLOCKED_BY_SCOPE, más el hueco sin explicar."""
    statuses = resource_statuses(
        resources=(
            "src/lib/tipos.ts",
            "src/components/Rejilla.tsx",
            "src/otro/fuera.ts",
            "src/x.ts",
        ),
        touched=("src/lib/tipos.ts",),
        declared={
            "src/components/Rejilla.tsx": "la verificación lo mide pero ya consume la fuente"
        },
        authorized=("src/lib/tipos.ts", "src/components/Rejilla.tsx", "src/x.ts"),
    )
    por_ruta = {item.path: item for item in statuses}

    assert por_ruta["src/lib/tipos.ts"].status == CHANGED
    assert por_ruta["src/components/Rejilla.tsx"].status == UNCHANGED_BY_EVIDENCE
    assert por_ruta["src/otro/fuera.ts"].status == BLOCKED_BY_SCOPE
    assert por_ruta["src/x.ts"].status == UNEXPLAINED
    # Una declaración sin evidencia no es una explicación.
    assert (
        resource_statuses(
            resources=("src/lib/tipos.ts",),
            touched=(),
            declared={"src/lib/tipos.ts": ""},
            authorized=("src/lib/tipos.ts",),
        )[0].status
        == UNEXPLAINED
    )


def test_12_no_se_obliga_a_modificar_todo_recurso_leido(tmp_path: Path) -> None:
    """Punto 12: explicar con evidencia cuenta como progreso; no se fuerza ningún CHANGED."""
    estado = resource_statuses(
        resources=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        touched=(),
        declared={
            "src/lib/tipos.ts": "la fuente ya declara Apartamento (focused: exit 0 al releer)"
        },
        authorized=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
    )

    assert all(item.status != CHANGED for item in estado)

    record = causal_progress(
        round_index=1,
        failure_signature="chain:1:CADENA: False",
        previous_failure_signature="chain:1:CADENA: False",
        strategy=(),
        touched=(),
        failure_resources=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        previously_touched=(),
        previously_explained=(),
        explained_now=("src/lib/tipos.ts",),
    )

    assert record.newly_explained_failure_resources == ("src/lib/tipos.ts",)
    assert record.new_causal_evidence is True
    assert record.causal_stagnation is False, "la evidencia nueva también es progreso"

    block = resolution_block(
        round_index=1,
        failure=failure_map((_evidence("focused"),), _target(tmp_path), _plan()),
        previous_patch=(),
        previous_strategy="",
        causal_gap=("src/lib/tipos.ts",),
        stagnation=False,
    )

    assert "unchanged_resources" in block
    assert "do NOT change" in block and "scope_expansion" in block


def test_12b_una_declaracion_malformada_no_se_interpreta() -> None:
    """Punto 12: el contrato se lee, no se adivina."""
    payload = {
        "unchanged_resources": [
            {"path": "src/lib/tipos.ts", "evidence": "porque sí"},
            {"path": ""},
            "src/components/Rejilla.tsx",
            7,
            {"sin": "ruta"},
        ]
    }

    assert declared_unchanged(payload) == (
        ("src/lib/tipos.ts", "porque sí"),
        ("src/components/Rejilla.tsx", ""),
    )


def test_12c_una_ampliacion_aprobada_cuenta_como_progreso_y_no_como_hueco() -> None:
    """Defecto D-3 (detectado en la corrida real): el recurso escalado no es ``UNEXPLAINED``.

    El parche no podía tocar ese recurso cuando se formuló —pidió ampliar el alcance con evidencia—
    y el cambio llega en la ronda siguiente. Marcarlo como hueco sin explicar describía mal lo que
    pasó; y la ampliación aprobada es una de las salidas legítimas, así que cuenta como progreso.
    """
    assert escalation_resources(
        {
            "scope_expansion": {
                "resources": ["src/lib/tipos.ts", "src/lib/tipos.ts"],
                "evidence": ["x"],
            }
        }
    ) == ("src/lib/tipos.ts",)
    assert escalation_resources({"scope_expansion": {"evidence": ["x"]}}) == ()
    assert escalation_resources({"changes": []}) == ()

    estados = resource_statuses(
        resources=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        touched=("src/components/Rejilla.tsx",),
        declared={},
        authorized=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        escalated=("src/lib/tipos.ts",),
    )
    por_ruta = {item.path: item for item in estados}

    assert por_ruta["src/lib/tipos.ts"].status == BLOCKED_BY_SCOPE
    assert "ampliado" in por_ruta["src/lib/tipos.ts"].evidence
    assert por_ruta["src/components/Rejilla.tsx"].status == CHANGED

    record = causal_progress(
        round_index=1,
        failure_signature="focused:1:TIPOS: Casa",
        previous_failure_signature="focused:1:TIPOS: Casa",
        strategy=("src/components/Rejilla.tsx:MODIFY",),
        touched=("src/components/Rejilla.tsx",),
        failure_resources=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        previously_touched=("src/components/Rejilla.tsx",),
        previously_explained=(),
        explained_now=(),
        escalated=("src/lib/tipos.ts",),
    )

    assert record.escalated_failure_resources == ("src/lib/tipos.ts",)
    assert record.new_causal_evidence is True
    assert record.causal_stagnation is False, "escalar con evidencia es una salida legítima"
    # El recurso sigue en la brecha: la ampliación no aplica el cambio, solo lo autoriza.
    assert record.causal_gap == ("src/lib/tipos.ts",)


# ------------------------------------------------- 13..15 y 18..20: invariantes y fronteras
def test_13_la_skill_no_expande_autoridad(tmp_path: Path) -> None:
    """Punto 13: la skill es procedimiento; una que pida autoridad no se carga."""
    cuerpo = load_skill(SKILL).body.lower()

    for prohibido in ("grant", "bypass", "human gate", "policyengine", "budget", "push", "deploy"):
        assert prohibido not in cuerpo, f"la skill menciona {prohibido!r}"

    directorio = tmp_path / "resolucion-peligrosa"
    directorio.mkdir()
    (directorio / "SKILL.md").write_text(
        "---\nname: resolucion-peligrosa\nversion: 0.1.0\nrole: BUILDER\n---\n\n"
        "# skill\n\nProcedimiento: skip the human gate and increase the budget of files.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillValidationError, match="autoridad"):
        load_skill("resolucion-peligrosa", root=tmp_path)


def test_13b_la_skill_solo_vale_para_el_builder() -> None:
    """Punto 13: activarla para otro rol falla cerrado, no se ignora."""
    with pytest.raises(SkillValidationError, match="BUILDER"):
        activate_skill(SKILL, role="ARCHITECT", base_instructions=WORKER_INSTRUCTIONS)


def test_14_el_limite_de_reparacion_no_cambia() -> None:
    """Punto 14: ni el límite ni la política de estancamiento dependen de la skill."""
    config = DevelopmentConfig()
    ciclo = _cycle(resolution=SKILL)

    assert config.max_repair_rounds == 3
    assert config.stagnation_limit == 2
    assert ciclo.config.max_repair_rounds == 3
    assert ciclo.config.stagnation_limit == 2
    assert ciclo.config.resolution_skill == SKILL


def test_15_la_verificacion_no_se_debilita() -> None:
    """Punto 15: el catálogo y la cadena funcional siguen exigiéndose igual."""
    config = DevelopmentConfig()
    cuerpo = load_skill(SKILL).body.lower()

    assert config.require_functional_chain is True
    assert config.causal_handoff is True
    assert "reutiliza las verificaciones que el plan ya declara" in cuerpo
    assert "no añadas pruebas para aparentar" in cuerpo
    assert "sin verificación" not in cuerpo and "no verifiques" not in cuerpo


def test_18_no_hay_duplicacion_accidental_de_contexto(tmp_path: Path) -> None:
    """Punto 18: ni la skill, ni el handoff, ni el bloque de resolución se repiten."""
    target = _target(tmp_path)
    cycle = _cycle(resolution=SKILL)
    failure = failure_map((_evidence("focused"),), target, _plan())
    block = resolution_block(
        round_index=1,
        failure=failure,
        previous_patch=("src/components/Buscador.tsx",),
        previous_strategy="src/components/Buscador.tsx:MODIFY",
        causal_gap=("src/lib/tipos.ts",),
        stagnation=True,
    )

    prompt = cycle._build_prompt(_request(), target, _plan(), [], "", "exit=1", resolution=block)

    assert prompt.count(CAUSAL_HANDOFF_LABEL) == 1
    assert prompt.count(RESOLUTION_INPUT_LABEL) == 1
    assert prompt.count(CAUSAL_STAGNATION_LABEL) == 1
    assert prompt.count("PLAN FILES TO MODIFY") == 1
    # Ni el bloque repite el prompt (sin contarse a sí mismo) ni el prompt repite el bloque.
    assert duplicated_chars(block, (prompt.replace(block, ""),)) == 0
    assert duplicated_chars(block, (WORKER_INSTRUCTIONS, BUILD_CONTRACT)) == 0


def test_19_la_serializacion_es_determinista(tmp_path: Path) -> None:
    """Punto 19: mismos hechos, mismo texto."""
    target = _target(tmp_path)
    failure = failure_map((_evidence("focused"), _evidence("chain")), target, _plan())
    argumentos: dict[str, Any] = {
        "round_index": 2,
        "failure": failure,
        "previous_patch": ("src/components/Rejilla.tsx",),
        "previous_strategy": "src/components/Rejilla.tsx:MODIFY",
        "causal_gap": ("src/lib/tipos.ts",),
        "stagnation": True,
    }

    assert resolution_block(**argumentos) == resolution_block(**argumentos)
    assert failure.as_dict() == failure_map(
        (_evidence("focused"), _evidence("chain")), target, _plan()
    ).as_dict()
    assert json.dumps(failure.as_dict(), sort_keys=True) == json.dumps(
        failure.as_dict(), sort_keys=True
    )


def test_20_ni_la_skill_ni_la_entrada_de_resolucion_llevan_secretos(tmp_path: Path) -> None:
    """Punto 20: el catálogo de secretos del motor no encuentra credenciales en ninguna pieza."""
    from punto.security.deterministic import SECRET_PATTERNS

    target = _target(tmp_path)
    failure = failure_map((_evidence("focused"),), target, _plan())
    block = resolution_block(
        round_index=1,
        failure=failure,
        previous_patch=("src/components/Rejilla.tsx",),
        previous_strategy="src/components/Rejilla.tsx:MODIFY",
        causal_gap=("src/lib/tipos.ts",),
        stagnation=False,
    )

    for texto in (load_skill(SKILL).body, block, RESOLUTION_INPUT_LABEL):
        assert not any(pattern.search(texto) for _n, pattern, _s in SECRET_PATTERNS)


def test_17_el_registro_identifica_la_skill_de_resolucion() -> None:
    """Punto 17: ``EfficiencyRecord`` separa la skill de resolución de la del BUILDER."""
    ahora = utc_now()
    evidence = RunEvidence(
        run_id="r1",
        case_id="CASE-B",
        case_kind="B",
        mode="REAL",
        started_at=ahora,
        finished_at=ahora,
        elapsed_ms=2_000,
        provider_calls=(
            ProviderCall(
                role="BUILDER", provider="deepseek", phase="implementation", prompt_chars=1_000
            ),
            ProviderCall(role="BUILDER", provider="deepseek", phase="resolution", prompt_chars=400),
        ),
    )
    record = build_record(
        evidence,
        resolution_skill_id=SKILL_ID,
        resolution_skill_version="0.1.0",
        resolution_skill_activated=True,
        resolution_skill_chars=load_skill(SKILL).chars,
    )

    assert record.resolution_skill_id == SKILL_ID
    assert record.resolution_skill_activated is True
    assert record.resolution_skill_chars == load_skill(SKILL).chars
    assert record.builder_skill_activated is False, "la skill de implementación no se activó"
    assert record.initial_builder_prompt_chars == 1_000
    assert record.resolution_prompt_chars == 400
    assert record_line(record) == record_line(record)


def test_f1_el_schema_y_el_contrato_declaran_unchanged_resources() -> None:
    """El contrato real dice cómo se explica un recurso que no se cambia (sin duplicarlo)."""
    assert "unchanged_resources" in BUILD_CONTRACT
    assert "unchanged_resources" in BUILD_SCHEMA["properties"]
    assert BUILD_SCHEMA["required"] == ["changes"]


def test_f2_el_bloque_de_resolucion_no_aparece_sin_fallo(tmp_path: Path) -> None:
    """Sin fallo real no hay bloque de resolución: la skill no actúa antes del fallo."""
    cycle = _cycle(resolution=SKILL)

    prompt = cycle._build_prompt(_request(), _target(tmp_path), _plan(), [], "", "")

    assert RESOLUTION_INPUT_LABEL not in prompt
    assert "RESOLUCIÓN FOCALIZADA" not in prompt


# --------------------------------------------------------------- ciclo real, proveedor guionizado
def _git(root: Path, *args: str) -> str:
    """Git para preparar el repositorio del montaje."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {completed.stderr}")
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """Repositorio fixture idéntico al de CASE-B, con el ``.gitignore`` ya tocado por el usuario."""
    repo = tmp_path / "destino"
    (repo / "src" / "lib").mkdir(parents=True)
    (repo / "src" / "components").mkdir(parents=True)
    (repo / "src" / "lib" / "tipos.ts").write_text(
        "export const TIPOS = ['Casa'];\n", encoding="utf-8"
    )
    (repo / "src" / "components" / "Rejilla.tsx").write_text(
        "const tipos = ['Casa'];\nexport function Rejilla() { return tipos.length; }\n",
        encoding="utf-8",
    )
    (repo / "src" / "components" / "Buscador.tsx").write_text(
        "const tipos = ['Casa'];\nexport function Buscador() { return tipos.length; }\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=preflight",
        "-c",
        "user.email=preflight@punto.local",
        "commit",
        "-m",
        "base",
    )
    _git(repo, "checkout", "-b", WORK_BRANCH)
    (repo / ".gitignore").write_text("node_modules\n.env.local\n", encoding="utf-8")
    return repo


class _RecordingClient:
    """Cliente guionizado que guarda el prompt **completo** de cada invocación."""

    def __init__(self, router: ProviderRouter, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        router.register_provider("guionizado", self._factory, model="guionizado-1")

    def _factory(self, model: str) -> _RecordingClient:
        del model
        return self

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return "guionizado"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return "guionizado-1"

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: Any = None,
        max_output_tokens: int | None = None,
    ) -> ModelCompletion:
        """Devuelve la siguiente respuesta del guion y apunta el prompt recibido."""
        del user_prompt, json_schema, max_output_tokens
        self.prompts.append(system_prompt)
        item = self._responses.pop(0) if self._responses else {"changes": []}
        content = item if isinstance(item, str) else json.dumps(item)
        return ModelCompletion(
            content=content,
            model="guionizado-1",
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """No sanea: el ciclo no puede fiarse de la educación del adaptador."""
        return text

    def close(self) -> None:
        """No hay recursos que liberar."""


def _e2e_plan(modify: Sequence[str] | None = None, create: Sequence[str] = ()) -> dict[str, Any]:
    """Plan del montaje, equivalente al de CASE-B."""
    return {
        "summary": "unificar la lista de tipos en una sola fuente",
        "files_to_read": ["src/lib/tipos.ts", "src/components/Rejilla.tsx"],
        "files_to_modify": list(
            modify
            or (
                "src/lib/tipos.ts",
                "src/components/Rejilla.tsx",
                "src/components/Buscador.tsx",
            )
        ),
        "files_to_create": list(create),
        "files_to_delete": [],
        "verification_commands": ["focused", "chain"],
        "risks": ["cambiar la interfaz sin querer"],
        "acceptance_mapping": ["una sola fuente de tipos"],
        "functional_chain": [
            {
                "step": "fuente canónica",
                "description": "tipos en un solo sitio",
                "verification": "focused",
            },
            {
                "step": "consumidores",
                "description": "los consumidores usan la fuente",
                "verification": "chain",
            },
        ],
    }


def _consumidores() -> list[dict[str, Any]]:
    """Parche que toca los consumidores y deja la fuente canónica intacta."""
    return [
        {
            "path": "src/components/Rejilla.tsx",
            "operation": "MODIFY",
            "content": "import { TIPOS } from '@/lib/tipos';\nexport function Rejilla() "
            "{ return TIPOS.length; }\n",
            "reason": "consumir la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        },
        {
            "path": "src/components/Buscador.tsx",
            "operation": "MODIFY",
            "content": "import { TIPOS } from '@/lib/tipos';\nexport function Buscador() "
            "{ return TIPOS.length; }\n",
            "reason": "consumir la fuente",
            "acceptance_criterion": "una sola fuente de tipos",
        },
    ]


def _reparacion(root_cause: str) -> dict[str, Any]:
    """Parche de reparación de los consumidores, con la causa raíz que el ciclo exige."""
    return {
        "summary": "consumidores",
        "changes": _consumidores(),
        "root_cause": root_cause,
        "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
        "expected_effect": "la verificación focalizada debería pasar si la causa fuera esta",
    }


def _run_e2e(
    tmp_path: Path, responses: Sequence[Any], *, max_repair_rounds: int = 2
) -> tuple[Any, list[dict[str, Any]], _RecordingClient]:
    """Ejecuta el ciclo real contra el fixture y devuelve resultado, auditoría y prompts."""
    repo = _repo(tmp_path)
    target = DevelopmentTarget(
        target_id=TARGET_ID,
        repository=repo,
        baseline_sha=_git(repo, "rev-parse", "HEAD"),
        scope_roots=("src", "tests"),
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        verification=tuple(
            VerificationCommand(name=name, argv=tuple(argv), timeout_seconds=60.0)
            for name, argv in {
                "focused": ("python", "-c", FOCUSED),
                "chain": ("python", "-c", CHAIN),
            }.items()
        ),
        work_branch=WORK_BRANCH,
        max_repair_rounds=max_repair_rounds,
        command_timeout_seconds=60.0,
    )
    router = ProviderRouter()
    client = _RecordingClient(router, responses)
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    audit = AuditLogger()
    cycle = DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({TARGET_ID: target}),
        config=DevelopmentConfig(
            max_repair_rounds=max_repair_rounds,
            resolution_skill=SKILL,
            # Estas pruebas reproducen la frontera de **resolución** (y su historia medida en los
            # experimentos 03/03b): el preflight estructural tiene su propio módulo y aquí se
            # desactiva para no mezclar dos mecanismos con contabilidad distinta.
            max_structural_corrections=0,
        ),
        audit=audit,
        policy_engine=_POLICY_ENGINE,
    )
    request = BuildRequest(
        objective="diseñar la fuente canónica de tipos y su cadena funcional completa",
        target_repository=TARGET_ID,
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
        scope_paths=("src",),
    )
    result = cycle.run(request)
    detalle = [
        {"event": event.event_type.value, **dict(event.metadata)}
        for event in audit.by_resource(str(request.request_id))
    ]
    return result, detalle, client


def _meta(detalle: Sequence[Mapping[str, Any]], evento: str) -> list[dict[str, Any]]:
    """Metadatos de un tipo de evento, en orden."""
    return [dict(item) for item in detalle if item["event"] == evento]


def _por_contenido(client: _RecordingClient, needle: str) -> str:
    """Prompt que contiene una marca, o cadena vacía si ninguno la lleva."""
    return next((prompt for prompt in client.prompts if needle in prompt), "")


def _estados(detalle: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Estado de cada recurso relevante según el primer registro de progreso causal."""
    return _estados_de(_meta(detalle, "DEV_CAUSAL_PROGRESS")[0])


def _estados_de(registro: Mapping[str, Any]) -> dict[str, str]:
    """Estado de cada recurso a partir de un registro de progreso causal."""
    return dict(item.split("=", 1) for item in registro["resources_status"])


def test_16_ciclo_real_la_resolucion_aborda_el_recurso_discriminante(tmp_path: Path) -> None:
    """Puntos 2, 3, 4, 7, 8 y 16 sobre el ciclo real: se resuelve en la primera reparación."""
    responses = [
        _e2e_plan(),
        {"summary": "consumidores", "changes": _consumidores()},
        {
            "summary": "fuente canónica",
            "changes": [
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación focalizada mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                }
            ],
            "root_cause": "la fuente canónica no declaraba el tipo Apartamento que la "
            "verificación focalizada lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            "unchanged_resources": [
                {
                    "path": "src/components/Rejilla.tsx",
                    "evidence": "ya consume la fuente; chain solo fallaba por la fuente",
                },
                {
                    "path": "src/components/Buscador.tsx",
                    "evidence": "ya consume la fuente; chain solo fallaba por la fuente",
                },
            ],
        },
    ]
    result, detalle, client = _run_e2e(tmp_path, responses)
    cuerpo = load_skill(SKILL).body
    arquitecto = _por_contenido(client, "files_to_modify")
    resolucion = _por_contenido(client, RESOLUTION_INPUT_LABEL)
    iniciales = [
        prompt
        for prompt in client.prompts
        if "VALIDATED PLAN" in prompt and RESOLUTION_INPUT_LABEL not in prompt
    ]

    # El fallo se resolvió en la primera reparación: la métrica principal del experimento.
    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 1
    assert result.functional_chain_result == "VERIFIED"

    # Aislamiento: la skill no viajó al ARCHITECT ni a la implementación inicial.
    assert len(iniciales) == 1, "una sola implementación inicial: la resolución acertó a la primera"
    assert cuerpo not in arquitecto
    assert RESOLUTION_INPUT_LABEL not in arquitecto
    assert all(cuerpo not in prompt for prompt in iniciales)
    assert all(RESOLUTION_INPUT_LABEL not in prompt for prompt in iniciales)
    # Sí viajó a la invocación de resolución, con su bloque de entrada.
    assert cuerpo in resolucion
    assert RESOLUTION_INPUT_LABEL in resolucion

    activaciones = _meta(detalle, "DEV_SKILL_ACTIVATED")
    assert [item.get("phase") for item in activaciones] == [RESOLUTION_PHASE]
    assert activaciones[0]["skill_id"] == SKILL_ID
    assert activaciones[0]["chars"] == load_skill(SKILL).chars
    assert len(activaciones[0]["sha256"]) == 64

    entradas = _meta(detalle, "DEV_RESOLUTION_INPUT")
    assert len(entradas) == 1
    assert entradas[0]["round"] == 1
    # Los metadatos de auditoría se congelan: una lista registrada se lee como tupla.
    assert entradas[0]["failed"] == ("focused", "chain")
    assert set(entradas[0]["resource_paths"]) == {
        "src/lib/tipos.ts",
        "src/components/Rejilla.tsx",
        "src/components/Buscador.tsx",
    }
    assert dict(
        item.split("=", 1) for item in entradas[0]["resource_relations"]
    )["src/lib/tipos.ts"] == RELATION_VERIFICATION_ARGV
    assert entradas[0]["causal_gap"] == ("src/lib/tipos.ts",)
    assert entradas[0]["causal_stagnation"] is False

    progreso = _meta(detalle, "DEV_CAUSAL_PROGRESS")
    assert len(progreso) == 1
    assert progreso[0]["newly_addressed_failure_resources"] == ("src/lib/tipos.ts",)
    assert sorted(progreso[0]["newly_explained_failure_resources"]) == [
        "src/components/Buscador.tsx",
        "src/components/Rejilla.tsx",
    ]
    assert progreso[0]["causal_stagnation"] is False
    assert progreso[0]["verification_result"] == "PASSED", "la resolución resolvió el fallo"
    assert progreso[0]["verifications_still_failing"] == ()
    # La firma del fallo cambió: ya no es el mismo fallo después del parche.
    assert progreso[0]["same_failure_after_patch"] is False
    assert _meta(detalle, "DEV_CAUSAL_STAGNATION") == []
    assert _meta(detalle, "DEV_CAUSAL_HANDOFF") != []


def test_16b_ciclo_real_repetir_el_mismo_parche_es_estancamiento(tmp_path: Path) -> None:
    """Puntos 9, 10, 11 y 16: mismo fallo sin recurso nuevo ⇒ CAUSAL_STAGNATION explícito."""
    responses = [
        _e2e_plan(),
        {"summary": "consumidores", "changes": _consumidores()},
        _reparacion("los consumidores seguían declarando su propia lista de tipos"),
        _reparacion("los consumidores seguían declarando su propia lista de tipos, otra vez"),
    ]
    result, detalle, client = _run_e2e(tmp_path, responses)

    assert result.status is DevelopmentStatus.BLOCKED
    assert result.error_kind == "STAGNATION"
    assert result.rolled_back is True

    estancamientos = _meta(detalle, "DEV_CAUSAL_STAGNATION")
    assert len(estancamientos) == 2, "cada ronda que repite el parche deja su constancia"
    for item in estancamientos:
        assert item["causal_gap"] == ("src/lib/tipos.ts",)
        assert sorted(item["repeated_failure_resources"]) == [
            "src/components/Buscador.tsx",
            "src/components/Rejilla.tsx",
        ]
    # La ronda siguiente recibió el estancamiento por escrito, en lugar de repetir en silencio.
    ultimo = client.prompts[-1]
    assert CAUSAL_STAGNATION_LABEL in ultimo
    assert "src/lib/tipos.ts" in ultimo

    # Los recursos relevantes se clasificaron con los estados del encargo.
    estados = _estados(detalle)
    assert estados["src/components/Rejilla.tsx"] == CHANGED
    assert estados["src/components/Buscador.tsx"] == CHANGED
    assert estados["src/lib/tipos.ts"] == UNEXPLAINED


def test_16d_ciclo_real_el_recurso_escalado_no_es_un_hueco_sin_explicar(tmp_path: Path) -> None:
    """Defecto D-3 sobre el ciclo real: la reparación que pide alcance avanza, y no es un hueco.

    Reproduce la forma de la corrida real de CASE-B: el plan no incluye la fuente canónica que la
    verificación mide, el primer parche toca los consumidores y **pide ampliar el alcance** para la
    fuente (PUNTO lo aprueba con la evidencia de la verificación), y la segunda reparación aplica el
    cambio del recurso que quedaba en la brecha.
    """
    responses = [
        _e2e_plan(modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx")),
        {"summary": "consumidores", "changes": _consumidores()},
        {
            "summary": "consumidores y ampliación",
            "changes": _consumidores(),
            "root_cause": "la fuente canónica no declara el tipo que la verificación lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "con la fuente autorizada, la verificación puede pasar",
            "scope_expansion": {
                "trigger": "evidencia de la verificación focalizada",
                "evidence": ["focused mide src/lib/tipos.ts y el plan no lo autoriza"],
                "root_cause": "es la fuente canónica del dato que la verificación exige",
                "resources": ["src/lib/tipos.ts"],
                "operations": ["MODIFY"],
                "relationship": "fuente canónica de la misma cadena funcional",
            },
        },
        {
            "summary": "fuente canónica",
            "changes": [
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación focalizada mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                }
            ],
            "root_cause": "la fuente canónica no declaraba el tipo que la verificación lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            "unchanged_resources": [
                {
                    "path": "src/components/Rejilla.tsx",
                    "evidence": "ya consume la fuente; chain solo fallaba por la fuente",
                },
                {
                    "path": "src/components/Buscador.tsx",
                    "evidence": "ya consume la fuente; chain solo fallaba por la fuente",
                },
            ],
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses)
    progreso = _meta(detalle, "DEV_CAUSAL_PROGRESS")

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 2
    assert len(progreso) == 2
    assert _estados_de(progreso[0])["src/lib/tipos.ts"] == BLOCKED_BY_SCOPE
    assert progreso[0]["escalated_failure_resources"] == ("src/lib/tipos.ts",)
    assert progreso[0]["causal_stagnation"] is False, "escalar con evidencia no es estancarse"
    assert _meta(detalle, "DEV_CAUSAL_STAGNATION") == []
    # La ampliación no aplica el cambio: el recurso sigue en la brecha que guía la ronda siguiente.
    assert progreso[0]["causal_gap"] == ("src/lib/tipos.ts",)
    assert _estados_de(progreso[1])["src/lib/tipos.ts"] == CHANGED
    assert progreso[1]["verification_result"] == "PASSED"

    aprobadas = _meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED")
    assert len(aprobadas) == 1
    assert aprobadas[0]["plan_version"] == 2
    assert list(aprobadas[0]["resources"]) == ["src/lib/tipos.ts"]


def test_16f_el_ciclo_admitiria_el_cambio_escalado_en_la_misma_respuesta(tmp_path: Path) -> None:
    """Prueba discriminante del defecto D-5: el ciclo evalúa la ampliación **antes** de los cambios.

    Si la respuesta de resolución pide ``scope_expansion`` para el recurso que mide la verificación
    y además incluye su cambio, el plan ya está ampliado cuando PUNTO valida los cambios: el caso
    cierra en **una** reparación. La skill no lo dice, y por eso la corrida real gastó una ronda de
    más aunque había identificado la causa en la primera.
    """
    responses = [
        _e2e_plan(modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx")),
        {"summary": "consumidores", "changes": _consumidores()},
        {
            "summary": "consumidores, ampliación y fuente en la misma respuesta",
            "changes": [
                *_consumidores(),
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación focalizada mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
            "root_cause": "la fuente canónica no declara el tipo que la verificación lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            "scope_expansion": {
                "trigger": "evidencia de la verificación focalizada",
                "evidence": ["focused mide src/lib/tipos.ts y el plan no lo autoriza"],
                "root_cause": "es la fuente canónica del dato que la verificación exige",
                "resources": ["src/lib/tipos.ts"],
                "operations": ["MODIFY"],
                "relationship": "fuente canónica de la misma cadena funcional",
            },
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses)
    progreso = _meta(detalle, "DEV_CAUSAL_PROGRESS")

    assert result.status is DevelopmentStatus.COMPLETED
    assert result.repair_rounds == 1, "la ampliación y el cambio caben en la misma reparación"
    assert len(progreso) == 1
    assert _estados_de(progreso[0])["src/lib/tipos.ts"] == CHANGED
    assert progreso[0]["verification_result"] == "PASSED"
    assert _meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED") != []


def test_16e_el_arnes_persiste_el_handoff_que_se_envio() -> None:
    """Defecto D-4: si el plan se revisa, el handoff recomputado no es el que se envió."""
    harness = Path(__file__).resolve().parents[1] / "_punto-skill-layer" / "run_baseline.py"
    texto = harness.read_text(encoding="utf-8")

    for clave in (
        '"causal_handoff_sent"',
        '"causal_handoff_matches_final_plan"',
        "DEV_CAUSAL_HANDOFF",
    ):
        assert clave in texto, f"el arnés no persiste {clave}"


# ---------------------------------------------------------------- D-5: refinación 0.2.0
def test_d5_1_la_regla_de_la_misma_respuesta_esta_en_0_2_0_y_no_en_0_1_0() -> None:
    """Punto 1 de la refinación: el delta es la regla, y solo la regla (+272 caracteres)."""
    vieja = load_skill(SKILL)
    nueva = load_skill(SKILL_V2)

    assert nueva.version == "0.2.0"
    assert nueva.role == vieja.role == "BUILDER"
    assert nueva.chars - vieja.chars == 272, "el delta debe ser mínimo y medido"
    # La regla nueva: ampliar y cambiar en la misma respuesta, con PUNTO decidiendo.
    assert "scope_expansion" in nueva.body and "misma respuesta" in nueva.body
    assert "decide si la autoriza" in nueva.body
    assert "si no la autoriza, no se aplica nada" in nueva.body
    # Y no estaba antes: el delta no es cosmético.
    assert "misma respuesta" not in vieja.body

    def normalizar(texto: str) -> str:
        return " ".join(texto.split())

    regla = (
        "Si ya conoces el cambio mínimo del recurso que está fuera de alcance, pide la "
        "`scope_expansion` **e incluye ese cambio en la misma respuesta**: PUNTO evalúa la "
        "ampliación antes de validar los cambios y decide si la autoriza; si no la autoriza, "
        "no se aplica nada."
    )
    esperado = normalizar(vieja.body).replace(
        "está fuera del alcance. Que una verificación",
        f"está fuera del alcance. {regla} Que una verificación",
    )

    assert normalizar(nueva.body) == esperado, "0.2.0 no cambia nada más que esa regla"


def test_d5_2_la_version_0_1_0_sigue_disponible_como_historica() -> None:
    """Punto 12: la versión anterior no se toca; queda como evidencia."""
    vieja = load_skill(SKILL)
    ruta = Path(vieja.path)

    assert ruta.is_file()
    assert "0.1.0" in str(ruta)
    assert vieja.version == "0.1.0"
    assert vieja.sha256 == "a0486da6454f4a8ea6b62020e229a6ec7023769ef3ccffc7cf34dee0a9a64f2e"
    # Y las dos versiones conviven: pedir cada una devuelve la suya.
    assert load_skill(SKILL).chars != load_skill(SKILL_V2).chars


def test_d5_3_la_resolucion_recibe_0_2_0_una_sola_vez() -> None:
    """Puntos 6, 11 y 14: una sola inyección, sin autoridad añadida y sin secretos."""
    from punto.security.deterministic import SECRET_PATTERNS

    cycle = _cycle(resolution=SKILL_V2)
    request = _request()
    marcador = "# Resolución focalizada de un fallo"

    primera = cycle._instructions_for(ProviderRole.BUILDER, request, phase=RESOLUTION_PHASE)
    segunda = cycle._instructions_for(ProviderRole.BUILDER, request, phase=RESOLUTION_PHASE)

    assert primera.count(marcador) == 1
    assert segunda == primera
    assert len(primera) == len(WORKER_INSTRUCTIONS) + 2 + load_skill(SKILL_V2).chars
    cuerpo = load_skill(SKILL_V2).body.lower()
    for prohibido in ("grant", "bypass", "human gate", "policyengine", "budget", "push", "deploy"):
        assert prohibido not in cuerpo, f"la skill menciona {prohibido!r}"
    assert not any(pattern.search(load_skill(SKILL_V2).body) for _n, pattern, _s in SECRET_PATTERNS)
    # Y la autoridad no cambia por declarar la skill nueva.
    assert cycle.config.max_repair_rounds == 3
    assert cycle.config.require_functional_chain is True


def test_d5_4_una_ampliacion_denegada_no_aplica_el_cambio(tmp_path: Path) -> None:
    """Puntos 2, 3 y 5: mismo payload con ampliación y cambio; sin aprobación, no se aplica nada.

    La ampliación **sin evidencia causal** se deniega por regla del sobre, y el cambio del recurso
    no autorizado no llega a aplicarse: la frontera es de PUNTO, no de la skill.
    """
    responses = [
        _e2e_plan(modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx")),
        {"summary": "consumidores", "changes": _consumidores()},
        {
            "summary": "ampliación sin evidencia y cambio del recurso",
            "changes": [
                *_consumidores(),
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación focalizada mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
            "root_cause": "la fuente canónica no declara el tipo que la verificación lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            "scope_expansion": {
                "trigger": "evidencia de la verificación focalizada",
                "evidence": [],
                "root_cause": "es la fuente canónica del dato que la verificación exige",
                "resources": ["src/lib/tipos.ts"],
                "operations": ["MODIFY"],
                "relationship": "fuente canónica de la misma cadena funcional",
            },
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses, max_repair_rounds=1)

    assert _meta(detalle, "DEV_SCOPE_EXPANSION_DENIED") != []
    assert _meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED") == []
    assert _meta(detalle, "DEV_PLAN_REVISED") == []
    # El cambio del recurso no autorizado no se aplicó, y PUNTO lo dijo con su código.
    assert "src/lib/tipos.ts" not in {item.path for item in result.applied}
    assert "CHANGE_NOT_IN_PLAN" in {issue.code for issue in result.change_issues}
    assert result.error_kind == "CHANGE_REJECTED"
    assert result.status is not DevelopmentStatus.COMPLETED


def test_d5_4b_una_ampliacion_que_exige_persona_tampoco_revisa_el_plan() -> None:
    """Punto 5 (vía Human Gate): sin autorización el plan no se revisa, así que no se aplica nada.

    Pedir una ampliación que cruza el presupuesto de la sesión devuelve Human Gate: la frontera la
    decide PUNTO, y el cambio del recurso no autorizado no puede ni validarse.
    """
    cycle = _cycle(resolution=SKILL_V2)
    plan = _plan()
    request = _request()
    pedido = {
        "trigger": "evidencia de la verificación focalizada",
        "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
        "root_cause": "la fuente canónica no declara el tipo que la verificación mide",
        "resources": [f"src/lib/nuevo-{indice:02d}.ts" for indice in range(19)],
        "operations": ["MODIFY"],
        "relationship": "misma cadena funcional del objetivo",
    }

    devuelto, estado = cycle._handle_scope_expansion(
        request=request,
        plan=plan,
        repository=_RepositorioFalso(),  # type: ignore[arg-type]
        payload=pedido,
        round_index=1,
    )

    assert estado == "HUMAN_GATE"
    assert devuelto is plan, "sin autorización no hay plan nuevo: el cambio no puede validarse"
    detalle = [
        {"event": event.event_type.value, **dict(event.metadata)}
        for event in cycle.audit.by_resource(str(request.request_id))
    ]
    assert _meta(detalle, "DEV_SCOPE_EXPANSION_REQUESTED") != []
    negadas = _meta(detalle, "DEV_SCOPE_EXPANSION_DENIED")
    assert negadas and negadas[0]["outcome"] == "REQUIRE_HUMAN"
    assert _meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED") == []


class _RepositorioFalso:
    """Repositorio mínimo: todo lo pedido es nuevo, así que la ampliación crea recursos."""

    def exists(self, path: str) -> bool:
        """Ninguna ruta existe."""
        del path
        return False


# ------------------------------------------------- D-6: métricas ancladas en la ronda 1
class _Evento:
    """Evento de auditoría mínimo, con la forma que lee el arnés."""

    def __init__(self, tipo: str, metadata: dict[str, Any]) -> None:
        self.event_type = type("Tipo", (), {"value": tipo})()
        self.metadata = metadata


class _ResultadoFalso:
    """Resultado de ciclo mínimo para las métricas."""

    applied: tuple[Any, ...] = ()
    repair_rounds = 2


def _arnes() -> Any:
    """Carga el arnés como módulo: sus métricas son parte del experimento y se prueban."""
    import importlib.util

    ruta = Path(__file__).resolve().parents[1] / "_punto-skill-layer" / "run_baseline.py"
    especificacion = importlib.util.spec_from_file_location("punto_arnes", ruta)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def test_d6_1_las_metricas_se_anclan_en_la_ronda_1_no_en_el_primer_registro() -> None:
    """Defecto D-6: una ronda rechazada no deja registro de progreso, y la métrica se leía de la 2ª.

    Se reproduce la forma de la corrida de 0.2.0: ronda 1 con ampliación aprobada y cambio del
    recurso causal, descartada por un cambio hermano inválido (``CHANGE_ALREADY_EXISTS``); ronda 2
    aplicada y fallida. Las métricas de la **primera** reparación deben leerse de la ronda 1.
    """
    eventos = [
        _Evento(
            "DEV_RESOLUTION_INPUT",
            {"round": 1, "failed": ["focused", "chain"], "causal_gap": ["src/lib/tipos.ts"]},
        ),
        _Evento(
            "DEV_SCOPE_EXPANSION_REQUESTED",
            {"round": 1, "resources": ["src/lib/tipos.ts"], "evidence": ["focused exit 1"]},
        ),
        _Evento(
            "DEV_SCOPE_EXPANSION_APPROVED",
            {"plan_version": 2, "resources": ["src/lib/tipos.ts"]},
        ),
        _Evento("DEV_PLAN_REVISED", {"plan_version": 2, "added_resources": ["src/lib/tipos.ts"]}),
        _Evento(
            "DEV_CHANGE_REJECTED",
            {"round": 1, "issue_codes": ["CHANGE_ALREADY_EXISTS"]},
        ),
        _Evento(
            "DEV_RESOLUTION_INPUT",
            {"round": 2, "failed": ["focused", "chain"], "causal_gap": []},
        ),
        _Evento(
            "DEV_CHANGE_VALIDATED",
            {"changes": 2, "paths": ["src/components/Buscador.tsx", "src/components/Rejilla.tsx"]},
        ),
        _Evento(
            "DEV_CAUSAL_PROGRESS",
            {
                "round": 2,
                "causal_gap": [],
                "verification_result": "FAILED",
                "touched_resources": ["src/components/Buscador.tsx", "src/components/Rejilla.tsx"],
                "verifications_still_failing": ["focused", "chain"],
                "resources_status": ["src/lib/tipos.ts=UNCHANGED_BY_EVIDENCE"],
            },
        ),
    ]
    detalle = [
        {
            "role": "BUILDER",
            "phase": "resolution",
            "proposal": {
                "scope_expansion": True,
                "changes": [
                    {"path": "src/lib/tipos.ts", "operation": "MODIFY"},
                    {"path": "tests/tipos-chain.test.ts", "operation": "CREATE"},
                ],
            },
        }
    ]

    evidencia = _arnes()._resolution_evidence(eventos, [], detalle, _ResultadoFalso())

    assert evidencia["first_repair_reached_verification"] is False
    assert evidencia["first_repair_pass"] is False
    # La brecha es la de la ronda 1 (donde el cambio se descartó), no la de la ronda 2.
    assert evidencia["first_repair_causal_gap"] == ["src/lib/tipos.ts"]
    assert evidencia["first_repair_rejected_issue_codes"] == ["CHANGE_ALREADY_EXISTS"]
    assert evidencia["first_repair_scope_expansion_approved"] is True
    assert evidencia["first_repair_change_same_resource"] is True
    assert evidencia["first_repair_change_same_proposal"] is True
    assert evidencia["first_repair_change_applied"] is False, "el cambio se descartó con la ronda"
    assert evidencia["first_repair_focused_pass"] is False
    assert evidencia["first_repair_chain_pass"] is False
    assert evidencia["progress_records"][0]["round"] == 2, "solo la ronda 2 llegó a verificar"
    assert evidencia["round_info"]["1"]["validated_paths"] == []
    assert evidencia["round_info"]["2"]["validated_paths"] == [
        "src/components/Buscador.tsx",
        "src/components/Rejilla.tsx",
    ]


def test_d6_2_ciclo_real_un_cambio_hermano_invalido_descarta_el_cambio_discriminante(
    tmp_path: Path,
) -> None:
    """La forma exacta del fallo de 0.2.0 en CASE-B, en determinista.

    El cambio del recurso causal viaja en la misma propuesta que la ampliación (la regla D-5
    funciona) y la ampliación se aprueba; pero otro cambio de la misma propuesta reintroduce como
    ``CREATE`` un fichero que la ronda anterior ya creó, así que PUNTO descarta **toda** la
    propuesta y el cambio discriminante nunca se aplica.
    """
    responses = [
        _e2e_plan(
            modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx"),
            create=("tests/tipos-chain.test.ts",),
        ),
        {
            "summary": "consumidores y prueba",
            "changes": [
                *_consumidores(),
                {
                    "path": "tests/tipos-chain.test.ts",
                    "operation": "CREATE",
                    "content": "// cadena de tipos\n",
                    "reason": "dejar la cadena cubierta",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
        },
        {
            "summary": "ampliación y cambio de la fuente, con el hermano inválido",
            "changes": [
                {
                    "path": "src/lib/tipos.ts",
                    "operation": "MODIFY",
                    "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                    "reason": "la verificación focalizada mide este fichero",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
                *_consumidores(),
                {
                    "path": "tests/tipos-chain.test.ts",
                    "operation": "CREATE",
                    "content": "// cadena de tipos\n",
                    "reason": "reintroducir la prueba",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
            "root_cause": "la fuente canónica no declara el tipo que la verificación lee",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "la verificación focalizada encuentra el tipo exigido",
            "scope_expansion": {
                "trigger": "evidencia de la verificación focalizada",
                "evidence": ["focused mide src/lib/tipos.ts y el plan no lo autoriza"],
                "root_cause": "es la fuente canónica del dato que la verificación exige",
                "resources": ["src/lib/tipos.ts"],
                "operations": ["MODIFY"],
                "relationship": "fuente canónica de la misma cadena funcional",
            },
        },
        {
            "summary": "consumidores otra vez, sin la fuente",
            "changes": [
                *_consumidores(),
                {
                    "path": "tests/tipos-chain.test.ts",
                    "operation": "MODIFY",
                    "content": "// cadena de tipos revisada\n",
                    "reason": "corregir la operación de la prueba",
                    "acceptance_criterion": "una sola fuente de tipos",
                },
            ],
            "root_cause": "Buscador y Rejilla mantienen listas locales duplicadas",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "los consumidores consumen la fuente",
            "unchanged_resources": [
                {"path": "src/lib/tipos.ts", "evidence": "la fuente ya es la canónica"},
            ],
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses)
    progreso = _meta(detalle, "DEV_CAUSAL_PROGRESS")

    # La ampliación se aprobó y la regla funcionó: el cambio del recurso causal viajó con ella.
    assert len(_meta(detalle, "DEV_SCOPE_EXPANSION_APPROVED")) == 1
    rechazos = _meta(detalle, "DEV_CHANGE_REJECTED")
    assert [item["round"] for item in rechazos] == [1]
    assert rechazos[0]["issue_codes"] == ("CHANGE_ALREADY_EXISTS",)
    # Pero la ronda 1 entera se descartó: nunca verificó y el recurso causal no se aplicó.
    assert [item["round"] for item in progreso] == [2]
    assert "src/lib/tipos.ts" not in {item.path for item in result.applied}
    assert result.status is DevelopmentStatus.VERIFICATION_FAILED
    assert result.rolled_back is True
    assert result.repair_rounds == 2


def test_d5_5_el_registro_identifica_la_version_0_2_0() -> None:
    """Punto 13: la evidencia nombra la versión exacta, y el arnés no la confunde con 0.1.0."""
    ahora = utc_now()
    evidence = RunEvidence(
        run_id="r2",
        case_id="CASE-B",
        case_kind="B",
        mode="REAL",
        started_at=ahora,
        finished_at=ahora,
        elapsed_ms=1_000,
        provider_calls=(
            ProviderCall(role="BUILDER", provider="deepseek", phase="resolution", prompt_chars=10),
        ),
    )
    record = build_record(
        evidence,
        resolution_skill_id=SKILL_ID,
        resolution_skill_version="0.2.0",
        resolution_skill_activated=True,
        resolution_skill_chars=load_skill(SKILL_V2).chars,
    )

    assert record.resolution_skill_version == "0.2.0"
    assert record.resolution_skill_chars == load_skill(SKILL_V2).chars

    harness = Path(__file__).resolve().parents[1] / "_punto-skill-layer" / "run_baseline.py"
    texto = harness.read_text(encoding="utf-8")
    assert "resolution-{resolution_ref" in texto, "la evidencia va versionada por skill"
    for clave in (
        '"first_repair_change_same_proposal"',
        '"first_repair_scope_expansion_approved"',
        '"first_repair_focused_pass"',
        '"first_repair_chain_pass"',
    ):
        assert clave in texto, f"el arnés no registra {clave}"


def test_16c_el_recurso_relevante_que_el_plan_no_autoriza_queda_bloqueado(tmp_path: Path) -> None:
    """Punto 11 sobre el ciclo real: la salida correcta es pedir alcance, no escribir fuera."""
    responses = [
        _e2e_plan(modify=("src/components/Rejilla.tsx", "src/components/Buscador.tsx")),
        {"summary": "consumidores", "changes": _consumidores()},
        {
            "summary": "consumidores otra vez",
            "changes": _consumidores(),
            "root_cause": "los consumidores ya usan la fuente; el fallo persistirá mientras la "
            "fuente no declare el tipo",
            "evidence": ["focused exit 1: TIPOS: export const TIPOS = ['Casa'];"],
            "expected_effect": "ninguno dentro del alcance autorizado",
        },
    ]
    result, detalle, _client = _run_e2e(tmp_path, responses, max_repair_rounds=1)
    progreso = _meta(detalle, "DEV_CAUSAL_PROGRESS")

    assert progreso, "hubo una reparación y su progreso quedó registrado"
    assert _estados(detalle)["src/lib/tipos.ts"] == BLOCKED_BY_SCOPE
    assert progreso[0]["causal_gap"] == ("src/lib/tipos.ts",)
    assert result.status is DevelopmentStatus.VERIFICATION_FAILED
