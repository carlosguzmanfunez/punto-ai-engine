"""Envuelve cuatro líneas largas del pre-flight del experimento 02."""

from __future__ import annotations

import pathlib

PATH = pathlib.Path(__file__).resolve().parent.parent / "tests/test_builder_skill.py"

PAIRS: tuple[tuple[str, str], ...] = (
    (
        "Cubre los quince puntos que el encargo exige antes de gastar una ejecución: validación y versionado\n",
        "Cubre los quince puntos que el encargo exige antes de gastar una ejecución: validación y\nversionado\n",
    ),
    (
        "    activation = activate_skill(BUILDER_SKILL, role=\"BUILDER\", base_instructions=WORKER_INSTRUCTIONS)\n",
        "    activation = activate_skill(\n        BUILDER_SKILL, role=\"BUILDER\", base_instructions=WORKER_INSTRUCTIONS\n    )\n",
    ),
    (
        "    tercera = cycle._instructions_for(ProviderRole.REPAIR if False else ProviderRole.BUILDER, request)\n",
        "    tercera = cycle._instructions_for(ProviderRole.BUILDER, request)\n",
    ),
    (
        "    assert prompt.count(\"SELF-CHECK\") == 0, \"el self-check es de la invocación del BUILDER, no del prompt base\"\n",
        "    assert prompt.count(\"SELF-CHECK\") == 0, (\n        \"el self-check es de la invocación del BUILDER, no del prompt base\"\n    )\n",
    ),
)


def main() -> None:
    """Aplica los reemplazos exactos."""
    text = PATH.read_text(encoding="utf-8")
    for old, new in PAIRS:
        if old not in text:
            raise SystemExit(f"no encontrado: {old[:60]!r}")
        text = text.replace(old, new, 1)
    PATH.write_text(text, encoding="utf-8")
    print("parcheado")


if __name__ == "__main__":
    main()
