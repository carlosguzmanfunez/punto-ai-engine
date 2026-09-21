"""Destinos de desarrollo: registro, alcance y catálogo de verificación (PILOT-04).

Extiende el registro de PILOT-03 (``BuildTarget`` + ``PUNTO_BUILD_TARGETS``) con lo que un ciclo que
**aplica** cambios necesita saber de un destino y que una fase de propuesta no necesitaba:

- el **baseline**: el SHA sobre el que se empieza, para poder demostrar qué cambió después;
- las **operaciones autorizadas** (READ/WRITE/CREATE/DELETE/EXECUTE/COMMIT), una a una;
- la **rama de trabajo** (el motor no escribe código sobre ``main``);
- el **catálogo de verificación**: nombres → ``argv`` permitido. El proveedor elige un **nombre**,
  nunca un comando: así una alucinación no puede convertirse en ejecución arbitraria;
- los límites: ficheros, tiempo por comando y rondas de reparación.

Los destinos viven en configuración (``PUNTO_DEV_TARGETS``), no en la solicitud y no en el código:
una ruta local no se escribe nunca en el motor.

Además de la variable de entorno, el motor lee un archivo **local a la máquina**
(``<config>/targets.local.yaml``, la misma convención que ``providers.local.yaml``): así el
dashboard ofrece el destino sin que nadie tenga que exportar la variable en cada arranque, y la
ruta del repositorio sigue viviendo en configuración confiable — nunca en la solicitud ni en el
navegador.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from punto.schemas.authority import KNOWN_DEPLOY_MECHANISMS, TargetAuthority
from punto.schemas.dev import RepositoryOperation

#: Variable de entorno que declara los destinos de desarrollo, en JSON.
DEV_TARGETS_ENV: Final[str] = "PUNTO_DEV_TARGETS"

#: Archivo de destinos local a esta máquina (no se versiona). Su raíz declara la clave ``targets``
#: con la misma forma que un destino de ``PUNTO_DEV_TARGETS``.
LOCAL_TARGETS_FILE: Final[str] = "targets.local.yaml"

#: Variables con las que se localiza el directorio de configuración al leer el archivo local.
CONFIG_DIR_ENV: Final[str] = "PUNTO_CONFIG_DIR"
REPO_ROOT_ENV: Final[str] = "PUNTO_REPO_ROOT"

#: Cota de destinos declarables.
MAX_DEV_TARGETS: Final[int] = 8

#: Programas que un catálogo de verificación puede invocar. Es una allowlist de **primer nivel**:
#: la política de shell del motor aplica además su propia allowlist y su saneado de entorno.
VERIFICATION_PROGRAMS: Final[frozenset[str]] = frozenset(
    {"git", "mypy", "node", "npm", "npx", "pytest", "python", "ruff"}
)

#: Argumentos que jamás pueden aparecer en una línea de verificación: instalar, publicar o salir
#: a la red convertirían una comprobación en un efecto externo.
FORBIDDEN_VERIFICATION_ARGS: Final[frozenset[str]] = frozenset(
    {
        "install",
        "ci",
        "add",
        "publish",
        "deploy",
        "push",
        "fetch",
        "pull",
        "clone",
        "remote",
        "config",
        "--registry",
        "-g",
        "--global",
    }
)

#: Cota de una línea de verificación y de su tiempo.
MAX_VERIFICATION_ARGS: Final[int] = 12
MAX_COMMAND_TIMEOUT_SECONDS: Final[float] = 1_800.0

_SHA_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


class DevelopmentTargetError(RuntimeError):
    """El destino de desarrollo no se puede usar tal como está declarado."""


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """Un comando de verificación con nombre: lo que PUNTO puede ejecutar y por qué."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 600.0
    description: str = ""


@dataclass(frozen=True, slots=True)
class VisualInteraction:
    """Interacción **declarada** por el destino para demostrar un criterio de apariencia dinámico.

    Es configuración de confianza (nada viene de una petición ni de un proveedor): dice en qué
    ruta de bucle local hay que pasar el cursor y sobre **qué elemento**, identificado por un
    selector CSS. ``hover`` debe localizar un elemento; ``label`` (opcional) es el elemento donde
    se espera que aparezca un texto tras la interacción. ``index`` elige de forma determinista
    cuál de los elementos coincidentes se usa. Si el selector no localiza el elemento, la
    interacción no es demostrable y el criterio queda ``UNCLEAR``.
    """

    name: str
    route: str
    hover: str
    label: str = ""
    index: int = 0
    settle_ms: int = 800


@dataclass(frozen=True, slots=True)
class DevelopmentTarget:
    """Destino de desarrollo: dónde se trabaja, qué se puede hacer y cómo se verifica."""

    target_id: str
    repository: Path
    baseline_sha: str
    #: Nombre humano del destino, para la interfaz. La ruta no se muestra: el nombre es lo que una
    #: persona elige y PUNTO resuelve la clave a su repositorio declarado.
    display_name: str = ""
    scope_roots: tuple[str, ...] = ()
    allowed_operations: frozenset[RepositoryOperation] = frozenset(
        {
            RepositoryOperation.READ,
            RepositoryOperation.WRITE,
            RepositoryOperation.CREATE,
            RepositoryOperation.EXECUTE,
            RepositoryOperation.COMMIT,
        }
    )
    verification: tuple[VerificationCommand, ...] = ()
    work_branch: str = ""
    max_files_changed: int = 12
    command_timeout_seconds: float = 600.0
    max_repair_rounds: int = 3
    max_read_bytes: int = 200_000
    #: Publicación a producción del destino (opcional). Sin rama **y** URL declaradas, el destino no
    #: es publicable: PUNTO no adivina dónde vive producción. ``publish_remote`` es el nombre del
    #: remoto Git que se usa para integrar el commit aprobado.
    production_branch: str = ""
    production_url: str = ""
    production_marker: str = ""
    publish_remote: str = "origin"
    #: Rutas **de bucle local** de la aplicación renderizada que se capturan como evidencia visual
    #: (por ejemplo ``http://localhost:3000/propiedades``). Sin ellas no hay captura: PUNTO no
    #: adivina dónde corre la aplicación ni navega fuera de la máquina.
    visual_routes: tuple[str, ...] = ()
    visual_viewport: tuple[int, int] = (1280, 800)
    #: Interacciones (hover) declaradas para criterios que una captura estática no demuestra.
    visual_interactions: tuple[VisualInteraction, ...] = ()
    #: Autoridad persistente del destino (AP000-R01): qué operaciones están previamente
    #: autorizadas. Sin sobre explícito no hay autonomía (todo ``False``): fail closed.
    authority: TargetAuthority = field(default_factory=TargetAuthority)

    @property
    def publishable(self) -> bool:
        """True si el destino declara dónde publicar y cómo comprobarlo."""
        return bool(self.production_branch and self.production_url)

    @property
    def human_name(self) -> str:
        """Nombre humano del destino: el declarado, o su clave si no declara ninguno."""
        return self.display_name or self.target_id

    def command(self, name: str) -> VerificationCommand:
        """Comando de verificación por nombre.

        Raises:
            DevelopmentTargetError: si el nombre no está en el catálogo del destino.
        """
        for item in self.verification:
            if item.name == name:
                return item
        known = ", ".join(item.name for item in self.verification) or "ninguno"
        raise DevelopmentTargetError(
            f"el destino {self.target_id!r} no declara la verificación {name!r} (declara: {known})"
        )

    def command_names(self) -> tuple[str, ...]:
        """Nombres del catálogo, en orden declarado."""
        return tuple(item.name for item in self.verification)

    def argv_catalog(self) -> dict[str, tuple[str, ...]]:
        """Catálogo nombre → ``argv``, para la allowlist de la frontera de recursos."""
        return {item.name: item.argv for item in self.verification}

    def command_lines(self) -> tuple[tuple[str, ...], ...]:
        """Líneas autorizadas de ejecución (``argv`` exactos del catálogo)."""
        return tuple(item.argv for item in self.verification)


def _parse_command(name: str, raw: object, *, default_timeout: float) -> VerificationCommand:
    """Valida una entrada del catálogo de verificación.

    Raises:
        DevelopmentTargetError: si el ``argv`` está vacío, usa un programa no permitido, incluye un
            argumento prohibido o es demasiado largo.
    """
    if isinstance(raw, Mapping):
        argv_raw = raw.get("argv", ())
        timeout = float(raw.get("timeout_seconds", default_timeout))
        description = str(raw.get("description", ""))
    else:
        argv_raw = raw
        timeout = default_timeout
        description = ""
    if not isinstance(argv_raw, (list, tuple)) or not argv_raw:
        raise DevelopmentTargetError(f"la verificación {name!r} no declara argv")
    argv = tuple(str(item) for item in argv_raw)
    if len(argv) > MAX_VERIFICATION_ARGS:
        raise DevelopmentTargetError(f"la verificación {name!r} declara demasiados argumentos")
    program = argv[0].strip().lower()
    if program not in VERIFICATION_PROGRAMS:
        allowed = ", ".join(sorted(VERIFICATION_PROGRAMS))
        raise DevelopmentTargetError(
            f"la verificación {name!r} usa {program!r}, que no está en la allowlist ({allowed})"
        )
    for argument in argv[1:]:
        if argument.strip().lower() in FORBIDDEN_VERIFICATION_ARGS:
            raise DevelopmentTargetError(
                f"la verificación {name!r} incluye el argumento prohibido {argument!r}: "
                "instalar, publicar o salir a la red no es una comprobación"
            )
    if timeout <= 0 or timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise DevelopmentTargetError(
            f"la verificación {name!r} declara un timeout fuera de rango (0, "
            f"{MAX_COMMAND_TIMEOUT_SECONDS}]"
        )
    return VerificationCommand(
        name=name, argv=argv, timeout_seconds=timeout, description=description
    )


def _as_int(value: object, default: int, name: str, *, minimum: int = 0) -> int:
    """Convierte un valor de configuración a entero acotado.

    Raises:
        DevelopmentTargetError: si no es un entero o queda por debajo del mínimo.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        raise DevelopmentTargetError(f"{name} debe ser un número entero")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DevelopmentTargetError(f"{name} debe ser un número entero") from exc
    if parsed < minimum:
        raise DevelopmentTargetError(f"{name} no puede ser menor que {minimum}")
    return parsed


def _as_float(value: object, default: float, name: str) -> float:
    """Convierte un valor de configuración a número acotado.

    Raises:
        DevelopmentTargetError: si no es un número o queda fuera del rango admitido.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DevelopmentTargetError(f"{name} debe ser un número")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise DevelopmentTargetError(f"{name} debe ser un número") from exc
    if parsed <= 0 or parsed > MAX_COMMAND_TIMEOUT_SECONDS:
        raise DevelopmentTargetError(
            f"{name} debe estar en el rango (0, {MAX_COMMAND_TIMEOUT_SECONDS}]"
        )
    return parsed


def _parse_authority(
    raw: object, *, target_id: str, production_branch: str
) -> TargetAuthority:
    """Valida el sobre de autoridad persistente de un destino (AP000-R01).

    Raises:
        DevelopmentTargetError: si el sobre no es un objeto, una bandera no es booleana, autoriza
            despliegue o publicación sin declarar un mecanismo que PUNTO sepa ejecutar, o autoriza
            una rama de destino distinta de la rama de producción declarada.
    """
    if raw is None:
        return TargetAuthority()
    if not isinstance(raw, Mapping):
        raise DevelopmentTargetError(f"authority de {target_id!r} debe ser un objeto")

    flags = {
        "local_changes": False,
        "commit": False,
        "push": False,
        "deploy": False,
        "production_release": False,
        "allow_destructive": False,
        "require_qa": True,
    }
    declared: list[str] = []
    for name in flags:
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, bool):
            raise DevelopmentTargetError(f"authority.{name} de {target_id!r} debe ser booleano")
        flags[name] = value
        declared.append(name)

    mechanism = str(raw.get("deploy_mechanism", "")).strip()
    if "deploy_mechanism" in raw:
        declared.append("deploy_mechanism")
    if flags["deploy"] or flags["production_release"]:
        if not mechanism:
            raise DevelopmentTargetError(
                f"authority de {target_id!r} autoriza despliegue o publicación y no declara "
                "deploy_mechanism: PUNTO no ejecuta un mecanismo que no esté autorizado"
            )
        if mechanism not in KNOWN_DEPLOY_MECHANISMS:
            known = ", ".join(sorted(KNOWN_DEPLOY_MECHANISMS))
            raise DevelopmentTargetError(
                f"authority de {target_id!r} declara el mecanismo {mechanism!r}, que PUNTO no "
                f"ejecuta (conocidos: {known})"
            )

    branches_raw = raw.get("allowed_branches", ())
    if isinstance(branches_raw, str) or not isinstance(branches_raw, (list, tuple)):
        raise DevelopmentTargetError(f"authority.allowed_branches de {target_id!r} debe ser lista")
    branches: list[str] = []
    for item in branches_raw:
        candidate = str(item).strip()
        if not candidate or (".." in candidate.split("/")):
            raise DevelopmentTargetError(
                f"authority.allowed_branches de {target_id!r} contiene una rama inválida"
            )
        branches.append(candidate)
    if "allowed_branches" in raw:
        declared.append("allowed_branches")
    if branches and production_branch and production_branch not in branches:
        raise DevelopmentTargetError(
            f"authority.allowed_branches de {target_id!r} no incluye su rama de producción "
            f"{production_branch!r}"
        )

    return TargetAuthority(
        local_changes=flags["local_changes"],
        commit=flags["commit"],
        push=flags["push"],
        deploy=flags["deploy"],
        production_release=flags["production_release"],
        deploy_mechanism=mechanism,
        allowed_branches=tuple(branches) or ((production_branch,) if production_branch else ()),
        allow_destructive=flags["allow_destructive"],
        require_qa=flags["require_qa"],
        declared_fields=tuple(dict.fromkeys(declared)),
    )


def target_from_mapping(target_id: str, value: Mapping[str, object]) -> DevelopmentTarget:
    """Construye un destino desde su configuración, validándola entera.

    Raises:
        DevelopmentTargetError: si falta algo, la ruta no es un repositorio, el baseline no tiene
            forma de SHA o alguna operación no es conocida.
    """
    if not target_id or len(target_id) > 80 or any(char in target_id for char in "/\\"):
        raise DevelopmentTargetError("identificador de destino inválido")
    repository_raw = str(value.get("repository", "")).strip()
    if not repository_raw:
        raise DevelopmentTargetError(f"el destino {target_id!r} no declara repositorio")
    repository = Path(repository_raw)
    if not repository.is_absolute():
        # Una ruta relativa se resolvería contra el directorio de trabajo del motor: es una
        # ambigüedad que un destino no puede permitirse.
        raise DevelopmentTargetError(f"el destino {target_id!r} debe declarar una ruta absoluta")
    if not (repository / ".git").exists():
        raise DevelopmentTargetError(f"el destino {target_id!r} no es un repositorio Git")
    baseline = str(value.get("baseline_sha", "")).strip().lower()
    if not _SHA_PATTERN.match(baseline):
        raise DevelopmentTargetError(
            f"el destino {target_id!r} debe declarar un baseline_sha de 40 caracteres hexadecimales"
        )
    operations_raw = value.get("allowed_operations")
    if operations_raw is None:
        operations = DevelopmentTarget(
            target_id="x", repository=repository, baseline_sha=baseline
        ).allowed_operations
    else:
        if isinstance(operations_raw, str) or not isinstance(operations_raw, (list, tuple)):
            raise DevelopmentTargetError(
                f"allowed_operations de {target_id!r} debe ser una lista"
            )
        parsed: list[RepositoryOperation] = []
        for item in operations_raw:
            try:
                parsed.append(RepositoryOperation(str(item).strip().upper()))
            except ValueError as exc:
                known = ", ".join(operation.value for operation in RepositoryOperation)
                raise DevelopmentTargetError(
                    f"operación desconocida {item!r} en {target_id!r} (conocidas: {known})"
                ) from exc
        operations = frozenset(parsed)

    roots_raw = value.get("scope_roots", ())
    if isinstance(roots_raw, str) or not isinstance(roots_raw, (list, tuple)):
        raise DevelopmentTargetError(f"scope_roots de {target_id!r} debe ser una lista")
    roots: list[str] = []
    for root in roots_raw:
        candidate = str(root).strip().strip("/")
        if not candidate or ".." in candidate.split("/"):
            raise DevelopmentTargetError(f"scope_roots de {target_id!r} contiene una ruta inválida")
        roots.append(candidate)

    default_timeout = _as_float(
        value.get("command_timeout_seconds"), 600.0, "command_timeout_seconds"
    )
    verification_raw = value.get("verification", {})
    if not isinstance(verification_raw, Mapping):
        raise DevelopmentTargetError(f"verification de {target_id!r} debe ser un objeto")
    verification = tuple(
        _parse_command(str(name), raw, default_timeout=default_timeout)
        for name, raw in verification_raw.items()
    )

    visual_routes, visual_viewport = _parse_visual(value.get("visual"), target_id=target_id)
    visual_interactions = _parse_interactions(value.get("visual"), target_id=target_id)

    return DevelopmentTarget(
        target_id=target_id,
        repository=repository,
        baseline_sha=baseline,
        display_name=str(value.get("display_name", "")).strip()[:80],
        scope_roots=tuple(roots),
        allowed_operations=operations,
        verification=verification,
        work_branch=str(value.get("work_branch", "")).strip(),
        max_files_changed=_as_int(
            value.get("max_files_changed"), 12, "max_files_changed", minimum=1
        ),
        command_timeout_seconds=default_timeout,
        max_repair_rounds=_as_int(value.get("max_repair_rounds"), 3, "max_repair_rounds"),
        max_read_bytes=_as_int(value.get("max_read_bytes"), 200_000, "max_read_bytes", minimum=1),
        production_branch=str(value.get("production_branch", "")).strip(),
        production_url=str(value.get("production_url", "")).strip(),
        production_marker=str(value.get("production_marker", "")).strip()[:200],
        publish_remote=str(value.get("publish_remote", "origin")).strip() or "origin",
        visual_routes=visual_routes,
        visual_viewport=visual_viewport,
        visual_interactions=visual_interactions,
        authority=_parse_authority(
            value.get("authority"),
            target_id=target_id,
            production_branch=str(value.get("production_branch", "")).strip(),
        ),
    )


def _parse_visual(raw: Any, *, target_id: str) -> tuple[tuple[str, ...], tuple[int, int]]:
    """Lee ``visual: {routes: [...], viewport: [ancho, alto]}`` con validación estricta.

    Solo URLs ``http(s)`` de ``localhost``/``127.0.0.1``: la evidencia visual es de esta máquina.

    Raises:
        DevelopmentTargetError: si la forma es inválida, una URL sale del bucle local o el tamaño no
            es razonable.
    """
    if raw is None:
        return (), (1280, 800)
    if not isinstance(raw, Mapping):
        raise DevelopmentTargetError(f"visual de {target_id!r} debe ser un objeto")
    from urllib.parse import urlparse

    routes_raw = raw.get("routes", [])
    if not isinstance(routes_raw, (list, tuple)) or len(routes_raw) > 4:
        raise DevelopmentTargetError(f"visual.routes de {target_id!r} debe ser una lista de <= 4")
    routes: list[str] = []
    for item in routes_raw:
        url = str(item).strip()
        parsed = urlparse(url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1"}
        if parsed.scheme not in {"http", "https"} or not loopback:
            raise DevelopmentTargetError(
                f"visual.routes de {target_id!r}: {url!r} no es una URL de bucle local"
            )
        routes.append(url)
    viewport_raw = raw.get("viewport", [1280, 800])
    if (
        not isinstance(viewport_raw, (list, tuple))
        or len(viewport_raw) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in viewport_raw)
        or not (320 <= viewport_raw[0] <= 3840 and 320 <= viewport_raw[1] <= 2400)
    ):
        raise DevelopmentTargetError(
            f"visual.viewport de {target_id!r} debe ser [ancho, alto] entre 320 y 3840 x 2400"
        )
    return tuple(routes), (int(viewport_raw[0]), int(viewport_raw[1]))


_INTERACTION_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _parse_interactions(raw: Any, *, target_id: str) -> tuple[VisualInteraction, ...]:
    """Lee ``visual.interactions`` con validación estricta (lista de ``VisualInteraction``).

    Forma: ``[{name, route, hover, label?, index?, settle_ms?}]``. La ruta debe ser de bucle local;
    los selectores son texto plano acotado, sin caracteres de control.

    Raises:
        DevelopmentTargetError: si la forma es inválida, la ruta sale del bucle local o algún campo
            no es razonable.
    """
    if not isinstance(raw, Mapping):
        return ()
    items = raw.get("interactions")
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)) or len(items) > 4:
        raise DevelopmentTargetError(
            f"visual.interactions de {target_id!r} debe ser una lista de <= 4"
        )
    from urllib.parse import urlparse

    result: list[VisualInteraction] = []
    names: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise DevelopmentTargetError(f"cada interacción de {target_id!r} debe ser un objeto")
        name = str(item.get("name", "")).strip()
        if not _INTERACTION_NAME.match(name) or name in names:
            raise DevelopmentTargetError(
                f"interacción de {target_id!r}: nombre inválido o repetido {name!r}"
            )
        names.add(name)
        route = str(item.get("route", "")).strip()
        parsed = urlparse(route)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "localhost",
            "127.0.0.1",
        }:
            raise DevelopmentTargetError(
                f"interacción {name!r} de {target_id!r}: {route!r} no es una URL de bucle local"
            )
        selectors: list[str] = []
        for key, required in (("hover", True), ("label", False)):
            selector = str(item.get(key, "")).strip()
            if (required and not selector) or len(selector) > 300 or any(
                ord(character) < 32 for character in selector
            ):
                raise DevelopmentTargetError(
                    f"interacción {name!r} de {target_id!r}: {key!r} inválido"
                )
            selectors.append(selector)
        index = item.get("index", 0)
        settle = item.get("settle_ms", 800)
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= 200
            or isinstance(settle, bool)
            or not isinstance(settle, int)
            or not 100 <= settle <= 5000
        ):
            raise DevelopmentTargetError(
                f"interacción {name!r} de {target_id!r}: index (0-200) o settle_ms "
                "(100-5000) inválido"
            )
        result.append(
            VisualInteraction(
                name=name,
                route=route,
                hover=selectors[0],
                label=selectors[1],
                index=index,
                settle_ms=settle,
            )
        )
    return tuple(result)


def load_development_targets(
    environ: Mapping[str, str] | None = None,
) -> dict[str, DevelopmentTarget]:
    """Lee los destinos de desarrollo de la configuración vigente.

    Orden de resolución:

    1. ``PUNTO_DEV_TARGETS`` (JSON): la declaración explícita del operador manda.
    2. ``<config>/targets.local.yaml``: la declaración local de esta máquina, para que el dashboard
       ofrezca el destino sin exportar nada. El directorio se resuelve con ``PUNTO_CONFIG_DIR``,
       ``PUNTO_REPO_ROOT`` o el ``config/`` del repositorio; si no se puede resolver, o el archivo
       no existe, simplemente **no hay destinos** (no es un error).

    Cuando se pasa un entorno explícito, la búsqueda del directorio usa **solo** ese entorno: así
    una prueba (o un arranque con entorno filtrado) no lee la configuración de la máquina.

    Raises:
        DevelopmentTargetError: si la declaración existe (variable o archivo) pero no se puede usar.
    """
    source = os.environ if environ is None else environ
    raw = source.get(DEV_TARGETS_ENV, "").strip()
    if raw:
        return _targets_from_json(raw)
    config_dir = _default_config_dir() if environ is None else _config_dir_from(source)
    if config_dir is None:
        return {}
    return load_local_development_targets(config_dir)


def _targets_from_json(raw: str) -> dict[str, DevelopmentTarget]:
    """Destinos declarados en la variable de entorno.

    Raises:
        DevelopmentTargetError: si el JSON no es válido o un destino no se puede usar.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DevelopmentTargetError(f"{DEV_TARGETS_ENV} no es JSON válido: {exc}") from exc
    if not isinstance(data, dict):
        raise DevelopmentTargetError(f"{DEV_TARGETS_ENV} debe ser un objeto JSON de destinos")
    return _targets_from_mapping(data, source=DEV_TARGETS_ENV)


def load_local_development_targets(config_dir: Path) -> dict[str, DevelopmentTarget]:
    """Lee los destinos declarados en ``<config>/targets.local.yaml``.

    La raíz del archivo declara la clave ``targets``: un mapeo de clave de destino → destino, con
    la misma forma que un destino de ``PUNTO_DEV_TARGETS`` y las **mismas validaciones** (ruta
    absoluta, repositorio Git real, baseline con forma de SHA, operaciones conocidas y catálogo de
    verificación por allowlist).

    Args:
        config_dir: Directorio de configuración ya resuelto.

    Returns:
        Destinos declarados; vacío si el archivo no existe.

    Raises:
        DevelopmentTargetError: si el archivo existe pero no se puede usar.
    """
    path = config_dir / LOCAL_TARGETS_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DevelopmentTargetError(f"{LOCAL_TARGETS_FILE} no es YAML válido: {exc}") from exc
    except OSError as exc:  # pragma: no cover - depende del sistema de ficheros
        raise DevelopmentTargetError(f"{LOCAL_TARGETS_FILE} no se pudo leer: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise DevelopmentTargetError(f"{LOCAL_TARGETS_FILE} debe ser un objeto YAML")
    if "targets" not in data:
        raise DevelopmentTargetError(
            f"{LOCAL_TARGETS_FILE} debe declarar la clave 'targets' con los destinos"
        )
    declared = data["targets"]
    if not isinstance(declared, Mapping):
        raise DevelopmentTargetError(f"'targets' de {LOCAL_TARGETS_FILE} debe ser un objeto")
    return _targets_from_mapping(declared, source=LOCAL_TARGETS_FILE)


def _targets_from_mapping(
    data: Mapping[object, object], *, source: str
) -> dict[str, DevelopmentTarget]:
    """Valida y construye los destinos de una declaración.

    Raises:
        DevelopmentTargetError: si hay demasiados destinos o alguno no se puede usar.
    """
    if len(data) > MAX_DEV_TARGETS:
        raise DevelopmentTargetError(
            f"{source} declara {len(data)} destinos; el máximo es {MAX_DEV_TARGETS}"
        )
    targets: dict[str, DevelopmentTarget] = {}
    for key, value in data.items():
        target_id = str(key).strip()
        if not isinstance(value, Mapping):
            raise DevelopmentTargetError(f"el destino {target_id!r} no declara un objeto")
        targets[target_id] = target_from_mapping(target_id, value)
    return targets


def _default_config_dir() -> Path | None:
    """Directorio de configuración del motor, o ``None`` si no se puede resolver."""
    from punto.policy.config_loader import ConfigError, find_config_dir

    try:
        return find_config_dir()
    except ConfigError:
        return None


def _config_dir_from(environ: Mapping[str, str]) -> Path | None:
    """Directorio de configuración declarado en un entorno explícito, si lo hay."""
    explicit = str(environ.get(CONFIG_DIR_ENV, "")).strip()
    if explicit:
        return Path(explicit)
    root = str(environ.get(REPO_ROOT_ENV, "")).strip()
    if root:
        return Path(root) / "config"
    return None


@dataclass(slots=True)
class DevelopmentTargetRegistry:
    """Registro consultable de destinos de desarrollo."""

    targets: Mapping[str, DevelopmentTarget] = field(default_factory=dict)

    def get(self, target_id: str) -> DevelopmentTarget:
        """Destino por clave.

        Raises:
            DevelopmentTargetError: si la clave no está registrada.
        """
        found = self.targets.get(target_id)
        if found is None:
            known = ", ".join(sorted(self.targets)) or "ninguno"
            raise DevelopmentTargetError(
                f"el destino {target_id!r} no está registrado (registrados: {known})"
            )
        return found

    def ids(self) -> tuple[str, ...]:
        """Claves registradas, ordenadas."""
        return tuple(sorted(self.targets))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> DevelopmentTargetRegistry:
        """Registro construido desde la configuración vigente."""
        return cls(targets=load_development_targets(environ))


__all__ = [
    "DEV_TARGETS_ENV",
    "FORBIDDEN_VERIFICATION_ARGS",
    "LOCAL_TARGETS_FILE",
    "MAX_DEV_TARGETS",
    "VERIFICATION_PROGRAMS",
    "DevelopmentTarget",
    "DevelopmentTargetError",
    "DevelopmentTargetRegistry",
    "VerificationCommand",
    "VisualInteraction",
    "load_development_targets",
    "load_local_development_targets",
    "target_from_mapping",
]
