"""Corrige la causa del naming del arnés (dos skills) y muestra el plan persistido del experimento."""

from __future__ import annotations

import json
import pathlib

HARNESS = pathlib.Path(__file__).resolve().parent / "run_baseline.py"
EVIDENCE = pathlib.Path(__file__).resolve().parent

OLD = """    suffix = ""
    if skill:
        version = skill.partition("@")[2] or "sin-version"
        suffix = f"-skill-{version}"
    evidence_name = f"baseline-{mode.lower()}{suffix}.json"
"""
NEW = """    # El nombre lleva la versión de **cada** skill declarada: mirar solo la del ARCHITECT hizo que
    # una corrida del BUILDER pisara la evidencia del control (defecto detectado en el experimento 02).
    partes: list[str] = []
    if skill:
        partes.append(f"architect-{skill.partition('@')[2] or 'sin-version'}")
    if os.environ.get("PUNTO_BUILDER_SKILL", "").strip():
        builder = os.environ["PUNTO_BUILDER_SKILL"].strip()
        partes.append(f"builder-{builder.partition('@')[2] or 'sin-version'}")
    suffix = f"-skill-{'-'.join(partes)}" if partes else ""
    evidence_name = f"baseline-{mode.lower()}{suffix}.json"
"""


def main() -> None:
    """Aplica el arreglo y muestra el plan del experimento."""
    text = HARNESS.read_text(encoding="utf-8")
    if OLD not in text:
        raise SystemExit("no encontrado el bloque de naming")
    HARNESS.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print("arnés corregido\n")

    exp = json.loads(
        (EVIDENCE / "baseline-real-builder-0.1.0.json").read_text(encoding="utf-8")
    )[0]
    plan = exp.get("plan") or {}
    print("plan del ARCHITECT (persistido):")
    print("  files_to_modify:", plan.get("files_to_modify"))
    print("  files_to_create:", plan.get("files_to_create"))
    print("  verification:", plan.get("verification_commands"))
    print("  chain:", [step.get("step") for step in plan.get("functional_chain", [])])
    print("  acceptance:", plan.get("acceptance_mapping"))
    print("\nhandoff enviado:", exp.get("causal_handoff", "")[:220])
    print("\npropuestas del BUILDER (resumen):")
    for call in exp.get("calls_detail", []):
        if call["role"] != "BUILDER":
            continue
        propuesta = call.get("proposal", {})
        rutas = [f"{c.get('operation')} {c.get('path')}" for c in propuesta.get("changes", [])]
        print(
            f"  fase={call['phase']:14} chars={call['prompt_chars']:6} "
            f"cambios={rutas} root_cause={bool(propuesta.get('root_cause'))}"
        )


if __name__ == "__main__":
    main()
