"""Registra en PELL el aprendizaje reutilizable de VISUAL_QA efectivo (VERIFIED).

Mismas reglas que ``record_pell_failover.py``. Escribe ``pell-visual-qa-effective.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-visual-qa-effective.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    guardado = store.record(
        problem=(
            "una capacidad que un transporte declara no disponible puede ser real: la limitación "
            "estaba en lo que el transporte declara, no en lo que el cliente hace"
        ),
        context=(
            "CodexTransport declaraba supports_images=False y rechazaba adjuntos porque se escribió "
            "cuando codex exec era solo texto. Codex 0.155 adjunta imágenes con --image y, medido "
            "con la sesión real, leyó una captura de navegador (incluido un código aleatorio). "
            "Además, con capacidad disponible el ciclo seguía sin evidencia: no capturaba ni "
            "evaluaba nada, así que el criterio quedaba NOT_VERIFIED."
        ),
        attempts=("confiar en la declaración del transporte",),
        failure_reason="la declaración estática del transporte quedó desactualizada frente al binario",
        solution=(
            "acreditar la capacidad en el binario instalado (fail closed), seleccionar por "
            "capacidad efectiva y no por catálogo, y producir la evidencia en el ciclo: captura real "
            "de la app + veredicto con contrato cerrado donde todo lo dudoso es UNCLEAR"
        ),
        procedure=(
            "probar la capacidad con una prueba real discriminante (dato aleatorio solo en la "
            "imagen y control sin imagen), no con un mock",
            "declarar la capacidad solo si el binario la anuncia; sin binario o sin respuesta, no",
            "capacidad efectiva = declarada ∩ transporte activo ∩ conexión",
            "que la capacidad exista no basta: el ciclo debe producir la evidencia y ligarla a la "
            "Task",
            "los criterios de interacción no se demuestran con una captura estática: UNCLEAR",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/integration/test_codex_vision_live.py::test_codex_consume_imagenes_por_el_transporte_configurado",
            "tests/test_visual_qa_effective.py::test_3_vision_declarada_pero_no_efectiva_no_es_elegible",
            "tests/test_visual_qa_effective.py::test_11_un_pass_de_visual_qa_satisface_el_criterio_y_deja_evidencia_ligada_a_la_task",
            "audit:DEV_VISUAL_ASSESSED",
        ),
        tags=(
            "type:effective-capability",
            "trigger:evidence-required-no-visual-qa",
            "component:providers/transports/codex",
            "provenance:live-and-deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {guardado.id} {guardado.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
