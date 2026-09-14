"""Snapshot y rollback de reparaciones autónomas (ENGINE-6.1).

Las pruebas escriben archivos de **verdad** en ``tmp_path``: el módulo copia bytes a disco y
restaura byte a byte, así que un doble en memoria no probaría nada de lo que hay que probar. Se
cubren las dos propiedades que sostienen el mecanismo —el rollback deja los hashes idénticos a los
previos y rechaza sin tocar nada cuando no puede demostrar que es seguro— y los rechazos de entrada
malformada.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from punto.schemas.repair import MAX_REPAIR_SNAPSHOT_ENTRIES
from punto.schemas.workflow import WorkflowFailureCode
from punto.workflow.snapshots import SNAPSHOT_DIR_NAME, FileRepairSnapshots, RollbackVerdict

#: Código con el que se declara que hace falta reconciliar en vez de restaurar a ciegas.
_RECONCILIATION = WorkflowFailureCode.WORKFLOW_REPAIR_RECONCILIATION_REQUIRED


def _write(root: Path, relative: str, content: bytes) -> None:
    """Escribe un archivo real del árbol de prueba, creando sus directorios."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _sha256(content: bytes) -> str:
    """sha256 hex del contenido, calculado aquí para no depender del módulo bajo prueba."""
    return hashlib.sha256(content).hexdigest()


def _backup_path(root: Path, snapshot_id: object, relative: str) -> Path:
    """Ruta de la copia de seguridad de un archivo dentro del directorio del snapshot."""
    return root / SNAPSHOT_DIR_NAME / str(snapshot_id) / relative


# ---------------------------------------------------------------------------
# Captura
# ---------------------------------------------------------------------------
def test_snapshot_de_dos_archivos_captura_hash_tamano_y_ausencia(tmp_path: Path) -> None:
    """Un archivo existente se captura con hash y tamaño; uno ausente, declarado ausente.

    El ausente no es un error: es el dato que después significa «bórralo», y sin él el rollback no
    sabría si el archivo apareció por la reparación o ya estaba.
    """
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)

    snapshot = snapshots.create(repair_id=uuid4(), cycle=3, paths=["src/app.py", "src/nuevo.py"])

    assert snapshot.cycle == 3
    assert snapshot.workspace_path == tmp_path.as_posix()
    assert [entry.path for entry in snapshot.entries] == ["src/app.py", "src/nuevo.py"]
    first, second = snapshot.entries
    assert first.sha256 == _sha256(b"contenido-a")
    assert first.bytes == len(b"contenido-a")
    assert first.existed is True
    assert second.sha256 == ""
    assert second.bytes == 0
    assert second.existed is False
    assert snapshot.workspace_fingerprint == snapshots.fingerprint(snapshot)


def test_la_copia_queda_fuera_del_control_de_versiones(tmp_path: Path) -> None:
    """La copia real está en disco y su directorio se declara ignorado."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)

    snapshot = snapshots.create(repair_id=uuid4(), cycle=1, paths=["src/app.py"])

    assert _backup_path(tmp_path, snapshot.snapshot_id, "src/app.py").read_bytes() == b"contenido-a"
    ignore = tmp_path / SNAPSHOT_DIR_NAME / ".gitignore"
    assert "*" in ignore.read_text(encoding="utf-8").splitlines()


def test_rutas_duplicadas_y_con_otra_grafia_se_capturan_una_vez(tmp_path: Path) -> None:
    """``./src/app.py`` y ``src\\app.py`` son el mismo archivo: una sola entrada."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)

    snapshot = snapshots.create(
        repair_id=uuid4(), cycle=1, paths=["./src/app.py", "src\\app.py", "src/app.py"]
    )

    assert [entry.path for entry in snapshot.entries] == ["src/app.py"]


def test_fingerprint_determinista_e_independiente_del_orden(tmp_path: Path) -> None:
    """La huella depende del contenido capturado, no del orden en que llegaron las rutas."""
    _write(tmp_path, "a.py", b"a")
    _write(tmp_path, "b.py", b"b")
    snapshots = FileRepairSnapshots(tmp_path)

    first = snapshots.create(repair_id=uuid4(), cycle=1, paths=["a.py", "b.py"])
    second = snapshots.create(repair_id=uuid4(), cycle=1, paths=["b.py", "a.py"])

    assert snapshots.fingerprint(first) == snapshots.fingerprint(second)
    assert first.workspace_fingerprint == second.workspace_fingerprint

    _write(tmp_path, "b.py", b"b-modificado")
    third = snapshots.create(repair_id=uuid4(), cycle=1, paths=["a.py", "b.py"])

    assert snapshots.fingerprint(third) != snapshots.fingerprint(first)


# ---------------------------------------------------------------------------
# Verificación
# ---------------------------------------------------------------------------
def test_verify_confirma_el_estado_capturado(tmp_path: Path) -> None:
    """``verify`` es True mientras el árbol esté como se capturó, y False en cuanto cambia."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(
        repair_id=uuid4(), cycle=1, paths=["src/app.py", "src/ausente.py"]
    )

    assert snapshots.verify(snapshot) is True

    _write(tmp_path, "src/app.py", b"contenido-b")
    assert snapshots.verify(snapshot) is False

    _write(tmp_path, "src/app.py", b"contenido-a")
    assert snapshots.verify(snapshot) is True

    _write(tmp_path, "src/ausente.py", b"aparece")
    assert snapshots.verify(snapshot) is False


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def test_rollback_restaura_bytes_exactos_y_borra_el_archivo_nuevo(tmp_path: Path) -> None:
    """El rollback deja el árbol como estaba: hashes idénticos y el archivo nuevo borrado."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(
        repair_id=uuid4(), cycle=1, paths=["src/app.py", "src/nuevo.py"]
    )

    # La «reparación»: modifica el archivo existente y crea el que no existía.
    _write(tmp_path, "src/app.py", b"reparado")
    _write(tmp_path, "src/nuevo.py", b"creado por la reparacion")
    expected = {
        "src/app.py": snapshots.digest("src/app.py"),
        "src/nuevo.py": snapshots.digest("src/nuevo.py"),
    }

    verdict = snapshots.rollback(snapshot=snapshot, expected=expected)

    assert isinstance(verdict, RollbackVerdict)
    assert verdict.rolled_back is True
    assert verdict.code is None
    assert verdict.restored_files == ("src/app.py", "src/nuevo.py")
    assert (tmp_path / "src/app.py").read_bytes() == b"contenido-a"
    assert snapshots.digest("src/app.py") == snapshot.entries[0].sha256
    assert not (tmp_path / "src/nuevo.py").exists()
    assert snapshots.digest("src/nuevo.py") == ""


def test_rollback_rechazado_si_el_estado_actual_no_es_el_esperado(tmp_path: Path) -> None:
    """Si el hash actual no es el que dejó la reparación, no se toca nada y se pide reconciliar."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(repair_id=uuid4(), cycle=1, paths=["src/app.py"])
    _write(tmp_path, "src/app.py", b"reparado")

    verdict = snapshots.rollback(
        snapshot=snapshot, expected={"src/app.py": _sha256(b"un-estado-que-no-es")}
    )

    assert verdict.rolled_back is False
    assert verdict.code is _RECONCILIATION
    assert verdict.restored_files == ()
    assert "no se restauró nada" in verdict.detail
    assert (tmp_path / "src/app.py").read_bytes() == b"reparado"


def test_rollback_rechazado_si_no_consta_el_estado_esperado(tmp_path: Path) -> None:
    """Una clave ausente en ``expected`` es «no consta», no «no existe»: no se restaura a ciegas."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(repair_id=uuid4(), cycle=1, paths=["src/app.py"])
    _write(tmp_path, "src/app.py", b"reparado")

    verdict = snapshots.rollback(snapshot=snapshot, expected={})

    assert verdict.rolled_back is False
    assert verdict.code is _RECONCILIATION
    assert "no consta el estado" in verdict.detail
    assert (tmp_path / "src/app.py").read_bytes() == b"reparado"


def test_rollback_rechazado_con_efectos_externos(tmp_path: Path) -> None:
    """Deshacer archivos no deshace un efecto externo: se declara, no se finge que se deshizo."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(repair_id=uuid4(), cycle=1, paths=["src/app.py"])
    _write(tmp_path, "src/app.py", b"reparado")
    expected = {"src/app.py": snapshots.digest("src/app.py")}

    verdict = snapshots.rollback(
        snapshot=snapshot, expected=expected, external_side_effects=True
    )

    assert verdict.rolled_back is False
    assert verdict.code is _RECONCILIATION
    assert "efectos externos" in verdict.detail
    assert (tmp_path / "src/app.py").read_bytes() == b"reparado"


def test_rollback_rechazado_si_falta_la_copia_de_seguridad(tmp_path: Path) -> None:
    """Sin la copia no hay contenido previo que escribir: el rollback se rechaza, no se inventa."""
    _write(tmp_path, "src/app.py", b"contenido-a")
    snapshots = FileRepairSnapshots(tmp_path)
    snapshot = snapshots.create(repair_id=uuid4(), cycle=1, paths=["src/app.py"])
    _write(tmp_path, "src/app.py", b"reparado")
    expected = {"src/app.py": snapshots.digest("src/app.py")}
    _backup_path(tmp_path, snapshot.snapshot_id, "src/app.py").unlink()

    verdict = snapshots.rollback(snapshot=snapshot, expected=expected)

    assert verdict.rolled_back is False
    assert verdict.code is _RECONCILIATION
    assert "falta la copia de seguridad" in verdict.detail
    assert (tmp_path / "src/app.py").read_bytes() == b"reparado"


# ---------------------------------------------------------------------------
# Entradas rechazadas
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_path",
    ["../fuera.py", "src/../../fuera.py", "/etc/passwd", "C:/Windows/system32/evil.dll", ""],
)
def test_rechaza_rutas_que_salen_de_la_raiz(tmp_path: Path, bad_path: str) -> None:
    """Una ruta absoluta o con ``..`` no entra en un snapshot: se rechaza sin tocar el disco."""
    snapshots = FileRepairSnapshots(tmp_path)

    with pytest.raises(ValueError):
        snapshots.create(repair_id=uuid4(), cycle=1, paths=[bad_path])

    with pytest.raises(ValueError):
        snapshots.digest(bad_path)

    assert not (tmp_path / SNAPSHOT_DIR_NAME).exists()


def test_rechaza_un_almacen_de_secretos(tmp_path: Path) -> None:
    """La copia es contenido en claro: un ``.env`` no se captura ni se deja copia a medias."""
    _write(tmp_path, ".env", b"API_KEY=secreta")
    snapshots = FileRepairSnapshots(tmp_path)

    with pytest.raises(ValueError):
        snapshots.create(repair_id=uuid4(), cycle=1, paths=[".env"])

    base = tmp_path / SNAPSHOT_DIR_NAME
    copies = [child for child in base.iterdir() if child.is_dir()] if base.exists() else []
    assert copies == []
    assert (tmp_path / ".env").read_bytes() == b"API_KEY=secreta"


def test_rechaza_un_directorio(tmp_path: Path) -> None:
    """Un directorio no tiene contenido que capturar ni hash que comparar."""
    (tmp_path / "carpeta").mkdir()
    snapshots = FileRepairSnapshots(tmp_path)

    with pytest.raises(ValueError):
        snapshots.create(repair_id=uuid4(), cycle=1, paths=["carpeta"])

    with pytest.raises(ValueError):
        snapshots.digest("carpeta")


def test_rechaza_mas_entradas_que_el_contrato(tmp_path: Path) -> None:
    """Más entradas que ``MAX_REPAIR_SNAPSHOT_ENTRIES`` harían el registro inválido."""
    snapshots = FileRepairSnapshots(tmp_path)
    paths = [f"f{i}.py" for i in range(MAX_REPAIR_SNAPSHOT_ENTRIES + 1)]

    with pytest.raises(ValueError):
        snapshots.create(repair_id=uuid4(), cycle=1, paths=paths)

    assert not (tmp_path / SNAPSHOT_DIR_NAME).exists()
