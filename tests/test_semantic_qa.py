"""QA visual/semántico: presencia estructural no es corrección factual (AP000-OBS-03).

Casos deterministas de A a H sobre un repositorio de prueba con la forma del caso real del mapa, más
la integridad del dataset cartográfico real que se incorpora como fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from punto.acceptance import (
    ClaimKind,
    VisualCapability,
    claims_result,
    extract_claims,
    verify_claims,
)
from punto.audit.logger import AuditLogger
from punto.cartography import (
    EXPECTED_DEPARTMENTS,
    canonical_name,
    renders_from_dataset,
    validate_department_dataset,
)
from punto.schemas.build import BuildRequest
from punto.schemas.enums import RiskLevel
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from test_acceptance import (
    CONTENIDO_EXPLORADOR_REEMPLAZADO,
    EXPLORADOR,
    PROPIEDADES,
    _cambio_reparacion,
    _ciclo_mapa,
    _plan_mapa,
    _repo_ciclo,
    _solicitud_mapa,
)

DATASET_REAL = Path(__file__).resolve().parent / "fixtures" / "honduras-departamentos.geojson"
RUTA_DATASET = "src/lib/honduras-departamentos.geojson"


def _cuadrado(indice: int, *, ancho: float = 0.5) -> dict[str, object]:
    """Polígono arbitrario dentro de Honduras (no es geografía real: es una figura)."""
    lon = -89.0 + (indice % 6) * 1.0
    lat = 13.5 + (indice // 6) * 1.0
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon, lat],
                [lon + ancho, lat],
                [lon + ancho, lat + ancho],
                [lon, lat + ancho],
                [lon, lat],
            ]
        ],
    }


def _dataset(
    nombres: list[str],
    *,
    procedencia: bool = True,
    geometria: dict[str, object] | None = None,
) -> str:
    """GeoJSON sintético con los nombres indicados."""
    features = [
        {
            "type": "Feature",
            "properties": {"shapeName": nombre},
            "geometry": geometria if geometria is not None else _cuadrado(indice),
        }
        for indice, nombre in enumerate(nombres)
    ]
    payload: dict[str, object] = {"type": "FeatureCollection", "features": features}
    if procedencia:
        payload["punto"] = {
            "source": "dataset de prueba",
            "license": "CC0 1.0",
        }
    return json.dumps(payload)


def _politica_publicacion() -> PolicyDecision:
    """Decisión de política real sobre publicar (para el contexto de las afirmaciones)."""
    from punto.policy.policy_engine import PolicyEngine
    from punto.schemas.decision import ActionRequest

    return PolicyEngine.from_config().evaluate(
        ActionRequest(
            action="deploy_production",
            technical=True,
            reversible=False,
            risk_level=RiskLevel.HIGH,
            production_impact=True,
        )
    )


# --------------------------------------------------- afirmaciones: qué se exige
def test_las_afirmaciones_factuales_se_extraen_de_la_solicitud() -> None:
    """Un criterio de corrección sobre el territorio es una afirmación, no una superficie."""
    claims = extract_claims(
        "Reemplazar el placeholder por un mapa cartografico real de Honduras",
        (
            "los 18 departamentos están representados correctamente",
            "el mapa se integra visualmente",
        ),
    )
    clases = {item.kind for item in claims}

    assert ClaimKind.CARTOGRAPHIC_CORRECTNESS.value in clases
    assert ClaimKind.VISUAL_APPEARANCE.value in clases


def test_una_peticion_funcional_no_genera_afirmaciones() -> None:
    """Precisión: una petición normal no exige evidencia factual que nadie ha pedido."""
    assert extract_claims("Anadir un filtro de precio maximo", ("el filtro funciona",)) == ()


# ------------------------------------------------------------ A · figuras y enlaces
def test_case_a_18_figuras_con_enlaces_no_demuestran_cartografia() -> None:
    """A: estructura correcta (18 figuras, enlaces, typecheck) no demuestra cartografía."""
    claims = extract_claims("un mapa cartografico real de Honduras")
    assert claims, "la afirmación se extrae"
    codigo = "export const MAPA = ['Honduras'];\nconst d = 'M 0 0 L 10 0 Z';\n"

    # La estructura pasa (hay enlaces), pero sin dataset válido no hay evidencia cartográfica.
    registro = verify_claims(
        claims,
        datasets=[],
        rendered=(False, "el código dibuja figuras propias"),
        visual=VisualCapability(available=False, detail="sin imágenes"),
    )

    assert registro[0].result == "UNSATISFIED"
    assert "no hay ningún dataset administrativo válido" in registro[0].evidence
    assert "Honduras" in codigo  # el texto no es evidencia


def test_un_dataset_sin_procedencia_no_sirve_como_evidencia() -> None:
    """A (bis): 18 unidades con nombres correctos pero sin fuente/licencia no son evidencia."""
    texto = _dataset(list(EXPECTED_DEPARTMENTS), procedencia=False)

    informe = validate_department_dataset("x.geojson", texto)

    assert informe.complete, "están las 18 unidades"
    assert informe.valid is False
    assert "sin procedencia declarada" in informe.detail


def test_dos_unidades_con_la_misma_geometria_no_son_un_mapa() -> None:
    """A (bis): 18 figuras idénticas apiladas no representan 18 departamentos."""
    texto = _dataset(list(EXPECTED_DEPARTMENTS), geometria=_cuadrado(0))

    informe = validate_department_dataset("x.geojson", texto)

    assert informe.valid is False
    assert any("solapadas" in item for item in informe.invalid_geometries)


# ------------------------------------------------------------------ B y C · faltas
def test_case_b_17_departamentos_falla() -> None:
    """B: si falta una unidad, el dataset no vale como evidencia geográfica."""
    texto = _dataset(list(EXPECTED_DEPARTMENTS[:-1]))

    informe = validate_department_dataset("x.geojson", texto)

    assert informe.valid is False
    assert informe.missing == (EXPECTED_DEPARTMENTS[-1],)
    assert "faltan" in informe.detail


def test_case_c_nombre_duplicado_o_no_mapeable_falla() -> None:
    """C: nombres duplicados o no mapeables a la taxonomía del proyecto ⇒ inválido."""
    con_duplicado = list(EXPECTED_DEPARTMENTS)
    con_duplicado[-1] = EXPECTED_DEPARTMENTS[0]
    informe_duplicado = validate_department_dataset("x.geojson", _dataset(con_duplicado))
    con_desconocido = list(EXPECTED_DEPARTMENTS)
    con_desconocido[-1] = "Provincia Inventada"
    informe_desconocido = validate_department_dataset("x.geojson", _dataset(con_desconocido))

    assert informe_duplicado.valid is False
    assert informe_duplicado.duplicates == (EXPECTED_DEPARTMENTS[0],)
    assert informe_desconocido.valid is False
    assert any("no mapeable" in item for item in informe_desconocido.invalid_geometries)


# ------------------------------------------------------------- D · dataset real
def test_case_d_el_dataset_real_de_honduras_es_valido() -> None:
    """D: el dataset cartográfico incorporado representa los 18 departamentos de Honduras."""
    texto = DATASET_REAL.read_text(encoding="utf-8")

    informe = validate_department_dataset(RUTA_DATASET, texto)

    assert informe.valid is True, informe.detail
    assert len(informe.units) == 18
    assert informe.missing == () and informe.extra == () and informe.duplicates == ()
    assert informe.bbox is not None
    assert informe.bbox[0] < -87.0 and informe.bbox[2] > -85.0, "envolvente de Honduras"
    assert "geoBoundaries" in informe.source
    assert "Open Data Commons" in informe.license
    assert informe.sha256, "huella del dataset para la evidencia"


def test_las_islas_de_la_bahia_se_mapean_a_la_taxonomia_del_proyecto() -> None:
    """D: el nombre insular del dataset se mapea sin ambigüedad al nombre del proyecto."""
    assert canonical_name("Bay Islands") == "Islas de la Bahía"
    assert canonical_name("Gracias a Dios") == "Gracias a Dios"
    assert canonical_name("Provincia Inventada") == ""


# ----------------------------------------------------- E · navegación desde datos
def test_case_e_la_navegacion_se_construye_con_los_nombres_del_dataset(tmp_path: Path) -> None:
    """E: el enlace del departamento sale del dataset, no de una lista a mano."""
    from_data = (
        'const datos = await fetch("/lib/honduras-departamentos.geojson");\n'
        "const enlace = (nombre) => `/propiedades?departamento=${encodeURIComponent(nombre)}`;\n"
        "return datos.features.map((f) => <a href={enlace(f.properties.shapeName)} />);\n"
    )
    a_mano = (
        'const datos = await fetch("/lib/honduras-departamentos.geojson");\n'
        'return <a href="/propiedades?departamento=Colon">Colon</a>;\n'
    )

    def leer(texto: str):
        return lambda path: texto

    assert renders_from_dataset([EXPLORADOR], leer(from_data), RUTA_DATASET)[0] is True
    assert renders_from_dataset([EXPLORADOR], leer(a_mano), RUTA_DATASET)[0] is False


# ------------------------------------------------------- F · sin evidencia visual
def test_case_f_sin_capacidad_visual_no_se_inventa_un_pass() -> None:
    """F: sin transporte con imágenes, el criterio visual queda NOT_VERIFIED (nunca PASS)."""
    claims = extract_claims("el mapa se integra visualmente con el diseno actual")

    registro = verify_claims(
        claims, visual=VisualCapability(available=False, detail="claude --print es texto")
    )

    assert registro[0].result == "NOT_VERIFIED"
    assert "sin verificar" in registro[0].evidence
    assert claims_result(registro) == "EVIDENCE_REQUIRED"


def test_case_f_una_atestacion_humana_si_demuestra_la_apariencia() -> None:
    """F (bis): la apariencia se puede demostrar con una persona que la mire y lo diga."""
    claims = extract_claims("el mapa se integra visualmente con el diseno actual")

    registro = verify_claims(
        claims,
        visual=VisualCapability(available=False, detail="sin imágenes"),
        attestation="revisado en local: el mapa se integra con el diseño",
    )

    assert registro[0].result == "SATISFIED"
    assert "atestación humana" in registro[0].evidence


# ------------------------------------------------- G y H · el ciclo completo
def _solicitud_con_afirmacion(*, criterios: tuple[str, ...]) -> BuildRequest:
    """Solicitud del caso real con las afirmaciones indicadas."""
    base = _solicitud_mapa()
    return base.model_copy(update={"acceptance_criteria": criterios})


def test_case_g_un_criterio_sin_evidencia_no_cierra_la_tarea(tmp_path: Path) -> None:
    """G: sin capacidad visual, el criterio requerido no verificado impide completar."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR, PROPIEDADES)),
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa se integra visualmente",
            ),
        ],
        audit,
    )
    solicitud = _solicitud_con_afirmacion(
        criterios=("el mapa se integra visualmente con el diseno actual",)
    )

    resultado = ciclo.run(solicitud)

    assert resultado.status.value != "DEVELOPMENT_COMPLETED"
    assert resultado.error_kind == "EVIDENCE_REQUIRED"
    assert resultado.claims_result == "EVIDENCE_REQUIRED"
    assert resultado.claims and resultado.claims[0].result == "NOT_VERIFIED"
    eventos = {evento.event_type.value for evento in audit.by_resource(solicitud.request_id)}
    assert "DEV_CLAIMS_EVALUATED" in eventos
    assert "GIT_COMMIT_CREATED" not in eventos, "sin evidencia no hay commit de cierre"


def test_case_h_cartografia_real_mas_resto_verde_cierra(tmp_path: Path) -> None:
    """H: dataset real usado por el código + resto verde ⇒ el desarrollo puede completarse."""
    root = _repo_ciclo(tmp_path)
    (root / RUTA_DATASET).parent.mkdir(parents=True, exist_ok=True)
    (root / RUTA_DATASET).write_text(
        DATASET_REAL.read_text(encoding="utf-8"), encoding="utf-8"
    )
    audit = AuditLogger()
    componente = (
        'import { useEffect, useState } from "react";\n'
        "export function DepartmentExplorer() {\n"
        "  const [datos, setDatos] = useState(null);\n"
        "  useEffect(() => {\n"
        '    fetch("/lib/honduras-departamentos.geojson").then((r) => r.json()).then(setDatos);\n'
        "  }, []);\n"
        '  if (!datos) return <p>Cargando mapa…</p>;\n'
        "  const enlace = (nombre) => `/propiedades?departamento=${encodeURIComponent(nombre)}`;\n"
        "  return (\n"
        '    <div className="department-layout">\n'
        '      <svg viewBox="-90 12.5 8 5.5">\n'
        "        {datos.features.map((f) => (\n"
        "          <a key={f.properties.shapeName}"
        "             href={enlace(f.properties.shapeName)}>\n"
        '            <title>{f.properties.shapeName}</title>\n'
        "          </a>\n"
        "        ))}\n"
        "      </svg>\n"
        '      <div className="department-grid">'
        "{datos.features.map((f) => f.properties.shapeName)}\n"
        "      </div>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR,)),
            _cambio_reparacion(EXPLORADOR, componente, "el mapa representa los 18 departamentos"),
        ],
        audit,
    )
    solicitud = _solicitud_con_afirmacion(
        criterios=("el mapa real de Honduras representa los 18 departamentos correctamente",)
    )

    resultado = ciclo.run(solicitud)

    assert resultado.status.value == "DEVELOPMENT_COMPLETED", resultado.error
    assert resultado.claims_result == "SATISFIED", resultado.claims
    assert resultado.acceptance_result == "SATISFIED"
    assert resultado.commit_sha
    assert resultado.claims[0].kind == ClaimKind.CARTOGRAPHIC_CORRECTNESS.value


def test_un_dataset_valido_sin_uso_no_demuestra_la_cartografia() -> None:
    """Invariante: tener el dataset no basta; el producto tiene que usarlo."""
    texto = DATASET_REAL.read_text(encoding="utf-8")
    informe = validate_department_dataset(RUTA_DATASET, texto)
    claims = extract_claims("un mapa cartografico real de Honduras")

    registro = verify_claims(
        claims,
        datasets=[informe],
        rendered=(
            False, "ningún fichero modificado referencia honduras-departamentos.geojson"
        ),
    )

    assert registro[0].result == "UNSATISFIED"
    assert "no lo usa" in registro[0].evidence


@pytest.mark.parametrize("clave", ["shapeName", "name", "NOMBRE", "ADM1_ES"])
def test_el_nombre_de_la_unidad_se_lee_de_las_claves_habituales(clave: str) -> None:
    """El validador reconoce las claves de nombre habituales de un GeoJSON administrativo."""
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {clave: nombre},
                "geometry": _cuadrado(indice),
            }
            for indice, nombre in enumerate(EXPECTED_DEPARTMENTS)
        ],
        "punto": {"source": "prueba", "license": "CC0 1.0"},
    }

    informe = validate_department_dataset("x.geojson", json.dumps(payload))

    assert informe.valid is True, informe.detail


def test_una_geometria_fuera_de_honduras_invalida_el_dataset() -> None:
    """Un polígono en otro continente no representa un departamento de Honduras."""
    fuera = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
    }

    informe = validate_department_dataset(
        "x.geojson", _dataset(list(EXPECTED_DEPARTMENTS), geometria=fuera)
    )

    assert informe.valid is False
    assert any("fuera de la envolvente" in item for item in informe.invalid_geometries)


def test_una_decision_de_politica_real_sostiene_el_contexto_de_prueba() -> None:
    """El contexto de las pruebas usa la política real del motor (no un doble)."""
    decision = _politica_publicacion()

    assert decision.outcome in {PolicyOutcome.REQUIRE_HUMAN, PolicyOutcome.REJECT}
    assert decision.effective_risk.name in {"HIGH", "CRITICAL"}
