"""Registra en PELL el aprendizaje reutilizable de la evidencia visual interactiva (VERIFIED).

Mismas reglas que ``record_pell_failover.py``. Escribe ``pell-visual-hover.jsonl``.
"""

from __future__ import annotations

from pathlib import Path

MEMORIA = Path(__file__).resolve().parent / "pell-visual-hover.jsonl"


def main() -> int:
    """Registra el aprendizaje y reporta el resultado."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORIA)
    guardado = store.record(
        problem=(
            "un criterio de interacción (hover) no se demuestra con una captura estática, y una "
            "captura de viewport puede no contener lo que el criterio exige ver"
        ),
        context=(
            "El gate vivo dio FAIL al criterio «aparece el nombre» en una página que sí lo "
            "mostraba: la etiqueta estaba en el DOM pero bajo el pliegue, fuera de la captura. El "
            "revisor tenía razón; el defecto era de la evidencia. Antes de eso, un hover «simulado» "
            "por :hover de CSS o por script no habría demostrado una interacción real."
        ),
        attempts=("capturar solo el viewport antes y después del hover",),
        failure_reason="la evidencia no cubría el elemento que el criterio exige ver",
        solution=(
            "declarar la interacción en la configuración del destino (ruta, selector del elemento y "
            "de la etiqueta), ejecutarla con eventos de entrada reales sobre un punto que golpea al "
            "elemento, exigir que el navegador confirme :hover y capturar antes/después de la "
            "región elemento+etiqueta; PASS solo si además los píxeles difieren"
        ),
        procedure=(
            "no inferir un hover de una captura: ejecutarlo y comprobar :hover en el navegador",
            "elegir el punto de hover con elementFromPoint (un elemento tapado no es demostrable)",
            "capturar la región que une el elemento y la etiqueta esperada, no solo el viewport",
            "degradar PASS a UNCLEAR si antes y después son idénticos; sin elemento localizado, "
            "UNCLEAR sin llamar al revisor",
            "probar con un contraste real: cambio completo, parcial y ausente",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/integration/test_hover_interaction_live.py::test_hover_real_y_codex_distinguen_cambio_verdadero_parcial_y_falso",
            "tests/test_visual_interaction.py::test_3c_la_captura_cubre_el_elemento_y_su_etiqueta_aunque_esta_quede_fuera_del_viewport",
            "tests/test_visual_interaction.py::test_5b_un_pass_con_capturas_identicas_se_degrada_a_unclear",
            "audit:DEV_VISUAL_ASSESSED",
        ),
        tags=(
            "type:interaction-evidence",
            "trigger:hover-criterion-unclear",
            "component:visualqa/interaction",
            "provenance:live-and-deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {guardado.id} {guardado.status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
