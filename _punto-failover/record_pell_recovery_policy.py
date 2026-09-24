"""Registra en PELL el aprendizaje de Fase 8A (VERIFIED): RECOVERY es una tercera política
causal independiente, y BUSY-sin-invocar se lee del propio LeaseRecord público, sin necesitar
un método nuevo en el ledger. Sin abrir auditoría aparte.
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
            'Fase 8A pedía una política de recuperación operacional ("dado que el provider '
            'actual falló de verdad, ¿quién continúa?") con un orden GENERAL de rotación '
            "(Codex -> Claude -> DeepSeek) que excluye siempre a quien causó el fallo -- "
            "aplicable sin importar CUÁL de los tres sea el causante. Ni FailoverPolicy "
            "(failover.roles.BUILDER=[anthropic], un solo sustituto, siempre asumiendo DeepSeek "
            "como primario) ni TakeoverPolicy (takeover.roles.BUILDER=[openai, anthropic], "
            "misma asunción) declaran ese orden general de tres: ambas están construidas para "
            "'dado el primario configurado, ¿con quién sigo', no para 'dado que ESTE (cualquiera "
            "de los tres) falló, ¿con quién sigo'"
        ),
        context=(
            "confirmado leyendo providers.yaml y punto.providers.failover/takeover antes de "
            "escribir nada: reutilizar cualquiera de las dos políticas existentes habría exigido "
            "o bien mezclar dos causas de recuperación bajo una lista pensada para otra cosa "
            "(el error exacto que 0a963c0 ya corrigió una vez, separando takeover de failover), "
            "o bien codificar el orden general directamente en Python en vez de en configuración"
        ),
        attempts=(),
        failure_reason="",
        solution=(
            "RecoveryPolicy (punto.providers.recovery_policy, nuevo módulo): misma forma que "
            "FailoverPolicy/TakeoverPolicy (roles -> candidatos en orden, max_substitutes, "
            "allow_metered) pero su propia sección de configuración (providers.yaml: "
            "`recovery:`, BUILDER: [openai, anthropic, deepseek] -- el orden general completo, "
            "sin asumir ningún primario). ProviderRouter.configure_recovery()/recovery_policy() "
            "son simétricos a configure_takeover()/takeover_policy() pero escriben/leen estado "
            "separado. El nuevo router.recovery_candidates(role, exclude=, needs_vision=) "
            "reutiliza el MISMO evaluador de capacidad/conexión (_judge_with) que failover y "
            "takeover ya comparten -- la pregunta 'puede este proveedor hacer el trabajo ahora' "
            "no cambia por qué se pregunta -- pero NUNCA juzga al `exclude`d: se quita del "
            "conjunto antes de construir la lista de candidatos, no se descarta después de "
            "evaluarlo como 'no elegible' -- confirmado por mutación: quitar esa exclusión deja "
            "seleccionable al provider causal aunque el evaluador lo diga sano. El router no "
            "sabe nada de leases (capa aparte): recovery_candidates() nunca ve BUSY, solo "
            "conectividad/capacidad/coste -- eso lo añade encima punto.scheduling.recovery_waits "
            "(RecoveryWaitCoordinator), que sí conoce el LeaseLedger y comprueba, candidato por "
            "candidato ya elegible por el router, si su ProviderLease sigue ACTIVE y sin expirar "
            "-- leyendo LeaseRecord.state/expires_at directamente vía el propio "
            "LeaseLedger.head() público (ya documentado como 'para diagnóstico y tests'), sin "
            "necesitar mutar el ledger ni añadir un método nuevo"
        ),
        procedure=(
            "cuando una tercera causa de recuperación aparece y las dos políticas existentes "
            "(operativa, calidad) ya declaran listas que ASUMEN un primario fijo, no forzar la "
            "tercera dentro de una de las dos -- preguntar si la nueva causa necesita un orden "
            "GENERAL (aplicable sin importar quién sea el causante) en vez de un orden "
            "'primario -> sustituto(s)'; si sí, es una política nueva, del mismo tamaño que las "
            "otras dos, en su propia sección de configuración",
            "un 'candidato causalmente excluido' debe quitarse ANTES de construir la lista a "
            "juzgar, no filtrarse después de juzgarlo -- juzgarlo y luego descartar el resultado "
            "dejaría una ventana (revertir solo el descarte) donde parecería 'legítimamente no "
            "elegible' en vez de 'estructuralmente no es candidato', y esa distinción es la que "
            "impide que un causante que vuelve a estar sano se reseleccione en la MISMA "
            "recuperación",
            "cuando una capa inferior (aquí: el router de proveedores) genuinamente no conoce un "
            "hecho que la decisión necesita (BUSY de un lease), no inventarle ese conocimiento -- "
            "layering explícito: la capa inferior devuelve lo que sí puede acreditar (conectado, "
            "capaz, coste) y la capa que SÍ conoce el hecho que falta (scheduling, con el "
            "LeaseLedger) lo añade encima, candidato por candidato, antes de decidir",
            "antes de mutar una condición de orden para probar 'el orden de entrada no debería "
            "importar', verificar que los dos escenarios de prueba realmente PRODUCEN órdenes "
            "relativos distintos tras excluir al causante -- dos órdenes de registro distintos "
            "pueden coincidir por casualidad en el subconjunto que sobrevive a la exclusión y "
            "dar una prueba que pasa sin discriminar nada (exactamente lo que ocurrió aquí en el "
            "primer intento: la mutación de orden pasó la prueba Q hasta rediseñar los dos "
            "órdenes de registro para que también divirgieran en el subconjunto restante)",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_recovery_wait.py (18 discriminantes A-T del encargo de Fase 8A): orden "
            "general correcto para los tres causantes posibles (deepseek/claude/codex fallan), "
            "el causal nunca es seleccionable ni siquiera si 'vuelve sano', BUSY se salta al "
            "siguiente candidato sin tratarlo como fallo, unavailable/capability-gap igual, cero "
            "candidatos -> WAITING_RECOVERY sin Human Gate/Attempt/ciclo nuevo, WAITING_PROVIDER "
            "de Fase 7 nunca entra a recovery (confirmado con un ProviderLease real), "
            "recovery_policy y takeover_policy demostrablemente independientes, restart conserva "
            "la espera y reevalúa la disponibilidad real (no la decisión vieja), reevaluación "
            "idéntica es idempotente, orden de registro no afecta la decisión, motivo de "
            "exclusión estructurado y explicable",
            "regresión (provider layer, ~300 pruebas): test_provider_wait, test_dependency_wait, "
            "test_resource_waiting, test_scheduler_phase1_contracts, test_provider_failover, "
            "test_quality_takeover_policy, test_multi_provider, test_subscription_transports, "
            "test_multitask_phase6_minipilot, test_multitask_phase7_minipilot, test_cold_imports "
            "-- todas en verde, ninguna tocada en su propio comportamiento",
            "12/12 mutaciones confirmadas en vivo (revertidas tras cada corrida; src/ idéntico "
            "al commit base tras la verificación, confirmado con git diff vacío): dos quedaron "
            "CAUGHT por una segunda defensa independiente en vez del chequeo desactivado "
            "-- BUSY-de-Fase-7-en-recovery, por el guardián propio de _reevaluation_key; "
            "self-dependency-style (causal 'sano' reconsiderado) ya cubierto por la exclusión "
            "estructural -- documentado explícitamente, no ocultado",
        ),
        tags=(
            "type:recovery-policy-phase8a",
            "trigger:third-independent-causal-dimension-for-provider-substitution",
            "component:providers/recovery_policy+providers/router+scheduling/recovery_waits",
            "provenance:discriminant-test-suite+live-mutation-check",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
