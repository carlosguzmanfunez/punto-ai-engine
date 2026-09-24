"""Registra el aprendizaje VERIFIED de Multi-Task Fase 7."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-active-evidence-recovery.jsonl"


def main() -> int:
    """Persiste solo las reglas reutilizables demostradas por tests y mutaciones."""
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    experience = store.record(
        problem=(
            "ProviderLease ya imponía exclusión durable, epoch y fencing, pero BUSY solo tenía "
            "un adaptador declarativo genérico: no existía una transición operacional durable a "
            "WAITING_PROVIDER, arbitraje de múltiples waiters ni reconciliación de restart"
        ),
        context=(
            "Multi-Task Fase 7 conecta el ledger existente después de DEPENDENCY y RESOURCE, sin "
            "invocar proveedores ni tocar health, attempts, repair budgets, failover o gates"
        ),
        attempts=(),
        failure_reason=(
            "el adaptador descartaba task_id, tiempos, fingerprint y holder; además las "
            "transiciones de dependencia/recurso no preservaban ProviderReference y trataban una "
            "Task en WAITING_PROVIDER como blocker de recursos pese a no conservar authority"
        ),
        solution=(
            "ProviderWaitReason conserva provider/slot/task/timestamps/fingerprint y solo UUIDs "
            "seguros del blocker; ProviderWaitCoordinator evalúa DEPENDENCY→RESOURCE→PROVIDER, "
            "adquiere TaskWriter antes de ProviderLease y, ante BUSY, libera TaskWriter antes de "
            "persistir WAITING_PROVIDER. Release/expiry permiten readquirir tokens propios; el "
            "reconcile ordena waiting_since→task_id y el CAS del ledger deja un solo winner"
        ),
        procedure=(
            "BUSY de capacidad compartida debe modelarse como scheduling, no traducirse a estado "
            "de salud/error del proveedor ni reutilizar failover; la capa de espera no debe tener "
            "acceso al objeto ProviderHealth",
            "un waiter no debe conservar authority mientras espera: adquirir en orden padre→hijo "
            "y liberar el padre si el hijo devuelve BUSY evita hold-and-wait",
            "restart recupera evidencia de espera, nunca tokens: una identidad nueva vuelve a "
            "adquirir contra el ledger y respeta el lease ajeno hasta release o TTL+grace",
            "cuando un coordinador compone otro, UNCHANGED debe proyectarse según el estado "
            "durable resultante (WAITING_DEPENDENCY/WAITING_RESOURCE), nunca degradarse a error "
            "genérico",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_provider_wait.py: discriminantes A-X, schema/adaptador, no health access",
            "tests/test_multitask_phase7_minipilot.py: fake local; release/reacquire y dos waiters",
            "14/14 mutaciones CAUGHT contra el código final y restauradas tras cada corrida",
            "regresión focalizada: leases, precedencia, providers, restart/fencing, console/gates/"
            "audit/cold imports; ruff y mypy --strict limpios",
        ),
        tags=(
            "type:provider-lease-wait-phase7",
            "trigger:provider-lease-busy-after-dependency-and-resource-ready",
            "component:scheduling/provider_waits+scheduling/leases+schemas/scheduling",
            "provenance:discriminant-tests+mini-pilot+14-live-mutations",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {experience.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
