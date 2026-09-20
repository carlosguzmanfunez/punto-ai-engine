"""Resolución focalizada de una verificación fallida (EXPERIMENTO 03).

Esta capa responde a una sola pregunta, con evidencia y sin proveedor de por medio: **cuando una
verificación falla, ¿qué recurso mide ese fallo, y la reparación anterior lo abordó?**

Todo lo que hay aquí es determinista y pasivo:

- **mapeo fallo → recurso** (``failure_map``): prefiere la relación **explícita** (la ruta aparece
  en el ``argv`` del comando de verificación) y, si no la hay, la relación **declarada** por el plan
  (el eslabón de la cadena funcional que cita esa verificación). Si no hay ninguna de las dos, el
  fallo queda ``unmapped``: no se inventa un recurso para poder decir que se ha mapeado.
- **estado de un recurso relevante** (``resource_statuses``): ``CHANGED``,
  ``UNCHANGED_BY_EVIDENCE``, ``BLOCKED_BY_SCOPE`` o ``UNEXPLAINED``. Leer un recurso no obliga a
  tocarlo: lo que se exige es **explicar o abordar**, nunca "toca todo lo que la verificación lea".
- **progreso causal** (``causal_progress``): distingue *cambiar el parche* de *cambiar la
  estrategia*. Dos parches distintos con el mismo fallo y sin tocar el recurso discriminante son la
  misma estrategia fallida, y eso es ``CAUSAL_STAGNATION``.

``SKILL != AUTHORITY``: nada de esto concede permisos, cambia presupuestos ni decide aplicaciones.
Solo describe el fallo y lo que la reparación hizo con él.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from punto.schemas.dev import CommandEvidence, DevelopmentPlan
from punto.workspace.target import DevelopmentTarget

__all__ = [
    "BLOCKED_BY_SCOPE",
    "CAUSAL_STAGNATION_LABEL",
    "CHANGED",
    "IMPLEMENTATION_PHASE",
    "RELATION_PLAN_CHAIN",
    "RELATION_VERIFICATION_ARGV",
    "RESOLUTION_CONTRACT",
    "RESOLUTION_INPUT_LABEL",
    "RESOLUTION_PHASE",
    "UNCHANGED_BY_EVIDENCE",
    "UNEXPLAINED",
    "FailureMap",
    "FailureResource",
    "ProgressRecord",
    "ResolutionState",
    "ResourceStatus",
    "candidate_paths",
    "causal_progress",
    "declared_unchanged",
    "duplicated_chars",
    "escalation_resources",
    "failure_map",
    "normalize_path",
    "resolution_block",
    "resource_statuses",
    "strategy_signature",
]

#: Relación **explícita**: la ruta aparece en el ``argv`` del comando de verificación que falló.
RELATION_VERIFICATION_ARGV: Final[str] = "VERIFICATION_ARGV"
#: Relación **declarada** por el plan: el recurso implementa el eslabón que cita esa verificación.
RELATION_PLAN_CHAIN: Final[str] = "PLAN_CHAIN"

#: Estados posibles de un recurso relevante para el fallo.
CHANGED: Final[str] = "CHANGED"
UNCHANGED_BY_EVIDENCE: Final[str] = "UNCHANGED_BY_EVIDENCE"
BLOCKED_BY_SCOPE: Final[str] = "BLOCKED_BY_SCOPE"
UNEXPLAINED: Final[str] = "UNEXPLAINED"

#: Fases observables del BUILDER: implementar por primera vez o resolver un fallo real.
IMPLEMENTATION_PHASE: Final[str] = "implementation"
RESOLUTION_PHASE: Final[str] = "resolution"

#: Etiqueta del bloque de resolución en el prompt. Su presencia es lo que distingue una invocación
#: de resolución de una de implementación, tanto para el proveedor como para el arnés que mide.
RESOLUTION_INPUT_LABEL: Final[str] = (
    "RESOLUTION INPUT (real verification failure; fix the cause, do not repeat the previous "
    "strategy):"
)
CAUSAL_STAGNATION_LABEL: Final[str] = "CAUSAL_STAGNATION:"

#: Contrato mínimo de la respuesta de resolución. No se pide ensayo ni razonamiento privado: se
#: pide una hipótesis operacional y, por cada recurso relevante, cambio o explicación.
RESOLUTION_CONTRACT: Final[str] = (
    'RESOLUTION CONTRACT: answer with "root_cause" (one operational hypothesis), "evidence" (the '
    'verification output that supports it) and "expected_effect", plus the minimal "changes" that '
    'resolve it. For every relevant resource you do NOT change, declare it in '
    '"unchanged_resources" as {"path": "...", "evidence": "why it needs no change"}; if it is '
    "outside the plan, ask for scope_expansion with evidence instead of assuming it."
)

#: Extensiones plausibles de un recurso de repositorio. El mapeo no adivina: solo reconoce rutas con
#: pinta de fichero, y descarta el resto de tokens del ``argv``. Las alternativas van de más larga a
#: más corta y con frontera final: si no, ``Rejilla.tsx`` se leería como ``Rejilla.ts`` (defecto
#: detectado por el pre-flight, no por el proveedor).
_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:tsx|ts|jsx|js|mjs|cjs|py|json|md|markdown|"
    r"scss|css|html|yml|yaml|toml|txt|sh|sql))(?![A-Za-z0-9])"
)

#: Recursos por encima de este número no entran en el prompt: el bloque es compacto, no un volcado.
MAX_PROMPT_RESOURCES: Final[int] = 8


def normalize_path(path: str) -> str:
    """Normaliza una ruta relativa de repositorio, sin decidir si existe."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def candidate_paths(text: str, *, known: Iterable[str] = ()) -> tuple[str, ...]:
    """Rutas con pinta de fichero dentro de un texto, en orden de aparición.

    Un candidato sin separador de directorio solo se acepta si ya se conocía (el plan lo declara):
    así un token suelto del ``argv`` no se convierte en un recurso inventado.
    """
    universo = {normalize_path(item) for item in known}
    found: list[str] = []
    for match in _PATH_PATTERN.finditer(text):
        path = normalize_path(match.group(1))
        if not path or path.startswith("/") or ".." in path.split("/"):
            continue
        if "/" not in path and path not in universo:
            continue
        if path not in found:
            found.append(path)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class FailureResource:
    """Un recurso que una verificación fallida mide, con la relación que lo sostiene."""

    path: str
    relation: str
    verifications: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {
            "path": self.path,
            "relation": self.relation,
            "verifications": list(self.verifications),
        }


@dataclass(frozen=True, slots=True)
class FailureMap:
    """Mapeo determinista de un fallo real a los recursos que ese fallo mide."""

    failed: tuple[str, ...] = ()
    resources: tuple[FailureResource, ...] = ()
    unmapped: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Recursos relevantes, sin repetir y en orden de aparición."""
        return tuple(dict.fromkeys(item.path for item in self.resources))

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable del mapeo."""
        return {
            "failed": list(self.failed),
            "resources": [item.as_dict() for item in self.resources],
            "unmapped": list(self.unmapped),
        }


def failure_map(
    verification: Sequence[CommandEvidence],
    target: DevelopmentTarget,
    plan: DevelopmentPlan,
) -> FailureMap:
    """Relaciona cada verificación fallida con los recursos que mide, sin llamar al proveedor.

    Orden de preferencia: la ruta que aparece en el ``argv`` del comando (relación explícita) y, si
    esa verificación no declara ninguna, los recursos del plan cuyos eslabones de cadena citan esa
    verificación (relación declarada). Nunca se deduce por parecido de nombres.
    """
    failed = tuple(item for item in verification if not item.passed)
    if not failed:
        return FailureMap()
    catalog = {item.name: item.argv for item in target.verification}
    known = (*plan.touched_paths(), *plan.files_to_read)
    explicit: dict[str, list[str]] = {}
    for item in failed:
        argv = catalog.get(item.name, item.argv)
        for path in candidate_paths(" ".join(argv), known=known):
            names = explicit.setdefault(path, [])
            if item.name not in names:
                names.append(item.name)
    resources = [
        FailureResource(path, RELATION_VERIFICATION_ARGV, tuple(names))
        for path, names in explicit.items()
    ]
    covered = {name for names in explicit.values() for name in names}
    chain = tuple(plan.functional_chain)
    declared: dict[str, list[str]] = {}
    for item in failed:
        if item.name in covered:
            continue
        if not any(step.verification == item.name for step in chain):
            continue
        attached = False
        for path in plan.touched_paths():
            if path in explicit:
                continue
            names = declared.setdefault(path, [])
            if item.name not in names:
                names.append(item.name)
            attached = True
        if attached:
            covered.add(item.name)
    resources.extend(
        FailureResource(path, RELATION_PLAN_CHAIN, tuple(names))
        for path, names in declared.items()
    )
    unmapped = tuple(item.name for item in failed if item.name not in covered)
    return FailureMap(
        failed=tuple(item.name for item in failed),
        resources=tuple(resources),
        unmapped=unmapped,
    )


def declared_unchanged(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Recursos que el BUILDER declara sin cambio, con la evidencia que lo sostiene.

    Una declaración sin evidencia no cuenta: el estado ``UNCHANGED_BY_EVIDENCE`` exige la evidencia,
    no la afirmación. Las entradas malformadas se descartan en vez de interpretarse.
    """
    raw = payload.get("unchanged_resources")
    if not isinstance(raw, list):
        return ()
    out: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            path = normalize_path(str(item.get("path") or ""))
            evidence = str(item.get("evidence") or item.get("reason") or "").strip()[:300]
        elif isinstance(item, str):
            path, evidence = normalize_path(item), ""
        else:
            continue
        if path and path not in {known for known, _ in out}:
            out.append((path, evidence))
    return tuple(out)


def escalation_resources(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Recursos cuyo alcance pide ampliar el BUILDER en esta ronda, ya normalizados.

    ``scope_expansion`` es la vía legítima cuando la evidencia demuestra que el recurso necesario
    está fuera de alcance (y no una forma de tocar de más): sin recursos declarados, no hay nada que
    considerar ampliado.
    """
    raw = payload.get("scope_expansion")
    if not isinstance(raw, Mapping):
        return ()
    resources = raw.get("resources")
    if not isinstance(resources, list):
        return ()
    out: list[str] = []
    for item in resources:
        path = normalize_path(str(item))
        if path and path not in out:
            out.append(path)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ResourceStatus:
    """Qué hizo la reparación con un recurso relevante para el fallo."""

    path: str
    status: str
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable."""
        return {"path": self.path, "status": self.status, "evidence": self.evidence}


def resource_statuses(
    *,
    resources: Sequence[str],
    touched: Sequence[str],
    declared: Mapping[str, str],
    authorized: Iterable[str],
    escalated: Iterable[str] = (),
) -> tuple[ResourceStatus, ...]:
    """Estado de cada recurso relevante: cambiado, explicado, bloqueado o sin explicar.

    ``authorized`` son los recursos que el plan vigente permite escribir. Un recurso relevante que
    no está ahí y que la reparación no tocó queda ``BLOCKED_BY_SCOPE``: la salida correcta es pedir
    ``scope_expansion`` con evidencia, no escribir fuera de alcance.

    ``escalated`` son los recursos cuyo alcance se amplió **en esta misma ronda**: el parche no
    podía tocarlos cuando se formuló (por eso pidió la ampliación) y el cambio llega en la ronda
    siguiente. Se distinguen del hueco sin explicar porque ya hay evidencia y una decisión de
    autoridad detrás (defecto detectado al leer la corrida real: salían como ``UNEXPLAINED``).
    """
    changed = {normalize_path(item) for item in touched}
    allowed = {normalize_path(item) for item in authorized}
    ampliados = {normalize_path(item) for item in escalated}
    out: list[ResourceStatus] = []
    for path in resources:
        key = normalize_path(path)
        if key in changed:
            out.append(ResourceStatus(key, CHANGED))
        elif declared.get(key):
            out.append(ResourceStatus(key, UNCHANGED_BY_EVIDENCE, declared[key][:300]))
        elif key in ampliados:
            out.append(
                ResourceStatus(
                    key,
                    BLOCKED_BY_SCOPE,
                    "fuera del plan al formularse el parche; alcance ampliado con evidencia "
                    "en esta ronda: el cambio llega en la ronda siguiente",
                )
            )
        elif key not in allowed:
            out.append(ResourceStatus(key, BLOCKED_BY_SCOPE))
        else:
            out.append(ResourceStatus(key, UNEXPLAINED))
    return tuple(out)


def strategy_signature(touched: Sequence[str]) -> str:
    """Firma determinista de una estrategia de reparación: qué recursos cambió, en qué orden."""
    return "|".join(str(item) for item in touched)


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    """Progreso causal de una ronda de reparación, en hechos observables."""

    round_index: int
    failure_signature: str
    strategy_signature: str
    touched_resources: tuple[str, ...]
    failure_resources: tuple[str, ...]
    newly_addressed_failure_resources: tuple[str, ...]
    repeated_failure_resources: tuple[str, ...]
    touched_failure_intersection: tuple[str, ...]
    newly_explained_failure_resources: tuple[str, ...]
    causal_gap: tuple[str, ...]
    same_failure_after_patch: bool
    new_causal_evidence: bool
    causal_stagnation: bool
    verifications_still_failing: tuple[str, ...] = ()
    escalated_failure_resources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Vista serializable, sin contenido de ficheros."""
        return {
            "round": self.round_index,
            "failure_signature": self.failure_signature,
            "strategy_signature": self.strategy_signature,
            "touched_resources": list(self.touched_resources),
            "failure_resources": list(self.failure_resources),
            "newly_addressed_failure_resources": list(self.newly_addressed_failure_resources),
            "repeated_failure_resources": list(self.repeated_failure_resources),
            "touched_failure_intersection": list(self.touched_failure_intersection),
            "newly_explained_failure_resources": list(self.newly_explained_failure_resources),
            "causal_gap": list(self.causal_gap),
            "same_failure_after_patch": self.same_failure_after_patch,
            "new_causal_evidence": self.new_causal_evidence,
            "causal_stagnation": self.causal_stagnation,
            "verifications_still_failing": list(self.verifications_still_failing),
            "escalated_failure_resources": list(self.escalated_failure_resources),
        }


def causal_progress(
    *,
    round_index: int,
    failure_signature: str,
    previous_failure_signature: str,
    strategy: Sequence[str],
    touched: Sequence[str],
    failure_resources: Sequence[str],
    previously_touched: Iterable[str],
    previously_explained: Iterable[str],
    explained_now: Iterable[str],
    still_failing: Sequence[str] = (),
    escalated: Iterable[str] = (),
) -> ProgressRecord:
    """Decide si una ronda de reparación hizo **progreso causal**, no solo un parche distinto.

    Progreso causal es una de tres cosas: haber abordado un recurso relevante que ninguna reparación
    anterior tocó, haber aportado evidencia nueva sobre un recurso que seguía sin explicación, o
    haber pedido con evidencia la ampliación de alcance del recurso que el parche no podía tocar
    (que es la vía legítima cuando el recurso necesario está fuera de autoridad). Cambiar de
    ficheros sin tocar el recurso que la verificación mide no es progreso, y repetir el recurso ya
    tocado sin evidencia nueva tampoco: en ambos casos el fallo sigue igual.
    """
    failure_set = tuple(dict.fromkeys(str(item) for item in failure_resources))
    touched_tuple = tuple(dict.fromkeys(str(item) for item in touched))
    before_touched = {str(item) for item in previously_touched}
    before_explained = {str(item) for item in previously_explained}
    explained = {str(item) for item in explained_now}
    escalated_set = {normalize_path(str(item)) for item in escalated}
    intersection = tuple(item for item in touched_tuple if item in failure_set)
    newly_addressed = tuple(
        item for item in intersection if item not in before_touched
    )
    repeated = tuple(item for item in intersection if item in before_touched)
    newly_explained = tuple(
        item
        for item in failure_set
        if item in explained and item not in before_explained
    )
    newly_escalated = tuple(
        item
        for item in failure_set
        if item in escalated_set
        and item not in before_touched
        and item not in touched_tuple
        and item not in before_explained
    )
    gap = tuple(
        item
        for item in failure_set
        if item not in before_touched
        and item not in touched_tuple
        and item not in explained
        and item not in before_explained
    )
    same_failure = (
        bool(previous_failure_signature) and failure_signature == previous_failure_signature
    )
    new_evidence = bool(newly_addressed or newly_explained or newly_escalated)
    return ProgressRecord(
        round_index=round_index,
        failure_signature=failure_signature,
        strategy_signature=strategy_signature(strategy),
        touched_resources=touched_tuple,
        failure_resources=failure_set,
        newly_addressed_failure_resources=newly_addressed,
        repeated_failure_resources=repeated,
        touched_failure_intersection=intersection,
        newly_explained_failure_resources=newly_explained,
        causal_gap=gap,
        same_failure_after_patch=same_failure,
        new_causal_evidence=new_evidence,
        causal_stagnation=same_failure and not new_evidence,
        verifications_still_failing=tuple(str(item) for item in still_failing),
        escalated_failure_resources=newly_escalated,
    )


@dataclass(slots=True)
class ResolutionState:
    """Estado acumulado de la resolución: qué se tocó, qué se explicó y si hay estancamiento.

    Se acumula **entre rondas** porque el progreso causal solo se puede juzgar contra todo lo
    intentado antes, no contra la ronda inmediatamente anterior.
    """

    resources: tuple[str, ...] = ()
    touched: set[str] = field(default_factory=set)
    explained: dict[str, str] = field(default_factory=dict)
    records: list[ProgressRecord] = field(default_factory=list)
    stagnation: bool = False

    def observe_failure(self, failure: FailureMap) -> tuple[str, ...]:
        """Fija los recursos que mide el fallo vigente y devuelve la brecha causal abierta."""
        self.resources = failure.paths
        return self.causal_gap()

    def causal_gap(self) -> tuple[str, ...]:
        """Recursos relevantes que ninguna reparación tocó ni explicó todavía."""
        return tuple(
            path
            for path in self.resources
            if path not in self.touched and path not in self.explained
        )

    def record(
        self, record: ProgressRecord, *, explained: Mapping[str, str] | None = None
    ) -> ProgressRecord:
        """Incorpora el progreso de una ronda y actualiza el estado acumulado."""
        self.records.append(record)
        self.touched.update(record.touched_resources)
        if explained:
            self.explained.update(
                {normalize_path(path): text[:300] for path, text in explained.items()}
            )
        self.stagnation = record.causal_stagnation
        return record


def resolution_block(
    *,
    round_index: int,
    failure: FailureMap,
    previous_patch: Sequence[str],
    previous_strategy: str,
    causal_gap: Sequence[str],
    stagnation: bool,
) -> str:
    """Bloque compacto de resolución: fallo, recursos que mide, intento previo y brecha causal.

    No repite la evidencia del fallo (ya viaja en su propio bloque), no repite el procedimiento
    general del BUILDER y no incluye razonamiento: solo lo que convierte un fallo en una reparación
    discriminante.
    """
    lines = [RESOLUTION_INPUT_LABEL, f"ROUND: {round_index}"]
    if failure.failed:
        lines.append("FAILED VERIFICATIONS: " + " | ".join(failure.failed))
    if failure.resources:
        lines.append("RELEVANT RESOURCES (what each failed verification measures):")
        for item in failure.resources[:MAX_PROMPT_RESOURCES]:
            names = ", ".join(item.verifications)
            lines.append(f"- {item.path} [{item.relation}] <- {names}")
    if failure.unmapped:
        lines.append(
            "UNMAPPED FAILURES (no declared resource to attach them to): "
            + ", ".join(failure.unmapped)
        )
    lines.append("FAILURE EVIDENCE: see VERIFICATION FAILED AND MUST BE FIXED above (excerpts).")
    if previous_patch:
        lines.append("PREVIOUS PATCH (already changed): " + " | ".join(previous_patch))
    if previous_strategy:
        lines.append(f"PREVIOUS STRATEGY: {previous_strategy[:300]}")
    if causal_gap:
        lines.append(
            "CAUSAL GAP (relevant resource never changed and never explained): "
            + " | ".join(causal_gap[:MAX_PROMPT_RESOURCES])
        )
    lines.append(RESOLUTION_CONTRACT)
    if stagnation:
        gap = " | ".join(causal_gap[:MAX_PROMPT_RESOURCES]) or "(sin recurso relevante mapeado)"
        lines.append(
            CAUSAL_STAGNATION_LABEL
            + " the same verification keeps failing and the previous patch brought no new causal "
            f"evidence: it did not address {gap} and did not explain it. Repeating the previous "
            "patch is not progress: change the hypothesis or address the unaddressed resource."
        )
    return "\n".join(line for line in lines if line)


def duplicated_chars(block: str, others: Sequence[str]) -> int:
    """Caracteres del bloque que repiten **literalmente** una línea ya presente en el prompt.

    Se mide sobre líneas completas de 40 caracteres o más: una clave repetida
    (``unchanged_resources``) no es procedimiento duplicado, una frase entera sí.
    """
    repeated = 0
    for line in block.splitlines():
        text = line.strip()
        if len(text) < 40:
            continue
        if any(text in other for other in others):
            repeated += len(text)
    return repeated
