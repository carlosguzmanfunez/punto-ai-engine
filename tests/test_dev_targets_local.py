"""Destinos de desarrollo declarados en la configuración local de la máquina.

El dashboard ofrece un destino porque existe una declaración confiable: la variable
``PUNTO_DEV_TARGETS`` o el archivo ``<config>/targets.local.yaml``. Lo que se demuestra aquí:

- el archivo local se lee, se valida con las **mismas** reglas que la variable de entorno y resuelve
  la clave a la ruta real del repositorio;
- sin declaración no hay destinos (y no es un error);
- una declaración que no se puede usar **falla fuerte** en vez de conceder algo;
- la variable de entorno tiene precedencia sobre el archivo;
- un entorno explícito no lee la configuración de la máquina por accidente.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from punto.workspace.target import (
    DEV_TARGETS_ENV,
    LOCAL_TARGETS_FILE,
    DevelopmentTargetError,
    load_development_targets,
    load_local_development_targets,
)

SHA = "a" * 40


def _git(root: Path, *args: str) -> str:
    """Git en el repositorio de la prueba."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} falló: {completed.stderr}")
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    """Repositorio Git mínimo: lo que un destino tiene que ser de verdad."""
    repo = tmp_path / "repositorio"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=destino",
        "-c",
        "user.email=destino@punto.local",
        "commit",
        "-m",
        "base",
    )
    return repo


def _declaracion(repo: Path, **extra: Any) -> dict[str, Any]:
    """Destino válido, con lo que cada prueba quiera cambiar."""
    entrada: dict[str, Any] = {
        "display_name": "Punto Inmobiliario HN",
        "repository": str(repo),
        "baseline_sha": SHA,
        "scope_roots": ["src"],
        "work_branch": "ai/tarea-local",
        "verification": {"focused": {"argv": ["node", "--version"]}},
    }
    entrada.update(extra)
    return {"targets": {"punto-inmobiliario-hn": entrada}}


def _escribir(config_dir: Path, contenido: dict[str, Any] | str) -> Path:
    """Escribe el archivo local de destinos en un directorio de configuración."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / LOCAL_TARGETS_FILE
    texto = contenido if isinstance(contenido, str) else yaml.safe_dump(contenido, sort_keys=False)
    path.write_text(texto, encoding="utf-8")
    return path


# ------------------------------------------------------------- lectura del archivo local
def test_el_archivo_local_declara_el_destino_y_resuelve_la_ruta(tmp_path: Path) -> None:
    """La clave del destino resuelve al repositorio declarado, con su nombre humano."""
    repo = _repo(tmp_path)
    config_dir = tmp_path / "config"
    _escribir(config_dir, _declaracion(repo))

    destinos = load_local_development_targets(config_dir)

    assert set(destinos) == {"punto-inmobiliario-hn"}
    destino = destinos["punto-inmobiliario-hn"]
    assert destino.human_name == "Punto Inmobiliario HN"
    assert destino.repository == repo
    assert destino.scope_roots == ("src",)
    assert destino.command_names() == ("focused",)
    assert destino.baseline_sha == SHA
    assert destino.publishable is False, "sin rama y URL declaradas, producción no se adivina"


def test_el_nombre_humano_cae_en_la_clave_si_no_se_declara(tmp_path: Path) -> None:
    """Sin nombre declarado, la interfaz tiene algo que mostrar: la clave del destino."""
    repo = _repo(tmp_path)
    config_dir = tmp_path / "config"
    declaracion = _declaracion(repo)
    del declaracion["targets"]["punto-inmobiliario-hn"]["display_name"]
    _escribir(config_dir, declaracion)

    destino = load_local_development_targets(config_dir)["punto-inmobiliario-hn"]

    assert destino.display_name == ""
    assert destino.human_name == "punto-inmobiliario-hn"


def test_la_variable_de_entorno_y_el_archivo_se_resuelven_igual(tmp_path: Path) -> None:
    """El archivo local usa el mismo mecanismo: entorno o archivo llevan al mismo destino."""
    repo = _repo(tmp_path)
    config_dir = tmp_path / "config"
    _escribir(config_dir, _declaracion(repo))
    entrada = _declaracion(repo)["targets"]

    desde_archivo = load_development_targets({"PUNTO_CONFIG_DIR": str(config_dir)})
    desde_entorno = load_development_targets({DEV_TARGETS_ENV: json.dumps(entrada)})

    assert desde_archivo == desde_entorno


def test_el_directorio_tambien_se_resuelve_por_la_raiz_del_repositorio(tmp_path: Path) -> None:
    """``PUNTO_REPO_ROOT`` es la otra forma de decir dónde está ``config/``."""
    repo = _repo(tmp_path)
    root = tmp_path / "motor"
    _escribir(root / "config", _declaracion(repo))

    destinos = load_development_targets({"PUNTO_REPO_ROOT": str(root)})

    assert set(destinos) == {"punto-inmobiliario-hn"}


def test_la_variable_de_entorno_tiene_precedencia_sobre_el_archivo(tmp_path: Path) -> None:
    """Una declaración explícita del operador manda sobre la local."""
    repo = _repo(tmp_path)
    config_dir = tmp_path / "config"
    _escribir(config_dir, _declaracion(repo))
    explicitos = {"otro-destino": {**_declaracion(repo)["targets"]["punto-inmobiliario-hn"]}}

    destinos = load_development_targets(
        {"PUNTO_CONFIG_DIR": str(config_dir), DEV_TARGETS_ENV: json.dumps(explicitos)}
    )

    assert set(destinos) == {"otro-destino"}


def test_sin_declaracion_no_hay_destinos_y_no_es_un_error(tmp_path: Path) -> None:
    """Un motor sin destinos declarados arranca: simplemente no acepta trabajo."""
    vacio = tmp_path / "config"
    vacio.mkdir()
    solo_archivo_vacio = tmp_path / "config-vacio"
    _escribir(solo_archivo_vacio, "")

    assert load_local_development_targets(vacio) == {}
    assert load_local_development_targets(solo_archivo_vacio) == {}
    assert load_development_targets({"PUNTO_CONFIG_DIR": str(vacio)}) == {}


def test_un_entorno_explicito_no_lee_la_configuracion_de_la_maquina() -> None:
    """Sin claves de configuración en el entorno dado, no se busca el ``config/`` del puesto."""
    assert load_development_targets({}) == {}


# --------------------------------------------------- una declaración inválida falla fuerte
@pytest.mark.parametrize(
    "contenido",
    [
        "[una lista, no un objeto]",
        "targets: [una lista]",
        "otra_cosa:\n  x: 1\n",
        "targets:\n  destino:\n    repository: 'ruta/relativa'\n    baseline_sha: '" + SHA + "'\n",
        "targets:\n  destino:\n    repository: '{repo}'\n    baseline_sha: 'no-es-un-sha'\n",
        (
            "targets:\n  destino:\n    repository: '{repo}'\n    baseline_sha: '" + SHA + "'\n"
            "    verification:\n      mala:\n        argv: ['curl', 'https://ejemplo']\n"
        ),
        (
            "targets:\n  destino:\n    repository: '{repo}'\n    baseline_sha: '" + SHA + "'\n"
            "    verification:\n      mala:\n        argv: ['npm', 'install']\n"
        ),
    ],
)
def test_una_declaracion_invalida_no_concede_ningun_destino(
    tmp_path: Path, contenido: str
) -> None:
    """Ni una ruta no-Git, ni un baseline falso, ni un programa fuera de la allowlist."""
    repo = _repo(tmp_path)
    config_dir = tmp_path / "config"
    _escribir(config_dir, contenido.replace("{repo}", str(repo).replace("\\", "/")))

    with pytest.raises(DevelopmentTargetError):
        load_local_development_targets(config_dir)


def test_una_ruta_que_no_es_repositorio_git_es_rechazada(tmp_path: Path) -> None:
    """Un directorio cualquiera no es un destino: PUNTO lo dice en vez de trabajar a ciegas."""
    cualquiera = tmp_path / "no-es-repo"
    cualquiera.mkdir()
    config_dir = tmp_path / "config"
    _escribir(config_dir, _declaracion(cualquiera))

    with pytest.raises(DevelopmentTargetError, match="no es un repositorio Git"):
        load_local_development_targets(config_dir)


def test_demasiados_destinos_es_un_error(tmp_path: Path) -> None:
    """La cota de destinos también aplica al archivo local."""
    repo = _repo(tmp_path)
    entrada = _declaracion(repo)["targets"]["punto-inmobiliario-hn"]
    config_dir = tmp_path / "config"
    _escribir(config_dir, {"targets": {f"destino-{i}": entrada for i in range(9)}})

    with pytest.raises(DevelopmentTargetError, match="el máximo"):
        load_local_development_targets(config_dir)
