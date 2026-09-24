"""Fase 8B: implementación real de ``OperationalRecoveryHook`` -- decidir, adquirir, revalidar,
invocar, encadenar con exclusión creciente, o persistir ``WAITING_RECOVERY``.

``RecoveryExecutor`` es lo único que ``DevelopmentCycle`` invoca (vía el protocolo, nunca importa
este módulo): conecta la decisión pura de Fase 8A (``RecoveryWaitCoordinator``) con la invocación
real de un candidato (``ProviderRouter.execute_recovery``), sin tocar Quality Takeover
(``execute_alternative``/``TakeoverPolicy``) ni el failover operativo del propio ``execute()``.

La cadena vive dentro de UNA sola llamada: no persiste ``chain_excluded`` entre invocaciones de
``__call__`` distintas. Lo que sí sobrevive a un reinicio es exactamente lo que Fase 8A ya
persiste (``RecoveryWaitReason``, con ``failed_provider``/``also_excluded`` completos) cuando la
cadena se agota en ``WAITING_RECOVERY`` -- eso es lo único que ``DevelopmentCycle`` (síncrono, sin
puntos de control a la granularidad de una sola invocación de proveedor) puede, honestamente,
garantizar que sobreviva.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from punto.api.console_state import TaskRecord
from punto.providers.contract import ProviderRequest, ProviderResult, ProviderRole
from punto.providers.effective import CAPABILITY_VISION
from punto.providers.failover import failover_cause_of
from punto.providers.router import ProviderRouter
from punto.scheduling.leases import FencingToken, LeaseHolder, LeaseKind, LeaseLedger, LeaseOutcome
from punto.scheduling.recovery_waits import RecoveryDecisionKind, RecoveryWaitCoordinator

#: Tope duro de candidatos intentados dentro de UNA cadena de recovery: protección contra loops
#: (§13) independiente de que la exclusión estructural ya lo impida por construcción.
DEFAULT_MAX_RECOVERY_ATTEMPTS: Final[int] = 3


@dataclass(slots=True)
class RecoveryExecutor:
    """Cadena de recovery operacional real para UNA Task/ciclo: mismo ``task_id``, mismo ciclo.

    Implementa el protocolo ``OperationalRecoveryHook`` de
    ``punto.orchestrator.dev_cycle`` por duck typing (incluye el kwarg ``role`` de la llamada,
    no un ``self.role`` fijo, para calzar exactamente con lo que ``DevelopmentCycle._invoke``
    pasa en cada invocación).
    """

    router: ProviderRouter
    ledger: LeaseLedger
    coordinator: RecoveryWaitCoordinator
    task: TaskRecord
    holder: LeaseHolder
    #: TaskWriterLease YA vigente del ciclo en curso (Fase 2A/7) -- este ejecutor NUNCA lo
    #: readquiere, solo lo usa como fencing del ProviderLease propio que sí adquiere.
    task_token: FencingToken
    max_attempts: int = DEFAULT_MAX_RECOVERY_ATTEMPTS
    ttl_seconds: int = 60
    #: Se invoca con el ``TaskRecord`` actualizado cada vez que cambia (entra/sale/actualiza
    #: WAITING_RECOVERY): el llamante decide si y cómo persistirlo. ``None`` no persiste nada --
    #: el ejecutor sigue funcionando en memoria dentro de esta única llamada.
    on_state_change: Callable[[TaskRecord], None] | None = None
    _attempts_used: int = field(default=0, init=False, repr=False)

    def __call__(
        self,
        *,
        role: ProviderRole,
        request: ProviderRequest,
        json_schema: Mapping[str, Any],
        max_output_tokens: int | None,
        failed: ProviderResult,
    ) -> ProviderResult:
        """Punto de entrada real: mismo contrato que ``OperationalRecoveryHook.__call__``."""
        original_failed_provider = failed.provider or "unknown"
        # Sembrado con el causante original (no solo con los candidatos ya intentados): si no,
        # la decisión del coordinador (que excluye por ``failed_provider`` -- el fallo MÁS
        # RECIENTE -- más ``also_exclude``) y la exclusión real de invocación podían divergir a
        # partir del segundo salto, dejando reseleccionable al causante original en la decisión
        # aunque la invocación ya lo excluyera -- un candidato distinto al decidido podía acabar
        # siendo el realmente invocado. Un único conjunto, sembrado aquí, alimenta ambos pasos.
        chain_excluded: set[str] = {original_failed_provider}
        current_failed = failed
        attempts = 0
        # La decisión (evaluate_recovery) y la invocación real (execute_recovery, más abajo)
        # tienen que juzgar la MISMA exigencia de capacidad -- si no, el candidato que decide
        # el coordinador (con VISION desconocida) y el que de verdad juzga elegible el router
        # (con los adjuntos reales de ``request``) podrían divergir, igual que ya podía divergir
        # la exclusión antes del fix de ``chain_excluded`` de arriba.
        required_capabilities = (CAPABILITY_VISION,) if request.has_attachments else ()
        while attempts < self.max_attempts:
            attempts += 1
            self._attempts_used = attempts
            failure_kind = (
                current_failed.error_kind.value if current_failed.error_kind else "UNKNOWN"
            )
            immediate_cause = current_failed.provider or original_failed_provider
            # RecoveryWaitReason exige failed_provider y also_excluded disjuntos (evidencia sin
            # redundancia): failed_provider ya cubre al causante inmediato por su cuenta, así que
            # aquí se resta -- la cobertura real hacia execute_recovery, más abajo, sigue siendo
            # el conjunto COMPLETO de chain_excluded, sin este descuento.
            evaluation = self.coordinator.evaluate_recovery(
                self.task,
                required_capabilities=required_capabilities,
                role=role,
                failed_provider=immediate_cause,
                failure_kind=failure_kind,
                also_exclude=frozenset(chain_excluded - {immediate_cause}),
            )
            self.task = evaluation.task
            if self.on_state_change is not None:
                self.on_state_change(self.task)
            if (
                evaluation.decision is None
                or evaluation.decision.decision is not RecoveryDecisionKind.RECOVER_TO
            ):
                # WAITING_RECOVERY (ya persistido por el coordinador) o RECOVERY_UNSUPPORTED/
                # TERMINAL/UNCHANGED: en cualquier caso, no hay a quién invocar ahora mismo.
                return current_failed
            selected = evaluation.decision.selected_candidate
            if selected is None:  # pragma: no cover - invariante de RECOVER_TO
                return current_failed

            lease_result = self.ledger.acquire(
                kind=LeaseKind.PROVIDER,
                key=f"{selected}:0",
                provider_id=selected,
                slot=0,
                holder=self.holder,
                ttl_seconds=self.ttl_seconds,
                task_id=self.task.task_id,
                task_epoch=self.task_token.epoch,
                task_token=self.task_token,
            )
            if lease_result.outcome is not LeaseOutcome.PASS or lease_result.token is None:
                # §11: la realidad cambió entre decidir y adquirir (alguien más lo tomó, o el
                # TaskWriterLease perdió autoridad) -- no se invoca ciegamente. Se excluye este
                # candidato de la MISMA cadena y se reevalúa desde el principio.
                chain_excluded.add(selected)
                continue

            # execute_recovery (más abajo) NO conoce ProviderLease/BUSY -- vuelve a recorrer
            # ``declared`` por su cuenta con solo un ``exclude`` plano, así que un candidato que
            # la DECISIÓN saltó por BUSY (sin pasar por ``chain_excluded``, que solo acumula lo
            # que este ejecutor selecciona-e-intenta) seguía disponible para que la invocación lo
            # recorriera y lo invocara en vez de ``selected``. Excluir aquí a TODO lo que la
            # decisión consideró (``ordered_candidates``) salvo ``selected`` cierra ese hueco:
            # quien se invoca es siempre, exactamente, a quien se decidió -- o nadie.
            exclude_for_invoke = frozenset(
                (chain_excluded | set(evaluation.decision.ordered_candidates)) - {selected}
            )
            try:
                result = self.router.execute_recovery(
                    role,
                    request,
                    exclude=exclude_for_invoke,
                    json_schema=json_schema,
                    max_output_tokens=max_output_tokens,
                )
            finally:
                # El ProviderLease de recovery nunca se conserva más allá de esta invocación: no
                # es autoridad permanente, es un turno propio para este candidato.
                self.ledger.release(lease_result.token)

            if result.ok:
                return result
            if failover_cause_of(result.error_kind) is not None:
                # §12: otro fallo operacional del candidato de recovery -- registrar causal y
                # continuar la MISMA cadena con el siguiente candidato elegible.
                chain_excluded.add(selected)
                current_failed = result
                continue
            # §12: fallo NO operacional (respuesta inválida, negativa...) -- no es asunto de
            # recovery operacional. Se devuelve tal cual: DevelopmentCycle sigue su camino normal
            # (repair rounds, Quality Takeover existente si corresponde), sin que este ejecutor
            # decida nada más por su cuenta.
            return result
        # §13: presupuesto de intentos agotado sin resolver -- el último fallo se devuelve tal
        # cual (el estado WAITING_RECOVERY, si lo hubo, ya quedó persistido en alguna iteración).
        return current_failed


__all__ = ["DEFAULT_MAX_RECOVERY_ATTEMPTS", "RecoveryExecutor"]
