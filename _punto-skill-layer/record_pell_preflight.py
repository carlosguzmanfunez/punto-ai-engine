"""Registra en PELL los aprendizajes de esta intervención, con la evidencia que los sostiene.

Reglas que se respetan:

- se usan los **estados existentes** de PELL (``CANDIDATE`` / ``VERIFIED``) y su validación real:
  ``VERIFIED`` exige evidencia declarada, ``FAILED`` exige causa;
- **no** se convierte un hallazgo empírico de n=1 en ``VERIFIED``: los aprendizajes que ahora son
  *propiedad del motor demostrada con pruebas deterministas* se registran ``VERIFIED``; los que son
  observación empírica de corridas reales se quedan ``CANDIDATE``;
- el esquema vigente no tiene aristas: las relaciones (``related_to`` / ``supersedes`` / tipo /
  procedencia) viajan como etiquetas, que es el mecanismo que el esquema sí soporta;
- no se guardan logs, ni prompts, ni secretos: solo causa, resolución, evidencia y relación.

Escribe ``_punto-skill-layer/pell-repair-preflight.jsonl`` (no toca la memoria por defecto del
motor ni sobrescribe evidencia histórica) e imprime el resultado para el informe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVIDENCE = Path(__file__).resolve().parent
MEMORIA = EVIDENCE / "pell-repair-preflight.jsonl"

#: Aprendizajes evaluados (mandato §15). ``estado`` sale de la calidad de la evidencia.
APRENDIZAJES: tuple[dict[str, Any], ...] = (
    {
        "clave": "L1",
        "estado": "CANDIDATE",
        "problem": "cambiar la firma del parche no significa cambiar la estrategia causal",
        "context": (
            "En CASE-B, dos reparaciones tocaron ficheros distintos y ninguna abordó el recurso que "
            "la verificación medía: el fallo siguió igual."
        ),
        "failure_reason": (
            "observación empírica de corridas reales (n=1 por brazo): la firma del parche cambió sin "
            "que cambiara la estrategia causal"
        ),
        "solution": (
            "medir el progreso por recurso causal abordado (intersección con los recursos que mide la "
            "verificación), no por el conjunto de ficheros tocados"
        ),
        "procedure": (
            "comparar recursos tocados con recursos del fallo",
            "marcar estancamiento causal cuando la verificación sigue igual y no se aborda ni explica "
            "ningún recurso relevante nuevo",
        ),
        "verification": (),
        "tags": (
            "type:causal-progress",
            "trigger:repair-round-analysis",
            "component:orchestrator/dev_cycle",
            "provenance:experimento-03",
            "related_to:L2",
        ),
    },
    {
        "clave": "L2",
        "estado": "CANDIDATE",
        "problem": "mismo fallo de verificación sin recurso causal abordado indica falta de progreso",
        "context": (
            "Detector implementado en ``focused_resolution.causal_progress`` y medido en las corridas "
            "reales de CASE-B con el experimento 03."
        ),
        "failure_reason": (
            "el valor predictivo se apoya en evidencia experimental de n=1 por brazo; la detección sí "
            "está implementada y probada, la predicción no está medida a escala"
        ),
        "solution": (
            "registrar por ronda la brecha causal y declarar CAUSAL_STAGNATION cuando el mismo fallo "
            "persiste sin abordar ni explicar ningún recurso relevante nuevo"
        ),
        "procedure": (
            "firma del fallo + recursos relevantes + recursos tocados",
            "estancamiento = mismo fallo y ninguna evidencia causal nueva",
        ),
        "verification": (),
        "tags": (
            "type:causal-progress",
            "trigger:stagnation-detection",
            "component:orchestrator/focused_resolution",
            "provenance:experimento-03",
            "related_to:L1",
        ),
    },
    {
        "clave": "L3",
        "estado": "VERIFIED",
        "problem": "una propuesta determinísticamente inválida debe rechazarse antes de la verificación",
        "context": (
            "REPAIR PROPOSAL PREFLIGHT: la coherencia estructural se comprueba contra el estado real "
            "del workspace antes de validar y aplicar."
        ),
        "solution": (
            "preflight determinista con códigos medibles y corrección estructurada acotada; la "
            "propuesta inválida no llega a apply ni a verificación funcional"
        ),
        "procedure": (
            "medir existe/no existe y contenido real por recurso",
            "devolver el hecho estructurado y pedir la corrección sin consumir ronda funcional",
        ),
        "verification": (
            "tests/test_proposal_preflight.py::test_1_create_sobre_algo_que_existe_se_detecta_antes_de_aplicar",
            "tests/test_proposal_preflight.py::test_14_d7_la_propuesta_invalida_no_llega_a_aplicarse_ni_gasta_ronda",
            "audit:DEV_PROPOSAL_PREFLIGHT_FAILED",
        ),
        "tags": (
            "type:proposal-preflight",
            "trigger:repair-proposal",
            "component:orchestrator/proposal_preflight",
            "provenance:deterministic-test",
        ),
    },
    {
        "clave": "L4",
        "estado": "VERIFIED",
        "problem": "un cambio hermano inválido invalida una propuesta atómica por lo demás correcta",
        "context": (
            "CASE-B con focused-resolution 0.2.0: la ampliación se aprobó y el cambio del recurso "
            "causal viajó con ella, pero un CREATE sobre un fichero ya existente descartó la propuesta "
            "completa (CHANGE_ALREADY_EXISTS) y el cambio autorizado no se aplicó."
        ),
        "solution": (
            "detectar la inconsistencia antes de aplicar y corregirla con el estado real delante, "
            "manteniendo la atomicidad y sin estados híbridos"
        ),
        "procedure": (
            "no aplicar parcialmente una propuesta inválida",
            "pedir la corrección estructurada del cambio inválido",
        ),
        "verification": (
            "tests/test_proposal_preflight.py::test_14_d7_la_propuesta_invalida_no_llega_a_aplicarse_ni_gasta_ronda",
            "_punto-skill-layer/baseline-real-skill-resolution-0.2.0.json",
        ),
        "tags": (
            "type:atomicity",
            "trigger:sibling-invalid-change",
            "component:orchestrator/dev_cycle",
            "provenance:experimento-03b",
            "related_to:L3",
        ),
    },
    {
        "clave": "L5",
        "estado": "VERIFIED",
        "problem": "la ampliación de alcance y el cambio correspondiente pueden ir en la misma propuesta",
        "context": (
            "El ciclo evalúa la autoridad **antes** de validar los cambios, así que una propuesta "
            "puede pedir la ampliación y traer el cambio ya listo."
        ),
        "solution": (
            "evaluar la ampliación primero y validar el cambio bajo el alcance ya ampliado; sin "
            "aprobación no se aplica nada"
        ),
        "procedure": (
            "proponer ampliación y cambio juntos",
            "PUNTO decide: si aprueba, el cambio se valida y se aplica; si deniega o exige persona, "
            "no se aplica",
        ),
        "verification": (
            "tests/test_focused_resolution.py::test_16f_el_ciclo_admitiria_el_cambio_escalado_en_la_misma_respuesta",
            "tests/test_proposal_preflight.py::test_18_alcance_denegado_con_cambio_no_aplica_nada",
            "audit:DEV_SCOPE_EXPANSION_APPROVED",
        ),
        "tags": (
            "type:authority-order",
            "trigger:scope-expansion",
            "component:orchestrator/dev_cycle",
            "provenance:deterministic-test",
        ),
    },
    {
        "clave": "L6",
        "estado": "VERIFIED",
        "problem": "los hechos deterministas del workspace los mide PUNTO, no el proveedor",
        "context": (
            "Gastar capacidad probabilística en descubrir que un fichero existe (o no) consume ronda "
            "de reparación y presupuesto sin aportar información nueva."
        ),
        "solution": (
            "comprobar existe/no existe y el contenido real en PUNTO antes de aplicar, y reservar al "
            "proveedor la decisión semántica"
        ),
        "procedure": (
            "medir el estado real del workspace",
            "devolver al proveedor solo el hecho que debe corregir",
        ),
        "verification": (
            "tests/test_proposal_preflight.py::test_21_la_frontera_resume_los_hechos",
            "tests/test_proposal_preflight.py::test_12_el_feedback_es_compacto_y_estructurado",
        ),
        "tags": (
            "type:deterministic-boundary",
            "trigger:provider-proposal",
            "component:orchestrator/proposal_preflight",
            "provenance:deterministic-test",
            "related_to:L3",
        ),
    },
    {
        "clave": "L7",
        "estado": "VERIFIED",
        "problem": "un fallo de verificación se mapea a los recursos que realmente mide",
        "context": (
            "En CASE-B, ``focused`` lee ``src/lib/tipos.ts`` y el plan no incluía ese recurso: sin "
            "mapeo, la reparación siguiente no sabía qué recurso estaba sin abordar."
        ),
        "solution": (
            "mapeo determinista fallo→recurso (argv del comando o eslabón declarado por el plan) y "
            "brecha causal explícita antes de generar la siguiente reparación"
        ),
        "procedure": (
            "relación explícita preferente; si no la hay, relación declarada; si no, sin mapear",
            "declarar la brecha causal en la entrada de la reparación",
        ),
        "verification": (
            "tests/test_focused_resolution.py::test_7_un_fallo_se_mapea_a_sus_recursos_sin_llamar_al_proveedor",
            "audit:DEV_RESOLUTION_INPUT",
        ),
        "tags": (
            "type:failure-mapping",
            "trigger:verification-failure",
            "component:orchestrator/focused_resolution",
            "provenance:deterministic-test",
        ),
    },
)


def main() -> int:
    """Registra los aprendizajes y reporta el estado resultante."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    for aprendizaje in APRENDIZAJES:
        verificado = aprendizaje["estado"] == "VERIFIED"
        guardado = store.record(
            problem=aprendizaje["problem"],
            context=aprendizaje["context"],
            attempts=(),
            failure_reason=aprendizaje.get("failure_reason", ""),
            solution=aprendizaje["solution"],
            procedure=aprendizaje["procedure"],
            result=ExperienceResult.SUCCESS if verificado else ExperienceResult.FAILED,
            verification=aprendizaje["verification"],
            tags=aprendizaje["tags"],
            status=ExperienceStatus(aprendizaje["estado"]),
        )
        marca = "V" if guardado.status is ExperienceStatus.VERIFIED else "c"
        print(
            f"[{marca}] {aprendizaje['clave']} {guardado.id} {guardado.status.value}: "
            f"{guardado.problem[:70]}"
        )

    print()
    for estado in ExperienceStatus:
        total = len(store.list(status=estado))
        print(f"{estado.value}: {total}")
    print(f"\nmemoria: {MEMORIA} ({len(store.list())} experiencias)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
