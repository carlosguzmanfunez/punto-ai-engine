"""EXPERIMENTO 03 · ronda de refinación — sobrecarga estática de 0.1.0 frente a 0.2.0, sin proveedor.

Mide, con el mismo montaje de CASE-B y el mismo motor:

- ``skill_chars`` de cada versión y su **delta**;
- ``resolution_context_chars``: el bloque de resolución (no depende de la versión de la skill);
- ``expected_resolution_prompt_chars``: contexto de resolución y prompt efectivo (contexto +
  instrucciones del rol + skill), con la misma convención que el arnés;
- ``duplicated_procedural_chars`` y ``duplicated_context_chars``: lo que el bloque repite de lo que ya
  viajaba en el prompt (esperado 0).

Escribe ``_punto-skill-layer/overhead-focused-0.2.0.json`` y **no** toca la medición de 0.1.0
(``overhead-focused.json``), que queda como evidencia de la ronda anterior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ENGINE = Path(__file__).resolve().parent.parent

VERSIONES = ("punto-focused-resolution@0.1.0", "punto-focused-resolution@0.2.0")

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


def _medir(referencia: str) -> dict[str, Any]:
    """Mide la sobrecarga estática de una versión de la skill."""
    from punto.orchestrator import dev_cycle
    from punto.orchestrator.dev_cycle import (
        BUILD_CONTRACT,
        CAUSAL_HANDOFF_LABEL,
        WORKER_INSTRUCTIONS,
    )
    from punto.orchestrator.focused_resolution import (
        RESOLUTION_INPUT_LABEL,
        duplicated_chars,
        failure_map,
        resolution_block,
    )
    from punto.providers.contract import ProviderRole
    from punto.schemas.build import BuildRequest
    from punto.schemas.dev import CommandEvidence, DevelopmentPlan, FunctionalChainStep
    from punto.skills import load_skill
    from punto.workspace.target import DevelopmentTarget, VerificationCommand

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
            VerificationCommand(
                name="focused", argv=("python", "-c", FOCUSED), timeout_seconds=60.0
            ),
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
    bloque = resolution_block(
        round_index=1,
        failure=failure_map(fallo, target, plan),
        previous_patch=("src/components/Buscador.tsx", "src/components/Rejilla.tsx"),
        previous_strategy="src/components/Buscador.tsx:MODIFY|src/components/Rejilla.tsx:MODIFY",
        causal_gap=("src/lib/tipos.ts",),
        stagnation=False,
    )
    contexto = [
        dev_cycle.ContextFile(path="src/lib/tipos.ts", sha256="0" * 64, content=TIPOS),
        dev_cycle.ContextFile(path="src/components/Rejilla.tsx", sha256="0" * 64, content=REJILLA),
        dev_cycle.ContextFile(path="src/components/Buscador.tsx", sha256="0" * 64, content=BUSCADOR),
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
        peticion,
        target,
        plan,
        contexto,
        "",
        "focused exit=1\nTIPOS: export const TIPOS = ['Casa']",
        resolution=bloque,
    )
    skill = load_skill(referencia)
    handoff = dev_cycle.causal_handoff(plan)
    instrucciones = f"{WORKER_INSTRUCTIONS}\n\n{skill.body}"
    return {
        "skill_reference": referencia,
        "skill_id": skill.skill_id,
        "skill_version": skill.version,
        "skill_sha256": skill.sha256,
        "skill_chars": skill.chars,
        "resolution_context_chars": len(bloque),
        "causal_handoff_chars": len(handoff),
        "expected_resolution_context_prompt_chars": len(prompt),
        "skill_instructions_chars": len(instrucciones),
        "expected_effective_prompt_chars": len(instrucciones) + len(prompt),
        "duplicated_procedural_chars": duplicated_chars(
            bloque, (WORKER_INSTRUCTIONS, BUILD_CONTRACT, skill.body)
        ),
        "duplicated_context_chars": duplicated_chars(bloque, (prompt.replace(bloque, ""),)),
        "labels_present_once": {
            "resolution_input": prompt.count(RESOLUTION_INPUT_LABEL) == 1,
            "causal_handoff": prompt.count(CAUSAL_HANDOFF_LABEL) == 1,
        },
    }


def main() -> int:
    """Mide las dos versiones, calcula el delta y lo persiste aparte."""
    medidas = {referencia: _medir(referencia) for referencia in VERSIONES}
    vieja, nueva = (medidas[referencia] for referencia in VERSIONES)
    informe = {
        "versiones": medidas,
        "delta": {
            "skill_chars": nueva["skill_chars"] - vieja["skill_chars"],
            "resolution_context_chars": (
                nueva["resolution_context_chars"] - vieja["resolution_context_chars"]
            ),
            "expected_effective_prompt_chars": (
                nueva["expected_effective_prompt_chars"]
                - vieja["expected_effective_prompt_chars"]
            ),
            "hunks": 1,
            "regla": (
                "si el recurso causal está fuera de alcance y el cambio mínimo ya se conoce, pedir "
                "la ampliación e incluir el cambio en la misma respuesta"
            ),
        },
        "comun": {
            "duplicated_procedural_chars": [
                vieja["duplicated_procedural_chars"],
                nueva["duplicated_procedural_chars"],
            ],
            "duplicated_context_chars": [
                vieja["duplicated_context_chars"],
                nueva["duplicated_context_chars"],
            ],
            "causal_handoff_chars": vieja["causal_handoff_chars"],
        },
    }
    print(f"{'medida':44}{'0.1.0':>10}{'0.2.0':>10}{'delta':>10}")
    for clave in (
        "skill_chars",
        "resolution_context_chars",
        "expected_resolution_context_prompt_chars",
        "skill_instructions_chars",
        "expected_effective_prompt_chars",
        "duplicated_procedural_chars",
        "duplicated_context_chars",
        "causal_handoff_chars",
    ):
        print(
            f"{clave:44}{vieja[clave]:>10}{nueva[clave]:>10}"
            f"{nueva[clave] - vieja[clave]:>10}"
        )
    print(f"\nsha256 0.1.0: {vieja['skill_sha256']}\nsha256 0.2.0: {nueva['skill_sha256']}")
    salida = Path(__file__).resolve().parent / "overhead-focused-0.2.0.json"
    salida.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nevidencia: {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
