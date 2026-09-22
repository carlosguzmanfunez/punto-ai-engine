"""Registra en PELL el aprendizaje de separar QUALITY TAKEOVER de OPERATIONAL FAILOVER
(VERIFIED). Misma cadena causal que BUILDER TAKEOVER (3c48db6); sin abrir auditoría aparte.
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
            "BUILDER TAKEOVER (3c48db6) reutilizaba FailoverPolicy.preferred() -- la MISMA lista "
            "de candidatos que el failover OPERATIVO (indisponibilidad demostrable: cuota, "
            "límite de tasa, desconexión) -- para decidir quién recupera una causa de CALIDAD "
            "(CHANGES_EMPTY ante FAILED accionable). Dos causas distintas compartían una sola "
            "política, así que no había forma de declarar que Codex (el proveedor prioritario de "
            "recuperación causal de PUNTO) fuera preferido para calidad sin también convertirlo "
            "en el sustituto operativo de todos los roles que lo declararan"
        ),
        context=(
            "la política de PUNTO es que Codex actúe como recuperación prioritaria de fallos "
            "CAUSALES de otros modelos (DeepSeek, Claude, futuros) para BUILDER, sin dejar de ser "
            "el ARCHITECT principal ni reasignarse permanentemente. Esa prioridad no tiene por "
            "qué coincidir con el orden de failover operativo (p. ej. Claude puede ser mejor "
            "sustituto operativo de DeepSeek por disponibilidad, y aun así Codex debe ser quien "
            "reciba primero una causa de calidad)"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "TakeoverPolicy (punto.providers.takeover, nuevo módulo): misma forma que "
            "FailoverPolicy (roles -> candidatos en orden, max_substitutes, allow_metered) pero "
            "una clase y una sección de configuración PROPIAS (providers.yaml: `takeover:`, "
            "independiente de `failover:`). ProviderRouter.configure_takeover()/"
            "takeover_policy() son simétricos a configure_failover()/failover_policy() pero "
            "escriben/leen un estado separado (_takeover_policy/_takeover_evaluator). "
            "execute_alternative() (el método que dev_cycle.py usa para el takeover de calidad) "
            "ahora consulta _takeover_policy, nunca _failover_policy -- execute() (el camino "
            "operativo normal) sigue intacto, consultando solo _failover_policy. Comparten el "
            "evaluador de capacidad/conexión (la pregunta no cambia según la causa) vía un "
            "helper común (_judge_with), pero la LISTA de candidatos es independiente por "
            "diseño"
        ),
        procedure=(
            "cuando dos causas de recuperación distintas (aquí: indisponibilidad operativa vs. "
            "respuesta exitosa pero inútil) comparten un mecanismo de selección de candidatos, "
            "preguntar si también deberían compartir la MISMA prioridad -- casi nunca es así: la "
            "disponibilidad y la calidad son dimensiones distintas y un proveedor puede ser buen "
            "candidato en una y mal candidato en la otra",
            "declarar la prioridad de recuperación (p. ej. 'Codex primero para fallos causales') "
            "en configuración, nunca en un `if provider == X`: la prueba de que es declarativo es "
            "que invertir el orden en el YAML invierte a quién se elige, sin tocar código",
            "reutilizar el evaluador de capacidad/conexión entre mecanismos hermanos es correcto "
            "(la pregunta 'puede este proveedor hacer el trabajo ahora' no depende de por qué se "
            "pregunta); reutilizar la LISTA de candidatos no lo es, si las prioridades pueden "
            "legítimamente diferir",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_quality_takeover_policy.py (9 pruebas A-H + F2): DeepSeek y Claude fallan "
            "causalmente -> Codex toma la reparación; Codex primario nunca se selecciona a sí "
            "mismo; Codex no disponible -> siguiente candidato autorizado; RATE_LIMIT/QUOTA sigue "
            "usando la política de failover operativo, nunca la de takeover (listas "
            "deliberadamente distintas en la prueba); invertir el orden declarado invierte la "
            "selección (dato, no código); el rol no se reasigna permanentemente; las dos "
            "políticas del router son independientes (configurar una nunca toca la otra); la "
            "configuración real de providers.yaml prioriza Codex para BUILDER",
            "tests/test_structural_repair_evidence.py y test_evidence_modality_routing.py "
            "actualizados (configuran configure_takeover en vez de configure_failover para sus "
            "escenarios de sustituto) -- 22 y 10 pruebas en verde",
            "confirmado por mutación: revertir execute_alternative() a leer _failover_policy "
            "vuelve a fallar 13 de las 22 pruebas de esos dos ficheros",
        ),
        tags=(
            "type:quality-takeover-policy-separation",
            "trigger:operational-and-quality-recovery-share-one-candidate-list",
            "component:providers/router+providers/takeover+providers/settings",
            "provenance:deterministic-test+mutation-check",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
