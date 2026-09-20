"""EXPERIMENTO 03 (y su ronda de refinación) — CASE-B contra el control congelado y todos los brazos.

Lee la evidencia del arnés (un JSON por brazo) y produce:

- la tabla de las métricas que el encargo pide (§18);
- las métricas de la **primera reparación** recalculadas con el instrumento corregido y **ancladas en
  la ronda 1** (§14), a partir de los eventos de auditoría guardados: no se repite ninguna corrida;
- qué métricas **no** se pueden comparar porque el control no las medía (se dice, no se rellena con
  ceros: un cero inventado sería una conclusión falsa);
- ``experiment-03-0.2-delta.json`` con los números crudos por brazo.

Uso:
    python _punto-skill-layer/compare_focused_resolution.py
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EVIDENCE = Path(__file__).resolve().parent

#: Brazo → fichero de evidencia. El control es el de la capa de skills sin ninguna skill.
BRAZOS: tuple[tuple[str, str], ...] = (
    ("control", "baseline-real.json"),
    ("architect-0.1.0", "baseline-real-skill.json"),
    ("architect-0.2.0+handoff", "baseline-real-round2.json"),
    ("builder-0.1.0", "baseline-real-builder-0.1.0.json"),
    ("resolution-0.1.0", "baseline-real-skill-resolution-0.1.0.json"),
    ("resolution-0.2.0", "baseline-real-skill-resolution-0.2.0.json"),
)

#: (título, ruta dentro del registro) de las métricas comparables.
CAMPOS: tuple[tuple[str, str], ...] = (
    ("status", "status"),
    ("success", "success"),
    ("functional_chain_pass", "functional_chain_pass"),
    ("provider_calls", "provider_calls"),
    ("architect_calls", "provider_calls_by_role.ARCHITECT"),
    ("builder_calls", "provider_calls_by_role.BUILDER"),
    ("repair_rounds", "repair_rounds"),
    ("scope_expansions", "scope_expansions"),
    ("tool_calls", "tool_calls"),
    ("files_read", "files_read"),
    ("files_changed", "files_changed"),
    ("verification_count", "verification_count"),
    ("verification_failures", "verification_failures"),
    ("human_gates", "human_gates"),
    ("prompt_chars", "prompt_chars"),
    ("initial_builder_prompt_chars", "initial_builder_prompt_chars"),
    ("resolution_prompt_chars", "resolution_prompt_chars"),
    ("input_tokens", "tokens.input_tokens"),
    ("output_tokens", "tokens.output_tokens"),
    ("total_tokens", "tokens.total_tokens"),
    ("provider_elapsed_ms", "provider_elapsed_ms"),
    ("elapsed_ms", "elapsed_ms"),
)

#: Métricas de la fase de resolución: el control **no** las medía (no existían).
CAMPOS_RESOLUCION: tuple[tuple[str, str], ...] = (
    ("first_attempt_pass", "first_attempt"),
    ("first_repair_attempted", "first_repair"),
    ("first_repair_reached_verification", "first_repair"),
    ("first_repair_strategy_changed", "first_repair"),
    ("first_repair_addressed_failure_resource", "first_repair"),
    ("first_repair_pass", "first_repair"),
    ("first_repair_scope_expansion_requested", "first_repair"),
    ("first_repair_scope_expansion_approved", "first_repair"),
    ("first_repair_change_same_resource", "first_repair"),
    ("first_repair_change_same_proposal", "first_repair"),
    ("first_repair_change_applied", "first_repair"),
    ("first_repair_focused_pass", "first_repair"),
    ("first_repair_chain_pass", "first_repair"),
    ("causal_stagnation_events", "first_repair"),
    ("repair_2_prompt_chars", "calculado"),
)


class _Evento:
    """Evento de auditoría mínimo, con la forma que lee el arnés."""

    def __init__(self, tipo: str, metadata: Mapping[str, Any]) -> None:
        self.event_type = type("Tipo", (), {"value": tipo})()
        self.metadata = dict(metadata)


class _Resultado:
    """Resultado de ciclo mínimo, para recalcular métricas de evidencia guardada."""

    def __init__(self, applied: Sequence[str], repair_rounds: int) -> None:
        self.applied = tuple(type("Cambio", (), {"path": item})() for item in applied)
        self.repair_rounds = repair_rounds


def _arnes() -> Any:
    """Carga el arnés para reutilizar **su** lógica de métricas, sin duplicarla aquí."""
    ruta = EVIDENCE / "run_baseline.py"
    especificacion = importlib.util.spec_from_file_location("punto_arnes", ruta)
    assert especificacion is not None and especificacion.loader is not None
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def _recalcular(caso: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recalcula las métricas de la primera reparación desde los eventos guardados.

    El instrumento de la corrida de 0.2.0 anclaba la «primera reparación» en el primer registro de
    progreso, que puede ser el de la ronda 2 si la ronda 1 terminó con cambios rechazados (D-6). Aquí
    se recalcula con el instrumento corregido, sobre la misma evidencia: no se repite ninguna corrida.
    """
    eventos = [
        _Evento(str(item["event_type"]), item.get("metadata", {}))
        for item in caso.get("audit_events", [])
    ]
    if not any(evento.event_type.value == "DEV_RESOLUTION_INPUT" for evento in eventos):
        return None
    record = caso.get("record", {})
    return _arnes()._resolution_evidence(
        eventos,
        caso.get("calls_detail", []),
        caso.get("calls_detail", []),
        _Resultado(caso.get("applied", ()), int(record.get("repair_rounds", 0) or 0)),
    )


def _valor(item: Mapping[str, Any], ruta: str) -> Any:
    """Lee un valor anidado con notación de puntos."""
    actual: Any = item
    for parte in ruta.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None
        actual = actual[parte]
    return actual


def _campo(caso: Mapping[str, Any], ruta: str) -> Any:
    """Lee una métrica del registro de eficiencia y, si no está, de la evidencia del caso.

    El estado final del ciclo vive en la evidencia del caso, no en el registro: sin esta búsqueda,
    ``status`` saldría vacío y una tabla con huecos invita a conclusiones falsas.
    """
    valor = _valor(caso.get("record", {}), ruta)
    return valor if valor is not None else _valor(caso, ruta)


def _reparaciones(caso: Mapping[str, Any]) -> list[int]:
    """Caracteres de prompt de cada ronda de reparación, en orden."""
    return [
        int(call.get("prompt_chars", 0))
        for call in caso.get("calls_detail", [])
        if call.get("role") == "BUILDER"
        and call.get("phase") in {"resolution", "repair"}
    ]


def _extra(caso: Mapping[str, Any]) -> dict[str, Any]:
    """Métricas propias de la resolución, con lo que cada brazo sí medía."""
    primera = caso.get("first_attempt", {})
    reparacion = caso.get("first_repair", {})
    rondas = _reparaciones(caso)
    return {
        "first_attempt_pass": primera.get("first_attempt_pass"),
        "first_repair_attempted": reparacion.get("first_repair_attempted"),
        "first_repair_strategy_changed": reparacion.get("first_repair_strategy_changed"),
        "first_repair_addressed_failure_resource": reparacion.get(
            "first_repair_addressed_failure_resource"
        ),
        "first_repair_pass": reparacion.get("first_repair_pass"),
        "causal_stagnation_events": reparacion.get("causal_stagnation_events"),
        "repair_1_prompt_chars": rondas[0] if rondas else None,
        "repair_2_prompt_chars": rondas[1] if len(rondas) > 1 else None,
        "resolution_causal_gap": reparacion.get("first_repair_causal_gap"),
        "resolution_skill": caso.get("resolution_skill", {}),
        "first_attempt_missing_resources": primera.get("missing_resources"),
        "proposals": [
            {
                "phase": call.get("phase"),
                "changes": [c["path"] for c in call.get("proposal", {}).get("changes", [])],
                "unchanged_resources": call.get("proposal", {}).get("unchanged_resources", []),
                "root_cause": call.get("proposal", {}).get("root_cause", ""),
            }
            for call in caso.get("calls_detail", [])
            if call.get("role") == "BUILDER"
        ],
    }


def cargar() -> dict[str, dict[str, Any]]:
    """Carga el caso CASE-B de cada brazo disponible."""
    brazos: dict[str, dict[str, Any]] = {}
    for nombre, fichero in BRAZOS:
        ruta = EVIDENCE / fichero
        if not ruta.is_file():
            print(f"aviso: falta {fichero} para el brazo {nombre}")
            continue
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        caso = next((item for item in datos if item["case"] == "CASE-B"), None)
        if caso is None:
            print(f"aviso: {fichero} no trae CASE-B")
            continue
        brazos[nombre] = caso
    return brazos


def main() -> int:
    """Imprime la comparación y guarda el delta crudo."""
    brazos = cargar()
    if "resolution-0.2.0" not in brazos:
        print("no hay evidencia del brazo de resolución 0.2.0: nada que comparar todavía")
        return 1
    # Se comparan los tres brazos del encargo; los demás se listan como contexto histórico.
    nombres = [nombre for nombre, _ in BRAZOS if nombre in brazos]
    ancho = 16
    print(f"{'métrica':40}" + "".join(f"{nombre[:ancho]:>{ancho}}" for nombre in nombres))
    for titulo, ruta in CAMPOS:
        fila = "".join(f"{str(_campo(brazos[nombre], ruta)):>{ancho}}" for nombre in nombres)
        print(f"{titulo:40}{fila}")
    print()
    print(f"{'métrica de resolución':40}" + "".join(f"{n[:ancho]:>{ancho}}" for n in nombres))
    extras = {nombre: _extra(brazos[nombre]) for nombre in nombres}
    recalculo = {nombre: _recalcular(brazos[nombre]) for nombre in nombres}
    for titulo, _ in CAMPOS_RESOLUCION:
        fila = ""
        for nombre in nombres:
            valor = extras[nombre].get(titulo)
            if valor is None and recalculo[nombre] is not None:
                valor = recalculo[nombre].get(titulo)
            fila += f"{('—' if valor is None else str(valor)):>{ancho}}"
        print(f"{titulo:40}{fila}")

    print("\npropuestas del BUILDER por brazo:")
    for nombre in nombres:
        print(f"  {nombre}:")
        for indice, propuesta in enumerate(extras[nombre]["proposals"]):
            print(
                f"    [{indice}] {propuesta['phase']}: cambios={propuesta['changes']} "
                f"sin_cambio={propuesta['unchanged_resources']}"
            )

    delta = {
        "brazos": nombres,
        "metricas": {
            titulo: {nombre: _campo(brazos[nombre], ruta) for nombre in nombres}
            for titulo, ruta in CAMPOS
        },
        "metricas_resolucion": {
            titulo: {
                nombre: (
                    extras[nombre].get(titulo)
                    if extras[nombre].get(titulo) is not None
                    else (recalculo[nombre] or {}).get(titulo)
                )
                for nombre in nombres
            }
            for titulo, _ in CAMPOS_RESOLUCION
        },
        "metricas_resolucion_recalculadas": recalculo,
        "propuestas": {nombre: extras[nombre]["proposals"] for nombre in nombres},
    }
    salida = EVIDENCE / "experiment-03-0.2-delta.json"
    salida.write_text(json.dumps(delta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nevidencia: {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
