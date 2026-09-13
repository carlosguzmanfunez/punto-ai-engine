"""Libro de intenciones de efecto: no repetir un efecto secundario tras una caída (V60-10).

El problema es concreto: un rol produce un efecto secundario —escribe fuera del workspace,
publica, despliega, lanza una orden— y el proceso muere **después** de que el efecto ocurrió
pero **antes** de que el kernel guardara su checkpoint. Al reanudar, el paso se repite y el
efecto se duplica. Cuando eso pasa ya no hay comprobación local que demuestre lo contrario: el
daño está hecho.

La respuesta de este módulo no es adivinar, es **apuntar la intención antes de actuar**. Cada
efecto tiene una clave de idempotencia estable (:func:`effect_key`) y el run guarda un
:class:`~punto.schemas.workflow.EffectRecord` con el estado de esa intención. El registro no vive
en ningún almacén propio: viaja dentro del :class:`~punto.schemas.workflow.WorkflowRun`, así que
se persiste con el checkpoint y se recupera con él. **La durabilidad la da el checkpoint**; aquí
solo se decide.

Qué significa cada estado para una reanudación:

- ``APPLIED``: el efecto ocurrió. Repetirlo es duplicarlo, así que no se permite.
- ``IN_FLIGHT`` / ``UNKNOWN``: se pidió, pero no consta si ocurrió. Con un efecto irreversible,
  repetir a ciegas puede ser catastrófico; y con uno reversible también se rechaza, porque el
  kernel no repite nada sin una decisión. En ambos casos el código es
  ``WORKFLOW_EFFECT_RECONCILIATION_REQUIRED``, y solo :meth:`EffectLedger.reconcile` —una
  decisión humana o una comprobación externa— desbloquea.
- ``FAILED``: consta que no ocurrió, y aun así no se reintenta solo: repetir un efecto es una
  decisión explícita, no un efecto colateral de una reanudación.

Lo que este módulo **no** hace, a propósito:

- **Ningún efecto real.** No se ejecuta nada: ni ``os``, ni ``subprocess``, ni red. Ejecutar el
  efecto es del kernel, y ocurre entre :meth:`EffectLedger.begin_intent` y
  :meth:`EffectLedger.resolve`.
- **Ningún estado propio.** El libro es el propio ``run``: cada operación devuelve un run nuevo y
  nunca muta el que recibe.
- **Ninguna excepción para un veredicto de política.** ``begin_intent`` devuelve
  :class:`EffectDecision`; el kernel decide si ese «no» es un fallo, una pausa o una petición
  humana.
- **Ningún reloj en las consultas.** ``pending`` y ``unresolved_irreversible`` solo leen: son
  puras y deterministas.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from punto.common import utc_now
from punto.schemas.workflow import (
    MAX_EFFECT_RECORDS,
    MAX_WORKFLOW_SUMMARY_CHARS,
    EffectRecord,
    EffectStatus,
    RoleName,
    WorkflowFailureCode,
    WorkflowRun,
)

#: Estados que significan «no sabemos si ocurrió». Son los que bloquean una reanudación.
_UNRESOLVED_STATUSES: Final[frozenset[EffectStatus]] = frozenset(
    {EffectStatus.IN_FLIGHT, EffectStatus.UNKNOWN}
)
#: Estados que una resolución puede escribir. No se «resuelve» hacia ``IN_FLIGHT``/``UNKNOWN``:
#: volver a un estado incierto no es resolver nada, es empezar de nuevo, y eso es ``begin_intent``.
_RESOLUTION_STATUSES: Final[frozenset[EffectStatus]] = frozenset(
    {EffectStatus.APPLIED, EffectStatus.FAILED}
)
#: Máximo de ``EffectRecord.idempotency_key`` en el contrato. Una clave más larga haría fallar la
#: construcción del registro y la intención no se podría apuntar justo cuando más falta hace.
MAX_EFFECT_KEY_CHARS: Final[int] = 120
#: Caracteres de la acción que se conservan legibles en la clave; el resto lo cubre el digest.
_KEY_ACTION_CHARS: Final[int] = 24
#: Caracteres del digest que cierran la clave: identifican la acción completa, no un prefijo.
_KEY_DIGEST_CHARS: Final[int] = 16


@dataclass(frozen=True, slots=True)
class EffectDecision:
    """Veredicto sobre una intención de efecto.

    ``allowed`` es la respuesta; ``code`` y ``detail`` explican el «no» con el mismo vocabulario
    que el resto del kernel, para que el motor construya su ``WorkflowFailure`` sin traducir
    nada. Un «no» **nunca** deja el run a medias: cuando no se permite, el run devuelto es el
    mismo que entró.
    """

    allowed: bool
    code: WorkflowFailureCode | None = None
    detail: str = ""


class EffectLedger:
    """Registra intenciones de efecto y decide si se pueden ejecutar.

    La clase no guarda nada: ``__slots__`` vacío y ni un atributo. El estado durable es
    ``run.effects``, que viaja en el checkpoint; un libro con memoria propia sería un segundo
    estado que podría discrepar del run y, además, se perdería en la caída que este módulo
    existe para sobrevivir. Por eso todas las operaciones son funciones del run: recibirlo y
    devolver otro.

    El orden de comprobación de :meth:`begin_intent` es fijo y deliberado —clave ya registrada,
    efecto sin resolver, capacidad— porque un veredicto que cambia de motivo según el orden de
    evaluación no sirve para diagnosticar. La seguridad va antes que el recurso: si hay un efecto
    sin resolver, ese es el motivo, aunque el libro esté además lleno.
    """

    __slots__ = ()

    def begin_intent(
        self,
        run: WorkflowRun,
        *,
        key: str,
        action: str,
        role: RoleName,
        step_index: int,
        reversible: bool,
    ) -> tuple[WorkflowRun, EffectDecision]:
        """Apunta la intención **antes** de ejecutar el efecto y dice si se puede ejecutar.

        Un ``allowed=True`` significa exactamente una cosa: que la clave no estaba registrada, que
        no hay ningún efecto sin resolver y que queda sitio en el libro. El registro queda
        ``IN_FLIGHT`` y el kernel debe persistir el checkpoint **antes** de producir el efecto:
        esa es la mitad de la garantía que aporta el llamante.

        Si la clave ya existe, no se añade un segundo registro: se devuelve el veredicto del
        primero. Ese es el dedupe real —por clave de idempotencia, no por parecido de la acción— y
        es lo que impide que una reanudación duplique un efecto ya aplicado.

        Args:
            run: Ejecución en curso. No se muta.
            key: Clave de idempotencia del efecto; normalmente de :func:`effect_key`.
            action: Acción concreta que se va a ejecutar.
            role: Rol que la ejecuta.
            step_index: Paso del workflow al que pertenece.
            reversible: ``False`` si repetir el efecto no tendría vuelta atrás.

        Returns:
            El run con la intención apuntada y ``EffectDecision(allowed=True)``; si no se permite,
            el run **sin tocar** y el veredicto con su código y su detalle.
        """
        index = _index_of(run, key)
        if index is not None:
            return run, _existing_decision(run.effects[index])

        pending = _unresolved(run)
        if pending:
            return run, _pending_decision(pending[0], reversible=reversible)

        # ``model_copy`` **no** valida, así que el ``max_length`` de ``effects`` no protege aquí:
        # la cota del contrato se comprueba explícitamente, igual que el presupuesto se comprueba
        # en su módulo. Sin esto, el run crecería por encima de lo que su propio esquema admite y
        # el checkpoint resultante sería imposible de volver a validar.
        if len(run.effects) >= MAX_EFFECT_RECORDS:
            return run, EffectDecision(
                allowed=False,
                code=WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED,
                detail=(
                    f"el run ya tiene {len(run.effects)} registros de efecto y el máximo es "
                    f"{MAX_EFFECT_RECORDS}: no se puede apuntar la intención {key!r}"
                ),
            )

        record = EffectRecord(
            idempotency_key=key,
            action=action,
            role=role,
            step_index=step_index,
            status=EffectStatus.IN_FLIGHT,
            reversible=reversible,
        )
        return run.model_copy(update={"effects": (*run.effects, record)}), EffectDecision(
            allowed=True
        )

    def resolve(
        self, run: WorkflowRun, *, key: str, status: EffectStatus, detail: str = ""
    ) -> WorkflowRun:
        """Marca el efecto como ``APPLIED`` o ``FAILED`` y sella ``resolved_at``.

        Es el final del camino normal: el kernel ejecutó el efecto y aquí registra lo que ocurrió.
        Solo ``APPLIED`` y ``FAILED`` son resoluciones; pedir otra cosa es un error de programa —
        no un veredicto de política— y por eso lanza :class:`ValueError` en vez de decidir en
        silencio algo que el llamante no quiso.

        Si la clave no está en el libro, el run se devuelve tal cual: resolver un efecto que nadie
        apuntó no puede inventar un registro, porque un registro inventado es indistinguible de un
        efecto real. Si ya estaba resuelto, tampoco se reescribe: la primera resolución es el
        hecho, y reescribirla borraría la traza que hace auditable el libro.

        Args:
            run: Ejecución en curso. No se muta.
            key: Clave de idempotencia del efecto.
            status: ``APPLIED`` o ``FAILED``.
            detail: Motivo acotado; se recorta al máximo del contrato.

        Returns:
            El run con el registro resuelto, o el mismo run si no había nada que resolver.

        Raises:
            ValueError: Si ``status`` no es ``APPLIED`` ni ``FAILED``.
        """
        return _apply_resolution(run, key=key, status=status, detail=detail)

    def mark_unknown(
        self, run: WorkflowRun, *, key: str, detail: str = ""
    ) -> WorkflowRun:
        """Declara que un efecto apuntado tiene resultado **incierto** (``UNKNOWN``).

        No es una resolución: es el reconocimiento de que no se sabe si el efecto ocurrió (el rol
        pudo aplicarlo y caerse después). El kernel lo usa para dejar el libro en un estado que no
        admite repetición automática: ``UNKNOWN`` sigue siendo «sin resolver», así que un reintento
        se rechaza igual que con ``IN_FLIGHT``, pero la traza dice explícitamente que la
        incertidumbre fue observada y no solo heredada (hallazgo V602-05).

        No inventa registros (una clave ausente es un no-op) y no reescribe una resolución previa.
        """
        index = _index_of(run, key)
        if index is None:
            return run
        record = run.effects[index]
        if record.status not in _UNRESOLVED_STATUSES:
            return run
        updated = record.model_copy(
            update={"status": EffectStatus.UNKNOWN, "detail": _clip(detail) or record.detail}
        )
        effects = list(run.effects)
        effects[index] = updated
        return run.model_copy(update={"effects": tuple(effects)})

    def pending(self, run: WorkflowRun) -> tuple[EffectRecord, ...]:
        """Efectos cuyo resultado se desconoce, en el orden en que se apuntaron.

        Consulta pura: no toca el run ni depende de nada externo. El orden es el de inserción
        —el de ``run.effects``— y no se ordena por clave, porque el primero que se apuntó es el
        primero que hay que reconciliar.
        """
        return _unresolved(run)

    def unresolved_irreversible(self, run: WorkflowRun) -> tuple[EffectRecord, ...]:
        """Efectos sin resolver que además son irreversibles.

        Es la consulta que decide si una reanudación puede seguir adelante sin intervención: un
        efecto irreversible en vuelo no admite ni repetición ni compensación, así que exige una
        reconciliación explícita. Pura y determinista, igual que :meth:`pending`.
        """
        return tuple(record for record in _unresolved(run) if not record.reversible)

    def reconcile(
        self, run: WorkflowRun, *, key: str, status: EffectStatus, detail: str = ""
    ) -> WorkflowRun:
        """Resuelve un efecto ``IN_FLIGHT``/``UNKNOWN`` tras una decisión explícita.

        Es la **única** forma de desbloquear un efecto en vuelo. La decisión que hay detrás no la
        toma este módulo: viene de una persona o de una comprobación externa que ya sabe si el
        efecto ocurrió (``APPLIED``) o no (``FAILED``). El ledger no la sustituye, la registra.

        Comparte con :meth:`resolve` la mecánica —sellar ``resolved_at``, no reescribir una
        resolución previa, no inventar registros— y se mantiene separado porque la intención es
        distinta: ``resolve`` cierra el camino normal, ``reconcile`` cierra una incertidumbre.

        Args:
            run: Ejecución en curso. No se muta.
            key: Clave de idempotencia del efecto.
            status: ``APPLIED`` o ``FAILED``, según lo que la decisión externa determine.
            detail: Motivo acotado de la reconciliación; se recorta al máximo del contrato.

        Returns:
            El run con el registro reconciliado, o el mismo run si no había nada que reconciliar.

        Raises:
            ValueError: Si ``status`` no es ``APPLIED`` ni ``FAILED``.
        """
        return _apply_resolution(run, key=key, status=status, detail=detail)


def effect_key(workflow_id: UUID, step_index: int, role: RoleName, action: str) -> str:
    """Clave de idempotencia estable de un efecto secundario.

    Depende solo de datos que ya están en el run —el workflow, el paso, el rol y la acción— así
    que la misma intención produce la misma clave en otro proceso, en otra máquina y tras una
    reanudación. Esa igualdad es lo que convierte el registro en un dedupe real: sin ella, una
    caída sería indistinguible de un efecto nuevo.

    La clave se mantiene legible —workflow, paso, rol y un trozo de la acción— y se cierra con un
    sha256 de la acción **completa**, para que dos acciones que empiecen igual no compartan clave.
    El resultado nunca supera ``MAX_EFFECT_KEY_CHARS``, el máximo del contrato: una clave más
    larga haría fallar la construcción del ``EffectRecord`` justo cuando la intención hay que
    apuntarla.

    Args:
        workflow_id: Workflow dueño del efecto.
        step_index: Paso que lo produce.
        role: Rol que lo produce.
        action: Acción concreta.

    Returns:
        Una clave determinista de 1 a ``MAX_EFFECT_KEY_CHARS`` caracteres.
    """
    head = f"{workflow_id}:{step_index}:{role.value}"
    digest = hashlib.sha256(f"{head}:{action}".encode()).hexdigest()[:_KEY_DIGEST_CHARS]
    # El hueco de la acción se calcula sobre la cabecera real, así que la cota se cumple con
    # cualquier índice de paso, incluso uno desmesurado.
    room = MAX_EFFECT_KEY_CHARS - len(head) - len(digest) - 2
    if room < 1:
        # Cabecera desmesurada: la clave se queda con los dos digests, que siguen identificando la
        # intención de forma estable.
        head_digest = hashlib.sha256(head.encode()).hexdigest()[:_KEY_DIGEST_CHARS]
        return f"{digest}:{head_digest}"
    return f"{head}:{action[: min(room, _KEY_ACTION_CHARS)]}:{digest}"


def _apply_resolution(
    run: WorkflowRun, *, key: str, status: EffectStatus, detail: str
) -> WorkflowRun:
    """Resolución compartida por ``resolve`` y ``reconcile``.

    Se centraliza para que las dos puertas tengan exactamente las mismas invariantes: solo
    ``APPLIED``/``FAILED``, el detalle recortado al máximo del contrato —un detalle más largo
    haría que el checkpoint no se pudiera volver a validar al cargarlo—, ``resolved_at`` sellado y
    la primera resolución conservada.
    """
    if status not in _RESOLUTION_STATUSES:
        raise ValueError(
            "una resolución solo puede ser APPLIED o FAILED, no "
            f"{status.value}: un efecto sin decisión no se resuelve, se apunta"
        )
    index = _index_of(run, key)
    if index is None:
        return run
    record = run.effects[index]
    if record.status not in _UNRESOLVED_STATUSES:
        return run
    resolved = record.model_copy(
        update={"status": status, "detail": _clip(detail), "resolved_at": utc_now()}
    )
    effects = (*run.effects[:index], resolved, *run.effects[index + 1 :])
    return run.model_copy(update={"effects": effects})


def _existing_decision(record: EffectRecord) -> EffectDecision:
    """Veredicto de una clave ya registrada: dedupe, nunca una segunda ejecución.

    Tres casos, tres motivos distintos, y ninguno permite ejecutar: un efecto aplicado no se
    repite, un efecto fallido no se reintenta por venir de una reanudación, y un efecto en vuelo
    —o en ``UNKNOWN``, que es lo mismo con otro nombre— no se repite porque su resultado se
    desconoce.
    """
    action = record.action
    key = record.idempotency_key
    if record.status is EffectStatus.APPLIED:
        return EffectDecision(
            allowed=False,
            detail=(
                f"el efecto {key!r} ({action}) ya está aplicado: repetirlo lo duplicaría"
            ),
        )
    if record.status is EffectStatus.FAILED:
        return EffectDecision(
            allowed=False,
            code=WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
            detail=(
                f"el efecto {key!r} ({action}) consta como FAILED: no se reintenta sin una "
                "decisión explícita (reconcile)"
            ),
        )
    return EffectDecision(
        allowed=False,
        code=WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
        detail=(
            f"el efecto {key!r} ({action}) está {record.status.value}: se pidió y no consta si "
            "ocurrió, así que no se repite; hace falta reconciliar"
        ),
    )


def _pending_decision(pending: EffectRecord, *, reversible: bool) -> EffectDecision:
    """Veredicto de una intención nueva mientras hay un efecto sin resolver.

    Se rechaza siempre —reversible o no— y el detalle dice por qué, que no es el mismo motivo en
    los dos casos: con un efecto irreversible, repetir puede no tener vuelta atrás; con uno
    reversible, el problema es que el kernel no repite nada a ciegas mientras el libro tenga una
    incógnita abierta. El mismo rechazo con explicaciones distintas es lo que evita que alguien
    lea el «no» como una formalidad.
    """
    if reversible:
        reason = (
            "no se repite nada a ciegas mientras haya un efecto sin resolver, ni siquiera un "
            "efecto reversible"
        )
    else:
        reason = (
            "un efecto irreversible que quizá ya ocurrió no tiene vuelta atrás si se repite"
        )
    return EffectDecision(
        allowed=False,
        code=WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED,
        detail=(
            f"hay un efecto sin resolver ({pending.idempotency_key!r}, "
            f"{pending.status.value}) y esta intención es "
            f"{'reversible' if reversible else 'irreversible'}: {reason}; "
            "reconcilia antes de continuar"
        ),
    )


def _unresolved(run: WorkflowRun) -> tuple[EffectRecord, ...]:
    """Registros cuyo resultado se desconoce, en orden de inserción."""
    return tuple(record for record in run.effects if record.status in _UNRESOLVED_STATUSES)


def _index_of(run: WorkflowRun, key: str) -> int | None:
    """Posición del primer registro con esa clave, o ``None`` si no hay ninguno.

    Duplicar una clave no debería ocurrir —:meth:`EffectLedger.begin_intent` lo impide—, pero si
    un run llegara manipulado con dos registros iguales se usa el primero: es el determinista y
    el que se apuntó antes.
    """
    for index, record in enumerate(run.effects):
        if record.idempotency_key == key:
            return index
    return None


def _clip(text: str) -> str:
    """Recorta un detalle al máximo del contrato.

    ``model_copy`` no valida, así que sin este recorte un detalle largo se escribiría en el
    checkpoint y el run ya no se podría volver a validar al cargarlo: el efecto se habría
    apuntado bien y el workflow quedaría irrecuperable por un texto de más.
    """
    return text[:MAX_WORKFLOW_SUMMARY_CHARS]


__all__ = [
    "MAX_EFFECT_KEY_CHARS",
    "EffectDecision",
    "EffectLedger",
    "effect_key",
]
