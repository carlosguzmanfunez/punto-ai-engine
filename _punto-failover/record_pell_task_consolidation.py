"""Registra en PELL aprendizajes causales reutilizables sobre consolidación de Tasks (VERIFIED)."""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-task-consolidation.jsonl"


def main() -> int:
    """Registra los aprendizajes y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    a = store.record(
        problem=(
            "dos Tasks distintas existen para el mismo trabajo (mismo destino, objetivo "
            "equivalente) porque nada comprobaba equivalencia antes de crear una nueva"
        ),
        context=(
            "Dos Tasks del mismo destino con objetivos casi idénticos coexistían activas, una "
            "con la rama de un fixture de pruebas en su resultado en vez de la rama de trabajo "
            "canónica del destino."
        ),
        attempts=("deduplicar por igualdad exacta de texto del objetivo",),
        failure_reason="el mismo trabajo casi nunca se pide con el mismo texto literal",
        solution=(
            "equivalencia por destino + objetivo normalizado (tokens sin acentos/plural/stopwords, "
            "Jaccard) + alcance que se contiene + criterios compatibles; al crear, bajo un cerrojo, "
            "buscar una Task activa equivalente y absorber la solicitud en ella (continuación si es "
            "reanudable, nada si ya corre/está completa); en la recuperación, consolidar duplicados "
            "históricos por el mismo criterio, eligiendo la canónica por avance real + evidencia + "
            "cronología, nunca por ID ni por orden de llegada"
        ),
        procedure=(
            "nunca deduplicar por igualdad literal de texto: normalizar y medir similitud",
            "la comprobación de equivalencia y el registro de la Task son una sola sección "
            "atómica bajo cerrojo: si no, dos solicitudes concurrentes crean dos Tasks",
            "la Task canónica es la que el estado real sostiene mejor (intentos, evidencia, "
            "cronología), no la primera ni la última",
            "una Task superada nunca pierde su historial: se le añade un estado de linaje y una "
            "relación explícita (superseded_by/duplicate_of), y su información persiste íntegra",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_task_consolidation_and_graph.py::test_a/test_a2/test_b/test_f/test_f2",
        ),
        tags=(
            "type:task-deduplication",
            "trigger:equivalent-work-two-tasks",
            "component:api/task_identity+console",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    b = store.record(
        problem=(
            "una identidad de destino (repositorio, rama de trabajo, rama de producción) se lee "
            "de configuración mutable en memoria y una prueba o un fixture puede filtrarse al "
            "estado operativo real sin que nada lo note"
        ),
        context=(
            "Una Task mostraba una rama de trabajo (`ai/console-fixture`) que no coincidía con la "
            "declarada por la configuración canónica del destino (`ai/...-tasks`); el origen era "
            "un test/fixture, no el destino real."
        ),
        attempts=("corregir el string de la rama a mano en el estado persistido",),
        failure_reason="arregla el síntoma una vez; la próxima ejecución de un fixture repite el fallo",
        solution=(
            "identidad canónica explícita del destino (repositorio + rama de trabajo + rama de "
            "producción, sin credenciales) con huella estable, persistida con cada Task al nacer; "
            "toda reanudación comprueba la huella contra la identidad vigente del destino y falla "
            "cerrado si difiere; una Task anterior a la huella se comprueba por la rama real de su "
            "resultado; el estado durable de las pruebas nunca usa la ruta por defecto (falla en "
            "voz alta si una prueba no fija su propia ruta de estado)"
        ),
        procedure=(
            "declarar una identidad canónica única en el propio modelo de configuración",
            "persistir la huella con cada entidad que dependa de ella, nunca reconstruirla a ciegas",
            "toda reanudación de trabajo revalida la identidad antes de actuar",
            "el aislamiento de pruebas debe fallar en voz alta si algo intenta usar el estado real "
            "por defecto, no solo confiar en que el fixture de aislamiento se cargue",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_task_consolidation_and_graph.py::test_c/test_c2",
            "src/punto/api/console_state.py::default_console_state_path (fail-closed en pruebas)",
        ),
        tags=(
            "type:identity-isolation",
            "trigger:fixture-branch-leaks-into-real-state",
            "component:workspace/target+console",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    c = store.record(
        problem=(
            "las relaciones entre unidades de trabajo (sustitución, duplicado, continuación) solo "
            "existían como texto libre en notas, sin poder reconstruir la cadena completa"
        ),
        context=(
            "Se pedía poder trazar target → task → intentos → artefacto → publicación y las "
            "relaciones supersedes/duplicate_of/continuation_of/retries sin crear un segundo "
            "almacén de estado."
        ),
        attempts=("guardar el grafo como una tabla aparte que se actualiza a mano",),
        failure_reason="una segunda fuente de verdad diverge del estado real tarde o temprano",
        solution=(
            "el grafo es una PROYECCIÓN pura del estado durable ya existente (tareas, intentos, "
            "gates, publicaciones) más las relaciones explícitas que sí son hechos (persistidas "
            "con la Task); se recalcula siempre desde ahí, nunca se persiste aparte"
        ),
        procedure=(
            "antes de crear un almacén nuevo, preguntar si se puede derivar del que ya existe",
            "las relaciones que son hechos (quién sustituye a quién) sí se persisten; el resto "
            "(nodos, aristas) se deriva en el momento de consultar",
        ),
        result=ExperienceResult.SUCCESS,
        verification=("tests/test_task_consolidation_and_graph.py::test_h/test_h2",),
        tags=(
            "type:derived-graph-projection",
            "trigger:structural-traceability-request",
            "component:api/task_graph",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id} / {b.id} / {c.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
