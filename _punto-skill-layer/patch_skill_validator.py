"""Parche puntual: el validador de skills comprueba contenido real, no una palabra mágica."""

from __future__ import annotations

import pathlib

PATH = pathlib.Path(__file__).resolve().parent.parent / "src/punto/skills/skill.py"

ANCHOR = 'MAX_SKILL_CHARS: Final[int] = 8_000\n'
NEW = (
    'MAX_SKILL_CHARS: Final[int] = 8_000\n\n'
    '#: Tamaño mínimo del cuerpo: por debajo de esto no es un procedimiento, es un título.\n'
    'MIN_SKILL_CHARS: Final[int] = 300\n'
)

CHECK_OLD = """    if "skill" not in lowered:
        raise SkillValidationError(f"{path}: el cuerpo no declara que es una skill")
    if not body.startswith("#"):
        raise SkillValidationError(f"{path}: el cuerpo debe empezar por un título")
    if skill_id not in lowered and "procedimiento" not in lowered:
        raise SkillValidationError(
            f"{path}: el cuerpo no menciona la skill ni declara un procedimiento"
        )
"""

CHECK_NEW = """    if not body.startswith("#"):
        raise SkillValidationError(f"{path}: el cuerpo debe empezar por un título")
    if len(body) < MIN_SKILL_CHARS:
        raise SkillValidationError(
            f"{path}: el cuerpo es demasiado corto ({len(body)} caracteres) para ser un "
            "procedimiento"
        )
    markers = ("procedimiento", "paso", "cadena", "chain", "verific", "evidencia")
    if not any(marker in lowered for marker in markers):
        raise SkillValidationError(
            f"{path}: el cuerpo no describe un procedimiento verificable (esperado uno de: "
            + ", ".join(markers)
            + ")"
        )
"""


def main() -> None:
    """Aplica los dos reemplazos exactos."""
    text = PATH.read_text(encoding="utf-8")
    for old, new in ((ANCHOR, NEW), (CHECK_OLD, CHECK_NEW)):
        if old not in text:
            raise SystemExit(f"no encontrado: {old[:60]!r}")
        text = text.replace(old, new, 1)
    PATH.write_text(text, encoding="utf-8")
    print("parcheado")


if __name__ == "__main__":
    main()
