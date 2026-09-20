"""AP000-OBS-05: la clase de autoridad de un recurso sale de una taxonomía técnica conocida.

Caso real: el plan de la Task `2e7822a0` se rechazó en planificación con `PLAN_REQUIRES_HUMAN` y la
regla `unknown-resource`, y el recurso que la disparaba era una hoja de estilos normal
(`src/app/globals.css`) dentro del scope `src`. Una extensión de frontend corriente no puede
convertirse en «clase desconocida».

Lo que se fija aquí, con el motor real:

A. una hoja de estilos dentro del scope ya **no** produce `unknown-resource`, y un plan que solo
   toca recursos conocidos del producto ya no exige persona (ciclo real, de extremo a extremo);
B. un `.tsx` normal conserva su clasificación correcta (y el resto de la taxonomía no se mueve);
C. un recurso verdaderamente desconocido sigue fallando cerrado;
D. un recurso reconocido **fuera del scope** no obtiene autoridad por su extensión;
E. los controles de secretos, destructivos, constitucionales y de workspace no se debilitan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from punto.policy.envelope import (
    AdaptiveAuthorityEnvelope,
    EnvelopeOperation,
    Environment,
    ExternalEffect,
    OperationRisk,
    Provenance,
    ResourceClass,
    VerificationStrength,
)
from punto.schemas.dev import ChangeOperation, RepositoryOperation
from punto.workspace.repository import (
    GovernedRepository,
    RepositoryDenied,
    RepositoryPolicy,
)
from test_human_console import (
    TARGET_ID,
    _app,
    _cambio,
    _git,
    _plan,
    _repos,
    _target,
)

SOLICITUD: dict[str, Any] = {
    "objective": "unificar la lista de tipos en una sola fuente",
    "target_id": TARGET_ID,
    "acceptance_criteria": ["una sola fuente de tipos"],
    "scope_paths": ["src"],
}

#: Recursos del plan real del caso (el `.css` es el que disparaba la clase desconocida).
RECURSOS_DEL_CASO: tuple[str, ...] = (
    "src/app/globals.css",
    "src/app/propiedades/page.tsx",
    "src/components/DepartmentExplorer.tsx",
    "src/components/HondurasMap.tsx",
    "tests/honduras-map.test.mjs",
)


@pytest.fixture
def envelope() -> AdaptiveAuthorityEnvelope:
    """Sobre con las rutas constitucionales que declara la constitución del motor."""
    return AdaptiveAuthorityEnvelope(
        constitutional_paths=(
            "config/constitution.yaml",
            "config/permissions.yaml",
            "config/budgets.yaml",
            "config/risk-rules.yaml",
        )
    )


def _write(files: tuple[str, ...], **overrides: object) -> OperationRisk:
    """Perfil de un cambio local típico sobre esos recursos."""
    payload: dict[str, object] = {
        "operation": EnvelopeOperation.PLAN_APPLY,
        "resources": files,
        "environment": Environment.LOCAL,
        "reversible": True,
        "verification_strength": VerificationStrength.STRONG,
        "provenance": Provenance.PUNTO_POLICY,
        "evidence": ("evidencia del plan",),
    }
    payload.update(overrides)
    return OperationRisk(**payload)  # type: ignore[arg-type]


def _repo_con_css(tmp_path: Path) -> tuple[Path, Path]:
    """Repositorio del montaje con una hoja de estilos de la aplicación."""
    repo, remoto = _repos(tmp_path)
    (repo / "src" / "app").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app" / "globals.css").write_text("body { margin: 0 }\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=console",
        "-c",
        "user.email=console@punto.local",
        "commit",
        "-m",
        "estilos base",
    )
    return repo, remoto


def _plan_con_css(fuera_de_scope: bool = False) -> dict[str, Any]:
    """Plan válido que toca la hoja de estilos (dentro o fuera del scope declarado)."""
    payload = _plan()
    hoja = "public/estilos.css" if fuera_de_scope else "src/app/globals.css"
    payload["files_to_modify"] = ["src/lib/tipos.ts", hoja]
    return payload


def _cambio_con_css(fuera_de_scope: bool = False) -> dict[str, Any]:
    """Cambio que acompaña al plan."""
    payload = _cambio()
    payload["changes"].append(
        {
            "path": "public/estilos.css" if fuera_de_scope else "src/app/globals.css",
            "operation": "MODIFY",
            "content": "body { margin: 0; }\n",
            "reason": "la aplicación necesita su hoja de estilos",
            "acceptance_criterion": "una sola fuente de tipos",
        }
    )
    return payload


# ------------------------------------------- A · la hoja de estilos ya no es desconocida
def test_a_una_hoja_de_estilos_en_scope_no_es_una_clase_desconocida(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """A · el clasificador reconoce el tipo y el sobre no exige persona por una extensión."""
    assert envelope.classify("src/app/globals.css") is ResourceClass.APPLICATION_CODE

    decision = envelope.assess(_write(RECURSOS_DEL_CASO))

    assert "unknown-resource" not in decision.rule_names
    assert not decision.requires_human
    assert ResourceClass.UNKNOWN not in decision.resource_classes
    assert ResourceClass.APPLICATION_CODE in decision.resource_classes


def test_a_un_plan_de_frontend_con_hoja_de_estilos_se_ejecuta_sin_gate(tmp_path: Path) -> None:
    """A · de extremo a extremo: el plan con `.css` ya no se detiene pidiendo una persona."""
    repo, remoto = _repo_con_css(tmp_path)
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, dependencies = _app(
        target=target, respuestas=[_plan_con_css(), _cambio_con_css()]
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    assert tarea["stage"] == "DEVELOPMENT_COMPLETED", tarea
    assert tarea["development"]["status"] == "DEVELOPMENT_COMPLETED"
    assert tarea["development"]["error_kind"] == ""
    assert tarea["gates"] == [], "ninguna hoja de estilos exige una persona"
    assert client.get("/console/human-gates").json()["pending"] == 0
    # El cambio real se aplicó y quedó confirmado.
    assert tarea["development"]["applied"] == ["src/lib/tipos.ts", "src/app/globals.css"]
    hoja = (repo / "src" / "app" / "globals.css").read_text(encoding="utf-8")
    assert hoja == "body { margin: 0; }\n"
    assert tarea["development"]["commit_sha"]
    assert dependencies.gates.list_all() == ()


# ------------------------------------------- B · la taxonomía conocida no se mueve
@pytest.mark.parametrize(
    ("ruta", "clase"),
    [
        # Frontend y activos del producto (lo que se corrige).
        ("src/app/globals.css", ResourceClass.APPLICATION_CODE),
        ("src/styles/main.scss", ResourceClass.APPLICATION_CODE),
        ("src/styles/base.less", ResourceClass.APPLICATION_CODE),
        ("src/app/layout.html", ResourceClass.APPLICATION_CODE),
        ("src/components/widget.vue", ResourceClass.APPLICATION_CODE),
        ("src/components/widget.svelte", ResourceClass.APPLICATION_CODE),
        ("src/app/icon.svg", ResourceClass.APPLICATION_CODE),
        ("public/imagen.png", ResourceClass.APPLICATION_CODE),
        ("public/fuente.woff2", ResourceClass.APPLICATION_CODE),
        # Código y clases que ya existían.
        ("src/components/HondurasMap.tsx", ResourceClass.APPLICATION_CODE),
        ("src/lib/tipos.ts", ResourceClass.APPLICATION_CODE),
        ("src/app/propiedades/page.tsx", ResourceClass.APPLICATION_CODE),
        ("tests/honduras-map.test.mjs", ResourceClass.TEST_CODE),
        ("src/lib/datos.sql", ResourceClass.DATA_SEED),
        ("README.md", ResourceClass.DOCUMENTATION),
        ("config/aplicacion.yaml", ResourceClass.PROJECT_CONFIGURATION),
        # La constitución del motor es intocable: su ruta no es «configuración» cualquiera.
        ("config/risk-rules.yaml", ResourceClass.CONSTITUTIONAL_CONFIG),
        ("package.json", ResourceClass.DEPENDENCY_MANIFEST),
        (".env", ResourceClass.SECRET_STORE),
        ("Dockerfile", ResourceClass.INFRASTRUCTURE),
        ("src/auth/sesion.ts", ResourceClass.IDENTITY_AUTH),
        ("src/lib/payment/stripe.ts", ResourceClass.PAYMENT_CODE),
        ("dist/bundle.js", ResourceClass.BUILD_ARTIFACT),
    ],
)
def test_b_la_taxonomia_conocida_clasifica_igual(
    envelope: AdaptiveAuthorityEnvelope, ruta: str, clase: ResourceClass
) -> None:
    """B · reconocer el frontend no mueve ninguna otra clase de la taxonomía."""
    assert envelope.classify(ruta) is clase


# ------------------------------------------- C · lo desconocido sigue fallando cerrado
@pytest.mark.parametrize(
    "ruta",
    [
        "src/app/recursos.bin",
        "src/lib/datos.binario",
        "src/app/plantilla.qqq",
        "Makefile",
        "src/app/sin-extension",
    ],
)
def test_c_un_recurso_desconocido_sigue_fallando_cerrado(
    envelope: AdaptiveAuthorityEnvelope, ruta: str
) -> None:
    """C · lo que no está en ninguna clase conocida sigue exigiendo persona."""
    assert envelope.classify(ruta) is ResourceClass.UNKNOWN

    decision = envelope.assess(_write((ruta,)))

    assert decision.requires_human
    assert "unknown-resource" in decision.rule_names
    assert ResourceClass.UNKNOWN in decision.resource_classes


def test_c_reconocer_un_tipo_no_concede_permiso_por_si_solo(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """C · la clase reconoce el tipo; la operación sigue decidiendo (borrado exige persona)."""
    decision = envelope.assess(
        _write(("src/app/globals.css",), operation=EnvelopeOperation.DELETE, destructive=True)
    )

    assert decision.requires_human
    assert "destructive-local-data" in decision.rule_names
    assert not decision.autonomous


# ------------------------------------------- D · fuera de scope no hay autoridad
def test_d_un_css_fuera_del_scope_no_obtiene_autoridad_por_su_extension(
    envelope: AdaptiveAuthorityEnvelope, tmp_path: Path
) -> None:
    """D · el sobre reconoce el tipo, pero el scope de la Task sigue mandando."""
    # El sobre, por sí solo, no exige persona por una hoja de estilos…
    assert not envelope.assess(_write(("public/estilos.css",))).requires_human

    # …y el ciclo la rechaza igual porque queda fuera del scope concedido.
    repo, remoto = _repo_con_css(tmp_path)
    (repo / "public").mkdir(parents=True, exist_ok=True)
    (repo / "public" / "estilos.css").write_text("body { margin: 0 }\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=console",
        "-c",
        "user.email=console@punto.local",
        "commit",
        "-m",
        "estilos publicos",
    )
    target = _target(repo, remoto=remoto)
    client, _audit, _target_obj, _deps = _app(
        target=target,
        respuestas=[
            _plan_con_css(fuera_de_scope=True),
            _plan_con_css(fuera_de_scope=True),
            _cambio_con_css(fuera_de_scope=True),
        ],
    )

    tarea = client.post("/console/tasks", json=SOLICITUD).json()

    # El sobre, por sí solo, **habría** aceptado el plan: la extensión ya es una clase conocida…
    evidencia = tarea["development"]["authority_evidence"][0]
    assert evidencia["outcome"] == "ALLOW"
    assert evidencia["authority_class"] == "AUTONOMOUS_LOCAL"
    assert "public/estilos.css" in evidencia["resources"]
    # …y aun así el scope concedido por la Task lo rechaza: reconocer el tipo no da permiso.
    assert tarea["stage"] == "DEVELOPMENT_FAILED", tarea
    assert tarea["development"]["error_kind"] == "PLAN_OUT_OF_SCOPE"
    codes = {item["code"] for item in tarea["development"]["plan_issues"]}
    assert "PLAN_OUT_OF_SCOPE" in codes
    assert "queda fuera del alcance" in tarea["development"]["error"]
    assert tarea["development"]["applied"] == []
    assert tarea["gates"] == []
    assert (repo / "public" / "estilos.css").read_text(encoding="utf-8") == "body { margin: 0 }\n"


def test_d_un_css_fuera_del_workspace_no_obtiene_autoridad_por_su_extension(
    tmp_path: Path,
) -> None:
    """D · reconocer la extensión no autoriza nada fuera del workspace."""
    repo, _remoto = _repos(tmp_path)
    policy = RepositoryPolicy(
        allowed_operations=frozenset(
            {
                RepositoryOperation.READ,
                RepositoryOperation.WRITE,
                RepositoryOperation.CREATE,
                RepositoryOperation.DELETE,
                RepositoryOperation.EXECUTE,
                RepositoryOperation.COMMIT,
            }
        ),
        allowed_commands=frozenset({"git", "python"}),
        allowed_command_lines=(),
        scope_roots=("src",),
        max_files_changed=12,
        command_timeout_seconds=60.0,
    )
    repository = GovernedRepository(root=repo, task_id=uuid4(), policy=policy, branch="")

    # Reconocer la extensión no autoriza nada fuera del workspace ni fuera del alcance…
    with pytest.raises(RepositoryDenied):
        repository.resolve("../estilos.css")
    with pytest.raises(RepositoryDenied):
        repository.resolve("public/estilos.css")
    with pytest.raises(RepositoryDenied):
        repository.write_text(
            "../public/estilos.css", "body { margin: 0 }\n", operation=ChangeOperation.MODIFY
        )
    with pytest.raises(RepositoryDenied):
        repository.read_text("../public/estilos.css")
    # …y el mismo tipo, dentro del workspace y del scope, sí resuelve.
    assert repository.resolve("src/app/globals.css").name == "globals.css"


# ------------------------------------------- E · los controles no se debilitan
@pytest.mark.parametrize(
    "ruta",
    [
        ".env",
        "src/app/.env.local",
        "config/credentials.json",
        "deploy/server.key",
        "secrets/id_rsa",
    ],
)
def test_e_los_secretos_siguen_denegados(
    envelope: AdaptiveAuthorityEnvelope, ruta: str
) -> None:
    """E · reconocer extensiones de producto no abre la puerta a un almacén de credenciales."""
    assert envelope.classify(ruta) is ResourceClass.SECRET_STORE

    decision = envelope.assess(_write((ruta,)))

    assert decision.prohibited
    assert "secret-store" in decision.rule_names


def test_e_lo_constitucional_sigue_denegado(envelope: AdaptiveAuthorityEnvelope) -> None:
    """E · la configuración constitucional sigue siendo intocable para el ciclo."""
    decision = envelope.assess(_write(("config/constitution.yaml",)))

    assert decision.prohibited
    assert "constitutional-resource" in decision.rule_names


def test_e_borrar_un_css_preexistente_sigue_exigiendo_persona(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """E · reconocer el tipo no convierte un borrado en autonomía."""
    decision = envelope.assess(
        _write(("src/app/globals.css",), operation=EnvelopeOperation.DELETE, destructive=True)
    )

    assert decision.requires_human
    assert not decision.autonomous


def test_e_un_efecto_externo_sigue_exigiendo_persona(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """E · publicar o crear coste sigue siendo decisión humana, con cualquier extensión."""
    decision = envelope.assess(
        _write(("src/app/globals.css",), external_effect=ExternalEffect.PUBLISH)
    )

    assert decision.requires_human
    assert "external-publish" in decision.rule_names


# ------------------------------------------- el caso real, recurso a recurso
def test_el_plan_real_ya_no_tiene_ningun_recurso_desconocido(
    envelope: AdaptiveAuthorityEnvelope,
) -> None:
    """Control directo del caso: ningún recurso del plan real cae en la clase desconocida."""
    clases = {ruta: envelope.classify(ruta).value for ruta in RECURSOS_DEL_CASO}

    assert clases == {
        "src/app/globals.css": "application_code",
        "src/app/propiedades/page.tsx": "application_code",
        "src/components/DepartmentExplorer.tsx": "application_code",
        "src/components/HondurasMap.tsx": "application_code",
        "tests/honduras-map.test.mjs": "test_code",
    }
    assert "unknown" not in clases.values()
    assert not envelope.assess(_write(RECURSOS_DEL_CASO)).requires_human
