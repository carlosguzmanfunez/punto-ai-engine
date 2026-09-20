"""Carga y validación de **skills** (SKILL-LAYER-0, mínimo indispensable).

Una skill es un fichero ``SKILL.md`` con front-matter (``name``, ``version``, ``role``) y un
cuerpo de procedimiento. Esta capa hace lo mínimo que el encargo exige y nada más:

1. **localizarla** en una raíz declarada (``skills/<name>/SKILL.md``);
2. **validarla** como material **no confiable** (identificador, versión, tamaño, contenido esperado,
   ausencia de secretos y ausencia de intentos de ampliar autoridad);
3. **entregarla** al rol correspondiente como instrucciones adicionales;
4. **registrar** que se activó, con identificador, versión y huella del contenido.

Invariante: ``SKILL != AUTHORITY``. Una skill es texto: no concede permisos, no cambia presupuestos,
no toca ``PolicyEngine``, no salta un Human Gate y no declara nada autorizado. El texto de una skill
que **pida** alguna de esas cosas se rechaza aquí.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

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

#: Variable de entorno que declara la raíz de skills (por defecto ``skills/`` en la raíz del motor).
SKILLS_ROOT_ENV: Final[str] = "PUNTO_SKILLS_ROOT"

#: Tope de tamaño del cuerpo: una skill es un procedimiento, no un manual. Un fichero mayor se
#: rechaza en vez de convertirse en un prompt enorme (el encargo lo prohíbe explícitamente).
MAX_SKILL_CHARS: Final[int] = 8_000

#: Tamaño mínimo del cuerpo: por debajo de esto no es un procedimiento, es un título.
MIN_SKILL_CHARS: Final[int] = 300

_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")
_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{2,60}$")

#: Frases que un texto no confiable usaría para intentar ampliar autoridad. No son un análisis
#: semántico: son la frontera declarada de esta capa, y se comprueban en minúsculas.
_AUTHORITY_CLAIMS: Final[tuple[str, ...]] = (
    "grant yourself",
    "grant permission",
    "ignore the policy",
    "ignore policy",
    "bypass",
    "skip the human gate",
    "skip human gate",
    "saltarse el human gate",
    "omitir la politica",
    "omitir la política",
    "concedete",
    "concédete",
    "aumenta el presupuesto",
    "increase the budget",
    "modify policyengine",
    "modificar policyengine",
    "no need for verification",
    "skip verification",
)


class SkillValidationError(RuntimeError):
    """La skill declarada no es utilizable: se falla cerrado, no se ignora."""

    code = "SKILL_INVALID"


class Skill(BaseModel):
    """Skill cargada y validada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str = Field(max_length=80)
    version: str = Field(max_length=20)
    role: str = Field(max_length=40)
    description: str = Field(default="", max_length=400)
    body: str = Field(min_length=1)
    path: str = Field(default="", max_length=400)
    sha256: str = Field(default="", max_length=64)

    @property
    def chars(self) -> int:
        """Tamaño del procedimiento que se añadirá a las instrucciones del rol."""
        return len(self.body)

    @property
    def reference(self) -> str:
        """Referencia ``id@version`` con la que se registra la activación."""
        return f"{self.skill_id}@{self.version}"


class SkillActivation(BaseModel):
    """Constancia de una activación: qué skill, qué versión y con qué contenido."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str = Field(default="", max_length=80)
    skill_version: str = Field(default="", max_length=20)
    activated: bool = False
    chars: int = Field(default=0, ge=0)
    sha256: str = Field(default="", max_length=64)
    instructions: str = Field(default="", max_length=MAX_SKILL_CHARS + 2_000)
    error: str = Field(default="", max_length=300)

    @property
    def reference(self) -> str:
        """Referencia ``id@version`` o vacío si no se activó ninguna skill."""
        if not self.activated:
            return ""
        return f"{self.skill_id}@{self.skill_version}"


def skills_root(environ: dict[str, str] | None = None) -> Path:
    """Raíz donde se buscan las skills: la declarada por entorno o ``skills/`` del motor."""
    import os

    source = environ if environ is not None else os.environ
    declared = source.get(SKILLS_ROOT_ENV, "").strip()
    if declared:
        return Path(declared).resolve()
    return Path(__file__).resolve().parents[3] / "skills"


def _split_front_matter(text: str, *, path: Path) -> tuple[dict[str, str], str]:
    """Separa el front-matter simple (``clave: valor``) del cuerpo."""
    if not text.startswith("---"):
        raise SkillValidationError(f"{path}: falta el front-matter de la skill")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillValidationError(f"{path}: front-matter sin cerrar")
    header: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise SkillValidationError(f"{path}: línea de front-matter inválida: {line!r}")
        key, value = line.split(":", 1)
        header[key.strip().lower()] = value.strip()
    return header, parts[2].strip()


def _assert_safe(body: str, *, path: Path, skill_id: str) -> None:
    """Comprueba contenido esperado, ausencia de secretos y ausencia de reclamos de autoridad.

    Raises:
        SkillValidationError: si el texto no es un procedimiento utilizable.
    """
    from punto.security.deterministic import SECRET_PATTERNS

    lowered = body.lower()
    for name, pattern, _severity in SECRET_PATTERNS:
        if pattern.search(body):
            raise SkillValidationError(f"{path}: la skill contiene algo con forma de {name}")
    for claim in _AUTHORITY_CLAIMS:
        if claim in lowered:
            raise SkillValidationError(
                f"{path}: la skill intenta ampliar autoridad ({claim!r}): SKILL != AUTHORITY"
            )
    if not body.startswith("#"):
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


def load_skill(reference: str, *, root: Path | None = None) -> Skill:
    """Carga y valida una skill por referencia ``id`` o ``id@version``.

    Args:
        reference: identificador, con versión opcional (``punto-causal-architect@0.1.0``).
        root: raíz de skills; por defecto la declarada por entorno.

    Returns:
        La skill validada.

    Raises:
        SkillValidationError: si la referencia, la ruta, la versión o el contenido no son válidos.
    """
    declared = reference.strip()
    if not declared:
        raise SkillValidationError("referencia de skill vacía")
    skill_id, _, wanted_version = declared.partition("@")
    skill_id = skill_id.strip()
    if not _NAME_PATTERN.match(skill_id):
        raise SkillValidationError(f"identificador de skill inválido: {skill_id!r}")
    base = (root or skills_root()).resolve()
    # Una versión pedida se busca en su carpeta (``<id>/<version>/SKILL.md``) para poder **conservar
    # intacta** la versión anterior como evidencia histórica; sin versión se usa la vigente.
    candidates = []
    if wanted_version:
        candidates.append(base / skill_id / wanted_version / "SKILL.md")
    candidates.append(base / skill_id / "SKILL.md")
    path = next((item.resolve() for item in candidates if item.is_file()), candidates[0].resolve())
    if base not in path.parents:
        raise SkillValidationError(f"la skill sale de la raíz declarada: {path}")
    if not path.is_file():
        raise SkillValidationError(f"no existe la skill {skill_id!r} en {base}")
    text = path.read_text(encoding="utf-8")
    header, body = _split_front_matter(text, path=path)
    name = header.get("name", "")
    if name != skill_id:
        raise SkillValidationError(f"{path}: el front-matter declara {name!r} y no {skill_id!r}")
    version = header.get("version", "")
    if not _VERSION_PATTERN.match(version):
        raise SkillValidationError(f"{path}: versión inválida {version!r}")
    if wanted_version and wanted_version != version:
        raise SkillValidationError(
            f"{path}: se pidió la versión {wanted_version!r} y la skill es {version!r}"
        )
    role = header.get("role", "").upper()
    if role not in {"ARCHITECT", "BUILDER", "VISUAL_QA"}:
        raise SkillValidationError(f"{path}: rol no soportado {role!r}")
    if len(body) > MAX_SKILL_CHARS:
        raise SkillValidationError(
            f"{path}: {len(body)} caracteres superan el tope de {MAX_SKILL_CHARS}"
        )
    _assert_safe(body, path=path, skill_id=skill_id)
    return Skill(
        skill_id=skill_id,
        version=version,
        role=role,
        description=header.get("description", ""),
        body=body,
        path=str(path),
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )


def activate_skill(
    reference: str, *, role: str, base_instructions: str, root: Path | None = None
) -> SkillActivation:
    """Activa una skill para un rol concreto y devuelve la constancia de la activación.

    Si la referencia es vacía, no hay skill y se conserva el comportamiento previo. Si la skill no
    pasa la validación, **no se ignora**: se falla cerrado, porque ejecutar sin la skill declarada
    daría un resultado que no se podría atribuir al experimento.

    Raises:
        SkillValidationError: si la skill declarada no es válida o no le corresponde ese rol.
    """
    if not reference.strip():
        return SkillActivation(instructions=base_instructions)
    skill = load_skill(reference, root=root)
    if skill.role != role.upper():
        raise SkillValidationError(
            f"la skill {skill.skill_id!r} es para {skill.role} y se pidió para {role.upper()}"
        )
    instructions = f"{base_instructions}\n\n{skill.body}"
    return SkillActivation(
        skill_id=skill.skill_id,
        skill_version=skill.version,
        activated=True,
        chars=skill.chars,
        sha256=skill.sha256,
        instructions=instructions,
    )
