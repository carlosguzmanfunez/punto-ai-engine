"""Frontera proveedor → ``FileChangeProposal``: límites contractuales de los campos descriptivos.

Reproduce el defecto real (Task 2e7822a0, intento 9): un ``acceptance_criterion`` de más de 300
caracteres invalidaba **todo** el cambio (``CHANGE_INVALID``). La corrección no relaja el modelo:

- el contrato que recibe el proveedor declara los límites (derivados del propio modelo);
- solo los campos descriptivos (``reason``, ``acceptance_criterion``) se ajustan, de forma
  determinista y registrada; ``path``, ``content``, ``operation`` y demás siguen fallando cerrado.

    pytest tests/test_proposal_boundary.py -q
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from punto.orchestrator.dev_cycle import BUILD_CONTRACT, BUILD_SCHEMA
from punto.orchestrator.proposal_boundary import (
    ELLIPSIS,
    field_limit,
    fit_annotation,
    length_limits_text,
    normalize_descriptive_fields,
)
from punto.schemas.audit import AuditEventType
from punto.schemas.dev import ContextRequest, FileChangeProposal
from test_human_console import TARGET_ID, _app, _cambio, _plan, _repos, _target

CRITERIO = "una sola fuente de tipos"
LIMITE_CRITERIO = 300
LIMITE_MOTIVO = 400


def _cambio_con(**campos: Any) -> dict[str, Any]:
    """Respuesta del BUILDER con un único cambio válido al que se le pisan campos."""
    cambio: dict[str, Any] = {
        "path": "src/lib/tipos.ts",
        "operation": "MODIFY",
        "content": "export const TIPOS = ['Casa', 'Apartamento'];\n",
        "reason": "la verificación mide este fichero",
        "acceptance_criterion": CRITERIO,
    }
    cambio.update(campos)
    return {"summary": "fuente canónica", "changes": [cambio]}


def _criterio_largo(longitud: int = 812) -> str:
    """El criterio real seguido de una justificación larga (lo que hizo el proveedor)."""
    cola = (
        " y además se verifica que el listado, el buscador y la rejilla usen exactamente esa fuente"
    )
    texto = CRITERIO
    while len(texto) < longitud:
        texto += cola
    return texto[:longitud]


# --------------------------------------------------------------- 1 · dentro del límite: intacto
def test_1_un_criterio_dentro_del_limite_no_se_toca() -> None:
    """<= 300: mismo objeto, ninguna constancia y el modelo lo acepta sin cambios."""
    payload = _cambio_con(acceptance_criterion="x" * LIMITE_CRITERIO)

    normalizado, notas = normalize_descriptive_fields(payload)

    assert normalizado is payload and notas == ()
    FileChangeProposal.model_validate(normalizado["changes"][0])


def test_1b_el_limite_exacto_y_el_siguiente() -> None:
    """300 pasa tal cual; 301 se ajusta a 300 como máximo."""
    exacto = _cambio_con(acceptance_criterion="a" * 300)
    siguiente = _cambio_con(acceptance_criterion="a" * 301)

    assert normalize_descriptive_fields(exacto)[1] == ()
    ajustado, notas = normalize_descriptive_fields(siguiente)
    assert len(notas) == 1 and len(ajustado["changes"][0]["acceptance_criterion"]) <= 300


# ------------------------------------------------------------------ 2 · salida > 300, ajustada
def test_2_un_criterio_mayor_de_300_se_ajusta_y_el_modelo_lo_acepta() -> None:
    """Antes: ``string_too_long`` invalidaba el cambio. Ahora: ajuste determinista y registrado."""
    original = _criterio_largo(812)
    with pytest.raises(ValueError, match="at most 300 characters"):
        FileChangeProposal.model_validate(_cambio_con(acceptance_criterion=original)["changes"][0])

    normalizado, notas = normalize_descriptive_fields(_cambio_con(acceptance_criterion=original))

    ajustado = normalizado["changes"][0]["acceptance_criterion"]
    assert len(ajustado) <= LIMITE_CRITERIO and ajustado.endswith(ELLIPSIS)
    FileChangeProposal.model_validate(normalizado["changes"][0])
    (nota,) = notas
    assert nota.location == "changes[0].acceptance_criterion"
    assert nota.original_chars == 812 and nota.kept_chars == len(ajustado)
    assert nota.original_sha256 == hashlib.sha256(original.encode("utf-8")).hexdigest()


def test_2b_el_ajuste_es_determinista() -> None:
    """La misma entrada da siempre la misma salida (reproducible y auditable)."""
    payload = _cambio_con(acceptance_criterion=_criterio_largo())

    assert normalize_descriptive_fields(payload)[0] == normalize_descriptive_fields(payload)[0]


# ------------------------------------------------- 3 · significado verificable preservado
def test_3_se_conserva_el_comienzo_del_criterio_y_su_huella() -> None:
    """El criterio vive al comienzo: el ajustado es un prefijo del original, cortado en palabra."""
    original = _criterio_largo(812)

    ajustado = fit_annotation(original, LIMITE_CRITERIO)

    prefijo = ajustado.removesuffix(ELLIPSIS)
    assert original.startswith(prefijo) and prefijo.startswith(CRITERIO)
    assert original[len(prefijo)] == " ", "corta en frontera de palabra, no a mitad de una"
    assert len(ajustado) <= LIMITE_CRITERIO


def test_3b_sin_espacios_se_corta_duro_pero_nunca_se_excede() -> None:
    """Un texto sin fronteras de palabra se corta al límite exacto, con la marca."""
    ajustado = fit_annotation("a" * 5_000, 300)

    assert len(ajustado) == 300 and ajustado.endswith(ELLIPSIS)


def test_3c_un_texto_que_cabe_no_cambia() -> None:
    """Sin exceso no hay recorte ni marca."""
    assert fit_annotation("corto", 300) == "corto"


# ------------------------------------------------------ 4 · otros campos con límite (misma cadena)
def test_4_reason_y_la_razon_de_una_peticion_de_contexto_tienen_el_mismo_tratamiento() -> None:
    """Misma cadena, mismo defecto predecible: ``reason`` de cambios y de peticiones de contexto."""
    payload = _cambio_con(reason="r" * 900)
    payload["context_requests"] = [{"path": "src/lib/otro.ts", "reason": "c" * 900}]

    normalizado, notas = normalize_descriptive_fields(payload)

    assert {nota.location for nota in notas} == {
        "changes[0].reason",
        "context_requests[0].reason",
    }
    FileChangeProposal.model_validate(normalizado["changes"][0])
    ContextRequest.model_validate(normalizado["context_requests"][0])
    assert len(normalizado["changes"][0]["reason"]) <= LIMITE_MOTIVO


# --------------------------------------------------------------- 5 · lo demás falla cerrado
@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        pytest.param("path", "src/" + "d/" * 400 + "f.ts", id="ruta-mayor-de-400"),
        pytest.param("content", "x" * 200_001, id="contenido-mayor-de-200000"),
        pytest.param("expected_sha256", "f" * 65, id="huella-mayor-de-64"),
        pytest.param("operation", "EXPLOTAR", id="operacion-desconocida"),
        pytest.param("path", "../fuera.ts", id="ruta-no-declarable"),
        pytest.param("acceptance_criterion", 12345, id="criterio-no-es-texto"),
        pytest.param("reason", ["no", "es", "texto"], id="motivo-no-es-texto"),
    ],
)
def test_5_cualquier_otro_defecto_sigue_fallando_cerrado(campo: str, valor: Any) -> None:
    """La normalización no toca nada que no sea anotación: el modelo sigue rechazando."""
    payload = _cambio_con(**{campo: valor})

    normalizado, _ = normalize_descriptive_fields(payload)

    assert normalizado["changes"][0][campo] == valor, "no se altera ni se recorta"
    with pytest.raises(ValueError):
        FileChangeProposal.model_validate(normalizado["changes"][0])


def test_5b_un_campo_extra_o_una_forma_rota_tampoco_se_arregla() -> None:
    """``extra="forbid"`` y las formas inválidas siguen intactas."""
    con_extra = _cambio_con(privilegio="root")
    roto = {"summary": "x", "changes": ["no es un objeto", 7]}

    assert normalize_descriptive_fields(con_extra)[0]["changes"][0]["privilegio"] == "root"
    with pytest.raises(ValueError):
        FileChangeProposal.model_validate(con_extra["changes"][0])
    assert normalize_descriptive_fields(roto)[0] is roto


# ------------------------------------------------------------- 6 · sin ampliación de autoridad
def test_6_solo_cambian_las_anotaciones_nunca_rutas_operaciones_ni_contenidos() -> None:
    """Todo lo que decide qué se escribe llega idéntico; la entrada original no se muta."""
    payload = _cambio_con(
        acceptance_criterion=_criterio_largo(), reason="r" * 900, expected_sha256="a" * 64
    )
    payload["changes"].append({"path": "src/lib/b.ts", "operation": "DELETE", "reason": "obsoleto"})
    copia = copy.deepcopy(payload)

    normalizado, _ = normalize_descriptive_fields(payload)

    assert payload == copia, "la respuesta original no se muta"
    for antes, despues in zip(payload["changes"], normalizado["changes"], strict=True):
        for campo in ("path", "operation", "content", "source_path", "expected_sha256"):
            assert antes.get(campo) == despues.get(campo), campo
        assert set(antes) == set(despues), "no se añaden ni se quitan campos"
    assert normalizado["summary"] == payload["summary"]


# ------------------------------------------------------------------ 7 · el contrato del proveedor
def test_7_el_contrato_declara_los_limites_derivados_del_modelo() -> None:
    """El prompt dice lo que el modelo exige: si el límite cambia en el modelo, cambia el texto."""
    assert field_limit(FileChangeProposal, "acceptance_criterion") == LIMITE_CRITERIO
    assert field_limit(FileChangeProposal, "reason") == LIMITE_MOTIVO
    assert length_limits_text() in BUILD_CONTRACT
    assert "acceptance_criterion <= 300" in BUILD_CONTRACT
    assert "reason <= 400" in BUILD_CONTRACT
    assert "never trimmed" in BUILD_CONTRACT, "el proveedor sabe qué campos no admiten exceso"


def test_7b_el_limite_no_viaja_en_el_schema_porque_el_dialecto_no_lo_admite() -> None:
    """Causa: ``maxLength`` no cabe en el ``json_schema`` del proveedor; el canal es el texto."""
    from punto.providers.json_schema import UNSUPPORTED_CONSTRAINTS

    assert "maxLength" in UNSUPPORTED_CONSTRAINTS
    assert "maxLength" not in str(BUILD_SCHEMA)


# ------------------------------------------------------- 8 · ciclo real, extremo a extremo
def test_8_el_ciclo_completa_con_un_criterio_largo_y_lo_deja_registrado(tmp_path: Path) -> None:
    """Antes: CHANGE_INVALID. Ahora: el cambio se aplica, se verifica y el ajuste queda auditado."""
    original = _criterio_largo(812)
    repo, remoto = _repos(tmp_path)
    cambio = _cambio()
    cambio["changes"][0]["acceptance_criterion"] = original
    client, audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[_plan(), cambio])

    tarea = client.post(
        "/console/tasks",
        json={
            "objective": "unificar la lista de tipos en una sola fuente",
            "target_id": TARGET_ID,
            "acceptance_criteria": [CRITERIO],
            "scope_paths": ["src"],
        },
    ).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea["development"]
    assert "Apartamento" in (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")
    (evento,) = audit.by_type(AuditEventType.DEV_PROPOSAL_NORMALIZED)
    meta = dict(evento.metadata)
    assert list(meta["fields"]) == ["changes[0].acceptance_criterion"]
    assert list(meta["original_chars"]) == [812]
    assert list(meta["original_sha256"]) == [hashlib.sha256(original.encode()).hexdigest()]
    assert original not in str(meta), "el registro lleva huella y longitudes, no el texto"


def test_8b_una_ruta_desmesurada_sigue_invalidando_el_cambio_y_no_escribe_nada(
    tmp_path: Path,
) -> None:
    """Fallo cerrado de extremo a extremo: la frontera no ajusta lo que decide qué se escribe."""
    repo, remoto = _repos(tmp_path)
    cambio = _cambio()
    cambio["changes"][0]["path"] = "src/" + "d/" * 400 + "f.ts"
    client, audit, _t, _d = _app(target=_target(repo, remoto=remoto), respuestas=[_plan(), cambio])

    tarea = client.post(
        "/console/tasks",
        json={
            "objective": "unificar la lista de tipos en una sola fuente",
            "target_id": TARGET_ID,
            "scope_paths": ["src"],
        },
    ).json()

    assert tarea["stage"] != "DEVELOPMENT_COMPLETED"
    assert "Apartamento" not in (repo / "src" / "lib" / "tipos.ts").read_text(encoding="utf-8")
    assert audit.by_type(AuditEventType.DEV_PROPOSAL_NORMALIZED) == ()
