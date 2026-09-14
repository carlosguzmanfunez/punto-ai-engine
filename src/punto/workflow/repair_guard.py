"""Guard determinista del alcance y de los gates de una reparación (ENGINE-6.1).

Una reparación autónoma tiene un problema de incentivos: el mismo ciclo que intenta poner los gates
en verde puede conseguirlo por el camino equivocado —tocando lo que el plan no autorizó, borrando la
prueba que fallaba o bajando el umbral que la hacía fallar—. El kernel ya decide *qué* se repara
(``RepairDecision``) y con qué autorización (``RepairPlan``); lo que faltaba era comprobar, antes de
aceptar el resultado, que lo hecho **cabe dentro de ese plan** y que el verde no se consiguió
apagando lo que lo comprueba.

Qué protege, y qué no
---------------------
Este módulo es deliberadamente **textual y determinista**: mira la lista de archivos cambiados y el
diff, no construye el AST del proyecto. Ese límite se declara antes de confiar en él:

- Protege los bypass **obvios**: borrar un ``def test_``, añadir ``skip``/``xfail``, silenciar con
  ``# noqa``, quitar el ``assert``, subir el presupuesto, bajar un umbral, desactivar un gate
  obligatorio o salirse de los archivos autorizados. Son los que un modelo bajo presión produce de
  forma natural.
- **No** protege un bypass semántico: renombrar una prueba, vaciarle el cuerpo, debilitar el
  ``assert`` en vez de quitarlo o esconder el fallo dentro de un ``try``. Eso exige comparar
  comportamiento —volver a ejecutar los gates y la revisión de un humano—, no texto. Un guard que
  prometiera lo contrario sería peor que este, porque daría por cubierto lo que no lo está.

Por qué acumula todas las violaciones
-------------------------------------
Un veredicto que corta en la primera violación obliga a reparar, volver a pasar el guard y descubrir
la siguiente: el bucle aprendería de una en una. :meth:`RepairGuard.check` recorre **todas** las
comprobaciones y devuelve la lista completa; el orden es fijo —prohibido, alcance, bypass de
pruebas, gates, sin cambios, antes/después, volumen— para que dos ejecuciones sobre el mismo diff
produzcan exactamente las mismas líneas.

Y no decide: devuelve :class:`GuardVerdict` con el vocabulario de fallo del kernel. Si ese «no» es
un fallo, una pausa o una petición humana lo decide el kernel, igual que con
:class:`~punto.workflow.effects.EffectDecision`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from punto.common import basename_of, normalize_path
from punto.policy.permissions import is_protected_path
from punto.schemas.repair import MAX_REPAIR_FILES, RepairPlan
from punto.schemas.workflow import MAX_WORKFLOW_SUMMARY_CHARS, WorkflowFailureCode
from punto.workflow.repair import PROTECTED_PATHS

#: Marcas que, en una línea **añadida**, desactivan o silencian una prueba en vez de arreglarla. Se
#: comparan como subcadena y en el texto tal cual: el diff es código, no prosa, y estas formas son
#: las que usa el propio repositorio (pytest, ruff, mypy, cobertura).
_BYPASS_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("@pytest.mark.skip", "añade @pytest.mark.skip"),
    ("@pytest.mark.xfail", "añade @pytest.mark.xfail"),
    ("pytest.skip(", "añade pytest.skip("),
    ("pytest.xfail(", "añade pytest.xfail("),
    ("# noqa", "silencia con # noqa"),
    ("# type: ignore", "silencia con # type: ignore"),
    ("--no-cov", "desactiva la cobertura con --no-cov"),
    ('-k "not ', "excluye pruebas con -k \"not \""),
    ("-k 'not ", "excluye pruebas con -k 'not '"),
)
#: Claves numéricas de autorización: subirlas amplía lo que la reparación puede gastar o repetir,
#: y eso no arregla ningún defecto. Se comparan con su valor anterior del mismo diff para no marcar
#: una bajada, que es lo contrario de un debilitamiento.
_NUMERIC_LIMIT_KEYS: Final[tuple[str, ...]] = (
    "max_model_calls",
    "max_repairs",
    "max_total_tokens",
)
#: Patrón por clave numérica. Las claves son literales sin metacaracteres, así que no hace falta
#: escaparlas; se acepta ``=`` y ``:`` porque el límite puede vivir en código o en configuración.
_NUMERIC_LIMIT_RES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (key, re.compile(rf"\b{key}\s*[:=]\s*(\d+)")) for key in _NUMERIC_LIMIT_KEYS
)
#: Cualquier clave que contenga ``threshold`` cuenta: ``coverage_threshold`` o
#: ``min_quality_threshold`` son el mismo debilitamiento con otro nombre.
_THRESHOLD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*threshold[A-Za-z0-9_]*)\s*[:=]\s*(\d+(?:\.\d+)?)"
)
#: ``min_severity`` es una categoría, no un número: se compara por rango declarado.
_MIN_SEVERITY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bmin_severity\s*[:=]\s*[\"']?([A-Za-z_]+)[\"']?"
)
#: Orden de gravedad de ``FindingSeverity``: bajar ``min_severity`` es aceptar defectos más leves.
_SEVERITY_RANK: Final[tuple[str, ...]] = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
#: Gates que una reparación no puede apagar: son obligación del motor, no preferencia del plan.
_DISABLED_FLAGS: Final[tuple[str, ...]] = ("cross_audit_required", "web_visual_required")
#: Patrón por gate apagado, en código (``=False``) o en configuración (``"x": false``).
_DISABLED_FLAG_RES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (flag, re.compile(rf"\b{flag}\b[\"']?\s*[:=]\s*(?:False|false)\b"))
    for flag in _DISABLED_FLAGS
)
#: Caracteres de la línea del diff que se conservan en el detalle de una violación.
_SNIPPET_CHARS: Final[int] = 60
#: Marca de recorte del resumen. Es explícita: un resumen recortado en silencio se leería completo.
_TRUNCATION_MARKER: Final[str] = " …[recortado]"


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """Veredicto del guard: permitido o no, con su código estable y sus violaciones.

    ``violations`` lleva **todas** las violaciones, una por línea y en el orden fijo de las
    comprobaciones; ``detail`` es el resumen acotado que se puede meter en un ``WorkflowFailure``.
    Cuando ``allowed`` es ``True`` los otros tres campos van vacíos: un permiso no necesita
    explicación, y un ``code`` en un permiso invitaría a tratarlo como un fallo a medias.
    """

    allowed: bool
    code: WorkflowFailureCode | None = None
    detail: str = ""
    violations: tuple[str, ...] = ()


class RepairGuard:
    """Comprobaciones deterministas sobre una reparación antes de aceptarla.

    No guarda estado: ``__slots__`` vacío y ni un atributo. La autorización está en el
    ``RepairPlan`` que recibe :meth:`check` y el estado del mundo en ``before``/``after``/
    ``diff_text``; un guard con memoria propia podría discrepar del plan que autorizó el intento,
    que es justo lo que existe para impedir.
    """

    __slots__ = ()

    def check(
        self,
        *,
        plan: RepairPlan,
        changed_files: Sequence[str],
        before: Mapping[str, str],
        after: Mapping[str, str],
        diff_text: str = "",
    ) -> GuardVerdict:
        """Acumula las violaciones del intento y devuelve el veredicto.

        Comprueba, en este orden: archivos prohibidos o protegidos, archivos fuera de los
        ``target_files``/``allowed_file_globs`` del plan, bypass de pruebas en el diff,
        debilitamiento de umbrales y presupuesto en el diff, intento sin archivos cambiados,
        estado antes/después incoherente y volumen por encima de la cota del contrato. Un intento
        limpio devuelve ``GuardVerdict(allowed=True)`` sin código ni violaciones.

        El veredicto es una **opinión fundada, no una decisión**: el kernel es quien decide si este
        «no» es un fallo del intento, una pausa o una petición humana.

        Args:
            plan: Contrato que autoriza la reparación. Define alcance y prohibiciones.
            changed_files: Archivos que la reparación dice haber cambiado.
            before: Estado previo por ruta (normalmente hashes); ``""``/ausente es «no existía».
            after: Estado posterior por ruta, con las mismas convenciones que ``before``.
            diff_text: Diff unificado del intento. Vacío significa «no hay diff que juzgar», no «no
                hubo cambios»: los cambios se juzgan por ``changed_files``/``before``/``after``.

        Returns:
            ``GuardVerdict`` permitido, o rechazado con
            ``WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION``, el resumen en ``detail`` y una
            línea por violación en ``violations``.
        """
        violations: tuple[str, ...] = (
            *_forbidden_violations(plan, changed_files),
            *_out_of_scope_violations(plan, changed_files),
            *_test_bypass_violations(diff_text),
            *_gate_weakening_violations(diff_text),
            *_no_change_violations(changed_files),
            *_incoherent_violations(changed_files, before, after),
            *_too_many_files_violations(changed_files),
        )
        if not violations:
            return GuardVerdict(allowed=True)
        return GuardVerdict(
            allowed=False,
            code=WorkflowFailureCode.WORKFLOW_REPAIR_SCOPE_VIOLATION,
            detail=_summary(violations),
            violations=violations,
        )

    def is_protected(self, path: str) -> bool:
        """True si la ruta es intocable para una reparación, aunque el plan la autorice.

        Es la consulta pública de la protección que aplica :meth:`check`, expuesta aparte para que
        el kernel pueda rechazar una ruta **antes** de planificar el intento en vez de después de
        mutarla. La respuesta no depende del plan: el plan autoriza dentro de lo permitido, no
        puede ampliar lo permitido.
        """
        return _is_protected_path(path)


def _forbidden_violations(plan: RepairPlan, changed_files: Sequence[str]) -> tuple[str, ...]:
    """Archivos que el plan prohíbe o que son ruta protegida, en el orden en que se cambiaron.

    Se compara con ``forbidden_files`` por igualdad **y** por glob —una entrada como
    ``config/*.yaml`` es una prohibición igual de explícita— y con :data:`PROTECTED_PATHS`, que es
    el piso que una reparación no toca ni con autorización del plan. La ruta normalizada es la que
    se compara: ``./config/permissions.yaml`` y ``config/permissions.yaml`` son el mismo archivo.
    """
    violations: list[str] = []
    forbidden = tuple(
        entry for entry in (normalize_path(raw) for raw in plan.forbidden_files) if entry
    )
    for path in changed_files:
        normalized = normalize_path(path)
        if not normalized:
            continue
        if any(normalized == entry or fnmatch.fnmatch(normalized, entry) for entry in forbidden):
            violations.append(f"archivo prohibido: {path}")
            continue
        if _is_protected_path(normalized):
            violations.append(f"archivo prohibido: {path}")
    return tuple(violations)


def _out_of_scope_violations(plan: RepairPlan, changed_files: Sequence[str]) -> tuple[str, ...]:
    """Archivos cambiados que el plan no autoriza ni por lista ni por glob.

    El plan es la autorización de escritura: ``target_files`` es lo declarado y
    ``allowed_file_globs`` lo que se admite sin enumerar (una familia de pruebas, un paquete
    entero). Un archivo que no cae en ninguno de los dos es una salida del plan, aunque el cambio
    sea bueno: aceptarla convertiría el plan en una sugerencia y la autorización en algo que se
    descubre después.
    """
    targets = tuple(entry for entry in (normalize_path(raw) for raw in plan.target_files) if entry)
    globs = tuple(
        entry for entry in (normalize_path(raw) for raw in plan.allowed_file_globs) if entry
    )
    violations: list[str] = []
    for path in changed_files:
        normalized = normalize_path(path)
        if not normalized:
            continue
        if normalized in targets:
            continue
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in globs):
            continue
        violations.append(f"fuera de alcance: {path}")
    return tuple(violations)


def _test_bypass_violations(diff_text: str) -> tuple[str, ...]:
    """Debilitamiento de pruebas detectado en el diff: borrar, saltar o callar comprobaciones.

    Tres formas, y las tres hacen que el gate deje de comprobar lo mismo sin arreglar el defecto:
    eliminar una definición ``def test_``, añadir una marca que salta o silencia (``skip``,
    ``xfail``, ``# noqa``, ``--no-cov``, ``-k "not ...``) y quitar el ``assert`` sin poner otro. La
    última se juzga por **conjunto** —hay líneas ``-`` con ``assert`` y ninguna ``+`` con
    ``assert``— porque reescribir un ``assert`` es legítimo y borrarlo no; una línea concreta no
    distingue las dos.
    """
    added, removed = _diff_sides(diff_text)
    violations: list[str] = []
    for line in removed:
        code = line.strip()
        if code.startswith(("def test_", "async def test_")):
            violations.append(f"bypass de pruebas: se elimina la prueba {_snippet(code)}")
    for line in added:
        for marker, motive in _BYPASS_MARKERS:
            if marker in line:
                violations.append(f"bypass de pruebas: {motive} en {_snippet(line)}")
                break
    has_removed_assert = any("assert " in line for line in removed)
    has_added_assert = any("assert " in line for line in added)
    if has_removed_assert and not has_added_assert:
        violations.append(
            "bypass de pruebas: se elimina una comprobación assert y no se añade ninguna"
        )
    return tuple(violations)


def _gate_weakening_violations(diff_text: str) -> tuple[str, ...]:
    """Subidas de presupuesto, bajadas de umbral y gates apagados detectados en el diff.

    Es la otra mitad del mismo riesgo: no tocar la prueba, sino aflojar lo que la hace exigible.
    Cada familia tiene su función porque el motivo del rechazo importa —no es lo mismo subir el
    presupuesto que bajar la severidad mínima— y las cuatro se acumulan en el veredicto.
    """
    added, removed = _diff_sides(diff_text)
    violations: list[str] = []
    violations.extend(_raised_limit_violations(added, removed))
    violations.extend(_lowered_threshold_violations(added, removed))
    violations.extend(_lowered_severity_violations(added, removed))
    violations.extend(_disabled_gate_violations(added))
    return tuple(violations)


def _no_change_violations(changed_files: Sequence[str]) -> tuple[str, ...]:
    """Un intento sin archivos cambiados no es una reparación, es un no-op.

    Se rechaza aunque no haya nada más mal: aceptarlo cerraría un ciclo con ``APPLIED`` sin que el
    árbol haya cambiado, el defecto seguiría ahí y el bucle habría contado un intento consumido como
    si hubiera progresado. La falta de progreso es un diagnóstico, no un éxito silencioso.
    """
    if changed_files:
        return ()
    return ("la reparación no cambió ningún archivo",)


def _incoherent_violations(
    changed_files: Sequence[str], before: Mapping[str, str], after: Mapping[str, str]
) -> tuple[str, ...]:
    """Estado antes/después que no sostiene lo que la reparación dice haber cambiado.

    Dos incoherencias, y con las dos es imposible demostrar que hubo cambio: un archivo del que no
    consta el estado posterior —no se puede verificar ni comparar— y un conjunto cuyo contenido es
    idéntico antes y después. Las claves se normalizan para comparar, porque ``src/a.py`` y
    ``./src/a.py`` son el mismo archivo y un mapa con otra grafía no puede decidir el veredicto.
    """
    before_by_key = {normalize_path(path): value for path, value in before.items()}
    after_by_key = {normalize_path(path): value for path, value in after.items()}
    violations: list[str] = []
    missing = [path for path in changed_files if normalize_path(path) not in after_by_key]
    if missing:
        violations.append(
            "sin cambios efectivos: no consta el estado posterior de "
            + ", ".join(missing)
        )
    comparable = [
        path
        for path in changed_files
        if normalize_path(path) in after_by_key and normalize_path(path) in before_by_key
    ]
    if comparable and all(
        before_by_key[normalize_path(path)] == after_by_key[normalize_path(path)]
        for path in comparable
    ):
        violations.append(
            f"sin cambios efectivos: el contenido de {len(comparable)} archivo(s) es idéntico "
            "antes y después"
        )
    return tuple(violations)


def _too_many_files_violations(changed_files: Sequence[str]) -> tuple[str, ...]:
    """Volumen por encima de la cota del contrato.

    ``RepairAttempt.changed_files`` admite ``MAX_REPAIR_FILES`` entradas: aceptar un intento con más
    haría que el registro del propio intento no se pudiera volver a validar al cargar el checkpoint,
    justo después de haber mutado el árbol. Es una violación de alcance, no una advertencia.
    """
    if len(changed_files) <= MAX_REPAIR_FILES:
        return ()
    return (
        f"demasiados archivos: {len(changed_files)} cambiados y el máximo es {MAX_REPAIR_FILES}",
    )


def _raised_limit_violations(added: Sequence[str], removed: Sequence[str]) -> list[str]:
    """Subidas de ``max_repairs``/``max_model_calls``/``max_total_tokens``.

    Se compara con el valor anterior del **mismo** diff: bajar un límite no es un debilitamiento y
    marcarlo convertiría el guard en un estorbo. Si la línea aparece por primera vez —no hay valor
    previo— se trata como subida: introducir una cota donde no había ninguna es exactamente la
    maniobra que este guard existe para ver.
    """
    violations: list[str] = []
    for key, pattern in _NUMERIC_LIMIT_RES:
        current = _max_value(pattern, added)
        if current is None:
            continue
        previous = _max_value(pattern, removed)
        if previous is not None and current <= previous:
            continue
        origin = "sin valor previo" if previous is None else str(previous)
        violations.append(f"gate debilitado: sube {key} de {origin} a {current}")
    return violations


def _lowered_threshold_violations(added: Sequence[str], removed: Sequence[str]) -> list[str]:
    """Bajadas de cualquier umbral numérico declarado en el diff.

    Un umbral más bajo es un gate más fácil de pasar con el mismo código: no arregla nada y hace
    verde lo que antes era rojo. Se compara por nombre de clave —``threshold``,
    ``coverage_threshold``, ``min_score_threshold``— porque el vocabulario del proyecto es libre y
    el patrón de la maniobra no.
    """
    violations: list[str] = []
    for line in added:
        match = _THRESHOLD_RE.search(line)
        if match is None:
            continue
        name = match.group(1)
        current = float(match.group(2))
        previous = _min_threshold(name, removed)
        if previous is not None and current >= previous:
            continue
        origin = "sin valor previo" if previous is None else _number(previous)
        violations.append(f"gate debilitado: baja {name} de {origin} a {_number(current)}")
    return violations


def _lowered_severity_violations(added: Sequence[str], removed: Sequence[str]) -> list[str]:
    """Bajadas de ``min_severity``, comparadas por rango y no por texto.

    Bajar la severidad mínima es dejar de mirar los defectos que antes bloqueaban. Se usa el orden
    declarado en ``FindingSeverity``; un valor que no se reconoce no se juzga, porque afirmar una
    bajada sin poder ordenar los dos valores sería inventar el veredicto.
    """
    violations: list[str] = []
    previous_rank = _max_severity_rank(removed)
    for line in added:
        match = _MIN_SEVERITY_RE.search(line)
        if match is None:
            continue
        current = match.group(1).upper()
        rank = _SEVERITY_RANK.index(current) if current in _SEVERITY_RANK else None
        if rank is None:
            continue
        if previous_rank is not None and rank >= previous_rank:
            continue
        origin = "sin valor previo" if previous_rank is None else _SEVERITY_RANK[previous_rank]
        violations.append(f"gate debilitado: baja min_severity de {origin} a {current}")
    return violations


def _disabled_gate_violations(added: Sequence[str]) -> list[str]:
    """Gates obligatorios apagados en el diff (``cross_audit_required``, ``web_visual_required``).

    Son obligaciones del motor: apagarlas no cambia lo que se comprobó, cambia lo que se exige. Se
    miran solo las líneas añadidas, porque una línea eliminada con ``False`` es precisamente lo
    contrario: volver a exigir el gate.
    """
    violations: list[str] = []
    for line in added:
        for flag, pattern in _DISABLED_FLAG_RES:
            if pattern.search(line):
                violations.append(f"gate debilitado: desactiva {flag}")
    return violations


def _diff_sides(diff_text: str) -> tuple[list[str], list[str]]:
    """Líneas añadidas y eliminadas del diff, sin las cabeceras.

    Un diff unificado marca el contenido con ``+``/``-`` en la primera columna y las cabeceras con
    ``+++``/``---``; las cabeceras se descartan porque nombran el archivo y no cambian nada. Lo que
    no empieza por ``+`` ni ``-`` es contexto y se ignora: el guard juzga **lo que la reparación
    hizo**, no lo que ya estaba.
    """
    added: list[str] = []
    removed: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _max_value(pattern: re.Pattern[str], lines: Sequence[str]) -> int | None:
    """Mayor valor entero que ``pattern`` encuentra en ``lines``, o ``None`` si no hay ninguno."""
    values = [
        int(match.group(1))
        for line in lines
        for match in (pattern.search(line),)
        if match is not None
    ]
    return max(values) if values else None


def _min_threshold(name: str, lines: Sequence[str]) -> float | None:
    """Menor valor de esa clave de umbral en ``lines``, o ``None`` si la clave no aparece."""
    values: list[float] = []
    for line in lines:
        match = _THRESHOLD_RE.search(line)
        if match is not None and match.group(1) == name:
            values.append(float(match.group(2)))
    return min(values) if values else None


def _max_severity_rank(lines: Sequence[str]) -> int | None:
    """Rango de severidad más alto de ``lines``, o ``None`` si no hay ninguno legible."""
    ranks = [
        _SEVERITY_RANK.index(value)
        for value in (
            match.group(1).upper()
            for line in lines
            for match in (_MIN_SEVERITY_RE.search(line),)
            if match is not None
        )
        if value in _SEVERITY_RANK
    ]
    return max(ranks) if ranks else None


def _is_protected_path(path: str) -> bool:
    """True si la ruta cae en :data:`PROTECTED_PATHS` o en el piso constitucional.

    Se compara normalizado —separadores, ``./`` y mayúsculas— y de cuatro formas: igualdad, prefijo
    de directorio, glob y nombre base. ``PROTECTED_PATHS`` puede declarar tanto un archivo exacto
    como un árbol entero (``tests/human_gate/**``) o un nombre base suelto, y las tres
    declaraciones protegen lo mismo. Además se pregunta al piso constitucional
    (:func:`~punto.policy.permissions.is_protected_path`), que reconoce la ruta declarada con
    cualquier prefijo: una reparación no puede desactivar su propia autoridad aunque un plan la
    autorice.
    """
    normalized = normalize_path(path)
    if not normalized:
        return False
    if is_protected_path(normalized):
        return True
    for raw in PROTECTED_PATHS:
        entry = normalize_path(raw)
        if not entry:
            continue
        if normalized == entry or normalized.startswith(f"{entry.rstrip('/')}/"):
            return True
        if fnmatch.fnmatch(normalized, entry):
            return True
        if "/" not in entry and basename_of(normalized) == entry:
            return True
    return False


def _summary(violations: tuple[str, ...]) -> str:
    """Resumen acotado del veredicto para el ``WorkflowFailure`` del kernel.

    Se acota a ``MAX_WORKFLOW_SUMMARY_CHARS`` porque este texto viaja en el motivo de un fallo y un
    motivo más largo haría que el checkpoint no se pudiera volver a validar. La lista sin recortar
    va en ``GuardVerdict.violations``: el resumen puede perder matices, las violaciones no.
    """
    text = f"{len(violations)} violación(es) del plan de reparación: " + " | ".join(violations)
    if len(text) <= MAX_WORKFLOW_SUMMARY_CHARS:
        return text
    room = MAX_WORKFLOW_SUMMARY_CHARS - len(_TRUNCATION_MARKER)
    return text[:room] + _TRUNCATION_MARKER


def _snippet(line: str) -> str:
    """Fragmento legible y acotado de una línea del diff, para el detalle de la violación."""
    text = line.strip()
    if len(text) <= _SNIPPET_CHARS:
        return repr(text)
    return repr(text[:_SNIPPET_CHARS] + "…")


def _number(value: float) -> str:
    """Formatea un umbral sin decimales postizos (``85.0`` se lee ``85``)."""
    return f"{value:g}"


__all__ = [
    "GuardVerdict",
    "RepairGuard",
]
