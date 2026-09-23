"""TAKEOVER REAL DEL INTENTO 10 — un recovery provider que sí intentó no queda excluido para
siempre.

Causa real (Task 0983a418, intento 10, continuación de 0a963c0): con la política de takeover ya
separada y priorizando Codex, el ciclo real volvió a terminar en ``CHANGES_EMPTY`` /
``DEVELOPMENT_VERIFICATION_FAILED`` tras un takeover que SÍ funcionó a medias — Codex (openai) SÍ
tomó la reparación (excluyendo a Claude, que había respondido ``CHANGES_EMPTY``) y SÍ produjo un
cambio material real (4 ficheros, SALVAGE), pero ese cambio no bastó para pasar
``typecheck``/``property-types``. La ronda siguiente volvió a caer, por failover OPERATIVO
persistente (DeepSeek sin crédito durante todo el ciclo), en Claude — que volvió a intentar con un
cambio real (2 ficheros) que TAMPOCO bastó — y la ronda final terminó, de nuevo, en
``CHANGES_EMPTY``. Ese ``CHANGES_EMPTY`` final era, en los mismos términos que el primero, un
criterio ``STRUCTURAL_CONSISTENCY`` FAILED con remedio accionable — pero el presupuesto de
takeover (``max_builder_takeovers=1``) ya estaba agotado por el primer uso, así que el ciclo cerró
en ``VERIFICATION_FAILED`` en vez de darle a Codex una segunda oportunidad.

Causa raíz (esta ronda), dos defectos combinados:

1. ``builder_tried`` (la exclusión de TAKEOVER) se llenaba con **cualquier** proveedor que
   respondiera, no solo con quien respondía ``CHANGES_EMPTY``. Un proveedor que SÍ propuso un
   cambio real (aunque no bastara para pasar la verificación) quedaba excluido para siempre de
   futuras recuperaciones, aunque no fuera «la misma observación vacía repetida» — era iteración
   legítima sobre su propio intento anterior.
2. ``max_builder_takeovers`` por defecto era ``1``: un ciclo real puede legítimamente producir
   más de un ``CHANGES_EMPTY`` distinto (el mismo proveedor operativo puede volver a fallar en
   calidad tras la ronda de otro), y el presupuesto no dejaba margen para una segunda
   recuperación.

Corrección: ``builder_tried`` solo se llena cuando el proveedor invocado deja un
``BuildValidationIssue`` con ``code == "CHANGES_EMPTY"`` (respondió con éxito y no produjo nada);
un proveedor que propuso un cambio real, aunque PUNTO lo rechace o no baste, sigue siendo un
candidato legítimo para el SIGUIENTE takeover. ``max_builder_takeovers`` pasa de 1 a 2 por
defecto.

    pytest tests/test_takeover_reattempt.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from punto.orchestrator.dev_cycle import DevelopmentConfig
from punto.schemas.audit import AuditEventType
from test_structural_repair_evidence import (
    CRITERIO,
    _consola_con_takeover,
    _plan_duplicado,
    _solicitud,
)


def _reparacion_insuficiente() -> dict[str, Any]:
    """Cambio REAL (arregla la duplicación) pero rompe, por error, la verificación FOCUSED."""
    return {
        "summary": "reexportar desde la fuente canónica",
        "root_cause": "dos ficheros declaran una constante TIPOS por separado",
        "evidence": ["src/lib/tipos_legacy.ts declara su propia versión de TIPOS"],
        "expected_effect": "el criterio de consistencia estructural pasa a SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos_legacy.ts",
                "operation": "MODIFY",
                "content": "export { TIPOS } from './tipos';\n",
                "reason": "fuente duplicada: reexporta la canónica en vez de declarar la suya",
                "acceptance_criterion": CRITERIO,
            },
            {
                "path": "src/lib/tipos.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS = ['Casa'];\n",
                "reason": "normaliza la fuente canónica (defecto real: pierde un valor)",
                "acceptance_criterion": CRITERIO,
            },
        ],
    }


def _reparacion_completa() -> dict[str, Any]:
    """El MISMO proveedor, en su segunda oportunidad, corrige lo que le faltó la primera vez."""
    return {
        "summary": "restaurar el catálogo completo en la fuente canónica",
        "root_cause": "la reparación anterior dejó incompleto el catálogo de tipos",
        "evidence": ["la verificación focalizada exige 'Apartamento' en la fuente canónica"],
        "expected_effect": "verificación y consistencia estructural quedan SATISFIED",
        "changes": [
            {
                "path": "src/lib/tipos.ts",
                "operation": "MODIFY",
                "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
                "reason": "restaura el valor que faltaba en el intento anterior",
                "acceptance_criterion": CRITERIO,
            }
        ],
    }


# ============ A/G · un recovery provider con un intento REAL insuficiente no queda excluido
def test_a_un_intento_real_insuficiente_no_excluye_al_proveedor_de_un_segundo_takeover(
    tmp_path: Path,
) -> None:
    """Reproduce el intento 10 real: Codex (aquí «sustituto») repara dos veces, no una."""
    plan = _plan_duplicado()
    client, _audit, repo, primario, sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=plan,
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion_insuficiente(), _reparacion_completa()],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert tarea["development"]["claims_result"] == "SATISFIED"
    contenido = (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")
    assert "Apartamento" in contenido, "el segundo intento del mismo sustituto sí corrigió"
    # El sustituto respondió DOS veces (una insuficiente, una completa): nunca quedó excluido.
    assert sustituto is not None and len(sustituto.prompts) == 2
    # El primario, en cambio, respondió vacío las dos veces que le tocó: sigue siendo él quien
    # se excluye, nunca quien intentó de verdad.
    assert len(primario.prompts) == 2


# ==================================== B · dos eventos de TAKEOVER, no uno
def test_b_dos_eventos_de_takeover_distintos_quedan_auditados(tmp_path: Path) -> None:
    """El presupuesto (2 por defecto) permite un segundo takeover real cuando hace falta."""
    plan = _plan_duplicado()
    client, audit, _repo, _primario, _sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=plan,
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion_insuficiente(), _reparacion_completa()],
    )

    client.post("/console/tasks", json=_solicitud())

    peticiones = [
        dict(e.metadata)
        for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
        if "excluded" in dict(e.metadata)
    ]
    assert len(peticiones) == 2, "dos CHANGES_EMPTY distintos, dos peticiones de takeover"
    for peticion in peticiones:
        assert list(peticion["excluded"]) == ["primario"], (
            "el sustituto nunca aparece excluido: su intento anterior fue real, no vacío"
        )
    resultados = [
        dict(e.metadata)
        for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
        if "outcome" in dict(e.metadata)
    ]
    assert len(resultados) == 2
    assert all(r["provider"] == "sustituto" for r in resultados)


# =============================== C · el presupuesto por defecto ya es 2, no 1
def test_c_el_presupuesto_por_defecto_de_takeover_es_dos() -> None:
    """Un ciclo real puede producir más de un CHANGES_EMPTY distinto: el default lo permite."""
    config = DevelopmentConfig()

    assert config.max_builder_takeovers == 2


# ============ D · un CHANGES_EMPTY real (no un intento) sí sigue excluyendo a quien lo dio
def test_d_un_changes_empty_real_si_excluye_a_quien_lo_dio(tmp_path: Path) -> None:
    """La corrección no relaja el caso original: CHANGES_EMPTY genuino sigue excluyendo."""
    plan = _plan_duplicado()
    client, audit, _repo, _primario, _sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=plan,
        primary_script=[{"changes": []}],
        substitute_script=[{"changes": []}],  # el sustituto TAMBIÉN responde vacío
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    # Ambos candidatos (primario y sustituto) respondieron CHANGES_EMPTY: ambos quedan
    # excluidos, y sin más candidatos declarados el ciclo falla explícito, no silenciosamente.
    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    peticiones = [
        dict(e.metadata)
        for e in audit.by_type(AuditEventType.DEV_BUILDER_TAKEOVER)
        if "excluded" in dict(e.metadata)
    ]
    assert peticiones and list(peticiones[0]["excluded"]) == ["primario"]


# ============================ J (mutación) · volver a excluir por cualquier respuesta rompe esto
def test_j_mutacion_excluir_cualquier_respuesta_rompe_la_reparacion_en_dos_pasos(
    tmp_path: Path,
) -> None:
    """Documenta la condición exacta: solo ``CHANGES_EMPTY`` debe alimentar la exclusión.

    Si una regresión vuelve a añadir a ``builder_tried`` a cualquier proveedor que responda
    (no solo a quien deja ``CHANGES_EMPTY``), el segundo takeover de
    ``test_a_...`` no encontraría candidato (el propio sustituto quedaría excluido por su
    primer intento real) y el ciclo fallaría en vez de completar. Esta prueba fija ese
    resultado esperado como ancla de regresión.
    """
    plan = _plan_duplicado()
    client, _audit, _repo, _primario, _sustituto, _router = _consola_con_takeover(
        tmp_path,
        architect_plan=plan,
        primary_script=[{"changes": []}],
        substitute_script=[_reparacion_insuficiente(), _reparacion_completa()],
    )

    tarea = client.post("/console/tasks", json=_solicitud()).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", (
        "si esto falla, builder_tried volvió a excluir a un proveedor que sí intentó de verdad"
    )
