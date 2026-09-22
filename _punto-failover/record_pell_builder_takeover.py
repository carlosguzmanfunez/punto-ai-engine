"""Registra en PELL el aprendizaje de BUILDER TAKEOVER (VERIFIED). Misma cadena causal que
EVIDENCE MODALITY ROUTING y la propagación del remedio; sin abrir auditoría aparte.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "con la evidencia ya accionable (remedio incluido, 710abcf), un criterio FAILED "
            "seguía terminando en VERIFICATION_FAILED: el mismo BUILDER volvía a responder "
            "CHANGES_EMPTY ronda tras ronda hasta agotar el presupuesto de reparación, como si "
            "una llamada más al mismo proveedor con la misma causa fuera a cambiar el resultado"
        ),
        context=(
            "el failover operativo del router (punto.providers.failover) es, a propósito, "
            "estrecho: solo actúa ante indisponibilidad OPERATIVA demostrable (cuota, límite de "
            "tasa, desconexión) y explícitamente NO ante 'una respuesta técnicamente incorrecta "
            "del proveedor' — cambiar de proveedor por una respuesta mala escondería el problema. "
            "CHANGES_EMPTY es justo eso: una respuesta exitosa (JSON válido) que simplemente no "
            "actuó. Reutilizar el failover operativo para esto habría violado su propio límite "
            "declarado"
        ),
        attempts=(
            "enriquecer aún más el texto de reparación para el mismo proveedor: no ataca la "
            "causa — el proveedor ya recibía evidencia accionable (remedio incluido) y seguía "
            "sin actuar; más texto al mismo destinatario no cambia el resultado",
        ),
        failure_reason=(
            "CHANGES_EMPTY no es indisponibilidad del proveedor (el failover operativo no debe "
            "activarse: seguiría respondiendo con éxito), es una señal a un nivel distinto — el "
            "ciclo de desarrollo, no el transporte — de que ESTE proveedor, para ESTA causa, no "
            "va a producir el cambio"
        ),
        solution=(
            "un mecanismo NUEVO y explícitamente separado del failover operativo: "
            "ProviderRouter.execute_alternative(role, exclude=...) reutiliza exactamente los "
            "mismos candidatos declarados (FailoverPolicy.preferred) y el mismo evaluador de "
            "capacidad/conexión, pero se dispara por una condición distinta — dev_cycle.py "
            "decide el TAKEOVER cuando CHANGES_EMPTY coincide con un ClaimRecord FAILED que ya "
            "tiene remedy (accionable) — y con su propio presupuesto declarativo "
            "(DevelopmentConfig.max_builder_takeovers), sin tocar la asignación de rol "
            "permanente ni la semántica del failover operativo"
        ),
        procedure=(
            "antes de reutilizar un mecanismo de sustitución de proveedor ya existente para un "
            "caso nuevo, leer su propia documentación de límites: si el módulo dice "
            "explícitamente 'esto no es para X', no es una sugerencia — X necesita su PROPIO "
            "mecanismo, aunque comparta la selección de candidatos",
            "una respuesta 'exitosa pero inútil' (JSON válido, sin cambio material) es una clase "
            "de fallo distinta de un error de transporte: requiere su propia detección (aquí: "
            "CHANGES_EMPTY + claim FAILED con remedy) y su propio presupuesto, no una "
            "reinterpretación forzada del mecanismo de fallos operativos",
            "clasificar SALVAGE/REWRITE con una métrica real (similitud de texto, difflib) sobre "
            "el contenido antes/después, no por el tipo de operación declarada: un MODIFY que "
            "reemplaza casi todo es tan REWRITE como un DELETE+CREATE",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_structural_repair_evidence.py (13 pruebas A-K + 4 + mutación): takeover "
            "automático ante CHANGES_EMPTY accionable, el sustituto recibe la evidencia completa "
            "y corrige, SALVAGE y REWRITE clasificados por similitud real, el primario nunca se "
            "reintenta para la misma causa (a nivel de ciclo y a nivel de router), el rol "
            "primario no se reasigna permanentemente, misma Task/mismo ciclo, reinicio conserva "
            "el resultado, el grafo reconstruye primario->takeover->sustituto->resultado, sin "
            "sustituto disponible falla explícito sin reparar nada a medias",
            "confirmado por reversión temporal: 12/13 pruebas fallan sin la implementación",
            "regresión relacionada (evidence_modality_routing, provider_failover, "
            "architect_failover) en verde tras adaptar dos escenarios existentes que ahora "
            "toman la ruta de takeover en vez de reintentar el mismo proveedor",
        ),
        tags=(
            "type:builder-takeover",
            "trigger:successful-response-without-material-change",
            "component:orchestrator/dev_cycle+providers/router",
            "provenance:deterministic-test+reversion-check",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
