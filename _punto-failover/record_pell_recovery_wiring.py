"""Registra en PELL el aprendizaje de Fase 8B (VERIFIED): decisión pura e invocación real son
dos recorridos INDEPENDIENTES de la misma lista declarada y tienen que forzarse a coincidir
por construcción -- enumerar cada motivo de exclusión por separado no basta. Sin abrir
auditoría aparte.
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
            "Fase 8B conecta la DECISIÓN pura de Fase 8A (RecoveryWaitCoordinator."
            "evaluate_recovery, que elige un ``selected_candidate`` leyendo BUSY del "
            "LeaseLedger) con la INVOCACIÓN real (ProviderRouter.execute_recovery, que "
            "recorre la MISMA lista declarada por su cuenta con solo un ``exclude`` plano, "
            "sin conocer leases). Son dos recorridos independientes de la misma lista: "
            "cualquier motivo de exclusión que solo uno de los dos conozca los hace divergir "
            "-- se decide invocar a X y en la práctica se invoca a Y. Aparecieron tres "
            "manifestaciones del MISMO hueco raíz, cada una encontrada por una capa de "
            "verificación distinta: (1) al segundo salto de una cadena, el conjunto que veía "
            "la decisión y el que veía la invocación no coincidían (encontrado razonando "
            "sobre el código antes de escribir ninguna prueba); (2) la decisión nunca recibía "
            "la exigencia real de VISION de la petición, así que podía elegir a un candidato "
            "sin esa capacidad que la invocación luego rechazaría por su cuenta (encontrado "
            "extendiendo el mismo razonamiento a un segundo caso); (3) un candidato que la "
            "decisión saltó por BUSY -- sabido solo dentro de evaluate_recovery, nunca en el "
            "``chain_excluded`` que alimenta a la invocación -- seguía disponible para que "
            "execute_recovery lo recorriera e invocara en vez del realmente seleccionado "
            "(el único de los tres que sobrevivió a 17/17 pruebas unitarias en verde y solo "
            "lo atrapó el mini-piloto de extremo a extremo, con un ProviderLease de otra Task "
            "ocupando al candidato preferido de verdad)"
        ),
        context=(
            "los 17 discriminantes unitarios (router + ledger + coordinador, sin "
            "DevelopmentCycle) cubrían fielmente CADA pieza por separado -- selección, "
            "exclusión del causante, revalidación por carrera, epoch obsoleto, agotamiento -- "
            "pero ninguno ejercitaba el caso exacto 'la decisión salta a alguien por BUSY en "
            "su propio chequeo interno, sin que ese salto se refleje en la variable que la "
            "cadena del ejecutor acumula'; hizo falta el mini-piloto (DevelopmentCycle real, "
            "tres escenarios de punta a punta) para que ese hueco produjera un fallo "
            "observable: Codex, ocupado a propósito por otra Task, terminó invocado igual"
        ),
        attempts=(
            "sembrar chain_excluded con el proveedor causante original y usarlo tal cual como "
            "exclude tanto en evaluate_recovery como en execute_recovery -- cerró la "
            "divergencia de MULTI-SALTO (el causante ya no podía reaparecer) pero no la de "
            "BUSY, porque un salto por BUSY nunca pasa por chain_excluded (esa variable solo "
            "acumula candidatos que el ejecutor SELECCIONÓ e intentó, no los que la decisión "
            "descartó sin llegar a intentarlos)",
        ),
        failure_reason=(
            "chain_excluded describe 'a quién ya intentó ESTE ejecutor', no 'a quién la "
            "decisión de ESTA vuelta consideró y no eligió' -- un motivo de exclusión que "
            "nace y muere dentro de evaluate_recovery (BUSY, o cualquier otro que una capa "
            "inferior conozca y el ejecutor no) queda estructuralmente invisible para el "
            "exclude que ve execute_recovery, sin importar qué tan completo sea "
            "chain_excluded"
        ),
        solution=(
            "en vez de reconstruir el exclude de invocación enumerando cada motivo posible de "
            "exclusión (causante original, fallidos de esta cadena, BUSY, capacidad...), "
            "excluir POSITIVAMENTE todo lo que la decisión consideró salvo a quien "
            "seleccionó: ``exclude_for_invoke = (chain_excluded | set(decision."
            "ordered_candidates)) - {selected}``. ``ordered_candidates`` es exactamente la "
            "lista que evaluate_recovery ya juzgó (declarada menos causante/also_exclude), "
            "así que restarle todo salvo ``selected`` fuerza a execute_recovery a aterrizar "
            "en el mismo candidato que decidió el coordinador -- o a fallar cerrado si ese "
            "candidato deja de ser válido justo al re-verificarlo, un modo de fallo honesto y "
            "acotado, no una sustitución silenciosa por alguien distinto. Cierre por "
            "construcción: cualquier motivo de exclusión FUTURO que una capa inferior conozca "
            "y el ejecutor no queda cubierto automáticamente, sin enumerarlo"
        ),
        procedure=(
            "cuando una decisión pura y una invocación real recorren la MISMA lista "
            "declarada por separado (cada una con su propio criterio de qué excluir), no "
            "confiar en que sus dos exclusiones coincidan por construcción -- expresar la "
            "invocación como 'todo lo que la decisión vio, menos exactamente a quien "
            "seleccionó', nunca como una reconstrucción paralela de 'todo lo que debería "
            "estar excluido'",
            "un conjunto que acumula 'a quién ya intentó este ejecutor' (chain_excluded) y un "
            "conjunto que responde 'a quién consideró la decisión de ESTA vuelta' "
            "(ordered_candidates) son cosas DISTINTAS -- una exclusión que nace dentro de la "
            "decisión (BUSY de un lease que el router de invocación ni siquiera conoce) solo "
            "aparece en el segundo, nunca en el primero, así que un ejecutor de cadena "
            "necesita ambos, no solo el que acumula intentos propios",
            "una suite unitaria exhaustiva por PIEZA (cada discriminante aislado, 17/17 en "
            "verde) no prueba que las piezas COMBINADAS produzcan la invocación decidida -- "
            "un mini-piloto de extremo a extremo con el motor real (DevelopmentCycle, no un "
            "sustituto) sigue encontrando huecos de integración que ninguna prueba unitaria, "
            "por bien diseñada que esté, puede ver desde dentro de una sola pieza",
            "el ``Fake`` de pruebas de este repositorio (test_provider_failover.Fake) no es "
            "multimodal de verdad: cualquier intento de invocarlo con adjuntos reales choca "
            "con el propio chequeo de transporte del router, sin importar lo que diga un "
            "evaluador de capacidad a medida -- una prueba de exigencia de VISION puede "
            "demostrar 'el candidato sin capacidad nunca se invoca' pero no 'el candidato con "
            "capacidad completa la llamada', y no hace falta perseguir esa segunda cosa con "
            "este doble",
        ),
        result=ExperienceResult.SUCCESS,
        verification=(
            "tests/test_recovery_wiring.py (20 discriminantes A-V del encargo de Fase 8B, "
            "capa router+ledger+coordinador+RecoveryExecutor, sin DevelopmentCycle): 17/17 en "
            "verde -- selección real por causante (deepseek/claude/codex), causante jamás "
            "reinvocado incluso tras varios saltos, misma Task y mismo trabajo entregado, "
            "revalidación real por carrera al adquirir, WAITING_RECOVERY sin invocar a nadie, "
            "WAITING_PROVIDER de Fase 7 invisible para recovery, takeover de calidad jamás "
            "dispara recovery operacional, el ProviderLease pertenece a quien de verdad se "
            "invoca y se libera después, epoch obsoleto no invoca a nadie, la cadena no "
            "vuelve a un candidato intermedio ya fallido, agotamiento total termina en "
            "WAITING_RECOVERY, un ejecutor reconstruido tras reinicio simulado invoca limpio, "
            "el ProviderLease se libera incluso si la invocación revienta, un candidato sin "
            "la capacidad exigida jamás se invoca, el catálogo del router no se corrompe, sin "
            "referencia a workspace/checkpoint",
            "tests/test_multitask_phase8b_minipilot.py (3 escenarios de punta a punta, "
            "DevelopmentCycle real + repositorio Git real + RecoveryExecutor real): DeepSeek "
            "falla -> Codex recupera en la misma Task (commit real aplicado, Codex sirve "
            "plan+recovery, un único evento de trazabilidad dev_provider_recovery, sin Human "
            "Gate); DeepSeek falla -> Codex ocupado por un ProviderLease real de otra Task -> "
            "Claude recupera (Codex nunca se invoca para recovery, solo para su plan previo); "
            "DeepSeek, Codex y Claude fallan los tres -> WAITING_RECOVERY con also_excluded "
            "correcto, un único DevelopmentResult por llamada (sin Attempt extra, concepto "
            "que Fase 8B no toca), sin Human Gate",
            "regresión causal (recovery + failover + provider-wait + cold-imports, ~145 "
            "pruebas del núcleo DevelopmentCycle/takeover/failover más ~80 de "
            "recovery_wait/provider_wait/provider_failover): en verde, ninguna tocada en su "
            "propio comportamiento -- confirma que el hook OperationalRecoveryHook (``None`` "
            "por defecto) y el nuevo evento de auditoría dev_provider_recovery no alteran "
            "ningún camino existente cuando Fase 8B está inactiva",
        ),
        tags=(
            "type:recovery-wiring-phase8b",
            "trigger:decision-and-invocation-are-two-independent-walks-of-the-same-list",
            "component:scheduling/recovery_wiring+providers/router+orchestrator/dev_cycle",
            "provenance:discriminant-test-suite+end-to-end-minipilot",
        ),
        status=ExperienceStatus.VERIFIED,
    )
    print(f"[V] {a.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
