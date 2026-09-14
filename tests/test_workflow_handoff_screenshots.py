"""Codecs durables de los bytes de las capturas de la sesión web (ENGINE-6.0.4, V604-02).

Qué demuestra esta prueba y por qué está separada del resto
-----------------------------------------------------------
El defecto V604-02 dice que el handoff de 6.0.3 publicaba la **descripción** de las capturas
—nombre lógico, ruta, viewport, tamaño y sha256— pero no sus **bytes**: ``_screenshots_of``
devolvía un mapa vacío si la sesión no declaraba capturas y ``WORKFLOW_INCOMPLETE_EVIDENCE`` si
declaraba alguna, así que el caso real de ENGINE-5.3 —capturas verificadas medidas en un navegador—
no se podía reconstruir en un proceso nuevo. Aquí se ejercitan **solo** los dos códecs añadidos
para cerrarlo, sobre un :class:`~punto.workflow.artifacts.FileArtifactStore` real en ``tmp_path``,
sin red y sin podman: ningún proveedor de modelo, ningún navegador y ninguna llamada al kernel.

El defecto V605-06 añade la otra mitad del mismo contrato: el manifiesto ya guardaba la identidad de
la sesión (``task_id``, ``project_id`` e ``id``), pero el resolutor no la comparaba, así que un
manifiesto de **otro replay** del mismo proyecto, con las mismas capturas y los mismos nombres
lógicos, se aceptaba y Visual QA habría analizado la evidencia de otra ejecución. Por eso la sesión
de estas pruebas lleva un ``id`` **fijo** y no el que el contrato genera por defecto: publicar y
resolver tienen que hablar del mismo replay, y las pruebas de identidad piden a propósito el otro.

Qué se comprueba, en el orden de la prueba:

1. ida y vuelta feliz de dos capturas, con bytes idénticos y el manifiesto con su ``kind``, su
   digest y su tamaño;
2. que un proceso nuevo, con una instancia nueva del almacén sobre la misma raíz, resuelve los
   mismos bytes;
3. los ataques, cada uno con su caso: bytes modificados con el mismo tamaño, bytes del tamaño
   declarado con el hash canónico roto, captura ausente del almacén, referencia de bytes
   manipulada, hash del manifiesto que no cuadra, nombre lógico cambiado, manifiesto sin una
   captura declarada, entrada de más y payloads que no encajan al publicar;
4. que una captura extra no oculta una faltante y que no se devuelve nunca un mapa a medias;
5. que una sesión sin capturas resuelve a un mapa vacío y que publicar una sesión sin capturas se
   rechaza;
6. que el artefacto de bytes es exactamente lo que se publicó y que el manifiesto no lleva los
   bytes dentro;
7. la identidad de la sesión (V605-06): otro replay, otra tarea y otro proyecto son huecos de
   evidencia y no aprobaciones, la identidad correcta sigue resolviendo los bytes exactos y un
   manifiesto reescrito en su identidad no entrega ningún payload a medias.

Los PNG se generan aquí con su firma, su IHDR, un IDAT comprimido de verdad y su IEND: no hace
falta que sean bonitos, pero sí bytes no vacíos, distintos entre sí y con dimensiones reales.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from punto.providers.base import ImagePayload
from punto.schemas.enums import TaskStatus
from punto.schemas.web import (
    PNG_SIGNATURE,
    SCREENSHOT_MEDIA_TYPE,
    ScreenshotArtifact,
    Viewport,
    ViewportName,
    WebSessionReport,
    WebTechnicalStatus,
    build_screenshot_artifact,
)
from punto.schemas.workflow import (
    ArtifactReference,
    RoleExecutionRequest,
    RoleName,
    WorkflowFailureCode,
)
from punto.workflow.artifacts import FileArtifactStore
from punto.workflow.errors import WorkflowIncompleteEvidenceError
from punto.workflow.handoff import (
    HANDOFF_SCHEMA_VERSION,
    SCREENSHOT_KIND,
    SCREENSHOT_MANIFEST_KIND,
    publish_screenshots,
    resolve_screenshots,
)

#: Identidad fija del caso: la misma petición y la misma sesión alimentan publicar y resolver.
WORKFLOW_ID = UUID("44444444-4444-4444-8444-444444444444")
TASK_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
IDEMPOTENCY_KEY = "handoff-screenshots"
#: Identidad de la **sesión web** del caso (V605-06).
#:
#: El manifiesto la guarda y el resolutor la exige, así que el ``id`` de la sesión de la prueba es
#: fijo y no el que el contrato genera por defecto: un ``id`` nuevo por llamada haría que publicar y
#: resolver hablaran de dos replays distintos, que es exactamente lo que V605-06 dejaba pasar.
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
#: Identidades de otros replays: misma forma que la del caso y **un solo** campo distinto, para que
#: cada prueba aísle la identidad que no cuadra.
OTHER_TASK_ID = UUID("11111111-1111-4111-8111-111111111112")
OTHER_PROJECT_ID = UUID("22222222-2222-4222-8222-222222222223")
OTHER_SESSION_ID = UUID("33333333-3333-4333-8333-333333333334")
#: Viewports del caso: dimensiones explícitas, como exige el contrato de la capa web.
_DESKTOP = Viewport(name=ViewportName.DESKTOP, width=1440, height=900)
_MOBILE = Viewport(name=ViewportName.MOBILE, width=390, height=844)


def _chunk(kind: bytes, body: bytes) -> bytes:
    """Bloque PNG con su CRC real: longitud, tipo, cuerpo y CRC32."""
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(width: int, height: int, *, colour: tuple[int, int, int]) -> bytes:
    """PNG sintético válido y no vacío: firma, IHDR, IDAT comprimido e IEND.

    Las dimensiones van en la cabecera IHDR y se leen de ahí, así que el artefacto declarado tiene
    un ancho y un alto reales. El color distingue una captura de otra sin depender del azar.
    """
    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        (
            PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(raw)),
            _chunk(b"IEND", b""),
        )
    )


#: Tres capturas distintas: dos para la ida y vuelta y una tercera para el caso de la faltante.
_PNG_A = _png(4, 4, colour=(255, 0, 0))
_PNG_B = _png(6, 6, colour=(0, 0, 255))
_PNG_C = _png(5, 5, colour=(0, 255, 0))

_ARTIFACT_A = build_screenshot_artifact(
    logical_name="home-desktop.png",
    route="/",
    viewport=_DESKTOP,
    data=_PNG_A,
    rendered_route="/",
    browser="chromium",
)
_ARTIFACT_B = build_screenshot_artifact(
    logical_name="about-mobile.png",
    route="/about",
    viewport=_MOBILE,
    data=_PNG_B,
    rendered_route="/about",
    browser="chromium",
)
_ARTIFACT_C = build_screenshot_artifact(
    logical_name="settings-tablet.png",
    route="/settings",
    viewport=_DESKTOP,
    data=_PNG_C,
    rendered_route="/settings",
    browser="chromium",
)


def _session(
    *artifacts: ScreenshotArtifact,
    session_id: UUID = SESSION_ID,
    task_id: UUID = TASK_ID,
    project_id: UUID = PROJECT_ID,
) -> WebSessionReport:
    """Sesión web del caso con la identidad indicada, sin abrir ningún navegador.

    El ``id`` por defecto es el del caso —``SESSION_ID``, no el que el contrato genera—: desde
    V605-06 el manifiesto ata la evidencia a la sesión que la midió y el resolutor exige esa misma
    identidad, así que publicar y resolver tienen que hablar del mismo replay. Las pruebas de
    identidad piden explícitamente la otra tarea, el otro proyecto o el otro ``id`` de sesión.
    """
    return WebSessionReport(
        id=session_id,
        task_id=task_id,
        project_id=project_id,
        status=WebTechnicalStatus.PASS,
        summary="La página carga sin errores y sin desbordamiento.",
        screenshots=artifacts,
    )


def _images(*pairs: tuple[ScreenshotArtifact, bytes]) -> dict[str, ImagePayload]:
    """Payloads de las capturas dadas, indexados por su nombre lógico."""
    return {
        artifact.logical_name: ImagePayload(
            data=data, media_type=artifact.media_type, logical_name=artifact.logical_name
        )
        for artifact, data in pairs
    }


def _request(role: RoleName = RoleName.VISUAL_QA) -> RoleExecutionRequest:
    """Petición del kernel para la etapa visual, con la identidad fija del caso."""
    return RoleExecutionRequest(
        workflow_id=WORKFLOW_ID,
        step_index=0,
        role=role,
        stage=TaskStatus.IN_PROGRESS,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        objective="Publicar los bytes de las capturas de la sesión web",
        workspace_path="workspace-demo",
        idempotency_key=IDEMPOTENCY_KEY,
    )


def _store(tmp_path: Path) -> FileArtifactStore:
    """Almacén de artefactos real sobre ``tmp_path``. Cada prueba usa el suyo."""
    return FileArtifactStore(tmp_path / "artifacts")


def _publish(store: FileArtifactStore, session: WebSessionReport) -> ArtifactReference:
    """Publica una sesión de dos capturas y devuelve la referencia de su manifiesto."""
    return publish_screenshots(
        store,
        request=_request(),
        session=session,
        images=_images((_ARTIFACT_A, _PNG_A), (_ARTIFACT_B, _PNG_B)),
    )


def _manifest_payload(store: FileArtifactStore, reference: ArtifactReference) -> dict[str, object]:
    """Manifiesto ya deserializado, para poder manipularlo como lo haría un atacante."""
    return cast("dict[str, object]", json.loads(store.get(reference).decode("utf-8")))


def _entries(payload: dict[str, object]) -> list[dict[str, object]]:
    """Entradas del manifiesto por índice, en el orden en que el códec las escribió."""
    return cast("list[dict[str, object]]", payload["screenshots"])


def _entry_of(payload: dict[str, object], logical_name: str) -> dict[str, object]:
    """Entrada del manifiesto de una captura concreta, por nombre lógico."""
    return next(item for item in _entries(payload) if item["logical_name"] == logical_name)


def _republished_manifest(
    store: FileArtifactStore, payload: dict[str, object]
) -> ArtifactReference:
    """Vuelve a publicar un manifiesto manipulado y devuelve su referencia nueva.

    Es la forma honesta de probar los ataques: el artefacto del almacén sigue siendo coherente con
    su propio digest —lo escribió la prueba— y lo que se manipula es el **contenido** del índice.
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return store.put(
        workflow_id=WORKFLOW_ID,
        role=RoleName.VISUAL_QA,
        step_index=1,
        kind=SCREENSHOT_MANIFEST_KIND,
        label="manifiesto manipulado",
        data=body,
    )


def _mutated_manifest(
    store: FileArtifactStore,
    reference: ArtifactReference,
    mutate: Callable[[dict[str, object]], None],
) -> ArtifactReference:
    """Aplica ``mutate`` al manifiesto publicado y devuelve la referencia del manipulado."""
    payload = _manifest_payload(store, reference)
    mutate(payload)
    return _republished_manifest(store, payload)


def _bytes_reference(entry: dict[str, object]) -> ArtifactReference:
    """Referencia del artefacto de bytes que declara una entrada del manifiesto."""
    return ArtifactReference.model_validate(entry["reference"])


def _path_of(store: FileArtifactStore, reference: ArtifactReference) -> Path:
    """Ruta en disco del artefacto referenciado, para manipularlo como lo haría un atacante."""
    return store.root.joinpath(*reference.reference.split("/"))


def _assert_incomplete(error: pytest.ExceptionInfo[WorkflowIncompleteEvidenceError]) -> None:
    """Comprueba que el fallo es ``WORKFLOW_INCOMPLETE_EVIDENCE`` y no otra cosa."""
    assert error.value.code is WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE


# ---------------------------------------------------------------------------
# 1. Ida y vuelta feliz
# ---------------------------------------------------------------------------
def test_two_screenshots_round_trip_with_their_exact_bytes(tmp_path: Path) -> None:
    """Publicar dos capturas y resolverlas devuelve los mismos bytes, media type y nombre."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    assert manifest.kind == SCREENSHOT_MANIFEST_KIND
    assert len(manifest.digest) == 64
    assert manifest.bytes_written == len(store.get(manifest))

    payload = _manifest_payload(store, manifest)
    assert payload["schema_version"] == HANDOFF_SCHEMA_VERSION
    assert payload["kind"] == SCREENSHOT_MANIFEST_KIND
    assert payload["task_id"] == str(TASK_ID)
    assert payload["project_id"] == str(PROJECT_ID)

    entries = _entries(payload)
    assert [entry["logical_name"] for entry in entries] == [
        "about-mobile.png",
        "home-desktop.png",
    ]
    assert all(entry["reference"]["kind"] == SCREENSHOT_KIND for entry in entries)

    resolved = resolve_screenshots(store, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    assert set(resolved) == {"home-desktop.png", "about-mobile.png"}
    assert resolved["home-desktop.png"].data == _PNG_A
    assert resolved["about-mobile.png"].data == _PNG_B
    assert resolved["home-desktop.png"].media_type == SCREENSHOT_MEDIA_TYPE
    assert resolved["about-mobile.png"].media_type == SCREENSHOT_MEDIA_TYPE
    assert resolved["home-desktop.png"].logical_name == "home-desktop.png"
    assert resolved["about-mobile.png"].logical_name == "about-mobile.png"


# ---------------------------------------------------------------------------
# 2. Proceso nuevo: otra instancia del almacén sobre la misma raíz
# ---------------------------------------------------------------------------
def test_a_new_store_instance_resolves_the_same_bytes(tmp_path: Path) -> None:
    """Resolver con un almacén recién construido, sin memoria del anterior, da los mismos bytes."""
    publisher = _store(tmp_path)
    manifest = _publish(publisher, _session(_ARTIFACT_A, _ARTIFACT_B))

    fresh = FileArtifactStore(tmp_path / "artifacts")
    assert fresh is not publisher

    resolved = resolve_screenshots(fresh, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    assert resolved["home-desktop.png"].data == _PNG_A
    assert resolved["about-mobile.png"].data == _PNG_B
    assert hashlib.sha256(resolved["home-desktop.png"].data).hexdigest() == _ARTIFACT_A.sha256


# ---------------------------------------------------------------------------
# 3. Ataques, cada uno con su propio caso
# ---------------------------------------------------------------------------
def test_bytes_tampered_with_the_same_size_are_refused(tmp_path: Path) -> None:
    """Unos bytes del mismo tamaño modificados en disco no se entregan como los medidos."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    entry = _entry_of(_manifest_payload(store, manifest), "home-desktop.png")
    path = _path_of(store, _bytes_reference(entry))
    path.write_bytes(bytes(len(_PNG_A)))

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "integridad" in error.value.detail


def test_bytes_of_the_declared_size_with_a_broken_hash_are_refused(tmp_path: Path) -> None:
    """Si el almacén devuelve bytes que pasan su digest pero no el sha256 canónico, hay error.

    Es el caso que la validación de ENGINE-5.3 existe para cubrir: el índice y la sesión declaran un
    sha256 y el contenido tiene el mismo tamaño pero ya no es el que se midió.
    """
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    tampered = bytes([_PNG_A[0] ^ 0xFF]) + _PNG_A[1:]
    assert len(tampered) == len(_PNG_A)
    substitute = store.put(
        workflow_id=WORKFLOW_ID,
        role=RoleName.VISUAL_QA,
        step_index=2,
        kind=SCREENSHOT_KIND,
        label="bytes sustituidos del mismo tamaño",
        data=tampered,
    )

    def swap(payload: dict[str, object]) -> None:
        _entry_of(payload, "home-desktop.png")["reference"] = substitute.model_dump(mode="json")

    reissued = _mutated_manifest(store, manifest, swap)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "hash" in error.value.detail


def test_a_missing_bytes_artifact_is_incomplete_evidence(tmp_path: Path) -> None:
    """Borrar el fichero de bytes de una captura deja evidencia incompleta, no un mapa a medias."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    entry = _entry_of(_manifest_payload(store, manifest), "home-desktop.png")
    _path_of(store, _bytes_reference(entry)).unlink()

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "no están en el almacén" in error.value.detail


def test_a_manipulated_bytes_reference_is_incomplete_evidence(tmp_path: Path) -> None:
    """Una referencia de bytes con el digest manipulado se traduce a evidencia incompleta."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def corrupt(payload: dict[str, object]) -> None:
        reference = _entry_of(payload, "home-desktop.png")["reference"]
        assert isinstance(reference, dict)
        reference["digest"] = "0" * 64

    reissued = _mutated_manifest(store, manifest, corrupt)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "integridad" in error.value.detail


def test_a_manifest_hash_that_does_not_match_the_session_is_refused(tmp_path: Path) -> None:
    """Un sha256 distinto en el manifiesto no ata la misma imagen que declara la sesión."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def rewrite(payload: dict[str, object]) -> None:
        _entries(payload)[0]["sha256"] = "f" * 64

    reissued = _mutated_manifest(store, manifest, rewrite)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "sha256" in error.value.detail


def test_a_manifest_entry_under_another_logical_name_is_refused(tmp_path: Path) -> None:
    """Renombrar una entrada del manifiesto rompe el emparejamiento y se dice como tal."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def rename(payload: dict[str, object]) -> None:
        _entries(payload)[0]["logical_name"] = "captura-renombrada.png"

    reissued = _mutated_manifest(store, manifest, rename)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "nombre lógico" in error.value.detail


def test_a_manifest_without_one_declared_capture_is_refused(tmp_path: Path) -> None:
    """Un manifiesto al que le falta una captura declarada no entrega las que sí están."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def drop(payload: dict[str, object]) -> None:
        entries = _entries(payload)
        entries.pop(0)

    reissued = _mutated_manifest(store, manifest, drop)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "no lleva ninguna entrada" in error.value.detail


def test_an_extra_manifest_entry_is_refused(tmp_path: Path) -> None:
    """Una entrada que la sesión no declara tampoco se cuela: el índice no es un superconjunto."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def duplicate(payload: dict[str, object]) -> None:
        entries = _entries(payload)
        clone = dict(entries[0])
        clone["logical_name"] = "captura-extra.png"
        entries.append(clone)

    reissued = _mutated_manifest(store, manifest, duplicate)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "no declara" in error.value.detail


def test_publishing_with_a_missing_payload_publishes_nothing(tmp_path: Path) -> None:
    """Que falte el payload de una captura declarada es defecto del llamante y no escribe nada."""
    store = _store(tmp_path)
    images = _images((_ARTIFACT_A, _PNG_A))

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store, request=_request(), session=_session(_ARTIFACT_A, _ARTIFACT_B), images=images
        )
    assert "no llegó el payload" in str(error.value)
    assert not list((tmp_path / "artifacts").rglob("*.bin"))


def test_an_extra_payload_is_refused_when_publishing(tmp_path: Path) -> None:
    """Un payload de una captura que la sesión no declara se rechaza antes de escribir."""
    store = _store(tmp_path)

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store,
            request=_request(),
            session=_session(_ARTIFACT_A, _ARTIFACT_B),
            images=_images((_ARTIFACT_A, _PNG_A), (_ARTIFACT_B, _PNG_B), (_ARTIFACT_C, _PNG_C)),
        )
    assert "no declara" in str(error.value)
    assert not list((tmp_path / "artifacts").rglob("*.bin"))


def test_a_duplicated_logical_name_is_refused_when_publishing(tmp_path: Path) -> None:
    """Dos capturas con el mismo nombre lógico no pueden atarse a la misma entrada."""
    store = _store(tmp_path)

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store,
            request=_request(),
            session=_session(_ARTIFACT_A, _ARTIFACT_A),
            images=_images((_ARTIFACT_A, _PNG_A)),
        )
    assert "mismo nombre lógico" in str(error.value)


def test_bytes_that_do_not_validate_canonically_are_not_published(tmp_path: Path) -> None:
    """Unos bytes que no cuadran con el artefacto declarado suben como ``ValueError`` canónico."""
    store = _store(tmp_path)
    wrong = ImagePayload(
        data=_PNG_B, media_type=SCREENSHOT_MEDIA_TYPE, logical_name=_ARTIFACT_A.logical_name
    )

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store,
            request=_request(),
            session=_session(_ARTIFACT_A),
            images={_ARTIFACT_A.logical_name: wrong},
        )
    assert "no coinciden" in str(error.value) or "ocupan" in str(error.value)
    assert not list((tmp_path / "artifacts").rglob("*.bin"))


def test_publishing_from_another_role_is_refused(tmp_path: Path) -> None:
    """El artefacto es de ``VISUAL_QA``: otra etapa no puede atribuírselo."""
    store = _store(tmp_path)

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store,
            request=_request(RoleName.QA),
            session=_session(_ARTIFACT_A),
            images=_images((_ARTIFACT_A, _PNG_A)),
        )
    assert "VISUAL_QA" in str(error.value)


# ---------------------------------------------------------------------------
# 4. Una captura extra no oculta una faltante
# ---------------------------------------------------------------------------
def test_a_manifest_with_fewer_captures_never_returns_a_partial_map(tmp_path: Path) -> None:
    """El manifiesto tiene dos entradas y la sesión declara tres: error, sin mapa a medias."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    three = _session(_ARTIFACT_A, _ARTIFACT_B, _ARTIFACT_C)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), three)
    _assert_incomplete(error)
    assert "settings-tablet.png" in error.value.detail


def test_a_manifest_with_more_captures_than_the_session_is_refused(tmp_path: Path) -> None:
    """El caso simétrico: el manifiesto declara tres y la sesión dos, y tampoco se resuelve."""
    store = _store(tmp_path)
    manifest = publish_screenshots(
        store,
        request=_request(),
        session=_session(_ARTIFACT_A, _ARTIFACT_B, _ARTIFACT_C),
        images=_images((_ARTIFACT_A, _PNG_A), (_ARTIFACT_B, _PNG_B), (_ARTIFACT_C, _PNG_C)),
    )

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "settings-tablet.png" in error.value.detail


# ---------------------------------------------------------------------------
# 5. El caso «sin capturas»
# ---------------------------------------------------------------------------
def test_a_session_without_captures_resolves_to_an_empty_mapping(tmp_path: Path) -> None:
    """Sin capturas declaradas no hay error: no hay nada que analizar y el informe dirá cero."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    empty = _session()

    assert resolve_screenshots(store, (manifest,), empty) == {}
    assert resolve_screenshots(store, (), empty) == {}
    assert resolve_screenshots(store, (), None) == {}


def test_publishing_a_session_without_captures_is_refused(tmp_path: Path) -> None:
    """Una sesión sin capturas no publica bytes ni manifiesto: un índice vacío fingiría algo."""
    store = _store(tmp_path)

    with pytest.raises(ValueError) as error:
        publish_screenshots(
            store, request=_request(), session=_session(), images={}
        )
    assert "no declara ninguna" in str(error.value)
    assert not list((tmp_path / "artifacts").rglob("*.bin"))


def test_declared_captures_without_a_manifest_are_incomplete_evidence(tmp_path: Path) -> None:
    """Si la sesión declara capturas y no hay manifiesto, la evidencia está incompleta."""
    store = _store(tmp_path)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (), _session(_ARTIFACT_A))
    _assert_incomplete(error)
    assert SCREENSHOT_MANIFEST_KIND in error.value.detail


# ---------------------------------------------------------------------------
# 6. Los bytes publicados son exactamente los medidos y el manifiesto no los lleva
# ---------------------------------------------------------------------------
def test_the_bytes_artifact_is_exactly_what_was_published(tmp_path: Path) -> None:
    """Cada artefacto de bytes es el PNG original, sin secretos ni transformaciones añadidas."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    entries = _entries(_manifest_payload(store, manifest))
    originals = {"home-desktop.png": _PNG_A, "about-mobile.png": _PNG_B}

    for entry in entries:
        reference = _bytes_reference(entry)
        data = store.get(reference)
        assert data == originals[str(entry["logical_name"])]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]
        assert reference.bytes_written == len(data)
        assert reference.digest == entry["sha256"]


def test_the_manifest_carries_no_image_bytes(tmp_path: Path) -> None:
    """El manifiesto lleva metadatos y referencias: ni la firma PNG ni los bytes de una captura."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    body = store.get(manifest)

    assert PNG_SIGNATURE not in body
    assert _PNG_A not in body
    assert _PNG_B not in body
    assert _PNG_A[8:] not in body

    payload = _manifest_payload(store, manifest)
    for entry in _entries(payload):
        assert entry["media_type"] == SCREENSHOT_MEDIA_TYPE
        assert entry["route"].startswith("/")
        assert entry["viewport"] in {"MOBILE", "TABLET", "DESKTOP"}


# ---------------------------------------------------------------------------
# 7. La identidad de la sesión ata el manifiesto a su replay (ENGINE-6.0.5, V605-06)
# ---------------------------------------------------------------------------
def test_evidence_from_another_replay_is_incomplete_evidence(tmp_path: Path) -> None:
    """Mismo ``task_id``, mismo proyecto, mismas capturas y nombres, pero otro ``id`` de sesión."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    other_replay = _session(_ARTIFACT_A, _ARTIFACT_B, session_id=OTHER_SESSION_ID)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), other_replay)
    _assert_incomplete(error)
    assert "sesión" in error.value.detail
    assert str(OTHER_SESSION_ID) in error.value.detail
    assert str(SESSION_ID) in error.value.detail


def test_evidence_from_another_task_is_incomplete_evidence(tmp_path: Path) -> None:
    """La misma sesión de navegador, pero la tarea que se quiere resolver es otra."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    other_task = _session(_ARTIFACT_A, _ARTIFACT_B, task_id=OTHER_TASK_ID)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), other_task)
    _assert_incomplete(error)
    assert "tarea" in error.value.detail
    assert str(OTHER_TASK_ID) in error.value.detail


def test_evidence_from_another_project_is_incomplete_evidence(tmp_path: Path) -> None:
    """Misma tarea y misma sesión, pero otro proyecto: el índice no es de este workflow."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    other_project = _session(_ARTIFACT_A, _ARTIFACT_B, project_id=OTHER_PROJECT_ID)

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (manifest,), other_project)
    _assert_incomplete(error)
    assert "proyecto" in error.value.detail
    assert str(OTHER_PROJECT_ID) in error.value.detail


def test_the_same_session_identity_resolves_the_exact_bytes(tmp_path: Path) -> None:
    """La identidad correcta —tarea, proyecto y ``id`` de sesión— resuelve los dos payloads."""
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))
    same = _session(
        _ARTIFACT_A,
        _ARTIFACT_B,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        project_id=PROJECT_ID,
    )

    resolved = resolve_screenshots(store, (manifest,), same)
    assert set(resolved) == {"home-desktop.png", "about-mobile.png"}
    assert resolved["home-desktop.png"].data == _PNG_A
    assert resolved["about-mobile.png"].data == _PNG_B
    assert hashlib.sha256(resolved["about-mobile.png"].data).hexdigest() == _ARTIFACT_B.sha256


def test_a_manifest_rewritten_in_its_identity_delivers_no_payload(tmp_path: Path) -> None:
    """Un manifiesto manipulado en su identidad falla tipado y no entrega un mapa a medias.

    Se ataca por las dos vías reales. Primero el **contenido**: se reescribe ``session_id`` en el
    índice y se vuelve a publicar —el digest es coherente porque lo escribió la prueba—, y además se
    borran los bytes de las capturas; si el resolutor mirara los bytes antes que la identidad el
    fallo hablaría del almacén, y como mira la identidad primero el fallo es de sesión y no se lee
    ninguna captura. Después el **fichero**: se reescribe el manifiesto en disco a mano, que el
    almacén detecta como manipulación y el resolutor traduce al mismo error tipado.
    """
    store = _store(tmp_path)
    manifest = _publish(store, _session(_ARTIFACT_A, _ARTIFACT_B))

    def rewrite(payload: dict[str, object]) -> None:
        payload["session_id"] = str(OTHER_SESSION_ID)

    reissued = _mutated_manifest(store, manifest, rewrite)
    for entry in _entries(_manifest_payload(store, reissued)):
        _path_of(store, _bytes_reference(entry)).unlink()

    with pytest.raises(WorkflowIncompleteEvidenceError) as error:
        resolve_screenshots(store, (reissued,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(error)
    assert "sesión" in error.value.detail
    assert "no están en el almacén" not in error.value.detail

    tampered = _manifest_payload(store, manifest)
    tampered["session_id"] = str(OTHER_SESSION_ID)
    _path_of(store, manifest).write_bytes(
        json.dumps(tampered, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    )
    with pytest.raises(WorkflowIncompleteEvidenceError) as rewritten:
        resolve_screenshots(store, (manifest,), _session(_ARTIFACT_A, _ARTIFACT_B))
    _assert_incomplete(rewritten)
