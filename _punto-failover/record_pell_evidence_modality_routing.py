"""Registra en PELL el aprendizaje de EVIDENCE MODALITY ROUTING (VERIFIED).

Misma cadena causal que el resto de esta sesión: aprendizaje nuevo (no un duplicado), sin abrir
una auditoría aparte.
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
            "un criterio ESTRUCTURAL («unificar los tipos de propiedad en una sola fuente de "
            "verdad ... entre filtros, formularios, validaciones y visualizaciones») se "
            "clasificaba como VISUAL_APPEARANCE y PUNTO agotaba el presupuesto de evidencia "
            "visual (EXPAND_FRAMING) intentando demostrar con una captura algo que ninguna "
            "captura puede probar: una fuente canónica, sus consumidores y la ausencia de "
            "definiciones paralelas"
        ),
        context=(
            "la causa era una coincidencia de subcadena: «visual» es a la vez un adjetivo "
            "(«diseño visual») y la raíz literal de un sustantivo no estético «visualización»/"
            "«visualizaciones» (una vista/consumidor de datos). Cualquier frase con ese "
            "sustantivo activaba VISUAL_APPEARANCE aunque hablara de otra cosa por completo"
        ),
        attempts=(
            "dar precedencia ciega a lo estructural sobre lo visual cuando la frase contiene "
            "«unificar ... en una sola fuente»: rompió criterios legítimamente visuales que "
            "también usan esa frase («unificar el estilo visual ... en una sola fuente»)",
        ),
        failure_reason=(
            "la precedencia por frase-completa no distingue un criterio genuinamente visual que "
            "casualmente incluye lenguaje de «fuente única» de uno genuinamente estructural: la "
            "corrección tenía que ser LÉXICA (qué palabra es), no de PRECEDENCIA (qué categoría "
            "gana)"
        ),
        solution=(
            "1) exigir palabra completa para la marca «visual» (con sus formas adjetivas "
            "declaradas: visual/visuales/visualmente), nunca una subcadena — así «visualización»/"
            "«visualizaciones» deja de contarse como aspecto estético, sin tocar las formas "
            "legítimas; 2) añadir un tipo de afirmación nuevo, ESTRUCTURAL (STRUCTURAL_"
            "CONSISTENCY), verificado con análisis estático determinista (punto.structure: fuente "
            "canónica exportada, consumidores que la importan, ausencia de duplicados) — nunca "
            "con VISUAL_QA, así que un criterio estructural JAMÁS entra en el bucle de "
            "recuperación visual: no hay presupuesto que agotar en una modalidad incapaz. La "
            "precedencia entre categorías (visual primero, estructural después) se mantuvo desde "
            "siempre; lo que cambió es qué cuenta como una coincidencia visual genuina"
        ),
        procedure=(
            "cuando una palabra corta sirve de marca (STOPWORDS-like) y también es la raíz de "
            "una familia de sustantivos no relacionados, la subcadena SIEMPRE producirá falsos "
            "positivos: usar coincidencia de palabra completa con las formas declaradas "
            "explícitamente, nunca ampliar la marca a un prefijo genérico",
            "antes de resolver un conflicto de clasificación con PRECEDENCIA entre categorías, "
            "comprobar si el conflicto es en realidad léxico (la marca coincide con algo que no "
            "significa lo que la marca cree): la precedencia esconde el síntoma, la corrección "
            "léxica ataca la causa",
            "una modalidad de evidencia nueva no necesita su propio bucle de recuperación si el "
            "camino determinista ya existente (SATISFIED/FAILED, nunca INCONCLUSIVE, como lo "
            "cartográfico) la cubre: solo hace falta la clasificación correcta y un analizador",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_evidence_modality_routing.py (10 pruebas A-J: clasificación del criterio "
            "real, SATISFIED sin captura, criterio realmente visual conserva su ruta, sin "
            "capacidad de modelo exigida de más, duplicación FAILED determinista, integración "
            "completa SATISFIED sin gate, duplicación real reparada dentro del mismo ciclo, "
            "presupuesto visual no gastado, reinicio conserva el resultado, gate SUPERSEDED nunca "
            "auto-aprobado)",
            "regresión completa (suite entera menos infraestructura no disponible en esta "
            "máquina: sandbox/contenedor y navegador real) en verde tras corregir dos "
            "regresiones reales que la propia corrección expuso (fixture de test_dev_cycle.py con "
            "el mismo lenguaje del defecto real; precedencia estructural-sobre-visual demasiado "
            "amplia en test_visual_interaction.py)",
        ),
        tags=(
            "type:evidence-modality-routing",
            "trigger:substring-marker-collides-with-unrelated-noun-family",
            "component:acceptance+structure+orchestrator/dev_cycle",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
