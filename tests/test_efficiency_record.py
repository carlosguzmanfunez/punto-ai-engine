"""Pruebas de la instrumentación de eficiencia (SKILL-LAYER-0).

Qué demuestran, y solo eso:

- el registro se serializa de forma determinista (mismo contenido, mismo JSONL);
- los tokens son **reales o no disponibles**: nunca se inventan ni se mezclan con estimaciones;
- los conteos (llamadas por rol y fase, tiempos, caracteres) se derivan de la evidencia observada;
- no se escribe contenido con forma de credencial;
- la instrumentación es **pasiva**: construir el registro no toca el resultado medido.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from punto.common import utc_now
from punto.telemetry import (
    ProviderCall,
    RunEvidence,
    TokenUsage,
    build_record,
    record_line,
    records_jsonl,
)


def _call(**overrides: object) -> ProviderCall:
    """Llamada a proveedor de ejemplo."""
    payload: dict[str, object] = {
        "role": "BUILDER",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "transport": "existing",
        "status": "SUCCESS",
        "phase": "implementation",
        "duration_ms": 1200,
        "prompt_chars": 900,
        "context_chars": 400,
    }
    payload.update(overrides)
    return ProviderCall(**payload)  # type: ignore[arg-type]


def _evidence(calls: tuple[ProviderCall, ...]) -> RunEvidence:
    """Evidencia de una ejecución medida."""
    started = utc_now()
    return RunEvidence(
        run_id="run-1",
        case_id="CASE-D",
        case_kind="D",
        mode="REAL",
        started_at=started,
        finished_at=started + timedelta(milliseconds=5000),
        elapsed_ms=5000,
        provider_calls=calls,
        tool_calls=7,
        repair_rounds=1,
        files_discovered=26,
        files_read=4,
        files_changed=2,
        verification_count=3,
        verification_failures=1,
        functional_chain_pass=True,
        pell_retrievals=1,
        pell_hits=1,
        context_chars=400,
        success=True,
        final_status="DEVELOPMENT_COMPLETED",
    )


def test_serializacion_determinista() -> None:
    """El mismo registro produce exactamente el mismo JSONL."""
    record = build_record(_evidence((_call(),)), task_id="task-1")

    assert record_line(record) == record_line(record)
    assert records_jsonl([record]) == f"{record_line(record)}\n"
    assert json.loads(record_line(record))["case_id"] == "CASE-D"


def test_tokens_no_disponibles_no_se_inventan() -> None:
    """Un transporte que no expone consumo deja los tokens en ``None`` y lo declara."""
    record = build_record(_evidence((_call(tokens=TokenUsage()),)))

    assert record.tokens.input_tokens is None
    assert record.tokens.output_tokens is None
    assert record.tokens.total_tokens is None
    assert record.tokens.source == "UNAVAILABLE"
    assert record.tokens.available is False


def test_tokens_reales_cuando_el_transporte_los_expone() -> None:
    """Con consumo real, se suma el de las llamadas que lo exponen."""
    real = TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120, source="REAL")
    otra = TokenUsage(input_tokens=50, output_tokens=10, total_tokens=60, source="REAL")
    record = build_record(_evidence((_call(tokens=real), _call(tokens=otra))))

    assert record.tokens.source == "REAL"
    assert (record.tokens.input_tokens, record.tokens.output_tokens) == (150, 30)
    assert record.tokens.total_tokens == 180


def test_una_llamada_sin_consumo_no_contamina_el_total_real() -> None:
    """Si una llamada no expone tokens, el agregado sigue siendo el real de las demás."""
    real = TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120, source="REAL")
    record = build_record(
        _evidence((_call(tokens=real), _call(role="ARCHITECT", tokens=TokenUsage())))
    )

    assert record.tokens.total_tokens == 120
    assert record.tokens.source == "REAL"


def test_conteo_de_llamadas_por_rol_y_fase() -> None:
    """Las llamadas se cuentan por rol y por fase observable."""
    calls = (
        _call(role="ARCHITECT", phase="planning"),
        _call(role="BUILDER", phase="implementation"),
        _call(role="BUILDER", phase="repair"),
    )
    record = build_record(_evidence(calls))

    assert record.provider_calls == 3
    assert record.provider_calls_by_role == {"ARCHITECT": 1, "BUILDER": 2}
    assert record.provider_calls_by_phase == {
        "implementation": 1,
        "planning": 1,
        "repair": 1,
    }


def test_tiempos_derivados_de_la_evidencia() -> None:
    """El tiempo de proveedor es la suma de las llamadas; el total viene de la ejecución."""
    record = build_record(
        _evidence((_call(duration_ms=1200), _call(duration_ms=800))),
        verification_elapsed_ms=2500,
    )

    assert record.elapsed_ms == 5000
    assert record.provider_elapsed_ms == 2000
    assert record.verification_elapsed_ms == 2500


def test_caracteres_de_prompt_y_contexto() -> None:
    """Los caracteres medidos son los realmente enviados, sumados por llamada."""
    record = build_record(
        _evidence((_call(prompt_chars=900, context_chars=400), _call(prompt_chars=100)))
    )

    assert record.prompt_chars == 1000
    assert record.context_chars == 400


def test_no_se_escribe_contenido_con_forma_de_credencial() -> None:
    """El serializador rechaza una línea con forma de secreto en lugar de escribirla."""
    from punto.telemetry import EfficiencyRecord

    record = build_record(_evidence((_call(),)))
    contaminado = EfficiencyRecord(
        **{**record.as_dict(), "notes": ("api_key=sk-0123456789abcdef0123456789ab",)}
    )

    with pytest.raises(ValueError, match="secreto"):
        record_line(contaminado)


def test_la_instrumentacion_no_altera_el_resultado_medido() -> None:
    """Construir el registro es una función pura: no modifica la evidencia ni mide dos veces."""
    evidence = _evidence((_call(),))
    antes = evidence.model_dump(mode="json")

    build_record(evidence, task_id="task-1")
    build_record(evidence, task_id="task-2")

    assert evidence.model_dump(mode="json") == antes


def test_la_evidencia_declara_calidad_junto_a_eficiencia() -> None:
    """El registro guarda calidad (éxito, cadena funcional, regresión) además de eficiencia."""
    record = build_record(_evidence((_call(),)))

    assert record.success is True
    assert record.functional_chain_pass is True
    assert record.regression_detected is False
    assert record.repair_rounds == 1


def test_el_registro_identifica_la_skill_activada() -> None:
    """El registro dice qué skill y qué versión produjeron el resultado (o que no hubo ninguna)."""
    con_skill = build_record(
        _evidence((_call(),)),
        task_id="task-1",
        skill_id="punto-causal-architect",
        skill_version="0.1.0",
        skill_activated=True,
    )
    assert con_skill.skill_id == "punto-causal-architect"
    assert con_skill.skill_version == "0.1.0"
    assert con_skill.skill_activated is True

    sin_skill = build_record(_evidence((_call(),)))
    assert sin_skill.skill_id == ""
    assert sin_skill.skill_activated is False
