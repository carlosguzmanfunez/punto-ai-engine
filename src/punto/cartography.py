"""Integridad de datos cartográficos: la geografía se mide contra un dataset, no contra una promesa.

AP000-OBS-03. Un mapa puede *parecer* un mapa y no representar el territorio que dice representar.
Lo que PUNTO **sí** puede demostrar de forma determinista es que la geometría que pinta el producto
viene de un dataset administrativo real, completo y bien mapeado a la taxonomía del proyecto:

- corresponde al país declarado;
- tiene exactamente las 18 unidades de nivel administrativo 1 (departamentos);
- cada unidad tiene nombre/identificador y se mapea **sin ambigüedad** a los nombres del proyecto;
- no hay unidades duplicadas, faltantes ni de más;
- las geometrías son válidas, no vacías y caen dentro del territorio del país;
- el dataset declara su **fuente y licencia** (procedencia auditable).

Lo que este módulo **no** hace: no juzga si el resultado se ve bien (eso requiere evidencia visual)
ni afirma precisión cartográfica absoluta. Es la parte determinista; el juicio visual va aparte y,
si no hay capacidad para obtenerlo, queda ``NOT_VERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "EXPECTED_DEPARTMENTS",
    "HONDURAS_BBOX",
    "NAME_ALIASES",
    "GeoDatasetReport",
    "find_department_datasets",
    "renders_from_dataset",
    "validate_department_dataset",
]

#: Departamento esperados: la taxonomía del proyecto (los 18 de Honduras).
EXPECTED_DEPARTMENTS: Final[tuple[str, ...]] = (
    "Atlántida",
    "Colón",
    "Comayagua",
    "Copán",
    "Cortés",
    "Choluteca",
    "El Paraíso",
    "Francisco Morazán",
    "Gracias a Dios",
    "Intibucá",
    "Islas de la Bahía",
    "La Paz",
    "Lempira",
    "Ocotepeque",
    "Olancho",
    "Santa Bárbara",
    "Valle",
    "Yoro",
)

#: Nombres alternativos que un dataset puede usar para una unidad (mapeo inequívoco y explícito).
NAME_ALIASES: Final[dict[str, str]] = {
    "bay islands": "Islas de la Bahía",
    "islas de la bahia": "Islas de la Bahía",
    "islas de la bahía": "Islas de la Bahía",
    "gracias a dios": "Gracias a Dios",
    "francisco morazan": "Francisco Morazán",
    "el paraiso": "El Paraíso",
    "santa barbara": "Santa Bárbara",
    "cortes": "Cortés",
    "colon": "Colón",
    "copan": "Copán",
    "intibuca": "Intibucá",
    "atlantida": "Atlántida",
}

#: Envolvente geográfica de Honduras, **incluido su territorio insular** (Islas de la Bahía, con las
#: Islas del Cisne en 17,4° N / 83,9° O). Un departamento insular legítimo cae dentro.
HONDURAS_BBOX: Final[tuple[float, float, float, float]] = (-90.2, 12.5, -82.5, 17.6)

#: Claves habituales donde un GeoJSON guarda el nombre de la unidad.
_NAME_KEYS: Final[tuple[str, ...]] = (
    "shapeName",
    "name",
    "NAME",
    "NAME_1",
    "nombre",
    "NOMBRE",
    "adm1_name",
    "ADM1_ES",
)

#: Tope de lectura del dataset (los datasets simplificados de un país caben de sobra).
MAX_DATASET_BYTES: Final[int] = 4_000_000

#: Plantilla de navegación construida con el nombre del departamento (no escrita a mano).
_DATA_DRIVEN_LINK: Final[re.Pattern[str]] = re.compile(
    r"departamento=\$\{|departamento=\"\s*\+|encodeURIComponent\(|departamento=\$\{encodeURIComponent",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class GeoDatasetReport:
    """Integridad de un dataset administrativo, con su procedencia."""

    path: str
    source: str
    license: str
    sha256: str
    units: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    invalid_geometries: tuple[str, ...] = ()
    bbox: tuple[float, float, float, float] | None = None
    valid: bool = False
    detail: str = ""

    @property
    def complete(self) -> bool:
        """True si están las 18 unidades, sin faltantes, sobrantes ni duplicados."""
        return not self.missing and not self.extra and not self.duplicates

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin geometrías (no se copia el dataset a la evidencia)."""
        return {
            "path": self.path,
            "source": self.source,
            "license": self.license,
            "sha256": self.sha256,
            "units": len(self.units),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "duplicates": list(self.duplicates),
            "invalid_geometries": list(self.invalid_geometries),
            "bbox": list(self.bbox) if self.bbox else [],
            "valid": self.valid,
            "detail": self.detail,
        }


def _normalize(text: str) -> str:
    """Texto comparable: sin acentos, en minúsculas."""
    folded = unicodedata.normalize("NFKD", str(text))
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain.casefold()).strip()


def canonical_name(raw: str, expected: Sequence[str] = EXPECTED_DEPARTMENTS) -> str:
    """Nombre canónico del proyecto para una unidad del dataset (vacío si no se puede mapear)."""
    normalized = _normalize(raw)
    if not normalized:
        return ""
    for name in expected:
        if _normalize(name) == normalized:
            return name
    return NAME_ALIASES.get(normalized, "")


def _iter_positions(geometry: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    """Posiciones de una geometría (Polygon/MultiPolygon), sin interpretar nada más."""
    kind = str(geometry.get("type", ""))
    coordinates = geometry.get("coordinates")
    if kind == "Polygon" and isinstance(coordinates, list):
        return tuple(
            (float(point[0]), float(point[1]))
            for ring in coordinates
            if isinstance(ring, list)
            for point in ring
            if isinstance(point, list) and len(point) >= 2
        )
    if kind == "MultiPolygon" and isinstance(coordinates, list):
        return tuple(
            (float(point[0]), float(point[1]))
            for polygon in coordinates
            if isinstance(polygon, list)
            for ring in polygon
            if isinstance(ring, list)
            for point in ring
            if isinstance(point, list) and len(point) >= 2
        )
    return ()


def _bbox(positions: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Envolvente de un conjunto de posiciones."""
    return (
        min(point[0] for point in positions),
        min(point[1] for point in positions),
        max(point[0] for point in positions),
        max(point[1] for point in positions),
    )


def _overlap(first: Sequence[float], second: Sequence[float]) -> float:
    """Solape entre dos envolventes, relativo a la menor de las dos (0..1)."""
    ancho = min(first[2], second[2]) - max(first[0], second[0])
    alto = min(first[3], second[3]) - max(first[1], second[1])
    if ancho <= 0 or alto <= 0:
        return 0.0
    area = ancho * alto
    menor = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return area / menor if menor > 0 else 1.0


def _geometry_problem(feature: Mapping[str, Any], where: str) -> str:
    """Motivo por el que una geometría no sirve (vacío si es válida)."""
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping):
        return f"{where}: sin geometría"
    kind = str(geometry.get("type", ""))
    if kind not in {"Polygon", "MultiPolygon"}:
        return f"{where}: geometría {kind or 'desconocida'} (se esperan polígonos)"
    positions = _iter_positions(geometry)
    if len(positions) < 3:
        return f"{where}: geometría vacía o degenerada"
    lon_min, lat_min, lon_max, lat_max = HONDURAS_BBOX
    outside = [
        point
        for point in positions
        if not (lon_min <= point[0] <= lon_max and lat_min <= point[1] <= lat_max)
    ]
    if outside:
        return f"{where}: {len(outside)} punto(s) fuera de la envolvente de Honduras"
    return ""


def validate_department_dataset(
    path: str,
    text: str,
    *,
    expected: Sequence[str] = EXPECTED_DEPARTMENTS,
) -> GeoDatasetReport:
    """Valida un dataset administrativo contra los departamentos esperados.

    Args:
        path: Ruta relativa del dataset (para la evidencia).
        text: Contenido del fichero.
        expected: Nombres canónicos esperados (los del proyecto).

    Returns:
        El informe de integridad: unidades mapeadas, faltantes, sobrantes, duplicados, geometrías
        inválidas, envolvente, procedencia y validez.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if len(text.encode("utf-8")) > MAX_DATASET_BYTES:
        return GeoDatasetReport(
            path=path, source="", license="", sha256=digest, detail="dataset demasiado grande"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return GeoDatasetReport(
            path=path, source="", license="", sha256=digest, detail=f"JSON inválido: {exc}"
        )
    if not isinstance(data, Mapping) or str(data.get("type", "")) != "FeatureCollection":
        return GeoDatasetReport(
            path=path,
            source="",
            license="",
            sha256=digest,
            detail="no es una FeatureCollection de GeoJSON",
        )
    features = data.get("features")
    if not isinstance(features, list) or not features:
        return GeoDatasetReport(
            path=path, source="", license="", sha256=digest, detail="sin unidades (features)"
        )
    procedencia = data.get("punto")
    source = ""
    license_name = ""
    if isinstance(procedencia, Mapping):
        source = str(procedencia.get("source", ""))[:200]
        license_name = str(procedencia.get("license", ""))[:200]

    units: list[str] = []
    duplicates: list[str] = []
    invalid: list[str] = []
    positions: list[tuple[float, float]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, Mapping):
            invalid.append(f"feature {index}: no es un objeto")
            continue
        properties = feature.get("properties")
        raw = ""
        if isinstance(properties, Mapping):
            for key in _NAME_KEYS:
                if properties.get(key):
                    raw = str(properties[key])
                    break
        name = canonical_name(raw, expected)
        where = raw or f"feature {index}"
        if not name:
            invalid.append(f"{where}: nombre no mapeable a la taxonomía del proyecto")
        elif name in units:
            duplicates.append(name)
        else:
            units.append(name)
        problem = _geometry_problem(feature, where)
        if problem:
            invalid.append(problem)
            continue
        positions.extend(_iter_positions(feature["geometry"]))

    bbox: tuple[float, float, float, float] | None = None
    if positions:
        bbox = _bbox(positions)
    # Unidades con la **misma** envolvente no son un mapa administrativo: son figuras repetidas.
    # El umbral es alto a propósito: departamentos vecinos (o insulares frente a la costa) comparten
    # parte de su envolvente de forma legítima, y eso no invalida el dataset.
    cajas = [
        (_bbox(_iter_positions(item["geometry"])), item)
        for item in features
        if isinstance(item, Mapping) and "geometry" in item
    ]
    solapadas: list[str] = []
    for index, (caja, feature) in enumerate(cajas):
        for otra, _ in cajas[index + 1 :]:
            if _overlap(caja, otra) > 0.95:
                nombre = str((feature.get("properties") or {}).get("shapeName", "?"))
                if nombre not in solapadas:
                    solapadas.append(nombre)
    if solapadas:
        invalid.append(f"geometrías solapadas entre unidades: {', '.join(solapadas[:4])}")
    present = set(units)
    missing = tuple(item for item in expected if item not in present)
    extra = tuple(item for item in units if item not in set(expected))
    valid = (
        not missing
        and not extra
        and not duplicates
        and not invalid
        and len(units) == len(expected)
        and bool(source)
        and bool(license_name)
    )
    detail = (
        f"{len(units)}/{len(expected)} unidades mapeadas"
        + (f"; faltan: {', '.join(missing)}" if missing else "")
        + (f"; sobran: {', '.join(extra)}" if extra else "")
        + (f"; duplicadas: {', '.join(duplicates)}" if duplicates else "")
        + (f"; geometrías inválidas: {len(invalid)}" if invalid else "")
        + ("" if source and license_name else "; sin procedencia declarada (fuente/licencia)")
    )
    return GeoDatasetReport(
        path=path,
        source=source,
        license=license_name,
        sha256=digest,
        units=tuple(sorted(units)),
        missing=missing,
        extra=extra,
        duplicates=tuple(sorted(set(duplicates))),
        invalid_geometries=tuple(invalid[:8]),
        bbox=bbox,
        valid=valid,
        detail=detail,
    )


def find_department_datasets(
    files: Sequence[str],
    read_text: Callable[[str], str],
    *,
    expected: Sequence[str] = EXPECTED_DEPARTMENTS,
) -> tuple[GeoDatasetReport, ...]:
    """Busca datasets administrativos entre los ficheros candidatos y los valida.

    Solo se consideran ficheros que parecen GeoJSON (``.geojson``/``.json``) y contienen una
    ``FeatureCollection``; el resto se ignora sin leerlo entero.
    """
    reports: list[GeoDatasetReport] = []
    for path in files:
        lowered = path.casefold()
        if not lowered.endswith((".geojson", ".json")):
            continue
        try:
            text = read_text(path)
        except Exception:  # ilegible, fuera de alcance o con secretos: no es un dataset usable
            continue
        if "FeatureCollection" not in text:
            continue
        reports.append(validate_department_dataset(path, text, expected=expected))
    reports.sort(key=lambda item: (not item.valid, item.path))
    return tuple(reports)


def renders_from_dataset(
    changed_paths: Sequence[str],
    read_text: Callable[[str], str],
    dataset_path: str,
) -> tuple[bool, str]:
    """True si el código modificado usa el dataset real en vez de geometría propia.

    Es una comprobación textual acotada: algún fichero cambiado tiene que **referenciar** el dataset
    (import, ``fetch`` o lectura del fichero) **y** construir la navegación a partir de sus nombres
    (una plantilla que interpola el nombre del departamento), no de una lista escrita a mano.
    """
    needle = dataset_path.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    referencia = ""
    navegacion = ""
    for path in changed_paths:
        if not path.casefold().endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")):
            continue
        try:
            content = read_text(path)
        except Exception:
            continue
        if needle and needle in content and not referencia:
            referencia = path
        if _DATA_DRIVEN_LINK.search(content) and not navegacion:
            navegacion = path
    if not referencia:
        return False, f"ningún fichero modificado referencia {needle or dataset_path}"
    if not navegacion:
        return (
            False,
            f"{referencia} usa el dataset pero la navegación no se construye con sus nombres "
            "(no hay plantilla que interpole el departamento)",
        )
    return True, f"{referencia} usa {needle} y construye la navegación con sus nombres"
