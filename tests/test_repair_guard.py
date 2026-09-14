"""Puertas deterministas del guard de reparación autónoma (ENGINE-6.1).

Cada prueba aísla **una** maniobra y exige su línea de violación concreta: si el guard dejara de ver
una de ellas, el nombre de la prueba que falla dice cuál. Además se comprueban las dos propiedades
que hacen útil al guard —un intento limpio pasa, y varias maniobras en el mismo diff se acumulan en
un solo veredicto— y que el código de fallo es el del contrato, no uno inventado.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest

from punto.policy.permissions import CONSTITUTIONAL_PROTECTED_PATHS
from punto.schemas.repair import MAX_REPAIR_FILES, RepairPlan
from punto.schemas.workflow import WorkflowFailureCode
from punto.workflow.repair import PROTECTED_PATHS
from punto.workflow.repair_guard import GuardVerdict, RepairGuard

#: Código que el contrato reserva a una reparación que se sale del plan.
_SCOPE_CODE = WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION
#: Diff mínimo que sí cambia algo: un intento limpio necesita un cambio efectivo, no solo archivos.
_CLEAN_DIFF = "@@ -1 +1 @@\n-valor = 1\n+valor = 2\n"


def _plan(**overrides: object) -> RepairPlan:
    """Plan de reparación mínimo y válido, con lo que cada prueba quiera cambiar encima.

    El alcance por defecto es un archivo de ``src`` y los tests del paquete: así una prueba que no
    habla de alcance no tropieza con él, y las que sí lo hacen lo declaran explícitamente.
    """
    base: dict[str, object] = {
        "workflow_id": uuid4(),
        "cycle": 1,
        "target_files": ("src/app.py",),
        "allowed_file_globs": ("tests/test_*.py",),
        "forbidden_files": ("config/permissions.yaml",),
        "idempotency_key": "repair-ciclo-1",
        "plan_fingerprint": "0123456789abcdef",
    }
    base.update(overrides)
    return RepairPlan(**base)


def _verdict(
    *,
    plan: RepairPlan | None = None,
    changed_files: Sequence[str] = ("src/app.py",),
    before: Mapping[str, str] | None = None,
    after: Mapping[str, str] | None = None,
    diff_text: str = "",
) -> GuardVerdict:
    """Ejecuta el guard con un antes/después coherente por defecto.

    El defecto es un archivo autorizado que cambió de verdad: lo que no declara cada prueba es
    precisamente lo que no está juzgando.
    """
    return RepairGuard().check(
        plan=_plan() if plan is None else plan,
        changed_files=changed_files,
        before={"src/app.py": "hash-previo"} if before is None else before,
        after={"src/app.py": "hash-nuevo"} if after is None else after,
        diff_text=diff_text,
    )


# ---------------------------------------------------------------------------
# Caso limpio
# ---------------------------------------------------------------------------
def test_intento_limpio_se_permite() -> None:
    """Un cambio dentro del plan, con pruebas intactas, pasa sin ruido.

    El permiso no lleva código ni violaciones a medias: un ``code`` en un veredicto permitido
    invitaría a tratarlo como un fallo parcial.
    """
    verdict = _verdict(
        plan=_plan(allowed_file_globs=()),
        diff_text="@@ -1 +1 @@\n-valor = 1\n+valor = 2\n",
    )

    assert verdict.allowed is True
    assert verdict.code is None
    assert verdict.detail == ""
    assert verdict.violations == ()


def test_glob_autorizado_permite_el_archivo() -> None:
    """Un archivo que cae en ``allowed_file_globs`` está autorizado aunque no esté enumerado."""
    verdict = _verdict(
        changed_files=("tests/test_app.py",),
        before={"tests/test_app.py": "hash-previo"},
        after={"tests/test_app.py": "hash-nuevo"},
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# 1. Archivo prohibido y ruta protegida
# ---------------------------------------------------------------------------
def test_archivo_prohibido_por_el_plan() -> None:
    """Un archivo de ``forbidden_files`` se rechaza aunque el plan lo tenga como objetivo."""
    plan = _plan(target_files=("config/permissions.yaml",))
    verdict = _verdict(
        plan=plan,
        changed_files=("config/permissions.yaml",),
        before={"config/permissions.yaml": "hash-previo"},
        after={"config/permissions.yaml": "hash-nuevo"},
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is False
    assert verdict.code is _SCOPE_CODE
    assert verdict.violations == ("archivo prohibido: config/permissions.yaml",)


def test_ruta_constitucionalmente_protegida() -> None:
    """El piso constitucional manda sobre el plan: autorizarla no la hace tocable."""
    protected = CONSTITUTIONAL_PROTECTED_PATHS[0]
    plan = _plan(target_files=(protected,), forbidden_files=())
    verdict = _verdict(
        plan=plan,
        changed_files=(protected,),
        before={protected: "hash-previo"},
        after={protected: "hash-nuevo"},
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is False
    assert verdict.violations == (f"archivo prohibido: {protected}",)


def test_is_protected_reconoce_las_rutas_del_contrato() -> None:
    """``is_protected`` responde igual que el guard para cada ruta protegida declarada."""
    guard = RepairGuard()

    assert all(guard.is_protected(raw) for raw in PROTECTED_PATHS)
    assert any(guard.is_protected(raw) for raw in CONSTITUTIONAL_PROTECTED_PATHS)
    assert guard.is_protected("src/app.py") is False
    assert guard.is_protected("") is False


# ---------------------------------------------------------------------------
# 2. Fuera de alcance
# ---------------------------------------------------------------------------
def test_archivo_fuera_de_alcance() -> None:
    """Un archivo que no está en ``target_files`` ni en ningún glob es una salida del plan."""
    verdict = _verdict(
        changed_files=("docs/notas.md",),
        before={"docs/notas.md": "hash-previo"},
        after={"docs/notas.md": "hash-nuevo"},
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is False
    assert verdict.violations == ("fuera de alcance: docs/notas.md",)


# ---------------------------------------------------------------------------
# 3. Bypass de pruebas
# ---------------------------------------------------------------------------
def test_borrar_una_prueba() -> None:
    """Eliminar una definición ``def test_`` es un bypass, no una limpieza."""
    verdict = _verdict(diff_text="@@ -1 +0,0 @@\n-def test_login():\n")

    assert verdict.allowed is False
    assert len(verdict.violations) == 1
    assert verdict.violations[0].startswith("bypass de pruebas: se elimina la prueba")
    assert "def test_login" in verdict.violations[0]


def test_quitar_el_assert_sin_poner_otro() -> None:
    """Se borra una comprobación y no se añade ninguna: el gate deja de comprobar lo mismo."""
    verdict = _verdict(diff_text="@@ -1,2 +1,1 @@\n def test_x():\n-    assert valor == 1\n")

    assert verdict.violations == (
        "bypass de pruebas: se elimina una comprobación assert y no se añade ninguna",
    )


def test_reescribir_el_assert_no_es_bypass() -> None:
    """Reescribir un ``assert`` es legítimo: hay líneas ``-`` y ``+`` con ``assert``."""
    verdict = _verdict(
        diff_text=(
            "@@ -1,2 +1,2 @@\n def test_x():\n-    assert valor == 1\n+    assert valor == 2\n"
        )
    )

    assert verdict.allowed is True


@pytest.mark.parametrize(
    ("content", "motive"),
    [
        ('    valor = 1  # noqa: E501', "silencia con # noqa"),
        ("    valor: int = calcular()  # type: ignore[assignment]", "silencia con # type: ignore"),
        ("        '--no-cov',", "desactiva la cobertura con --no-cov"),
        ('@pytest.mark.xfail(reason="roto")', "añade @pytest.mark.xfail"),
        ('@pytest.mark.skip(reason="roto")', "añade @pytest.mark.skip"),
        ('    pytest_args = \'-k "not lento\']', 'excluye pruebas con -k "not "'),
    ],
)
def test_marcas_de_bypass_en_lineas_anadidas(content: str, motive: str) -> None:
    """Cada forma de saltar, silenciar o excluir pruebas añadida en el diff se detecta."""
    verdict = _verdict(diff_text=f"@@ -1,1 +1,2 @@\n import pytest\n+{content}\n")

    assert any(
        violation.startswith(f"bypass de pruebas: {motive}") for violation in verdict.violations
    ), verdict.violations


# ---------------------------------------------------------------------------
# 4. Umbrales y presupuesto
# ---------------------------------------------------------------------------
def test_subir_max_repairs() -> None:
    """Subir ``max_repairs`` amplía la autorización: no arregla el defecto."""
    verdict = _verdict(diff_text="@@ -1 +1 @@\n-max_repairs=2\n+max_repairs=8\n")

    assert verdict.allowed is False
    assert verdict.violations == ("gate debilitado: sube max_repairs de 2 a 8",)


def test_bajar_max_repairs_no_es_debilitamiento() -> None:
    """Bajar un límite es lo contrario de debilitar un gate y no se marca."""
    verdict = _verdict(diff_text="@@ -1 +1 @@\n-max_repairs=8\n+max_repairs=2\n")

    assert verdict.allowed is True


def test_desactivar_cross_audit() -> None:
    """Apagar ``cross_audit_required`` cambia lo que se exige, no lo que se comprobó."""
    verdict = _verdict(
        diff_text=(
            "@@ -1 +1 @@\n-cross_audit_required = True\n+cross_audit_required = False\n"
        )
    )

    assert verdict.allowed is False
    assert verdict.violations == ("gate debilitado: desactiva cross_audit_required",)


def test_bajar_un_umbral_de_cobertura() -> None:
    """Cualquier clave de umbral vale: ``coverage_threshold`` se juzga como ``threshold``."""
    verdict = _verdict(
        diff_text="@@ -1 +1 @@\n-coverage_threshold = 90\n+coverage_threshold = 60\n"
    )

    assert verdict.violations == ("gate debilitado: baja coverage_threshold de 90 a 60",)


def test_subir_un_umbral_no_se_marca() -> None:
    """Subir un umbral endurece el gate: el guard solo persigue aflojarlo."""
    verdict = _verdict(
        diff_text="@@ -1 +1 @@\n-coverage_threshold = 60\n+coverage_threshold = 90\n"
    )

    assert verdict.allowed is True


def test_bajar_min_severity() -> None:
    """``min_severity`` se compara por rango: de ``HIGH`` a ``LOW`` deja fuera lo que bloqueaba."""
    verdict = _verdict(
        diff_text='@@ -1 +1 @@\n-min_severity = "HIGH"\n+min_severity = "LOW"\n'
    )

    assert verdict.violations == ("gate debilitado: baja min_severity de HIGH a LOW",)


# ---------------------------------------------------------------------------
# 5. Intento sin cambios
# ---------------------------------------------------------------------------
def test_intento_sin_archivos_cambiados() -> None:
    """Un intento que no cambió nada no es una reparación y no puede contar como progreso."""
    verdict = _verdict(changed_files=())

    assert verdict.allowed is False
    assert verdict.violations == ("la reparación no cambió ningún archivo",)


# ---------------------------------------------------------------------------
# 6. Antes/después incoherente
# ---------------------------------------------------------------------------
def test_sin_estado_posterior_de_un_archivo_cambiado() -> None:
    """Un archivo del que no consta el estado posterior no se puede verificar."""
    verdict = _verdict(
        plan=_plan(target_files=("src/app.py", "src/otro.py")),
        changed_files=("src/app.py", "src/otro.py"),
        before={"src/app.py": "hash-previo", "src/otro.py": "hash-previo"},
        after={"src/app.py": "hash-nuevo"},
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is False
    assert verdict.violations == (
        "sin cambios efectivos: no consta el estado posterior de src/otro.py",
    )


def test_antes_y_despues_identicos() -> None:
    """Si el estado no cambió, el intento no reparó nada aunque diga haber tocado el archivo."""
    verdict = _verdict(before={"src/app.py": "hash"}, after={"src/app.py": "hash"})

    assert verdict.allowed is False
    assert verdict.violations == (
        "sin cambios efectivos: el contenido de 1 archivo(s) es idéntico antes y después",
    )


# ---------------------------------------------------------------------------
# Acumulación y cota de volumen
# ---------------------------------------------------------------------------
def test_acumula_todas_las_violaciones() -> None:
    """El veredicto trae todas las violaciones, en el orden fijo de las comprobaciones.

    Cortar en la primera obligaría al bucle de reparación a descubrirlas de una en una; acumularlas
    es lo que permite arreglar el intento de una vez.
    """
    plan = _plan(target_files=("src/app.py",), forbidden_files=("config/permissions.yaml",))
    verdict = _verdict(
        plan=plan,
        changed_files=("config/permissions.yaml", "docs/otro.md"),
        before={"config/permissions.yaml": "hash-previo", "docs/otro.md": "hash-previo"},
        after={"config/permissions.yaml": "hash-nuevo", "docs/otro.md": "hash-nuevo"},
        diff_text="@@ -1,1 +1,3 @@\n import pytest\n+@pytest.mark.skip\n+max_repairs=99\n",
    )

    assert verdict.allowed is False
    assert verdict.code is _SCOPE_CODE
    assert verdict.violations == (
        "archivo prohibido: config/permissions.yaml",
        "fuera de alcance: config/permissions.yaml",
        "fuera de alcance: docs/otro.md",
        'bypass de pruebas: añade @pytest.mark.skip en \'@pytest.mark.skip\'',
        "gate debilitado: sube max_repairs de sin valor previo a 99",
    )
    assert verdict.detail.startswith("5 violación(es) del plan de reparación")
    assert verdict.detail.count(" | ") == 4


def test_volumen_por_encima_de_la_cota_del_contrato() -> None:
    """Más archivos que ``MAX_REPAIR_FILES`` no se pueden registrar en el intento y se rechazan."""
    paths = tuple(f"src/f{i}.py" for i in range(MAX_REPAIR_FILES + 1))
    plan = _plan(target_files=(), allowed_file_globs=("src/*.py",), forbidden_files=())
    verdict = _verdict(
        plan=plan,
        changed_files=paths,
        before=dict.fromkeys(paths, "hash-previo"),
        after=dict.fromkeys(paths, "hash-nuevo"),
        diff_text=_CLEAN_DIFF,
    )

    assert verdict.allowed is False
    assert verdict.violations == (
        f"demasiados archivos: {MAX_REPAIR_FILES + 1} cambiados y el máximo es {MAX_REPAIR_FILES}",
    )
