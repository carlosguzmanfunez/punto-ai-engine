"""Identidad de ruta: normalización, comparación y nombres de captura (ENGINE-5.3.2).

Este módulo cierra el hueco V53-06: el probe declaraba la ruta **solicitada** y nunca contrastaba
dónde había terminado el navegador. Con una redirección (`/pricing` -> `/`) la captura contenía la
portada pero el artefacto seguía diciendo `/pricing`, así que la cobertura visual podía darse por
completa con una ruta que nadie renderizó.

Aquí vive la política, en un solo sitio y explícita:

- **La ruta es el pathname.** El query y el fragmento no cambian la identidad de la ruta: se
  conservan en ``final_url`` como evidencia, pero no se comparan. Es una decisión, no un descuido:
  `/pricing?plan=pro` y `/pricing` son la misma página con otro estado.
- **La barra final es indiferente.** ``/pricing`` y ``/pricing/`` son equivalentes. Es la política
  documentada y la única equivalencia «blanda» que se acepta.
- **Todo lo demás se compara tal cual**: mayúsculas, barras repetidas y porcentajes se conservan.
  Es deliberadamente conservador: es preferible declarar una diferencia que darla por equivalente.
- **La ruta nunca lleva credenciales**: de una URL se toma el pathname y se descarta la autoridad,
  así que `http://usuario:clave@host/x` produce `/x`.

El probe del sandbox no puede importar este módulo (no existe ``punto`` dentro de la imagen), así
que **duplica** esta política en ``sandbox/web/probes/run_web_session.py`` con una nota que apunta
aquí. El host no se fía de esa copia: vuelve a comprobar la coherencia entre la ruta solicitada, la
ruta final observada y la ruta del artefacto antes de aceptar cualquier evidencia.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final
from urllib.parse import urlsplit

from punto.schemas.web import ViewportName

#: Ruta raíz normalizada.
ROOT_ROUTE: Final[str] = "/"

#: Slug usado cuando la ruta no aporta ningún carácter aprovechable (por ejemplo, la raíz).
ROUTE_SLUG_FALLBACK: Final[str] = "index"

#: Longitud máxima del slug legible de una ruta.
ROUTE_SLUG_LIMIT: Final[int] = 40

#: Caracteres del digest de ruta que se incorporan al nombre lógico.
ROUTE_DIGEST_CHARS: Final[int] = 8

#: Caracteres no admitidos en un nombre lógico de captura.
_UNSAFE_SLUG_CHARS: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")


def normalize_route(value: str) -> str:
    """Ruta lógica normalizada, sin query, sin fragmento y sin barra final salvo la raíz.

    Acepta tanto una ruta (``/pricing``) como una URL completa (``http://host:4173/pricing?x=1``):
    en el segundo caso se queda con el pathname y **descarta la autoridad**, de modo que unas
    credenciales en la URL no pueden acabar formando parte de la identidad ni del nombre del
    archivo.
    """
    text = str(value or "").strip()
    if not text:
        return ROOT_ROUTE
    if "://" in text or text.startswith("//"):
        text = urlsplit(text).path or ROOT_ROUTE
    text = text.split("?", 1)[0].split("#", 1)[0]
    if not text.startswith("/"):
        text = f"/{text}"
    if len(text) > 1 and text.endswith("/"):
        text = text.rstrip("/") or ROOT_ROUTE
    return text or ROOT_ROUTE


def route_from_url(url: str) -> str:
    """Ruta lógica de la URL final del navegador, sin credenciales ni query.

    Es la pieza que convierte ``page.url()`` en una ruta comparable: la autoridad (que puede
    contener usuario y contraseña) se descarta siempre.
    """
    return normalize_route(url)


def route_matches(requested: str, rendered: str) -> bool:
    """True si la ruta realmente renderizada corresponde a la solicitada.

    La comparación es la de :func:`normalize_route`, así que ``/pricing`` y ``/pricing/`` son
    equivalentes, el query y el fragmento no intervienen, y ``/pricing`` frente a ``/`` es una
    diferencia.
    """
    return normalize_route(requested) == normalize_route(rendered)


def safe_route_slug(route: str) -> str:
    """Slug legible y acotado de una ruta, para el nombre del archivo de captura."""
    normalized = normalize_route(route).strip("/").lower()
    slug = _UNSAFE_SLUG_CHARS.sub("-", normalized).strip("-")
    return (slug[:ROUTE_SLUG_LIMIT].strip("-") or ROUTE_SLUG_FALLBACK)


def route_digest(route: str) -> str:
    """Digest corto y determinista de la ruta normalizada."""
    normalized = normalize_route(route)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:ROUTE_DIGEST_CHARS]


def screenshot_logical_name(route: str, viewport: ViewportName | str) -> str:
    """Nombre lógico determinista, legible y **resistente a colisiones**.

    El slug solo es legible: dos rutas distintas pueden producir el mismo (``/a/b`` y ``/a-b``), así
    que el nombre incorpora un digest corto de la ruta normalizada. Así dos rutas diferentes nunca
    comparten nombre, y el nombre nunca contiene una ruta del host porque se construye desde el
    pathname.
    """
    viewport_name = viewport.value if isinstance(viewport, ViewportName) else str(viewport)
    return f"{safe_route_slug(route)}-{route_digest(route)}-{viewport_name.lower()}.png"


__all__ = [
    "ROOT_ROUTE",
    "ROUTE_DIGEST_CHARS",
    "ROUTE_SLUG_FALLBACK",
    "ROUTE_SLUG_LIMIT",
    "normalize_route",
    "route_digest",
    "route_from_url",
    "route_matches",
    "safe_route_slug",
    "screenshot_logical_name",
]
