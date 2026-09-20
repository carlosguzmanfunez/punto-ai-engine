"""Evidencia de aceptación contra la superficie solicitada (AP000-OBS-02).

Casos deterministas de A a G sobre un repositorio de prueba con la forma del caso real: la
**homepage** contiene un ``PLACEHOLDER_MAP`` y otra ruta contiene una implementación de mapa. Una
modificación que solo mejore la segunda ruta **no** satisface la solicitud.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from punto.acceptance import (
    extract_references,
    ground_request,
    measurable,
    verify_acceptance,
)
from punto.audit.logger import AuditLogger
from punto.orchestrator.dev_cycle import DevelopmentConfig, DevelopmentCycle
from punto.policy.policy_engine import PolicyEngine
from punto.providers.base import ModelCompletion
from punto.providers.contract import ModelUsage, ProviderRole
from punto.providers.router import ProviderRouter
from punto.schemas.build import BuildRequest
from punto.schemas.dev import RepositoryOperation
from punto.workspace.target import (
    DevelopmentTarget,
    DevelopmentTargetRegistry,
    VerificationCommand,
)

OBJETIVO = "Reemplazar el placeholder actual del mapa de cobertura nacional por un mapa real"
OBJETIVO_CON_LITERAL = "Reemplazar el placeholder actual 'PLACEHOLDER_MAP' por un mapa real"

HOMEPAGE = "src/app/page.tsx"
EXPLORADOR = "src/components/DepartmentExplorer.tsx"
PROPIEDADES = "src/app/propiedades/page.tsx"

CONTENIDO_EXPLORADOR = """import { departments } from "@/lib/honduras";

export function DepartmentExplorer() {
  return (
    <section className="container section">
      <p className="eyebrow dark">Cobertura nacional</p>
      <div className="map-placeholder" aria-label="Mapa conceptual de Honduras">
        <p>PLACEHOLDER_MAP</p>
        <small>En Fase 2 se conectara a un proveedor cartografico.</small>
      </div>
      <div className="department-grid">
        {departments.map((department) => <a key={department}>{department}</a>)}
      </div>
    </section>
  );
}
"""

CONTENIDO_EXPLORADOR_REEMPLAZADO = """import { departments, MapHonduras } from "@/lib/honduras";

export function DepartmentExplorer() {
  return (
    <section className="container section">
      <p className="eyebrow dark">Cobertura nacional</p>
      <MapHonduras />
      <div className="department-grid">
        {departments.map((department) => <a key={department}>{department}</a>)}
      </div>
    </section>
  );
}
"""

CONTENIDO_PROPIEDADES = """import { departments } from "@/lib/honduras";

export default function Propiedades() {
  return (
    <div className="results-grid">
      <div className="results-map">Mapa de departamentos de Honduras</div>
    </div>
  );
}
"""


def _repo(tmp_path: Path) -> Path:
    """Repositorio de prueba con la forma del caso real (solo lectura para las pruebas)."""
    root = tmp_path / "repo"
    (root / "src" / "app" / "propiedades").mkdir(parents=True)
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "lib").mkdir(parents=True)
    (root / HOMEPAGE).write_text(
        'import { DepartmentExplorer } from "@/components/DepartmentExplorer";\n'
        "export default function HomePage() { return <DepartmentExplorer />; }\n",
        encoding="utf-8",
    )
    (root / EXPLORADOR).write_text(CONTENIDO_EXPLORADOR, encoding="utf-8")
    (root / PROPIEDADES).write_text(CONTENIDO_PROPIEDADES, encoding="utf-8")
    (root / "src" / "lib" / "honduras.ts").write_text(
        'export const departments = ["Atlantida", "Colon"];\n', encoding="utf-8"
    )
    return root


def _lector(root: Path):
    """Lectura acotada de los ficheros del repositorio de prueba."""

    def read_text(path: str) -> str:
        return (root / path).read_text(encoding="utf-8")

    return read_text


def _ficheros(root: Path) -> list[str]:
    return sorted(
        item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file()
    )


def _ground(root: Path, objective: str = OBJETIVO, criteria: tuple[str, ...] = ()):
    return ground_request(
        objective=objective,
        criteria=criteria,
        files=_ficheros(root),
        read_text=_lector(root),
    )


# ------------------------------------------------------------- extracción y precisión
def test_una_peticion_ordinaria_no_genera_obligaciones() -> None:
    """Sin referencia a algo existente no se inventa ninguna comprobación (precisión)."""
    referencias = extract_references(
        "Unificar la lista de tipos de propiedad en una sola fuente",
        ("una sola fuente de tipos",),
    )

    assert referencias == ()


def test_una_referencia_a_algo_existente_si_genera_obligacion() -> None:
    """«el placeholder actual» y «reemplazar X» sí son referencias localizables."""
    referencias = extract_references(OBJETIVO, ())

    assert len(referencias) == 1
    assert referencias[0].intent == "REPLACE"
    assert referencias[0].kind == "PLACEHOLDER"


def test_un_texto_entrecomillado_es_un_ancla_literal() -> None:
    """Un literal entrecomillado en la solicitud es la referencia más precisa."""
    referencias = extract_references(OBJETIVO_CON_LITERAL, ())

    assert referencias[0].literal == "PLACEHOLDER_MAP"


def test_el_grounding_localiza_la_superficie_del_placeholder(tmp_path: Path) -> None:
    """El placeholder de la homepage se localiza en su componente, no en otra ruta."""
    referencias = _ground(_repo(tmp_path))

    medibles = measurable(referencias)
    assert medibles
    superficies = {item.path for item in medibles[0].surfaces}
    assert EXPLORADOR in superficies
    assert PROPIEDADES not in superficies, "la ruta alternativa no es el elemento solicitado"


# ---------------------------------------------------------------------- casos A a G
def test_case_a_reemplazo_con_implementacion_relacionada_falla(tmp_path: Path) -> None:
    """A: X sigue en la homepage aunque exista una alternativa en otra ruta ⇒ FAIL."""
    root = _repo(tmp_path)
    referencias = _ground(root, OBJETIVO_CON_LITERAL)

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[PROPIEDADES, "src/lib/honduras.ts"],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros[0].result == "UNSATISFIED"
    assert EXPLORADOR in registros[0].surface
    assert "sigue presente" in registros[0].postcondition


def test_case_d_el_reemplazo_correcto_pasa(tmp_path: Path) -> None:
    """D: X desaparece de la superficie correcta y el reemplazo está presente ⇒ PASS."""
    root = _repo(tmp_path)
    referencias = _ground(root, OBJETIVO_CON_LITERAL)
    (root / EXPLORADOR).write_text(CONTENIDO_EXPLORADOR_REEMPLAZADO, encoding="utf-8")

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[EXPLORADOR],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros[0].result == "SATISFIED"
    assert "ya no está" in registros[0].postcondition


def test_case_b_eliminar_algo_que_sigue_existiendo_falla(tmp_path: Path) -> None:
    """B: se pidió eliminar X y X sigue en el repositorio ⇒ FAIL."""
    root = _repo(tmp_path)
    referencias = _ground(root, "Eliminar el placeholder actual 'PLACEHOLDER_MAP'")

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[PROPIEDADES],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros[0].result == "UNSATISFIED"


def test_case_c_modificar_en_otra_superficie_falla(tmp_path: Path) -> None:
    """C: se pidió modificar X en la superficie A y solo cambió B ⇒ FAIL."""
    root = _repo(tmp_path)
    referencias = _ground(root)

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[PROPIEDADES],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros
    assert all(item.result == "UNSATISFIED" for item in registros)
    assert "no se modificó" in registros[0].postcondition


def test_case_e_conservar_lo_que_desaparece_falla(tmp_path: Path) -> None:
    """E: se pidió conservar el listado de departamentos y desaparece ⇒ FAIL."""
    root = _repo(tmp_path)
    referencias = _ground(
        root,
        "Reemplazar el placeholder actual del mapa por un mapa real",
        ("conservar el listado actual de departamentos de Honduras en la portada",),
    )
    conservar = [item for item in referencias if item.intent == "PRESERVE"]
    assert conservar, "el criterio de conservación se localizó"
    (root / EXPLORADOR).write_text(
        'import { MapHonduras } from "@/lib/honduras";\n'
        "export function DepartmentExplorer() { return <MapHonduras />; }\n",
        encoding="utf-8",
    )

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[EXPLORADOR],
        exists=lambda path: (root / path).is_file(),
    )
    fallos = [item for item in registros if item.intent == "PRESERVE" and item.failed]

    assert fallos, "el listado desapareció y la aceptación lo detecta"
    assert any("desapareció" in item.postcondition for item in fallos)


def test_case_f_crear_en_otra_superficie_falla(tmp_path: Path) -> None:
    """F: se pidió crear Z en la superficie A y solo existe en B ⇒ FAIL."""
    root = _repo(tmp_path)
    referencias = _ground(root, "Crear el mapa interactivo en src/app/page.tsx")

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[PROPIEDADES],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros[0].result == "UNSATISFIED"
    assert HOMEPAGE in registros[0].surface


def test_case_g_un_criterio_subjetivo_no_se_convierte_en_comprobacion_falsa(
    tmp_path: Path,
) -> None:
    """G: «visualmente integrado con el diseño» no se mide en estático: sigue su QA."""
    root = _repo(tmp_path)
    referencias = _ground(root, OBJETIVO, ("el mapa se integra visualmente con el diseno actual",))

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[],
        exists=lambda path: (root / path).is_file(),
    )
    subjetivos = [
        item
        for item in registros
        if "integra visualmente" in item.sentence and item.result != "NOT_MEASURABLE"
    ]

    assert subjetivos == [], "un criterio subjetivo no produce un veredicto estático"


def test_las_superficies_sin_cambio_no_pueden_pasar_por_aceptacion(tmp_path: Path) -> None:
    """Invariante: sin ningún cambio, ninguna obligación de reemplazo puede quedar satisfecha."""
    root = _repo(tmp_path)
    referencias = _ground(root, OBJETIVO_CON_LITERAL)

    registros = verify_acceptance(
        referencias,
        read_text=_lector(root),
        changed_paths=[],
        exists=lambda path: (root / path).is_file(),
    )

    assert registros[0].result == "UNSATISFIED"


@pytest.mark.parametrize(
    "objetivo",
    [
        "Anadir un filtro de precio maximo en el buscador",
        "Mejorar el rendimiento del listado de propiedades",
        "Implementar el alta de leads en el formulario de contacto",
    ],
)
def test_peticiones_normales_no_bloquean_por_grounding(objetivo: str) -> None:
    """Precisión: una petición que no habla de algo existente no genera obligaciones."""
    assert extract_references(objetivo, ()) == ()


# ------------------------------------------- regresión discriminante del caso real
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


def _repo_ciclo(tmp_path: Path) -> Path:
    """Repositorio con homepage y ruta alternativa: la forma exacta del caso del mapa."""
    root = tmp_path / "destino"
    (root / "src" / "app" / "propiedades").mkdir(parents=True)
    (root / "src" / "components").mkdir(parents=True)
    (root / "src" / "lib").mkdir(parents=True)
    (root / HOMEPAGE).write_text(
        'import { DepartmentExplorer } from "@/components/DepartmentExplorer";\n'
        "export default function HomePage() { return <DepartmentExplorer />; }\n",
        encoding="utf-8",
    )
    (root / EXPLORADOR).write_text(CONTENIDO_EXPLORADOR, encoding="utf-8")
    (root / PROPIEDADES).write_text(CONTENIDO_PROPIEDADES, encoding="utf-8")
    (root / "src" / "lib" / "honduras.ts").write_text(
        'export const departments = ["Atlantida", "Colon"];\n', encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=PUNTO Fixture",
        "-c",
        "user.email=fixture@punto.local",
        "commit",
        "-m",
        "base",
    )
    return root


class _GuionProvider:
    """Proveedor guionizado: devuelve las respuestas preparadas, en orden."""

    def __init__(self, router: ProviderRouter, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        router.register_provider("guionizado", self._factory, model="guionizado-1")

    def _factory(self, model: str) -> _GuionProvider:
        del model
        return self

    @property
    def provider(self) -> str:
        """Identificador del proveedor."""
        return "guionizado"

    @property
    def model(self) -> str:
        """Modelo configurado."""
        return "guionizado-1"

    def complete_json(self, **kwargs: Any) -> ModelCompletion:
        """Devuelve la siguiente respuesta del guion."""
        del kwargs
        item = self._responses.pop(0) if self._responses else {"changes": []}
        return ModelCompletion(
            content=item if isinstance(item, str) else json.dumps(item),
            model="guionizado-1",
            usage=ModelUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
            latency_ms=1,
        )

    def redact(self, text: str) -> str:
        """No sanea: el ciclo no puede fiarse de la educación del adaptador."""
        return text

    def close(self) -> None:
        """No hay recursos que liberar."""


def _plan_mapa(*, superficies: tuple[str, ...]) -> dict[str, Any]:
    """Plan válido que declara las superficies que va a tocar."""
    return {
        "summary": "reemplazar el placeholder por un mapa real",
        "files_to_read": [EXPLORADOR],
        "files_to_modify": list(superficies),
        "files_to_create": [],
        "verification_commands": ["focused"],
        "risks": ["romper el listado de departamentos"],
        "acceptance_mapping": ["el mapa real sustituye al placeholder"],
        "functional_chain": [
            {
                "step": "mapa real en la portada",
                "description": "el placeholder desaparece de la portada",
                "verification": "focused",
            }
        ],
    }


def _cambio_superficie(path: str, content: str, criterio: str) -> dict[str, Any]:
    """Cambio sobre una sola superficie."""
    return {
        "summary": "mapa real",
        "changes": [
            {
                "path": path,
                "operation": "MODIFY",
                "content": content,
                "reason": "el mapa real sustituye al placeholder",
                "acceptance_criterion": criterio,
            }
        ],
    }


def _cambio_reparacion(path: str, content: str, criterio: str) -> dict[str, Any]:
    """Cambio de reparación: declara causa raíz, evidencia y efecto esperado."""
    payload = _cambio_superficie(path, content, criterio)
    payload["root_cause"] = (
        "la implementación quedó en otra ruta y el elemento solicitado sigue en la portada"
    )
    payload["evidence"] = ["acceptance: el placeholder sigue en la superficie solicitada"]
    payload["expected_effect"] = "el placeholder desaparece de la superficie solicitada"
    return payload


def _ciclo_mapa(root: Path, respuestas: Sequence[Any], audit: AuditLogger) -> DevelopmentCycle:
    """Ciclo real con proveedor guionizado sobre el repositorio del caso."""
    router = ProviderRouter()
    _GuionProvider(router, respuestas)
    for role in ProviderRole:
        router.assign_role(role, "guionizado")
    target = DevelopmentTarget(
        target_id="punto-inmobiliario-hn",
        repository=root,
        baseline_sha=_git(root, "rev-parse", "HEAD"),
        scope_roots=("src",),
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
        verification=(
            VerificationCommand(
                name="focused",
                argv=("python", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=60.0,
            ),
        ),
        work_branch="ai/mapa",
        max_files_changed=10,
        max_repair_rounds=2,
        command_timeout_seconds=60.0,
    )
    return DevelopmentCycle(
        router=router,
        targets=DevelopmentTargetRegistry({target.target_id: target}),
        config=DevelopmentConfig(max_repair_rounds=2, max_structural_corrections=2),
        audit=audit,
        policy_engine=PolicyEngine.from_config(),
    )


def _solicitud_mapa() -> BuildRequest:
    """Solicitud con la forma de la Task real del mapa."""
    return BuildRequest(
        objective=(
            "Reemplazar el placeholder actual del mapa de cobertura nacional por un mapa "
            "cartografico real e interactivo de Honduras"
        ),
        target_repository="punto-inmobiliario-hn",
        requested_role=ProviderRole.BUILDER,
        acceptance_criteria=("el mapa real es visible en la portada",),
        scope_paths=("src",),
    )


def test_el_preflight_rechaza_un_plan_que_solo_toca_otra_superficie(tmp_path: Path) -> None:
    """El plan que solo trabaja en otra ruta se rechaza **antes** de escribir nada."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [_plan_mapa(superficies=(PROPIEDADES,)), _plan_mapa(superficies=(PROPIEDADES,))],
        audit,
    )

    resultado = ciclo.run(_solicitud_mapa())

    assert resultado.status.value == "DEVELOPMENT_PLAN_REJECTED"
    codigos = [issue.code for issue in resultado.plan_issues]
    assert "PLAN_MISSES_REQUESTED_SURFACE" in codigos
    assert "PLACEHOLDER_MAP" in (root / EXPLORADOR).read_text(encoding="utf-8")
    assert _git(root, "log", "-1", "--format=%s") == "base", "no hubo commit"


def test_la_aceptacion_detecta_la_superficie_incorrecta_y_la_reparacion_la_corrige(
    tmp_path: Path,
) -> None:
    """El caso real: implementación relacionada ⇒ acceptance FAIL; reparación ⇒ VERIFIED."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    ciclo = _ciclo_mapa(
        root,
        [
            # El plan declara las dos superficies (pasa el preflight)…
            _plan_mapa(superficies=(EXPLORADOR, PROPIEDADES)),
            # …pero el primer cambio implementa el mapa solo en la ruta alternativa.
            _cambio_superficie(
                PROPIEDADES,
                'export default function Propiedades() { return <div className="results-map">'
                "Mapa SVG de Honduras</div>; }\n",
                "el mapa real es visible en la portada",
            ),
            # La reparación corrige la superficie solicitada.
            _cambio_reparacion(
                EXPLORADOR,
                CONTENIDO_EXPLORADOR_REEMPLAZADO,
                "el mapa real es visible en la portada",
            ),
        ],
        audit,
    )

    resultado = ciclo.run(_solicitud_mapa())

    assert resultado.status.value == "DEVELOPMENT_COMPLETED", resultado.error
    assert resultado.acceptance_result == "SATISFIED"
    assert "PLACEHOLDER_MAP" not in (root / EXPLORADOR).read_text(encoding="utf-8")
    assert resultado.commit_sha, "el commit existe solo cuando la aceptación está satisfecha"
    eventos = [evento.event_type.value for evento in audit.by_resource(resultado.request_id)]
    assert "DEV_ACCEPTANCE_GROUNDED" in eventos
    assert "DEV_ACCEPTANCE_FAILED" in eventos
    assert "DEV_ACCEPTANCE_VERIFIED" in eventos
    assert eventos.index("DEV_ACCEPTANCE_FAILED") < eventos.index("DEV_ACCEPTANCE_VERIFIED")
    fallo = next(
        evento
        for evento in audit.by_resource(resultado.request_id)
        if evento.event_type.value == "DEV_ACCEPTANCE_FAILED"
    )
    metadatos = dict(fallo.metadata)
    assert metadatos["failed"] == 1
    assert any(EXPLORADOR in item for item in metadatos["surfaces"]), metadatos
    assert all(item.result == "SATISFIED" for item in resultado.acceptance)


def test_sin_corregir_la_superficie_no_hay_verificado(tmp_path: Path) -> None:
    """Sin corrección de la superficie solicitada, la tarea **no** se declara completada."""
    root = _repo_ciclo(tmp_path)
    audit = AuditLogger()
    cambio_ajeno = _cambio_superficie(
        PROPIEDADES,
        'export default function Propiedades() { return <div className="results-map">'
        "Mapa SVG de Honduras</div>; }\n",
        "el mapa real es visible en la portada",
    )
    otra_ruta = _cambio_reparacion(
        "src/lib/honduras.ts",
        'export const departments = ["Atlantida", "Colon"];\nexport const MAPA = true;\n',
        "el mapa real es visible en la portada",
    )
    ciclo = _ciclo_mapa(
        root,
        [
            _plan_mapa(superficies=(EXPLORADOR, PROPIEDADES)),
            cambio_ajeno,
            otra_ruta,
            _cambio_reparacion(
                PROPIEDADES,
                "export default function Propiedades() { return <MapHonduras />; }\n",
                "el mapa real es visible en la portada",
            ),
        ],
        audit,
    )

    resultado = ciclo.run(_solicitud_mapa())

    assert resultado.status.value != "DEVELOPMENT_COMPLETED"
    assert resultado.acceptance_result == "FAILED"
    assert resultado.error_kind == "ACCEPTANCE_NOT_SATISFIED"
    assert resultado.functional_chain_result in {"", "VERIFIED"}
    assert "PLACEHOLDER_MAP" in (root / EXPLORADOR).read_text(encoding="utf-8")

