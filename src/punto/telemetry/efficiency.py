"""Instrumentación **pasiva** de eficiencia (SKILL-LAYER-0).

Objetivo de esta capa: medir cómo trabaja PUNTO **hoy**, como control del futuro experimento A/B.
No decide nada, no cambia prompts, no elige proveedor, no toca autoridad y no interviene en el
ciclo: se limita a **derivar** un registro auditable de la evidencia que el motor ya produce
(eventos de auditoría, resultado del ciclo, `ProviderResult` de cada llamada) y a serializarlo de
forma determinista.

Reglas de medición que se respetan aquí:

- **Tokens reales** solo si el transporte los expone (``usage`` del proveedor). Si no los expone,
  se registran como ``None`` con ``source="UNAVAILABLE"``: nunca se inventan ni se convierten
  caracteres en «tokens estimados».
- **Proxies observables** para transportes de suscripción: número de llamadas, caracteres de
  prompt y de contexto, turnos, reintentos de transporte y duración medida.
- **Sin contenido sensible**: se guardan cuentas, no prompts; y el serializador rechaza cualquier
  cadena con forma de credencial en lugar de escribirla.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from punto.common import utc_now

__all__ = [
    "EfficiencyRecord",
    "ProviderCall",
    "RunEvidence",
    "TokenUsage",
    "build_record",
    "record_line",
    "records_jsonl",
    "write_jsonl",
]

#: De dónde salen los tokens. ``ESTIMATED`` existe en el vocabulario pero **no se usa**: una
#: estimación experimental no puede mezclarse con consumo real, así que esta capa no estima nada.
TokenSource = Literal["REAL", "UNAVAILABLE", "ESTIMATED"]

class TokenUsage(BaseModel):
    """Consumo de tokens de una llamada o de una ejecución, con su procedencia."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    source: TokenSource = "UNAVAILABLE"

    @property
    def available(self) -> bool:
        """True si el transporte expuso consumo real."""
        return self.source == "REAL" and self.total_tokens is not None


class ProviderCall(BaseModel):
    """Una llamada a un proveedor, tal como la observó el router (sin alterarla)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(max_length=40)
    provider: str = Field(max_length=40)
    model: str = Field(default="", max_length=120)
    transport: str = Field(default="", max_length=40)
    status: str = Field(default="", max_length=40)
    phase: str = Field(
        default="",
        max_length=40,
        description="planificación, implementación, reparación o revisión, si es observable.",
    )
    duration_ms: int | None = Field(default=None, ge=0)
    transport_retries: int = Field(default=0, ge=0)
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    prompt_chars: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)


class RunEvidence(BaseModel):
    """Evidencia cruda de una ejecución, ya extraída de la auditoría y del resultado del ciclo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(max_length=64)
    case_id: str = Field(max_length=40)
    case_kind: str = Field(max_length=40, description="A..F, el tipo de trabajo del baseline.")
    mode: Literal["DETERMINISTIC", "REAL"]
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int = Field(ge=0)
    provider_calls: tuple[ProviderCall, ...] = ()
    tool_calls: int = Field(default=0, ge=0)
    repair_rounds: int = Field(default=0, ge=0)
    plan_revisions: int = Field(default=0, ge=0)
    scope_expansions: int = Field(default=0, ge=0)
    files_discovered: int = Field(default=0, ge=0)
    files_read: int = Field(default=0, ge=0)
    files_changed: int = Field(default=0, ge=0)
    verification_count: int = Field(default=0, ge=0)
    verification_failures: int = Field(default=0, ge=0)
    functional_chain_pass: bool = False
    pell_retrievals: int = Field(default=0, ge=0)
    pell_hits: int = Field(default=0, ge=0)
    human_gates: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)
    final_status: str = Field(default="", max_length=60)
    success: bool = False
    regression_detected: bool = False
    correctable_defects_created: int = Field(default=0, ge=0)
    human_gate_required: bool = False
    rollback_required: bool = False
    notes: tuple[str, ...] = ()


class EfficiencyRecord(BaseModel):
    """Una ejecución medida: hechos, no una puntuación opaca."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(max_length=64)
    task_id: str = Field(max_length=64)
    case_id: str = Field(max_length=40)
    case_kind: str = Field(max_length=40)
    mode: Literal["DETERMINISTIC", "REAL"]

    primary_role: str = Field(default="", max_length=40)
    primary_provider: str = Field(default="", max_length=40)
    primary_model: str = Field(default="", max_length=120)
    primary_transport: str = Field(default="", max_length=40)

    #: Skill del BUILDER (SKILL-LAYER-0, experimento 02): identificador, versión y tamaño.
    builder_skill_id: str = Field(default="", max_length=80)
    builder_skill_version: str = Field(default="", max_length=20)
    builder_skill_activated: bool = False
    builder_skill_chars: int = Field(default=0, ge=0)

    #: Skill de **resolución** (EXPERIMENTO 03): la que solo actúa cuando una verificación real ya
    #: falló. Se registra aparte de la del BUILDER para que no se puedan confundir.
    resolution_skill_id: str = Field(default="", max_length=80)
    resolution_skill_version: str = Field(default="", max_length=20)
    resolution_skill_activated: bool = False
    resolution_skill_chars: int = Field(default=0, ge=0)

    #: Handoff causal plan → BUILDER: si viajó y cuánto ocupó (SKILL-LAYER-0, ronda 2).
    causal_handoff_present: bool = False
    causal_handoff_chars: int = Field(default=0, ge=0)

    #: Skill activada en esta ejecución (SKILL-LAYER-0). Vacío y ``False`` significan el control.
    skill_id: str = Field(default="", max_length=80)
    skill_version: str = Field(default="", max_length=20)
    skill_activated: bool = False

    started_at: datetime
    finished_at: datetime
    elapsed_ms: int = Field(ge=0)
    provider_elapsed_ms: int = Field(default=0, ge=0)
    verification_elapsed_ms: int = Field(default=0, ge=0)

    provider_calls: int = Field(default=0, ge=0)
    provider_calls_by_role: dict[str, int] = Field(default_factory=dict)
    provider_calls_by_phase: dict[str, int] = Field(default_factory=dict)
    tool_calls: int = Field(default=0, ge=0)
    transport_retries: int = Field(default=0, ge=0)

    repair_rounds: int = Field(default=0, ge=0)
    plan_revisions: int = Field(default=0, ge=0)
    scope_expansions: int = Field(default=0, ge=0)
    stagnation_events: int = Field(default=0, ge=0)
    repeated_failure_signatures: int = Field(default=0, ge=0)

    files_discovered: int = Field(default=0, ge=0)
    files_read: int = Field(default=0, ge=0)
    files_changed: int = Field(default=0, ge=0)
    context_requests: int = Field(default=0, ge=0)

    verification_count: int = Field(default=0, ge=0)
    verification_failures: int = Field(default=0, ge=0)
    functional_chain_verifications: int = Field(default=0, ge=0)
    functional_chain_pass: bool = False

    pell_retrievals: int = Field(default=0, ge=0)
    pell_hits: int = Field(default=0, ge=0)
    human_gates: int = Field(default=0, ge=0)

    tokens: TokenUsage = Field(default_factory=TokenUsage)
    prompt_chars: int = Field(default=0, ge=0)
    #: Prompt de la **primera** implementación y de las invocaciones de **resolución**, medidos por
    #: separado: es la única forma de comparar el coste de resolver contra el de implementar.
    initial_builder_prompt_chars: int = Field(default=0, ge=0)
    resolution_prompt_chars: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)

    success: bool = False
    final_status: str = Field(default="", max_length=60)
    regression_detected: bool = False
    correctable_defects_created: int = Field(default=0, ge=0)
    human_gate_required: bool = False
    rollback_required: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin contenido de prompts."""
        return self.model_dump(mode="json")


def _aggregate_tokens(calls: tuple[ProviderCall, ...]) -> TokenUsage:
    """Suma el consumo **real** observado; si ningún transporte lo expone, queda no disponible.

    Nunca se completa con estimaciones: mezclar consumo real con estimado haría la comparación del
    A/B imposible de interpretar.
    """
    real = [call.tokens for call in calls if call.tokens.available]
    if not real:
        return TokenUsage()
    return TokenUsage(
        input_tokens=sum(item.input_tokens or 0 for item in real),
        output_tokens=sum(item.output_tokens or 0 for item in real),
        total_tokens=sum(item.total_tokens or 0 for item in real),
        source="REAL",
    )


def _phase_counts(calls: tuple[ProviderCall, ...]) -> dict[str, int]:
    """Llamadas por fase observable (planificación, implementación, reparación, revisión)."""
    counts: dict[str, int] = {}
    for call in calls:
        key = call.phase or "unspecified"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _role_counts(calls: tuple[ProviderCall, ...]) -> dict[str, int]:
    """Llamadas por rol."""
    counts: dict[str, int] = {}
    for call in calls:
        counts[call.role] = counts.get(call.role, 0) + 1
    return dict(sorted(counts.items()))


def _prompt_chars_for_phase(calls: tuple[ProviderCall, ...], phase: str) -> int:
    """Caracteres de prompt enviados en una fase observable, sumando las llamadas de esa fase."""
    return sum(call.prompt_chars for call in calls if call.phase == phase)


def _first_prompt_chars(
    calls: tuple[ProviderCall, ...], role: str, phases: frozenset[str]
) -> int:
    """Caracteres del prompt de la primera llamada de un rol en las fases indicadas."""
    for call in calls:
        if call.role == role and call.phase in phases:
            return call.prompt_chars
    return 0


def build_record(
    evidence: RunEvidence,
    *,
    task_id: str = "",
    primary_role: str = "",
    primary_provider: str = "",
    primary_model: str = "",
    primary_transport: str = "",
    builder_skill_id: str = "",
    builder_skill_version: str = "",
    builder_skill_activated: bool = False,
    builder_skill_chars: int = 0,
    resolution_skill_id: str = "",
    resolution_skill_version: str = "",
    resolution_skill_activated: bool = False,
    resolution_skill_chars: int = 0,
    causal_handoff_present: bool = False,
    causal_handoff_chars: int = 0,
    skill_id: str = "",
    skill_version: str = "",
    skill_activated: bool = False,
    verification_elapsed_ms: int = 0,
    stagnation_events: int = 0,
    repeated_failure_signatures: int = 0,
    context_requests: int = 0,
    functional_chain_verifications: int = 0,
) -> EfficiencyRecord:
    """Convierte la evidencia de una ejecución en su registro de eficiencia.

    Es una función pura: no lee del motor, no escribe nada y no puede alterar el resultado medido.
    """
    calls = evidence.provider_calls
    return EfficiencyRecord(
        run_id=evidence.run_id,
        task_id=task_id or evidence.run_id,
        case_id=evidence.case_id,
        case_kind=evidence.case_kind,
        mode=evidence.mode,
        primary_role=primary_role,
        primary_provider=primary_provider,
        primary_model=primary_model,
        primary_transport=primary_transport,
        builder_skill_id=builder_skill_id,
        builder_skill_version=builder_skill_version,
        builder_skill_activated=builder_skill_activated,
        builder_skill_chars=builder_skill_chars,
        resolution_skill_id=resolution_skill_id,
        resolution_skill_version=resolution_skill_version,
        resolution_skill_activated=resolution_skill_activated,
        resolution_skill_chars=resolution_skill_chars,
        causal_handoff_present=causal_handoff_present,
        causal_handoff_chars=causal_handoff_chars,
        skill_id=skill_id,
        skill_version=skill_version,
        skill_activated=skill_activated,
        started_at=evidence.started_at,
        finished_at=evidence.finished_at,
        elapsed_ms=evidence.elapsed_ms,
        provider_elapsed_ms=sum(call.duration_ms or 0 for call in calls),
        verification_elapsed_ms=verification_elapsed_ms,
        provider_calls=len(calls),
        provider_calls_by_role=_role_counts(calls),
        provider_calls_by_phase=_phase_counts(calls),
        tool_calls=evidence.tool_calls,
        transport_retries=sum(call.transport_retries for call in calls),
        repair_rounds=evidence.repair_rounds,
        plan_revisions=evidence.plan_revisions,
        scope_expansions=evidence.scope_expansions,
        stagnation_events=stagnation_events,
        repeated_failure_signatures=repeated_failure_signatures,
        files_discovered=evidence.files_discovered,
        files_read=evidence.files_read,
        files_changed=evidence.files_changed,
        context_requests=context_requests,
        verification_count=evidence.verification_count,
        verification_failures=evidence.verification_failures,
        functional_chain_verifications=functional_chain_verifications,
        functional_chain_pass=evidence.functional_chain_pass,
        pell_retrievals=evidence.pell_retrievals,
        pell_hits=evidence.pell_hits,
        human_gates=evidence.human_gates,
        tokens=_aggregate_tokens(calls),
        prompt_chars=sum(call.prompt_chars for call in calls),
        initial_builder_prompt_chars=_first_prompt_chars(
            calls, "BUILDER", frozenset({"implementation"})
        ),
        resolution_prompt_chars=_prompt_chars_for_phase(calls, "resolution"),
        context_chars=evidence.context_chars or sum(call.context_chars for call in calls),
        success=evidence.success,
        final_status=evidence.final_status,
        regression_detected=evidence.regression_detected,
        correctable_defects_created=evidence.correctable_defects_created,
        human_gate_required=evidence.human_gate_required,
        rollback_required=evidence.rollback_required,
        notes=evidence.notes,
    )


def _assert_no_secrets(line: str) -> None:
    """Rechaza escribir una línea con forma de credencial.

    Se reutiliza el catálogo de patrones del **propio motor** (``punto.security.deterministic``) en
    lugar de inventar marcadores: así la frontera de secretos es la misma en todas partes. Los
    patrones son de forma (``sk-`` + 16 caracteres, DSN con credenciales, bloque de clave privada…),
    no subcadenas sueltas: un identificador como ``task-1`` no es un secreto.

    Raises:
        ValueError: si la línea contiene un secreto con forma de credencial.
    """
    from punto.security.deterministic import SECRET_PATTERNS

    for name, pattern, _severity in SECRET_PATTERNS:
        if pattern.search(line):
            raise ValueError(
                f"la línea de eficiencia contiene un secreto con forma de credencial ({name}): "
                "no se escribe"
            )


def record_line(record: EfficiencyRecord) -> str:
    """Una línea JSONL determinista para un registro."""
    line = json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True)
    _assert_no_secrets(line)
    return line


def records_jsonl(records: list[EfficiencyRecord]) -> str:
    """JSONL determinista: mismas entradas, mismo texto, en el orden recibido."""
    return "".join(f"{record_line(record)}\n" for record in records)


def write_jsonl(path: Path, records: list[EfficiencyRecord]) -> int:
    """Escribe el JSONL y devuelve los bytes escritos."""
    payload = records_jsonl(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return len(payload.encode("utf-8"))


def now() -> datetime:
    """Instante actual en UTC (para los límites de una ejecución medida)."""
    return utc_now()
