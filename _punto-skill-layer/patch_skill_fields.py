"""Parche puntual: los tres campos de skill en el EfficiencyRecord (SKILL-LAYER-0, experimento 01)."""

from __future__ import annotations

import pathlib

TELEMETRY = pathlib.Path(__file__).resolve().parent.parent / "src/punto/telemetry/efficiency.py"
TESTS = pathlib.Path(__file__).resolve().parent.parent / "tests/test_efficiency_record.py"

FIELD_ANCHOR = """    primary_transport: str = Field(default="", max_length=40)
"""
FIELD_NEW = """    primary_transport: str = Field(default="", max_length=40)

    #: Skill activada en esta ejecución (SKILL-LAYER-0). Vacío y ``False`` significan el control.
    skill_id: str = Field(default="", max_length=80)
    skill_version: str = Field(default="", max_length=20)
    skill_activated: bool = False
"""

PARAM_ANCHOR = """    primary_transport: str = "",
"""
PARAM_NEW = """    primary_transport: str = "",
    skill_id: str = "",
    skill_version: str = "",
    skill_activated: bool = False,
"""

CTOR_ANCHOR = """        primary_transport=primary_transport,
"""
CTOR_NEW = """        primary_transport=primary_transport,
        skill_id=skill_id,
        skill_version=skill_version,
        skill_activated=skill_activated,
"""

TEST = '''

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
'''


def _patch(path: pathlib.Path, pairs: tuple[tuple[str, str], ...]) -> None:
    """Aplica reemplazos exactos y falla si alguno no aparece."""
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if old not in text:
            raise SystemExit(f"no encontrado en {path.name}: {old[:50]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    """Aplica el parche a la telemetría y añade la prueba del campo de skill."""
    _patch(TELEMETRY, ((FIELD_ANCHOR, FIELD_NEW), (PARAM_ANCHOR, PARAM_NEW), (CTOR_ANCHOR, CTOR_NEW)))
    text = TESTS.read_text(encoding="utf-8")
    if "test_el_registro_identifica_la_skill_activada" not in text:
        TESTS.write_text(text + TEST, encoding="utf-8")
    print("parcheado")


if __name__ == "__main__":
    main()
