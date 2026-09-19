"""Ciclo mínimo de construcción gobernada (PILOT-03).

Este módulo es el **primer punto de entrada orquestado** de PUNTO: convierte una intención declarada
en una propuesta de un proveedor, pasando por la memoria de experiencia y por sus propias
comprobaciones, y deja el ciclo entero auditable por su ``request_id``.

```
BuildRequest
  -> admisión en la frontera (forma, destino registrado, rol)
  -> normalización (huella y medidas)
  -> recuperación PELL            (antes de gastar ninguna llamada)
  -> contexto gobernado           (objetivo + criterios + alcance + experiencia)
  -> resolución del rol por el ProviderRouter   (la configuración manda; sin fallback)
  -> UNA invocación del proveedor
  -> normalización del resultado
  -> validación determinista de PUNTO
  -> BuildResult con authority=PROPOSAL_ONLY
```

Frontera de autoridad, explícita porque es lo que este ciclo demuestra:

- **el proveedor es un worker no confiable**: su salida es texto inerte. Aquí no se ejecuta nada, no
  se escriben ficheros, no se llama a ningún ejecutor y no se emite ningún evento de efecto;
- **la autoridad la conserva PUNTO**: el veredicto lo calcula este módulo con comprobaciones
  deterministas, y ``authority`` se fija aquí, nunca se lee del proveedor;
- **nada de la solicitud amplía autoridad**: no hay campos libres, no hay proveedor ni modelo
  elegibles y el destino es una clave registrada, no una ruta;
- **una sola invocación, un solo intento**: sin reintentos y sin redirigir el trabajo a otro.

Lo que este ciclo **no** hace, a propósito: aplicar la propuesta al repositorio, reparar, emitir
efectos, saltarse un Human Gate, crear checkpoints o escribir experiencia en PELL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from punto.audit.logger import AuditLogger
from punto.memory.experience import ExperienceStatus
from punto.memory.retrieval import (
    MemoryQuery,
    MemoryRetriever,
    PriorExperienceContext,
    RetrievalOutcome,
    RetrievalStatus,
    build_memory_query,
    render_experience_block,
)
from punto.providers.contract import (
    ProviderRequest,
    ProviderResult,
    ProviderRole,
    ProviderStatus,
)
from punto.providers.registry import ProviderRegistry
from punto.providers.router import ProviderRouter
from punto.providers.secrets import redact_secret_text
from punto.schemas.build import (
    MAX_PROPOSAL_CHARS,
    BuildRequest,
    BuildRequestStatus,
    BuildResult,
    BuildValidationIssue,
    ValidationVerdict,
)
from punto.schemas.enums import AuditResult
from punto.schemas.workflow import RoleName

#: Traducción entre el vocabulario de roles del proveedor y el de la tabla declarativa de
#: capacidades. Son dos vocabularios distintos del motor (``BUILDER`` frente a ``DEVELOPER``);
#: confundirlos daría un preflight que falla por el nombre, no por la capacidad.
_CAPABILITY_ROLE: Final[dict[ProviderRole, RoleName]] = {
    ProviderRole.ARCHITECT: RoleName.ARCHITECT,
    ProviderRole.BUILDER: RoleName.DEVELOPER,
    ProviderRole.VISUAL_QA: RoleName.VISUAL_QA,
}

#: Tope de tokens de salida por defecto: lo fija PUNTO, no la solicitud.
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 6_000

#: Instrucciones del worker: declaran su papel no confiable y el producto esperado (una propuesta).
WORKER_INSTRUCTIONS: Final[str] = (
    "Eres un worker de PUNTO AI ENGINE. Produces una PROPUESTA en texto para revision humana.\n"
    "Reglas que no puedes cambiar:\n"
    "- no tienes autoridad: no apruebas, no autorizas, no ejecutas y no aplicas cambios;\n"
    "- no afirmes que algo se ha aplicado, desplegado o verificado: no ha ocurrido;\n"
    "- no pidas credenciales ni incluyas secretos, tokens, DSN o claves en la respuesta;\n"
    "- no propongas tocar rutas fuera del alcance declarado;\n"
    "- responde con la propuesta y, si hace falta, los pasos y los riesgos, en texto plano breve."
)

#: Patrones de secreto que invalidan una salida de proveedor (se rechaza, no se guarda).
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bpostgres(?:ql)?://"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{16,}"),
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*[^\s;&'\"]{8,}"),
    re.compile(r"\bnpg_[A-Za-z0-9]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

#: Caracteres de control no permitidos en una propuesta.
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Referencias a rutas dentro de una propuesta, para poder contrastarlas con el destino real.
_PATH_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+|[\w.-]+\.(?:ts|tsx|js|mjs|json|md|sql|py|css|yml|yaml))"
)

#: Frases con las que un proveedor intentaría declararse autorizado. No cambian nada: se registran.
_AUTHORITY_CLAIMS: Final[tuple[str, ...]] = (
    "está autorizado",
    "esta autorizado",
    "autorizado para ejecutar",
    "aprobado para aplicar",
    "ya fue aplicado",
    "ya está aplicado",
    "he aplicado",
    "se ha desplegado",
    "omite la política",
    "omite la politica",
)

#: Afirmaciones de un efecto que este ciclo no puede producir (se marcan como incidencia).
_IMPOSSIBLE_EFFECT: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(he modificado|modifiqu[eé] el repositorio|desplegado en producci[oó]n)\b"
)


class BuildCycleError(RuntimeError):
    """La solicitud no se puede admitir en la frontera del ciclo."""


@dataclass(frozen=True, slots=True)
class BuildTarget:
    """Destino registrado en PUNTO: la ruta real vive aquí, nunca en la solicitud."""

    target_id: str
    repository: Path
    scope_roots: tuple[str, ...] = ()

    def resolve_scope(self, scope_paths: Sequence[str]) -> tuple[str, ...]:
        """Comprueba qué rutas declaradas existen de verdad dentro del destino.

        Una ruta que no existe **no** autoriza nada: queda fuera del alcance efectivo y el ciclo lo
        registra como incidencia. ``scope_roots`` acota además qué raíces del destino se admiten.
        """
        valid: list[str] = []
        for candidate in scope_paths:
            if self.scope_roots and not any(
                candidate == root or candidate.startswith(f"{root}/") for root in self.scope_roots
            ):
                continue
            if (self.repository / candidate).exists():
                valid.append(candidate)
        return tuple(valid)


@dataclass(frozen=True, slots=True)
class BuildCycleConfig:
    """Configuración confiable del ciclo: la fija PUNTO, no la solicitud."""

    targets: Mapping[str, BuildTarget]
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_proposal_chars: int = MAX_PROPOSAL_CHARS

    def target(self, target_id: str) -> BuildTarget:
        """Destino registrado por su clave.

        Raises:
            BuildCycleError: si la clave no corresponde a ningún destino registrado.
        """
        found = self.targets.get(target_id)
        if found is None:
            known = ", ".join(sorted(self.targets)) or "ninguno"
            raise BuildCycleError(
                f"el destino {target_id!r} no está registrado en PUNTO (registrados: {known})"
            )
        return found


@dataclass(frozen=True, slots=True)
class BuildValidation:
    """Veredicto de PUNTO sobre la salida del proveedor."""

    verdict: ValidationVerdict
    issues: tuple[BuildValidationIssue, ...] = ()
    proposal: str | None = None

    @property
    def valid(self) -> bool:
        """True solo si no hay ninguna incidencia."""
        return self.verdict is ValidationVerdict.VALID


def _fingerprint(text: str) -> str:
    """Huella estable de un texto normalizado (sin guardar el texto)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_form(request: BuildRequest) -> str:
    """Forma normalizada y determinista de la solicitud: misma entrada, misma huella."""
    parts = [
        f"objective={request.objective}",
        f"target={request.target_repository}",
        f"role={request.requested_role.value}",
        f"constraints={'|'.join(request.constraints)}",
        f"acceptance={'|'.join(request.acceptance_criteria)}",
        f"scope={'|'.join(request.scope_paths)}",
        f"context={request.context}",
    ]
    return "\n".join(parts)


def _sanitize_text(text: str) -> str:
    """Borra cualquier credencial detectable de un texto que va a salir del ciclo."""
    return redact_secret_text(text)


@dataclass(slots=True)
class BuildCycle:
    """Ciclo mínimo de construcción gobernada, compuesto sobre lo que ya existe.

    Las dependencias se inyectan: el router (que resuelve el rol y ejecuta), la tabla declarativa de
    capacidades (preflight), el recuperador de memoria, el registro de auditoría y la configuración
    confiable. El ciclo no construye ninguna de ellas por su cuenta.
    """

    router: ProviderRouter
    config: BuildCycleConfig
    retriever: MemoryRetriever | None = None
    audit: AuditLogger | None = None
    capabilities: Any | None = None
    actor: str = "punto-build-cycle"
    _last_result: BuildResult | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ público
    def run(self, request: BuildRequest) -> BuildResult:
        """Ejecuta el ciclo completo y devuelve el resultado normalizado.

        Nunca lanza por un fallo del proveedor: lo normaliza. Las excepciones quedan para los
        errores de programación del propio ciclo.
        """
        started = time.perf_counter()
        try:
            target = self.config.target(request.target_repository)
        except BuildCycleError as error:
            # El destino no está registrado: la solicitud no llega a ser admitida y se registra como
            # rechazo, no como un ciclo que empezó y no terminó.
            self._audit_rejected(request, "TARGET_NOT_REGISTERED", str(error))
            raise
        self._audit_accepted(request)
        effective_scope = target.resolve_scope(request.scope_paths)
        self._audit_normalized(request, _normalized_form(request), target, effective_scope)

        retrieval = self._retrieve(request)
        role = request.requested_role
        provider = self.router.get_provider_for_role(role)
        declared = self._capability_declared(role, provider)
        self._audit_provider_selected(request, provider, declared)

        provider_result = self.router.execute(
            role,
            self._provider_request(request, retrieval, target, effective_scope),
            max_output_tokens=self.config.max_output_tokens,
        )
        validation = self._validate(
            request=request,
            provider_result=provider_result,
            target=target,
            effective_scope=effective_scope,
        )
        result = self._build_result(
            request=request,
            provider=provider,
            declared=declared,
            provider_result=provider_result,
            validation=validation,
            retrieval=retrieval,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        self._last_result = result
        self._audit_validation(request, validation, result)
        self._audit_completed(request, result)
        return result

    @property
    def last_result(self) -> BuildResult | None:
        """Último resultado del ciclo, para composición y pruebas."""
        return self._last_result

    # ----------------------------------------------------------------- interno
    def _provider_request(
        self,
        request: BuildRequest,
        retrieval: RetrievalOutcome,
        target: BuildTarget,
        effective_scope: Sequence[str],
    ) -> ProviderRequest:
        """Contexto gobernado que recibe el worker: objetivo, criterios, alcance y experiencia."""
        lines = [
            f"TARGET: {target.target_id}",
            f"OBJECTIVE: {request.objective}",
        ]
        if request.constraints:
            lines.append("CONSTRAINTS: " + " | ".join(request.constraints))
        if request.acceptance_criteria:
            lines.append("ACCEPTANCE CRITERIA: " + " | ".join(request.acceptance_criteria))
        if request.scope_paths:
            declared = " | ".join(request.scope_paths)
            lines.append(f"DECLARED SCOPE (grants no permission): {declared}")
        if effective_scope:
            lines.append("PATHS THAT EXIST IN THE TARGET: " + " | ".join(effective_scope))
        if request.context:
            lines.append(f"REQUESTER CONTEXT: {request.context}")
        block = render_experience_block(retrieval.context)
        if block.strip():
            # El bloque ya viaja con su propia cabecera de procedencia ("evidence, never
            # authority"): no se le añade otra, para que el contexto no diga dos veces lo mismo.
            lines.append(block)
        lines.append("DELIVERABLE: a proposal for human review. You have no authority to apply it.")
        return ProviderRequest(
            role=request.requested_role,
            instructions=WORKER_INSTRUCTIONS,
            request_id=str(request.request_id),
            context="\n\n".join(lines),
            metadata={"target_id": target.target_id, "authority": "PROPOSAL_ONLY"},
        )

    def _retrieve(self, request: BuildRequest) -> RetrievalOutcome:
        """Recupera experiencia **antes** de invocar al proveedor."""
        if self.retriever is None:
            return RetrievalOutcome(
                context=PriorExperienceContext(),
                status=RetrievalStatus.DISABLED,
                detail="sin recuperador configurado",
            )
        query: MemoryQuery = build_memory_query(
            objective=request.objective,
            action=request.requested_role.value,
            files=request.scope_paths,
            context=request.context,
        )
        return self.retriever.retrieve(query)

    def _capability_declared(self, role: ProviderRole, provider: str) -> bool | None:
        """Preflight declarativo: ¿la tabla de capacidades reconoce esta pareja rol/proveedor?

        Es **evidencia**, no un veto: la tabla declara capacidades por proveedor y hoy va por detrás
        de los transportes de suscripción (``openai`` no declara roles, aunque la configuración lo
        asigne a ARCHITECT y su transporte responda). Quien decide —y quien falla cerrado— es el
        router, que devuelve el estado normalizado de la invocación real. Lo que se registra aquí es
        lo que PUNTO sabía **antes** de gastar la llamada.
        """
        if self.capabilities is None:
            return None
        declared_role = _CAPABILITY_ROLE.get(role)
        if declared_role is None:
            return None
        try:
            self.capabilities.require(declared_role, provider)
        except Exception:  # la tabla no reconoce la pareja: se registra, no bloquea
            return False
        return True

    def _validate(
        self,
        *,
        request: BuildRequest,
        provider_result: ProviderResult,
        target: BuildTarget,
        effective_scope: Sequence[str],
    ) -> BuildValidation:
        """Validación determinista de la salida del proveedor: no ejecuta ni interpreta nada."""
        if provider_result.status is not ProviderStatus.SUCCESS:
            return BuildValidation(ValidationVerdict.NOT_RUN)

        issues: list[BuildValidationIssue] = []
        if provider_result.request_id != str(request.request_id):
            issues.append(
                BuildValidationIssue(
                    code="REQUEST_ID_MISMATCH",
                    detail="el resultado del proveedor no corresponde a esta solicitud",
                )
            )
        if provider_result.role is not None and provider_result.role is not request.requested_role:
            issues.append(
                BuildValidationIssue(
                    code="ROLE_MISMATCH", detail="el rol del resultado no es el solicitado"
                )
            )

        content = provider_result.content or ""
        if not content.strip():
            issues.append(
                BuildValidationIssue(code="EMPTY_OUTPUT", detail="el proveedor no devolvió texto")
            )
        if len(content) > self.config.max_proposal_chars:
            issues.append(
                BuildValidationIssue(
                    code="PROPOSAL_TOO_LONG",
                    detail=f"la propuesta supera {self.config.max_proposal_chars} caracteres",
                )
            )
        if _CONTROL_CHARS.search(content):
            issues.append(
                BuildValidationIssue(
                    code="CONTROL_CHARS", detail="la propuesta trae caracteres de control"
                )
            )
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            issues.append(
                BuildValidationIssue(
                    code="SECRET_IN_OUTPUT",
                    detail="la propuesta contiene algo con forma de credencial: se descarta",
                )
            )
        usage = provider_result.usage
        if usage is not None and (
            usage.total_tokens < 0 or usage.prompt_tokens < 0 or usage.completion_tokens < 0
        ):
            issues.append(
                BuildValidationIssue(
                    code="NEGATIVE_USAGE", detail="el consumo declarado es negativo"
                )
            )
        if provider_result.duration_ms < 0:
            issues.append(
                BuildValidationIssue(
                    code="NEGATIVE_DURATION", detail="la duración declarada es negativa"
                )
            )

        lowered = content.casefold()
        if any(claim in lowered for claim in _AUTHORITY_CLAIMS):
            issues.append(
                BuildValidationIssue(
                    code="AUTHORITY_CLAIM",
                    detail="el proveedor afirma algo que solo PUNTO puede decidir",
                )
            )
        if _IMPOSSIBLE_EFFECT.search(content):
            issues.append(
                BuildValidationIssue(
                    code="IMPOSSIBLE_EFFECT_CLAIM",
                    detail="la propuesta declara un efecto que este ciclo no puede producir",
                )
            )

        out_of_scope = self._missing_paths(content, target, effective_scope)
        if out_of_scope:
            issues.append(
                BuildValidationIssue(
                    code="UNKNOWN_PATH_IN_PROPOSAL",
                    detail="la propuesta menciona rutas que no existen en el destino: "
                    + ", ".join(out_of_scope[:5]),
                )
            )

        verdict = ValidationVerdict.VALID if not issues else ValidationVerdict.INVALID
        return BuildValidation(verdict=verdict, issues=tuple(issues), proposal=content)

    @staticmethod
    def _missing_paths(
        content: str, target: BuildTarget, effective_scope: Sequence[str]
    ) -> tuple[str, ...]:
        """Rutas del destino que la propuesta menciona y que **no** existen allí.

        No concede nada: solo detecta que la propuesta habla de ficheros de este repositorio que no
        están donde dice. La comprobación es deliberadamente conservadora, porque un falso positivo
        invalida una propuesta buena:

        - una referencia de una sola palabra sin separador no es una ruta y se ignora;
        - solo cuenta si su **primera parte** existe en la raíz del destino (``src`, `app`, ...). Un
          tipo MIME (``application/json``) o un fragmento de URL no empiezan por una raíz real del
          destino y no se marcan.
        """
        allowed = set(effective_scope)
        try:
            roots = {entry.name for entry in target.repository.iterdir()}
        except OSError:  # un destino ilegible no convierte la propuesta en inválida
            return ()
        found: list[str] = []
        for match in _PATH_REFERENCE.finditer(content):
            path = match.group(1).rstrip(".,;:)")
            if path in allowed or "/" not in path:
                continue
            if path.split("/", 1)[0] not in roots:
                continue
            if (target.repository / path).exists():
                continue
            if path not in found:
                found.append(path)
        return tuple(found)

    def _build_result(
        self,
        *,
        request: BuildRequest,
        provider: str,
        declared: bool | None,
        provider_result: ProviderResult,
        validation: BuildValidation,
        retrieval: RetrievalOutcome,
        duration_ms: int,
    ) -> BuildResult:
        """Normaliza el resultado: estado de PUNTO, salida del proveedor y evidencia PELL."""
        status = self._status_of(provider_result, validation)
        proposal = validation.proposal
        if status is BuildRequestStatus.ACCEPTED and proposal is not None:
            proposal = proposal.strip()
        else:
            proposal = None
        context = retrieval.context
        return BuildResult(
            request_id=request.request_id,
            status=status,
            role=request.requested_role,
            provider=provider_result.provider or provider,
            model=provider_result.model,
            capability_declared=declared,
            provider_status=provider_result.status,
            proposal=proposal,
            validation_status=validation.verdict,
            validation_issues=validation.issues,
            pell_status=retrieval.status,
            trusted_experience_ids=tuple(
                item.id for item in context.verified if item.status is ExperienceStatus.VERIFIED
            ),
            failed_experience_ids=tuple(item.id for item in context.failed),
            usage=provider_result.usage,
            duration_ms=duration_ms,
            error_kind=(
                "" if provider_result.error_kind is None else provider_result.error_kind.value
            ),
            error=_sanitize_text(provider_result.error),
        )

    @staticmethod
    def _status_of(
        provider_result: ProviderResult, validation: BuildValidation
    ) -> BuildRequestStatus:
        """Traduce (estado del proveedor, veredicto de PUNTO) al estado final del ciclo."""
        if provider_result.status is not ProviderStatus.SUCCESS:
            return BuildRequestStatus.PROVIDER_FAILED
        if validation.verdict is ValidationVerdict.INVALID:
            return BuildRequestStatus.INVALID_PROVIDER_OUTPUT
        return BuildRequestStatus.ACCEPTED

    # ------------------------------------------------------------------ auditoría
    def _log(
        self, method: str, request_id: UUID, metadata: Mapping[str, Any], result: AuditResult
    ) -> None:
        """Emite un evento del ciclo si hay registro de auditoría."""
        if self.audit is None:
            return
        getattr(self.audit, method)(
            request_id=request_id, metadata=dict(metadata), result=result, actor=self.actor
        )

    def _audit_accepted(self, request: BuildRequest) -> None:
        """Evento 1: la solicitud entra en el ciclo."""
        self._log(
            "log_build_request_accepted",
            request.request_id,
            {
                "role": request.requested_role.value,
                "target_id": request.target_repository,
                "acceptance_criteria": len(request.acceptance_criteria),
                "constraints": len(request.constraints),
                "scope_paths": len(request.scope_paths),
                "objective_sha256": _fingerprint(request.objective),
                "objective_chars": len(request.objective),
            },
            AuditResult.SUCCESS,
        )

    def _audit_rejected(self, request: BuildRequest, code: str, detail: str) -> None:
        """Evento 0: la solicitud se rechaza en la frontera y nada se invoca después."""
        self._log(
            "log_build_request_rejected",
            request.request_id,
            {
                "code": code,
                "detail": _sanitize_text(detail)[:300],
                "role": request.requested_role.value,
                "target_id": request.target_repository,
                "provider_invoked": False,
            },
            AuditResult.FAILURE,
        )

    def _audit_normalized(
        self,
        request: BuildRequest,
        normalized: str,
        target: BuildTarget,
        effective_scope: Sequence[str],
    ) -> None:
        """Evento 2: forma normalizada, por huella y medidas.

        El alcance se registra por **conteos**: cuántas rutas declaró quien pide y cuántas existen
        de verdad en el destino. Una ruta declarada que no existe no se copia (es texto de la
        solicitud), pero su descarte queda visible como diferencia entre ambos números.
        """
        self._log(
            "log_build_request_normalized",
            request.request_id,
            {
                "form_sha256": _fingerprint(normalized),
                "form_chars": len(normalized),
                "target_id": target.target_id,
                "target_registered": True,
                "max_output_tokens": self.config.max_output_tokens,
                "scope_declared": len(request.scope_paths),
                "scope_effective": len(effective_scope),
                "scope_missing": len(request.scope_paths) - len(effective_scope),
            },
            AuditResult.SUCCESS,
        )

    def _audit_provider_selected(
        self, request: BuildRequest, provider: str, declared: bool | None
    ) -> None:
        """Evento 3: rol resuelto por la configuración, con la comprobación declarativa."""
        self._log(
            "log_build_provider_selected",
            request.request_id,
            {
                "role": request.requested_role.value,
                "provider": provider,
                "capability_declared": declared,
                "fallback": False,
            },
            AuditResult.SUCCESS,
        )

    def _audit_validation(
        self, request: BuildRequest, validation: BuildValidation, result: BuildResult
    ) -> None:
        """Evento 4: veredicto de PUNTO sobre la salida (sin copiar la salida)."""
        self._log(
            "log_build_proposal_validated",
            request.request_id,
            {
                "validation_status": validation.verdict.value,
                "issue_codes": [issue.code for issue in validation.issues],
                "proposal_chars": 0 if validation.proposal is None else len(validation.proposal),
                "proposal_sha256": (
                    ""
                    if validation.proposal is None
                    else _fingerprint(validation.proposal.strip())
                ),
                "authority": result.authority,
            },
            AuditResult.SUCCESS if validation.valid else AuditResult.FAILURE,
        )

    def _audit_completed(self, request: BuildRequest, result: BuildResult) -> None:
        """Evento 5: desenlace del ciclo."""
        self._log(
            "log_build_cycle_completed",
            request.request_id,
            {
                "status": result.status.value,
                "provider": result.provider,
                "provider_status": (
                    "" if result.provider_status is None else result.provider_status.value
                ),
                "validation_status": result.validation_status.value,
                "pell_status": result.pell_status.value,
                "trusted_experience": len(result.trusted_experience_ids),
                "failed_experience": len(result.failed_experience_ids),
                "duration_ms": result.duration_ms,
                "authority": result.authority,
            },
            AuditResult.SUCCESS if result.accepted else AuditResult.FAILURE,
        )


# ---------------------------------------------------------------------------------------------
# Composición por defecto
# ---------------------------------------------------------------------------------------------
#: Variable de entorno que declara los destinos registrados, en JSON:
#: ``{"<target_id>": {"repository": "<ruta>", "scope_roots": ["app", "src"]}}``.
#:
#: Los destinos viven en configuración y **no** en la solicitud: quien pide trabajo no elige a qué
#: repositorio se apunta, solo nombra una clave ya registrada. Tampoco se escribe ninguna ruta local
#: en el código del motor.
BUILD_TARGETS_ENV: Final[str] = "PUNTO_BUILD_TARGETS"

#: Cota de destinos declarables: es configuración, no un catálogo que crezca sin control.
MAX_BUILD_TARGETS: Final[int] = 16


def load_build_targets(environ: Mapping[str, str] | None = None) -> dict[str, BuildTarget]:
    """Lee los destinos registrados de la configuración del entorno.

    Un destino sin ``repository`` utilizable o con una forma inválida **se rechaza** en vez de
    ignorarse: una configuración a medias que se salta en silencio deja el ciclo sin destino y sin
    explicación.

    Raises:
        BuildCycleError: si la variable no es JSON válido, no tiene la forma esperada o un destino
            no declara un directorio existente.
    """
    source = os.environ if environ is None else environ
    raw = source.get(BUILD_TARGETS_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BuildCycleError(f"{BUILD_TARGETS_ENV} no es JSON válido: {error}") from error
    if not isinstance(data, dict):
        raise BuildCycleError(f"{BUILD_TARGETS_ENV} debe ser un objeto JSON de destinos")
    if len(data) > MAX_BUILD_TARGETS:
        raise BuildCycleError(
            f"{BUILD_TARGETS_ENV} declara {len(data)} destinos; el máximo es {MAX_BUILD_TARGETS}"
        )
    targets: dict[str, BuildTarget] = {}
    for key, value in data.items():
        target_id = str(key).strip()
        if not target_id or len(target_id) > 80 or any(char in target_id for char in "/\\"):
            raise BuildCycleError(f"identificador de destino inválido en {BUILD_TARGETS_ENV}")
        if not isinstance(value, dict):
            raise BuildCycleError(f"el destino {target_id!r} no declara un objeto de configuración")
        repository_raw = str(value.get("repository", "")).strip()
        repository = Path(repository_raw)
        if not repository_raw or not repository.is_absolute():
            # Una ruta relativa se resolvería contra el directorio de trabajo del motor: el destino
            # tiene que estar declarado sin ambigüedad, y una cadena vacía no es un destino.
            raise BuildCycleError(
                f"el destino {target_id!r} debe declarar una ruta absoluta de repositorio"
            )
        if not repository.is_dir():
            raise BuildCycleError(
                f"el destino {target_id!r} no apunta a un directorio existente"
            )
        roots_raw = value.get("scope_roots", ())
        if isinstance(roots_raw, str) or not isinstance(roots_raw, (list, tuple)):
            raise BuildCycleError(f"scope_roots de {target_id!r} debe ser una lista")
        roots = []
        for root in roots_raw:
            candidate = str(root).strip().strip("/")
            if not candidate or ".." in candidate.split("/"):
                raise BuildCycleError(f"scope_roots de {target_id!r} contiene una ruta inválida")
            roots.append(candidate)
        targets[target_id] = BuildTarget(
            target_id=target_id, repository=repository, scope_roots=tuple(roots)
        )
    return targets


def default_build_cycle(
    *,
    audit: AuditLogger | None = None,
    environ: Mapping[str, str] | None = None,
) -> BuildCycle:
    """Compone el ciclo con los componentes que ya existen, sin construir ninguno nuevo.

    Router de proveedores (configuración vigente), memoria de experiencia, tabla declarativa de
    capacidades y destinos registrados. Si algo de esto falta, se dice al ejecutar: el ciclo no
    inventa un proveedor ni un destino por su cuenta.
    """
    from punto.memory.store import ExperienceStore
    from punto.workflow.providers import default_capabilities

    registry = ProviderRegistry()
    return BuildCycle(
        router=registry.router_instance(),
        config=BuildCycleConfig(targets=load_build_targets(environ)),
        retriever=MemoryRetriever(ExperienceStore()),
        audit=audit,
        capabilities=default_capabilities(),
    )


__all__ = [
    "BUILD_TARGETS_ENV",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MAX_BUILD_TARGETS",
    "WORKER_INSTRUCTIONS",
    "BuildCycle",
    "BuildCycleConfig",
    "BuildCycleError",
    "BuildTarget",
    "BuildValidation",
    "default_build_cycle",
    "load_build_targets",
]
