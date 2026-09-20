"""Skills de PUNTO (SKILL-LAYER-0): material **no confiable** que instruye a un rol.

Invariante de toda esta capa: ``SKILL != AUTHORITY``. Una skill aporta procedimiento; la autoridad
sigue siendo del ``PolicyEngine``, del sobre adaptativo y del Human Gate.
"""

from punto.skills.skill import (
    MAX_SKILL_CHARS,
    SKILLS_ROOT_ENV,
    Skill,
    SkillActivation,
    SkillValidationError,
    activate_skill,
    load_skill,
    skills_root,
)

__all__ = [
    "MAX_SKILL_CHARS",
    "SKILLS_ROOT_ENV",
    "Skill",
    "SkillActivation",
    "SkillValidationError",
    "activate_skill",
    "load_skill",
    "skills_root",
]
