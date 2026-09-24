"""Registra el aprendizaje reusable y verificado de Fase 11R (renovación de leases)."""

from __future__ import annotations

from pathlib import Path

MEMORY = Path(__file__).resolve().parent / "pell-lease-renewal.jsonl"


def main() -> int:
    from punto.memory.experience import ExperienceResult, ExperienceStatus
    from punto.memory.store import ExperienceStore

    store = ExperienceStore(MEMORY)
    record = store.record(
        problem=(
            "una ejecución RUNNING más larga que el TTL perdía TaskWriterLease/ProviderLease a "
            "mitad del ciclo; renovar sin cuidado podía resucitar authority vieja o seguir "
            "produciendo efectos tras una renovación fallida"
        ),
        context=(
            "ledger append-only con CAS por os.link, LeaseLedger.renew ya exigía holder+epoch "
            "vigentes; dos ejecuciones concurrentes leen su fence mientras su renewer escribe"
        ),
        attempts=(),
        failure_reason=(
            "un flag local de revocación no basta: los fences construidos fuera del renewer solo "
            "consultan el ledger; y un lector que lista el directorio veía el temporal de un "
            "append concurrente ya publicado como 'temporal ambiguo' (TOCTOU) y lo declaraba "
            "corrupción"
        ),
        solution=(
            "un renewer acotado por ejecución que renueva writer y luego provider solo cuando la "
            "vida restante <= ttl/2 y, ante cualquier fallo, revoca marcando la ejecución Y "
            "soltando sus propios tokens para que todo fence del ledger falle antes del "
            "siguiente efecto; el lector del ledger ignora un temporal desaparecido entre "
            "listado y lstat pero sigue rechazando uno que no es fichero regular"
        ),
        procedure=(
            "renovar vía LeaseLedger.renew con el token original: misma holder/task/epoch, nunca "
            "reacquire",
            "revocar = flag local + release de los tokens propios (un token stale da "
            "STALE_RELEASE y no toca al holder nuevo)",
            "detener y esperar el renewer en finally, incluida la muerte simulada; shutdown "
            "abandonado detiene renewers; las huérfanas tras restart nunca se renuevan",
            "descartar como FENCED cualquier resultado devuelto después de una revocación",
            "en ledgers de ficheros con temporales, tolerar FileNotFoundError entre iterdir y "
            "lstat de un temporal; tratar solo tipos no regulares como corrupción",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_lease_renewal.py: 14 PASS, estable en 8 corridas",
            "mini-piloto real: dos DevelopmentCycles de 8 s con TTL=5 s completan sin perder "
            "authority",
            "11/11 mutaciones de renovación CAUGHT; 14/14 de Fase 11 siguen CAUGHT",
            "regresión causal leases/scheduler/waits/workspaces/takeover + cold imports 384 PASS; "
            "ruff/mypy strict PASS",
        ),
        tags=(
            "type:lease-renewal-phase11r",
            "trigger:running-execution-longer-than-lease-ttl",
            "component:scheduling/task_scheduler.LeaseRenewer+scheduling/leases._read",
            "provenance:discriminants+wallclock-minipilot+live-mutations+causal-regression",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {record.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
