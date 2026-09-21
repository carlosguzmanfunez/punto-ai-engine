"""Router de proveedores: registro, asignación de roles y ejecución normalizada (MULTI-PROVIDER v0).

El motor pide **rol + petición**; el router decide qué adaptador la atiende. La asignación es
configuración y se puede cambiar en caliente sin tocar ENGINE:

    router.assign_role(ProviderRole.BUILDER, "anthropic")
    router.select_model("anthropic", "claude-sonnet-5")

Lo que este módulo **no** hace, y es lo importante:

- **no** hay lógica del tipo ``if role == BUILDER: llamar_deepseek()``. El rol solo se consulta en
  el mapa de asignaciones;
- **no** hay fallback automático **implícito** entre proveedores. Si el asignado falla, el
  resultado es ``FAILED`` o ``UNAVAILABLE`` con su causa: cambiar de proveedor sin que nadie lo
  haya decidido convertiría una auditoría en la respuesta del mismo modelo de siempre (ENGINE-5.2
  ya fijó esa regla). La única excepción es el **failover explícito** de
  ``punto.providers.failover``: una política de configuración, por rol, solo ante indisponibilidad
  operativa demostrable y solo hacia un proveedor conectado con las capacidades efectivas del
  rol. Sin política, no hay failover;
- **no** hay autoridad. Un ``ProviderResult`` es inteligencia externa no confiable: el router no
  conoce capacidades, ``ResourceSet``, Human Gate ni políticas, y no puede conceder nada.

Además expone la API interna que la futura capa de configuración consumirá sin acoplarse a HTML:
``test_connection``, ``assign_role``, ``select_model`` y ``status``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import httpx

from punto.audit.logger import AuditLogger
from punto.providers.base import (
    PROVIDER_ANTHROPIC,
    PROVIDER_DEEPSEEK,
    ImageValidationError,
    ModelCompletion,
    MultimodalModelClient,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRefusalError,
    ProviderUnavailableError,
    StructuredModelClient,
)
from punto.providers.contract import (
    PROVIDER_OPENAI,
    FailoverOutcome,
    FailoverRecord,
    ProviderContractError,
    ProviderErrorKind,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
    parse_structured_output,
)
from punto.providers.failover import (
    FailoverCause,
    FailoverPolicy,
    RouteChoice,
    SubstituteEvaluator,
    SubstituteVerdict,
    failover_cause_of,
)
from punto.providers.openai import OpenAIError
from punto.providers.transport import TransportError, provider_error_kind_of
from punto.tools.errors import ProviderRouteError

if TYPE_CHECKING:
    from punto.providers.settings import ProviderSettings
    from punto.providers.transport import SubprocessRunner

#: Proveedores que el motor conoce en esta fase. Un nombre fuera de la lista es un error de
#: configuración, no un proveedor nuevo.
KNOWN_PROVIDERS: tuple[str, ...] = (PROVIDER_OPENAI, PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC)

#: Asignación inicial de roles (MULTI-PROVIDER §3). Es el valor por defecto: se cambia con
#: :meth:`ProviderRouter.assign_role` o desde ``config/providers.yaml``.
DEFAULT_ROLE_ASSIGNMENT: Mapping[ProviderRole, str] = {
    ProviderRole.ARCHITECT: PROVIDER_OPENAI,
    ProviderRole.BUILDER: PROVIDER_DEEPSEEK,
    ProviderRole.VISUAL_QA: PROVIDER_ANTHROPIC,
}

#: Modelo por defecto de cada proveedor cuando la configuración no dice otra cosa.
DEFAULT_PROVIDER_MODELS: Mapping[str, str] = {
    PROVIDER_OPENAI: "gpt-5-codex",
    PROVIDER_DEEPSEEK: "deepseek-v4-pro",
    PROVIDER_ANTHROPIC: "claude-sonnet-5",
}

#: Fábrica de adaptadores: recibe el modelo configurado y devuelve un cliente del contrato.
ProviderFactory = Callable[[str], StructuredModelClient]


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """Proveedor registrado: su fábrica de adaptadores y el modelo seleccionado."""

    name: str
    factory: ProviderFactory
    model: str


@dataclass(frozen=True, slots=True)
class ProviderStatusRow:
    """Fila del estado de un proveedor, para la capa de configuración (no para HTML)."""

    provider: str
    status: str
    model: str
    roles: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Vista serializable de la fila."""
        return {
            "provider": self.provider,
            "status": self.status,
            "model": self.model,
            "roles": list(self.roles),
        }


class ProviderRouter:
    """Registro de proveedores + asignación de roles + ejecución normalizada."""

    def __init__(
        self,
        *,
        assignment: Mapping[ProviderRole, str] | None = None,
        models: Mapping[str, str] | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        """Crea un router vacío con la asignación inicial declarada."""
        self._entries: dict[str, ProviderEntry] = {}
        self._assignment: dict[ProviderRole, str] = dict(
            DEFAULT_ROLE_ASSIGNMENT if assignment is None else assignment
        )
        self._models: dict[str, str] = dict(DEFAULT_PROVIDER_MODELS)
        if models is not None:
            self._models.update(models)
        self._audit = audit
        self._failover_policy: FailoverPolicy | None = None
        self._failover_evaluator: SubstituteEvaluator | None = None

    # ------------------------------------------------------------------ registro
    def register_provider(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        model: str = "",
    ) -> None:
        """Registra un proveedor con su fábrica de adaptadores.

        Args:
            name: Identificador del proveedor (``openai``, ``deepseek``, ``anthropic``).
            factory: Construye el cliente del contrato para un modelo dado.
            model: Modelo a usar; vacío significa el declarado por la configuración o el conocido.

        Raises:
            ProviderRouteError: si el nombre está vacío o la fábrica no es invocable.
        """
        provider = name.strip().lower()
        if not provider:
            raise ProviderRouteError("el nombre del proveedor no puede estar vacío")
        if not callable(factory):
            raise ProviderRouteError(f"la fábrica de {provider!r} no es invocable")
        selected = model.strip() or self._models.get(provider, "")
        if not selected:
            raise ProviderRouteError(f"el proveedor {provider!r} no declara modelo")
        self._entries[provider] = ProviderEntry(name=provider, factory=factory, model=selected)
        self._models[provider] = selected

    def has_provider(self, name: str) -> bool:
        """True si el proveedor está registrado."""
        return name.strip().lower() in self._entries

    def providers(self) -> tuple[str, ...]:
        """Proveedores registrados, en orden de registro."""
        return tuple(self._entries)

    def model_of(self, name: str) -> str:
        """Modelo seleccionado para un proveedor.

        Raises:
            ProviderRouteError: si el proveedor no está registrado.
        """
        return self._entry(name).model

    def select_model(self, provider: str, model: str) -> None:
        """Cambia el modelo de un proveedor sin tocar el motor (API para configuración).

        Raises:
            ProviderRouteError: si el proveedor no está registrado o el modelo está vacío.
        """
        entry = self._entry(provider)
        chosen = model.strip()
        if not chosen:
            raise ProviderRouteError("el modelo no puede estar vacío")
        self._models[entry.name] = chosen
        self._entries[entry.name] = ProviderEntry(
            name=entry.name, factory=entry.factory, model=chosen
        )

    # --------------------------------------------------------------- asignación
    def assign_role(self, role: ProviderRole, provider: str) -> None:
        """Asigna un rol a un proveedor registrado (API para configuración).

        Es lo único que hay que cambiar para mover un rol de proveedor: el motor pide el rol y no
        conoce ningún nombre de proveedor.

        Raises:
            ProviderRouteError: si el proveedor no está registrado.
        """
        entry = self._entry(provider)
        self._assignment[role] = entry.name

    def get_provider_for_role(self, role: ProviderRole) -> str:
        """Proveedor asignado a un rol.

        Raises:
            ProviderRouteError: si el rol no tiene asignación registrada.
        """
        provider = self._assignment.get(role)
        if provider is None:
            raise ProviderRouteError(f"el rol {role.value} no tiene proveedor asignado")
        return provider

    def roles_of(self, provider: str) -> tuple[ProviderRole, ...]:
        """Roles asignados a un proveedor, en orden declarado."""
        name = provider.strip().lower()
        return tuple(role for role in ProviderRole if self._assignment.get(role) == name)

    def assignment(self) -> Mapping[str, str]:
        """Asignación completa rol → proveedor, para la capa de configuración."""
        return {role.value: provider for role, provider in self._assignment.items()}

    # ----------------------------------------------------------------- failover
    def configure_failover(
        self, policy: FailoverPolicy | None, evaluator: SubstituteEvaluator | None = None
    ) -> None:
        """Declara la política de failover (configuración de confianza) y quién juzga candidatos.

        Sin ``evaluator`` **no** hay failover aunque haya política: el router no conoce sesiones,
        transportes ni capacidades, y un sustituto que nadie pudo juzgar no se usa (falla cerrado).
        ``None`` como política lo desactiva. La asignación de roles no se toca.
        """
        self._failover_policy = policy
        self._failover_evaluator = evaluator

    def failover_policy(self) -> FailoverPolicy | None:
        """Política de failover vigente, o ``None`` si el failover está desactivado."""
        return self._failover_policy

    # ---------------------------------------------------------------- ejecución
    def execute(
        self,
        role: ProviderRole,
        request: ProviderRequest,
        *,
        json_schema: Mapping[str, object] | None = None,
        max_output_tokens: int | None = None,
    ) -> ProviderResult:
        """Ejecuta una petición normalizada contra el proveedor asignado al rol.

        Cada petición empieza por el proveedor **asignado**. Si falla y la política de failover
        cubre el rol y el fallo es una indisponibilidad operativa demostrable, se intenta un
        sustituto conectado con las capacidades efectivas del rol (ver
        :mod:`punto.providers.failover`); en cualquier otro caso el resultado lleva su causa y su
        estado normalizado. Un fallo del proveedor **no** rompe el motor: siempre vuelve un
        ``ProviderResult``.

        Args:
            role: Rol que pide el modelo. La asignación decide quién responde.
            request: Petición normalizada (instrucciones, contexto, adjuntos, metadata).
            json_schema: Esquema que la respuesta debería cumplir, si el proveedor lo soporta.
            max_output_tokens: Tope de salida autorizado para esta invocación.

        Returns:
            El resultado normalizado, con éxito o con el fallo clasificado.
        """
        request_id = request.request_id or f"{role.value.lower()}-sin-id"
        if request.role is not role:
            request = ProviderRequest(
                role=role,
                instructions=request.instructions,
                request_id=request_id,
                context=request.context,
                attachments=request.attachments,
                metadata=dict(request.metadata),
            )
        try:
            provider = self.get_provider_for_role(role)
        except ProviderRouteError as error:
            return ProviderResult(
                request_id=request_id,
                provider="",
                model="",
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error=str(error),
                error_kind=ProviderErrorKind.CONFIG,
            )
        entry = self._entries.get(provider)
        if entry is None:  # pragma: no cover - assign_role exige un proveedor registrado
            return ProviderResult(
                request_id=request_id,
                provider=provider,
                model="",
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error=f"el proveedor {provider!r} no está registrado",
                error_kind=ProviderErrorKind.CONFIG,
            )

        gap = self._capability_gap(role, request, entry)
        if gap:
            # El asignado no tiene la capacidad efectiva que la petición exige (p. ej. imágenes en
            # un transporte de solo texto): no se gasta el primario, se pasa directo a los
            # sustitutos que la política declara y que sí la tienen.
            primary = ProviderResult(
                request_id=request_id,
                provider=entry.name,
                model=entry.model,
                status=ProviderStatus.UNAVAILABLE,
                role=role,
                error=gap,
                error_kind=ProviderErrorKind.UNAVAILABLE,
            )
            cause: FailoverCause | None = FailoverCause.CAPABILITY_MISSING
        else:
            primary = self._run_entry(
                role,
                request,
                entry,
                request_id=request_id,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
            cause = None
        return self._failover(
            role,
            request,
            primary,
            request_id=request_id,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            cause=cause,
        )

    def _capability_gap(
        self, role: ProviderRole, request: ProviderRequest, entry: ProviderEntry
    ) -> str:
        """Motivo por el que el asignado **no puede** con la petición por falta de capacidad.

        Solo aplica con política de failover para el rol, evaluador y una petición con imágenes: sin
        alguna de las tres cosas no se decide nada por adelantado y se ejecuta el asignado como
        siempre. ``""`` significa «el asignado sirve» (o «no se puede afirmar lo contrario»).
        """
        policy = self._failover_policy
        if (
            policy is None
            or not policy.covers(role)
            or self._failover_evaluator is None
            or not request.has_attachments
        ):
            return ""
        verdict = self._judge_substitute(role, entry.name, True)
        return "" if verdict.eligible or not verdict.capability_gap else verdict.reason

    def resolve_route(self, role: ProviderRole, *, needs_vision: bool = False) -> RouteChoice:
        """Ruta **efectiva** del rol: quién lo ejecutaría ahora, por capacidad efectiva.

        No ejecuta nada ni cambia la asignación. Juzga primero al asignado; si no sirve y la
        política de failover cubre el rol, al primer sustituto declarado que sí sirva. Sin evaluador
        no se puede acreditar capacidad efectiva: se devuelve el asignado **sin** afirmar nada
        (``reason`` lo dice) para que el llamante falle cerrado si lo necesita.
        """
        try:
            assigned = self.get_provider_for_role(role)
        except ProviderRouteError as error:
            return RouteChoice(assigned="", reason=str(error))
        if self._failover_evaluator is None:
            return RouteChoice(assigned=assigned, reason="sin evaluador: capacidad no acreditada")
        first = self._judge_substitute(role, assigned, needs_vision)
        if first.eligible:
            return RouteChoice(
                assigned=assigned,
                provider=assigned,
                model=self._models.get(assigned, ""),
                transport=first.transport,
            )
        rejections = [f"{assigned}: {first.reason or 'no elegible'}"]
        policy = self._failover_policy
        if policy is not None and policy.covers(role):
            for name, unusable in self._substitute_candidates(role, assigned, policy):
                if unusable:
                    rejections.append(f"{name}: {unusable}")
                    continue
                verdict = self._judge_substitute(role, name, needs_vision)
                if verdict.eligible and verdict.metered and not policy.allow_metered:
                    rejections.append(f"{name}: transporte de pago por uso (allow_metered=false)")
                    continue
                if not verdict.eligible:
                    rejections.append(f"{name}: {verdict.reason or 'no elegible'}")
                    continue
                return RouteChoice(
                    assigned=assigned,
                    provider=name,
                    model=self._models.get(name, ""),
                    transport=verdict.transport,
                    via_failover=True,
                    reason=rejections[0],
                )
        return RouteChoice(assigned=assigned, reason="; ".join(rejections))

    def _run_entry(
        self,
        role: ProviderRole,
        request: ProviderRequest,
        entry: ProviderEntry,
        *,
        request_id: str,
        json_schema: Mapping[str, object] | None,
        max_output_tokens: int | None,
    ) -> ProviderResult:
        """Ejecuta la petición contra **un** proveedor concreto, sin failover."""
        provider = entry.name
        self._audit_started(request_id=request_id, role=role, entry=entry)
        started = time.perf_counter()
        client: StructuredModelClient | None = None
        try:
            client = entry.factory(entry.model)
            completion = self._invoke(
                client,
                request=request,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
                provider=provider,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            content = client.redact(completion.content)
            result = ProviderResult(
                request_id=request_id,
                provider=provider,
                model=completion.model or entry.model,
                status=ProviderStatus.SUCCESS,
                role=role,
                content=content,
                structured_output=parse_structured_output(content),
                usage=completion.usage,
                duration_ms=duration_ms,
                finish_reason=completion.finish_reason,
                transport_retries=completion.transport_retries,
            )
            self._audit_completed(result)
            return result
        except ImageValidationError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
                kind=ProviderErrorKind.CONFIG,
            )
        except ProviderError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
            )
        except httpx.HTTPError as error:
            # La clase la decide ``classify_provider_error``: un timeout de ``httpx`` es un
            # ``HTTPError``, pero para el motor es TIMEOUT y esa distinción importa.
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
            )
        except ProviderContractError as error:
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
                kind=ProviderErrorKind.CONFIG,
            )
        except Exception as error:  # un fallo no clasificado del adaptador queda contenido
            # Un proveedor caído no puede romper PUNTO: cualquier fallo inesperado del adaptador se
            # normaliza en vez de propagarse. Los errores propios de un adaptador que no heredan de
            # ``ProviderError`` (los de DeepSeek son ``RuntimeError``) atraviesan el transporte sin
            # traducirse: se clasifican por su clase y solo lo que no se reconoce queda ``UNKNOWN``.
            # Antes se forzaba ``UNKNOWN`` aquí y un 402 (sin saldo) era indistinguible de cualquier
            # otro fallo, así que el motor no podía saber que el proveedor no estaba operativo.
            return self._fail(
                request_id=request_id,
                role=role,
                entry=entry,
                error=error,
                started=started,
                client=client,
            )
        finally:
            if client is not None:
                _close_quietly(client)

    def _failover(
        self,
        role: ProviderRole,
        request: ProviderRequest,
        primary: ProviderResult,
        *,
        request_id: str,
        json_schema: Mapping[str, object] | None,
        max_output_tokens: int | None,
        cause: FailoverCause | None = None,
    ) -> ProviderResult:
        """Intenta sustitutos **solo** si la política lo permite y el primario no estaba operativo.

        Es la única puerta de failover del motor y es estrecha a propósito:

        - sin política, o con un rol que la política no cubre, el resultado del primario se devuelve
          tal cual;
        - un fallo que no es indisponibilidad operativa demostrable (respuesta inválida, negativa,
          timeout, error desconocido...) también: cambiar de proveedor escondería el problema;
        - cada candidato lo juzga el evaluador (conectado + capacidades efectivas del rol) y un
          proveedor de pago por uso solo entra si la política lo permite;
        - cada proveedor se intenta como mucho una vez y el total está acotado: no hay bucles, y el
          resultado de un sustituto nunca dispara otro failover si su fallo no es operativo;
        - el sustituto recibe la **misma** petición del **mismo** rol: no hereda autoridad alguna.

        Si nadie es compatible se falla cerrado con el fallo del primario y la causa explícita.
        """
        policy = self._failover_policy
        if primary.ok or policy is None or not policy.covers(role):
            return primary
        capability_gap = cause is FailoverCause.CAPABILITY_MISSING
        cause = cause or failover_cause_of(primary.error_kind)
        if cause is None:
            return primary
        primary_kind = (
            "CAPABILITY_GAP"
            if capability_gap
            else ("" if primary.error_kind is None else primary.error_kind.value)
        )
        records: list[FailoverRecord] = []
        rejections: list[str] = []
        last = primary
        attempts = 0
        for name, unusable in self._substitute_candidates(role, primary.provider, policy):
            if attempts >= policy.max_substitutes:
                break
            if unusable:
                rejections.append(f"{name}: {unusable}")
                continue
            verdict = self._judge_substitute(role, name, request.has_attachments)
            if verdict.eligible and verdict.metered and not policy.allow_metered:
                verdict = SubstituteVerdict(
                    eligible=False,
                    reason=(
                        "transporte de pago por uso: la política no lo permite "
                        "(allow_metered=false)"
                    ),
                    metered=True,
                )
            if not verdict.eligible:
                rejections.append(f"{name}: {verdict.reason or 'no elegible'}")
                continue
            attempts += 1
            entry = self._entries[name]
            result = self._run_entry(
                role,
                request,
                entry,
                request_id=request_id,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
            record = FailoverRecord(
                role=role,
                primary_provider=primary.provider,
                primary_model=primary.model,
                primary_error_kind=primary_kind,
                cause=cause.value,
                substitute_provider=entry.name,
                substitute_model=entry.model,
                outcome=FailoverOutcome.SUCCEEDED if result.ok else FailoverOutcome.FAILED,
                detail="" if result.ok else _clip(result.error),
            )
            records.append(record)
            self._audit_failover(request_id, record)
            last = result
            if result.ok or failover_cause_of(result.error_kind) is None:
                break
        if attempts == 0:
            detail = "; ".join(rejections) or "no hay otros proveedores registrados"
            record = FailoverRecord(
                role=role,
                primary_provider=primary.provider,
                primary_model=primary.model,
                primary_error_kind=primary_kind,
                cause=cause.value,
                substitute_provider="",
                substitute_model="",
                outcome=FailoverOutcome.NO_COMPATIBLE_SUBSTITUTE,
                detail=_clip(detail),
            )
            self._audit_failover(request_id, record)
            return replace(
                primary,
                error=_clip(
                    f"{primary.error} | failover de {role.value} ({cause.value}): ningún "
                    f"sustituto compatible ({detail})",
                    600,
                ),
                failovers=(record,),
            )
        return replace(last, failovers=tuple(records))

    def _substitute_candidates(
        self, role: ProviderRole, primary: str, policy: FailoverPolicy
    ) -> tuple[tuple[str, str], ...]:
        """Candidatos en orden determinista, sin el primario y sin repetidos.

        Cada uno viaja con el motivo por el que ya no sirve (vacío si sigue en pie): un nombre
        declarado en la política que no está registrado se explica en vez de desaparecer.
        """
        declared = policy.preferred(role)
        names = declared if declared else tuple(self._entries)
        seen: set[str] = {primary}
        candidates: list[tuple[str, str]] = []
        for raw in names:
            name = raw.strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            candidates.append(
                (name, "" if name in self._entries else "no está registrado en el router")
            )
        return tuple(candidates)

    def _judge_substitute(
        self, role: ProviderRole, provider: str, needs_vision: bool
    ) -> SubstituteVerdict:
        """Veredicto del evaluador sobre un candidato; sin evaluador o si falla, no es elegible."""
        evaluator = self._failover_evaluator
        if evaluator is None:
            return SubstituteVerdict(
                eligible=False,
                reason="no hay evaluador: no se puede acreditar conexión ni capacidades efectivas",
            )
        try:
            return evaluator(role, provider, needs_vision)
        except Exception as error:  # un evaluador roto nunca habilita a un sustituto
            return SubstituteVerdict(
                eligible=False, reason=f"no se pudo evaluar ({type(error).__name__})"
            )

    def _invoke(
        self,
        client: StructuredModelClient,
        *,
        request: ProviderRequest,
        json_schema: Mapping[str, object] | None,
        max_output_tokens: int | None,
        provider: str,
    ) -> ModelCompletion:
        """Llama al adaptador con la primitiva que corresponda (texto o multimodal).

        Antes de invocar se usa, si existe, el gancho opcional ``bind_role``: un cliente que pone un
        transporte debajo necesita saber qué rol pidió la respuesta. El router no sabe nada más de
        ese cliente —ni de su transporte—: solo aprovecha un gancho declarado.
        """
        binder = getattr(client, "bind_role", None)
        if callable(binder):
            binder(request.role)
        system_prompt = request.instructions
        if request.context:
            system_prompt = f"{request.instructions}\n\n{request.context}"
        if request.attachments:
            if not isinstance(client, MultimodalModelClient):
                raise ProviderUnavailableError(
                    f"el proveedor {provider!r} no acepta imágenes y la petición lleva "
                    f"{len(request.attachments)}: no se descartan en silencio"
                )
            return client.complete_multimodal_json(
                system_prompt=system_prompt,
                user_prompt=request.instructions,
                images=request.attachments,
                json_schema=json_schema,
                max_output_tokens=max_output_tokens,
            )
        return client.complete_json(
            system_prompt=system_prompt,
            user_prompt=request.instructions,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )

    def _fail(
        self,
        *,
        request_id: str,
        role: ProviderRole,
        entry: ProviderEntry,
        error: BaseException,
        started: float,
        client: StructuredModelClient | None = None,
        kind: ProviderErrorKind | None = None,
    ) -> ProviderResult:
        """Construye el resultado de un fallo, sin dejar escapar la credencial."""
        detail = str(error)
        # Doble saneado: el adaptador conoce su credencial y el entorno declara las conocidas. La
        # garantía de que una clave no acaba en un resultado no puede depender de que el adaptador
        # sea educado.
        detail = _redact_without_client(
            client.redact(detail) if client is not None else detail
        )
        resolved = kind if kind is not None else classify_provider_error(error)
        status = (
            ProviderStatus.UNAVAILABLE
            if resolved
            in (
                ProviderErrorKind.UNAVAILABLE,
                ProviderErrorKind.AUTHENTICATION,
                ProviderErrorKind.QUOTA_EXHAUSTED,
            )
            else ProviderStatus.FAILED
        )
        result = ProviderResult(
            request_id=request_id,
            provider=entry.name,
            model=entry.model,
            status=status,
            role=role,
            error=detail,
            error_kind=resolved,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        self._audit_failed(result)
        return result

    # ------------------------------------------------------------------- salud
    def test_connection(self, provider: str) -> ProviderHealth:
        """Comprueba la conexión de un proveedor sin gastar tokens si es posible.

        Returns:
            La salud normalizada: ``CONNECTED``, ``UNAVAILABLE``, ``AUTH_FAILED`` o
            ``CONFIG_ERROR``.
        """
        try:
            entry = self._entry(provider)
        except ProviderRouteError as error:
            return ProviderHealth(
                provider=provider, status=ProviderHealthStatus.CONFIG_ERROR, detail=str(error)
            )
        try:
            client = entry.factory(entry.model)
        except ProviderAuthenticationError:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=entry.model,
                detail="la credencial falta o fue rechazada",
            )
        except ProviderUnavailableError as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.CONFIG_ERROR,
                model=entry.model,
                detail=_redact_without_client(str(error)),
            )
        except ProviderError as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.CONFIG_ERROR,
                model=entry.model,
                detail=_redact_without_client(str(error)),
            )
        try:
            checker = getattr(client, "health_check", None)
            if not callable(checker):
                raise ProviderUnavailableError(
                    "el adaptador no expone health_check: no se puede comprobar la conexión"
                )
            detail = str(checker())
        except ProviderAuthenticationError:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.AUTH_FAILED,
                model=entry.model,
                detail="la credencial fue rechazada por el proveedor",
            )
        except (ProviderError, httpx.HTTPError) as error:
            return ProviderHealth(
                provider=entry.name,
                status=ProviderHealthStatus.UNAVAILABLE,
                model=entry.model,
                detail=client.redact(str(error)),
            )
        finally:
            _close_quietly(client)
        return ProviderHealth(
            provider=entry.name,
            status=ProviderHealthStatus.CONNECTED,
            model=entry.model,
            detail=detail,
        )

    def status(self, *, check: bool = False) -> tuple[ProviderStatusRow, ...]:
        """Estado de cada proveedor registrado, para la capa de configuración.

        Args:
            check: Si es ``True``, además comprueba la conexión de cada proveedor. Por defecto no
                llama a nadie: la tabla se puede pintar sin gastar red.
        """
        rows: list[ProviderStatusRow] = []
        for name, entry in self._entries.items():
            if check:
                health = self.test_connection(name)
                state = health.status.value
            else:
                state = "REGISTERED"
            rows.append(
                ProviderStatusRow(
                    provider=name,
                    status=state,
                    model=entry.model,
                    roles=tuple(role.value for role in self.roles_of(name)),
                )
            )
        return tuple(rows)

    # ------------------------------------------------------------------ interno
    def _entry(self, provider: str) -> ProviderEntry:
        """Entrada registrada de un proveedor.

        Raises:
            ProviderRouteError: si no está registrado.
        """
        name = provider.strip().lower()
        entry = self._entries.get(name)
        if entry is None:
            raise ProviderRouteError(
                f"PROVIDER_UNAVAILABLE: el proveedor {name!r} no está registrado. "
                f"Registrados: {', '.join(self._entries) or 'ninguno'}"
            )
        return entry

    def _audit_started(self, *, request_id: str, role: ProviderRole, entry: ProviderEntry) -> None:
        """Registra el inicio de una petición (sin prompt y sin credencial)."""
        if self._audit is None:
            return
        self._audit.log_provider_request_started(
            request_id=request_id, role=role.value, provider=entry.name, model=entry.model
        )

    def _audit_completed(self, result: ProviderResult) -> None:
        """Registra el resultado de una petición con sus cifras, no con su contenido."""
        if self._audit is None:
            return
        self._audit.log_provider_request_completed(
            request_id=result.request_id,
            role="" if result.role is None else result.role.value,
            provider=result.provider,
            model=result.model,
            duration_ms=result.duration_ms,
            usage=result.usage,
        )

    def _audit_failover(self, request_id: str, record: FailoverRecord) -> None:
        """Registra la sustitución de proveedor con su causa y su desenlace."""
        if self._audit is None:
            return
        self._audit.log_provider_failover(request_id=request_id, record=record.as_dict())

    def _audit_failed(self, result: ProviderResult) -> None:
        """Registra el fallo normalizado de una petición."""
        if self._audit is None:
            return
        self._audit.log_provider_request_failed(
            request_id=result.request_id,
            role="" if result.role is None else result.role.value,
            provider=result.provider,
            model=result.model,
            status=result.status.value,
            error_kind="" if result.error_kind is None else result.error_kind.value,
            duration_ms=result.duration_ms,
        )


def classify_provider_error(error: BaseException) -> ProviderErrorKind:
    """Traduce un fallo del adaptador a un vocabulario que el motor entiende.

    El motor no debe conocer el dialecto de cada proveedor: aquí se normaliza una sola vez. La
    clasificación mira los tipos del contrato y, cuando el adaptador tiene su propia jerarquía, el
    nombre de la clase —que es estable y está declarado por el adaptador, no inferido del mensaje—.
    Un fallo de **transporte** (Codex, Claude Code) ya trae su propia clase normalizada y se
    proyecta tal cual sobre el vocabulario del contrato.
    """
    if isinstance(error, TransportError):
        return provider_error_kind_of(error.kind)
    if isinstance(error, ProviderAuthenticationError):
        return ProviderErrorKind.AUTHENTICATION
    if isinstance(error, ProviderUnavailableError):
        return ProviderErrorKind.UNAVAILABLE
    if isinstance(error, ProviderRefusalError):
        return ProviderErrorKind.REFUSAL
    if isinstance(error, httpx.TimeoutException):
        return ProviderErrorKind.TIMEOUT
    if isinstance(error, httpx.HTTPError):
        return ProviderErrorKind.NETWORK
    if isinstance(error, ProviderContractError):
        return ProviderErrorKind.CONFIG
    name = type(error).__name__
    if "Timeout" in name:
        return ProviderErrorKind.TIMEOUT
    # Sin créditos/saldo/cuota (402 de DeepSeek): antes caía en UNKNOWN y era indistinguible de un
    # fallo cualquiera. Se decide por la clase que declara el adaptador, nunca por el texto.
    if any(marker in name for marker in ("Balance", "Quota", "Credit")):
        return ProviderErrorKind.QUOTA_EXHAUSTED
    if "RateLimit" in name:
        return ProviderErrorKind.RATE_LIMIT
    # 401 del adaptador de DeepSeek: sin credencial válida es «desconectado».
    if "AuthError" in name or "Authentication" in name:
        return ProviderErrorKind.AUTHENTICATION
    # 5xx tras agotar los reintentos: el proveedor no está sirviendo.
    if "ServerError" in name:
        return ProviderErrorKind.UNAVAILABLE
    if "Transport" in name:
        return ProviderErrorKind.NETWORK
    if "InvalidResponse" in name or "Truncated" in name:
        return ProviderErrorKind.INVALID_RESPONSE
    if "Refusal" in name:
        return ProviderErrorKind.REFUSAL
    if isinstance(error, OpenAIError):
        return ProviderErrorKind.UNKNOWN
    if isinstance(error, ProviderError):
        return ProviderErrorKind.UNKNOWN
    return ProviderErrorKind.UNKNOWN


def load_default_router(
    *,
    audit: AuditLogger | None = None,
    assignment: Mapping[ProviderRole, str] | None = None,
    models: Mapping[str, str] | None = None,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> ProviderRouter:
    """Router con los tres proveedores reales, cada uno con su transporte configurado.

    Los clientes se construyen **al ejecutar**, no al registrar: un router sin credenciales ni
    sesiones se puede construir y consultar (la tabla de estado funciona) y el fallo aparece como
    ``AUTH_FAILED``/``UNAVAILABLE`` en la comprobación, no como una excepción de importación.

    El router no sabe qué transporte hay debajo: pide un cliente del contrato de proveedor y la
    configuración decide si eso es Codex, Claude Code o la API.
    """
    router = ProviderRouter(assignment=assignment, models=models, audit=audit)
    for name in (PROVIDER_OPENAI, PROVIDER_DEEPSEEK, PROVIDER_ANTHROPIC):
        router.register_provider(name, _transport_factory(name, settings=settings, runner=runner))
    return router


def _transport_factory(
    provider: str,
    *,
    settings: ProviderSettings | None = None,
    runner: SubprocessRunner | None = None,
) -> ProviderFactory:
    """Fábrica del cliente de un proveedor, con el transporte que elija la configuración."""

    def _build(model: str) -> StructuredModelClient:
        from punto.providers.transport_registry import transport_client

        return transport_client(provider, model=model, settings=settings, runner=runner)

    return _build


def _clip(text: str, limit: int = 300) -> str:
    """Acota un texto que viaja en la constancia de un failover."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _redact_without_client(text: str) -> str:
    """Sanea credenciales conocidas cuando todavía no hay cliente que las conozca."""
    redacted = text
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def _close_quietly(client: object) -> None:
    """Cierra el cliente si sabe cerrarse, sin enmascarar el resultado de la operación."""
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # el cierre no puede cambiar el veredicto
            return


def providers_summary(
    router: ProviderRouter, *, check: bool = False
) -> Iterable[dict[str, object]]:
    """Resumen serializable para la capa de configuración (PROVIDER | STATUS | MODEL | ROLE)."""
    return (row.as_dict() for row in router.status(check=check))


__all__ = [
    "DEFAULT_PROVIDER_MODELS",
    "DEFAULT_ROLE_ASSIGNMENT",
    "KNOWN_PROVIDERS",
    "ProviderEntry",
    "ProviderFactory",
    "ProviderRouter",
    "ProviderStatusRow",
    "classify_provider_error",
    "load_default_router",
    "providers_summary",
]
