"""Registra en PELL (misma cadena causal) el aprendizaje del failover del rol ARCHITECT.

VERIFIED.
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
            "el ARCHITECT primario (OpenAI/Codex) agotó su límite de uso (LIMIT_REACHED) y PUNTO "
            "terminó Planning con DEVELOPMENT_PLAN_REJECTED/ARCHITECT_UNAVAILABLE en vez de "
            "recuperarse solo con un sustituto autorizado y capaz, aunque Claude seguía CONNECTED"
        ),
        context=(
            "el router (``ProviderRouter.execute``/``_failover``) ya es genérico por rol desde "
            "PROVIDER FAILOVER v0: no hay ninguna rama ``if role == BUILDER`` — cualquier rol "
            "recibe el mismo mecanismo (causa operativa demostrable, candidatos conectados con "
            "capacidad efectiva, presupuesto de sustitutos, auditoría). Lo único que decide si un "
            "rol se recupera es si ``config/providers.yaml``.``failover.roles`` lo declara: solo "
            "BUILDER y VISUAL_QA lo tenían, ARCHITECT no"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "no hizo falta ningún cambio de código: el router ya trata cualquier rol igual "
            "(confirmado leyendo el código y con ``test_7c`` ya existente, que documenta que un "
            "rol sin política no hace failover — el mismo predicado que causaba el defecto). La "
            "corrección de causa raíz es puramente declarativa: añadir "
            "``ARCHITECT: [anthropic]`` a ``failover.roles`` en ``config/providers.yaml``, con la "
            "misma semántica (conectado + capacidad efectiva del rol, sin heredar autoridad, sin "
            "reasignar el primario) que ya tenían BUILDER y VISUAL_QA"
        ),
        procedure=(
            "antes de escribir código nuevo para 'el rol X no se recupera solo', comprobar si el "
            "mecanismo de failover ya es genérico por rol y el problema real es que la "
            "configuración simplemente no declara ese rol — un router ya diseñado sin ramas por "
            "rol no necesita una rama nueva, necesita una línea de configuración",
            "un ARCHITECT no necesita capacidad VISION (solo JSON/texto), así que el mismo "
            "sustituto de texto que ya sirve a BUILDER (Claude por suscripción) es un candidato "
            "razonable, sin coste añadido (allow_metered sigue en false)",
            "validar la integración completa (DevelopmentCycle + Task real + consola), no solo el "
            "router en aislado: el punto de fallo real era que ``_plan()`` convierte CUALQUIER "
            "resultado no-SUCCESS en ARCHITECT_UNAVAILABLE sin distinguir si hubo o no intento de "
            "sustituto — probarlo con la Task completa es lo que demuestra que la Task real "
            "vuelve a completar Planning sola, en el mismo ciclo, sin gate",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_architect_failover.py (8 pruebas A-H: sustituto único, cadena de "
            "sustitutos, sin sustituto compatible, no reasignación permanente, mismo ciclo/Task "
            "sin gate, reinicio conserva el historial, auditoría reconstruye "
            "primario->causa->sustituto->resultado, y la mutación documental que confirma el "
            "fail-closed cuando el rol no está declarado)",
            "tests/test_provider_failover.py::test_13 actualizado (la política real ahora "
            "declara ARCHITECT, BUILDER y VISUAL_QA) sigue en verde",
            "regresión relacionada (test_provider_failover, test_architect_failover, "
            "test_multi_provider, test_subscription_transports) y suite completa en verde",
        ),
        tags=(
            "type:provider-failover",
            "trigger:role-missing-from-declarative-failover-policy",
            "component:config/providers.yaml+providers/router",
            "provenance:deterministic-test",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
