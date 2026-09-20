"""EXPERIMENTO 03 — sobrecarga estática de la skill de resolución, medida **sin proveedor**.

Calcula, con el mismo montaje de CASE-B y el mismo motor, lo que la resolución añade al prompt:

- ``resolution_skill_chars``: el procedimiento que se añade a las instrucciones del rol.
- ``resolution_context_chars``: el bloque de resolución que se añade al contexto.
- ``causal_handoff_chars``: lo que ya viajaba antes y sigue viajando igual.
- ``expected_resolution_prompt_chars``: el prompt de resolución esperado, medido con los ficheros
  reales del montaje.
- ``duplicated_procedural_chars``: caracteres del bloque que repiten literalmente procedimiento ya
  presente en el prompt (esperado: 0).

Escribe ``_punto-skill-layer/overhead-focused.json`` y lo imprime en pantalla.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parent.parent

FOCUSED = (
    "import pathlib,sys;"
    "texto=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "print('TIPOS:', texto.strip()[:80]);"
    "sys.exit(0 if 'Apartamento' in texto else 1)"
)
CHAIN = (
    "import pathlib,sys;"
    "fuente=pathlib.Path('src/lib/tipos.ts').read_text(encoding='utf-8');"
    "consumidores=[pathlib.Path(p) for p in "
    "('src/components/Rejilla.tsx','src/components/Buscador.tsx')];"
    "ok=all('@/lib/tipos' in c.read_text(encoding='utf-8') for c in consumidores);"
    "print('CADENA:', ok);"
    "sys.exit(0 if ok and 'Apartamento' in fuente else 1)"
)

TIPOS = "export const TIPOS = ['Casa'];\n"
REJILLA = "const tipos = ['Casa'];\nexport function Rejilla() { return tipos.length; }\n"
BUSCADOR = "const tipos = ['Casa'];\nexport function Buscador() { return tipos.length; }\n"


def _bloques() -> dict[str, Any]:
    """Construye el prompt de resolución del montaje con las piezas reales del motor."""
    from punto.orchestrator import dev_cycle
    from punto.orchestrator.focused_resolution import (
        RESOLUTION_INPUT_LABEL,
        duplicated_chars,
        failure_map,
        resolution_block,
    )
    from punto.orchestrator.dev_cycle import (
        BUILD_CONTRACT,
        CAUSAL_HANDOFF_LABEL,
        WORKER_INSTRUCTIONS,
        CAUSAL_HANDOFF_LABEL as _handoff_label,
    )
    from punto.schemas.build import BuildRequest
    from punto.providers.contract import ProviderRole
    from punto.schemas.dev import CommandEvidence, DevelopmentPlan, FunctionalChainStep
    from punto.skills import load_skill
    from punto.workspace.target import DevelopmentTarget, VerificationCommand

    clase = dev_cycle.ContextFile
    plan = DevelopmentPlan(
        summary="unificar la lista de tipos en una sola fuente",
        files_to_read=("src/lib/tipos.ts", "src/components/Rejilla.tsx"),
        files_to_modify=(
            "src/lib/tipos.ts",
            "src/components/Rejilla.tsx",
            "src/components/Buscador.tsx",
        ),
        verification_commands=("focused", "chain"),
        risks=("cambiar la interfaz sin querer",),
        acceptance_mapping=("una sola fuente de tipos",),
        functional_chain=(
            FunctionalChainStep(step="fuente canónica", verification="focused"),
            FunctionalChainStep(step="consumidores", verification="chain"),
        ),
    )
    target = DevelopmentTarget(
        target_id="baseline-fixture",
        repository=ENGINE,
        baseline_sha="",
        scope_roots=("src", "tests"),
        verification=(
            VerificationCommand(name="focused", argv=("python", "-c", FOCUSED), timeout_seconds=60.0),
            VerificationCommand(name="chain", argv=("python", "-c", CHAIN), timeout_seconds=60.0),
        ),
        work_branch="ai/skill-layer-baseline",
    )
    fallo = (
        CommandEvidence(
            name="focused",
            argv=("python", "-c", FOCUSED),
            exit_code=1,
            duration_ms=12,
            output_excerpt="TIPOS: export const TIPOS = ['Casa']",
            passed=False,
        ),
        CommandEvidence(
            name="chain",
            argv=("python", "-c", CHAIN),
            exit_code=1,
            duration_ms=12,
            output_excerpt="CADENA: False",
            passed=False,
        ),
    )
    mapa = failure_map(fallo, target, plan)
    bloque = resolution_block(
        round_index=1,
        failure=mapa,
        previous_patch=("src/components/Buscador.tsx", "src/components/Rejilla.tsx"),
        previous_strategy="src/components/Buscador.tsx:MODIFY|src/components/Rejilla.tsx:MODIFY",
        causal_gap=("src/lib/tipos.ts",),
        stagnation=False,
    )
    contexto = [
        clase(path="src/lib/tipos.ts", sha256="0" * 64, content=TIPOS),
        clase(path="src/components/Rejilla.tsx", sha256="0" * 64, content=REJILLA),
        clase(path="src/components/Buscador.tsx", sha256="0" * 64, content=BUSCADOR),
    ]
    peticion = BuildRequest(
        objective="diseñar la fuente canónica de tipos y su cadena funcional completa",
        target_repository="baseline-fixture",
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("una sola fuente de tipos",),
        scope_paths=("src",),
    )
    ciclo = dev_cycle.DevelopmentCycle.__new__(dev_cycle.DevelopmentCycle)
    ciclo.config = dev_cycle.DevelopmentConfig()
    prompt = ciclo._build_prompt(
        peticion, target, plan, contexto, "", "focused exit=1\nTIPOS: export const TIPOS = ['Casa']",
        resolution=bloque,
    )
    sin_bloque = prompt.replace(bloque, "")
    cuerpo = load_skill("punto-focused-resolution@0.1.0")
    handoff = dev_cycle.causal_handoff(plan)
    return {
        "prompt": prompt,
        "block": bloque,
        "sin_bloque": sin_bloque,
        "skill": cuerpo,
        "handoff": handoff,
        "label": RESOLUTION_INPUT_LABEL,
        "labels": (CAUSAL_HANDOFF_LABEL,),
        "handoff_label": _handoff_label,
        "procedural": (WORKER_INSTRUCTIONS, BUILD_CONTRACT, cuerpo.body),
        "duplicated": duplicated_chars,
    }


def main() -> int:
    """Mide y persiste la sobrecarga estática del experimento."""
    datos = _bloques()
    bloque: str = datos["block"]
    prompt: str = datos["prompt"]
    sin_bloque: str = datos["sin_bloque"]
    skill = datos["skill"]
    handoff: str = datos["handoff"]
    duplicated = datos["duplicated"]
    procedimiento = datos["procedural"]

    informe = {
        "skill_id": skill.skill_id,
        "skill_version": skill.version,
        "skill_role": skill.role,
        "skill_sha256": skill.sha256,
        "resolution_skill_chars": skill.chars,
        "resolution_context_chars": len(bloque),
        "causal_handoff_chars": len(handoff),
        "expected_resolution_prompt_chars": len(prompt),
        "expected_initial_prompt_chars": len(sin_bloque),
        "duplicated_procedural_chars": duplicated(bloque, procedimiento),
        "duplicated_context_chars": duplicated(bloque, (sin_bloque,)),
        "resolution_lines": len(bloque.splitlines()),
        "labels_present_once": {
            "resolution_input": prompt.count(datos["label"]) == 1,
            "causal_handoff": prompt.count(datos["handoff_label"]) == 1,
        },
        "resolution_block": bloque,
    }
    salida = Path(__file__).resolve().parent / "overhead-focused.json"
    salida.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    for clave, valor in informe.items():
        if clave == "resolution_block":
            continue
        print(f"{clave}: {valor}")
    print(f"\nevidencia: {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
