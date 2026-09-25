"""Registra el aprendizaje reusable y verificado de F14-G1 (handoff del ProviderLease)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-recovery-lease-handoff.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "bajo el TwoTaskScheduler la recovery operacional nunca podía completar: la Task "
            "sostenía su ProviderLease primario durante el ciclo, el ledger admite uno por Task, "
            "el candidato salía BUSY contra la propia Task, se persistía en also_excluded y "
            "WAITING_RECOVERY quedaba sin salida aunque el candidato estuviera libre"
        ),
        context=(
            "ProviderLease único por Task (F7) + RecoveryExecutor que adquiere el lease del "
            "candidato a mitad del ciclo (F8B) + renovación y fence de la ejecución fijados al "
            "token inicial (F11R)"
        ),
        attempts=(),
        failure_reason=(
            "dos invariantes correctos compuestos sin contrato de transferencia: la authority se "
            "intentaba ACUMULAR (segundo lease) en vez de TRANSFERIR; y un BUSY transitorio se "
            "trataba como fallo permanente del candidato. Al soltar el primario, si el candidato "
            "no se ocupaba, la ejecución quedaba sin provider y el fence convertía un "
            "WAITING_RECOVERY legítimo en FENCED"
        ),
        solution=(
            "ProviderAuthority por ejecución: transfer suelta el lease vigente (token fenced) y "
            "adquiere el del candidato bajo la misma exclusión que la renovación; si no puede, "
            "recupera un lease nuevo del anterior. Renovación, fence y release siguen al token "
            "vigente; el cambio de provider se persiste en la Task. BUSY es transient_exclude "
            "(nunca also_excluded); RECOVER_TO desde WAITING_RECOVERY transfiere el provider de "
            "la Task al candidato para que el causante no se redespache"
        ),
        procedure=(
            "cuando dos invariantes de authority chocan, buscar un contrato de TRANSFERENCIA "
            "antes que relajar uno de ellos (nunca dos leases a la vez)",
            "la renovación y el fence deben leer el token VIGENTE, no el de arranque, si la "
            "authority puede transferirse",
            "separar exclusión permanente (causante, candidato que falló de verdad) de "
            "inelegibilidad transitoria (BUSY): solo la primera se persiste",
            "discriminar la protección del causante en un SEGUNDO salto: en el primero el propio "
            "lease de la Task lo enmascara",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_multitask_phase14_g1_handoff.py: A/E/F/J, B, B2, C, D (2 saltos), I PASS",
            "tests/test_multitask_phase14_g1_mutations.py: 9/9 CAUGHT (8 exigidas + 4b)",
            "piloto F14 13/13 PASS sin xfail; fault injections F14 15/15",
            "cono causal G1 528 PASS; ruff; mypy strict",
        ),
        tags=(
            "type:recovery-provider-lease-handoff-phase14-g1",
            "trigger:recovery-candidate-busy-against-own-task",
            "component:scheduling/task_scheduler.ProviderAuthority+LeaseRenewer",
            "component:scheduling/recovery_wiring+recovery_waits",
            "provenance:e2e-pilot+source-mutations",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
