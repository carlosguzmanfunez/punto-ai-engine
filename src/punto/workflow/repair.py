"""Lógica determinista del bucle de reparación autónoma acotado (ENGINE-6.1).

Aquí vive el **criterio** del ciclo de reparación, separado del contrato (``punto.schemas.repair``)
y del orquestador: convierte hallazgos de rol en defectos con identidad estable, decide si un
defecto se puede reparar sin una persona, calcula la cadena de verificación que hay que volver a
pasar y detecta cuándo el bucle dejó de avanzar.

Cuatro decisiones que conviene leer antes de tocar nada:

- **Funciones puras, sin red, sin IA y sin estado.** Ninguna de estas funciones llama a un modelo ni
  recuerda nada entre llamadas: el estado vive en el checkpoint del workflow. Así los mismos hechos
  producen siempre la misma decisión, y una decisión se puede reproducir en otro proceso para
  auditarla. Un bucle de reparación que decidiera «a ojo de modelo» no sería auditable.
- **El modelo propone, PUNTO clasifica.** Un diagnóstico puede venir de un modelo, pero
  :func:`classify_repairability` aplica reglas fijas en un orden fijo: lo que la constitución
  reserva a una persona no se vuelve reparable porque un rol lo llame «arreglo menor».
- **El fingerprint es la identidad del defecto.** Se calcula con campos estructurados y
  normalizados, nunca con marcas de tiempo: es lo que permite reconocer «este mismo defecto volvió»
  después de reparar. Sin identidad estable no hay detección de falta de progreso, y sin ella el
  bucle sería «prueba hasta que salga», que es justo lo que el presupuesto no puede permitirse.
- **Ninguna verificación se salta.** Reparar es mutar código, así que la verificación vuelve a
  empezar por QA y solo la acorta lo que la petición no exige; no la acorta un modelo.
- **Sin diagnóstico demostrable no hay reparación** (ENGINE-6.1.1, F611-02).
  :func:`build_repair_diagnosis` traduce a ``RepairDiagnosis`` **solo** lo que el informe del rol ya
  demuestra: un defecto bloqueante con evidencia y con archivos afectados que la petición autorizó.
  Cuando eso falta —no hay evidencia, o el defecto no está situado en ningún archivo autorizado—
  devuelve ``None``, y quien llama bloquea con ``BLOCKED_EVIDENCE`` /
  ``WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE`` sin mutar nada. La frontera es honesta porque el
  diagnóstico nunca afirma un **mecanismo**: copia la ubicación (archivos), el criterio incumplido
  (código y categoría) y la evidencia que el propio informe aporta, y declara en ``unknowns`` lo que
  PUNTO no puede demostrar. Un `root_cause_summary` del tipo «el finding falló» sería una tautología
  que solo serviría para llenar el contrato, y por eso no se produce: lo que no está en el informe
  no se escribe en el diagnóstico.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Final
from uuid import UUID

from punto.schemas.enums import AuthorityLevel, FindingSeverity, RiskLevel, TaskStatus
from punto.schemas.repair import (
    MAX_IDENTICAL_REPAIR_FAILURES,
    MAX_REPAIR_EVIDENCE,
    MAX_REPAIR_FILES,
    MAX_REPAIR_FINDINGS,
    Repairability,
    RepairConfidence,
    RepairCycle,
    RepairCycleStatus,
    RepairDecision,
    RepairDiagnosis,
    RepairFinding,
    RepairFindingStatus,
    RepairPlan,
)
from punto.schemas.workflow import (
    MAX_WORKFLOW_SUMMARY_CHARS,
    MAX_WORKFLOW_TEXT_CHARS,
    ArtifactReference,
    RoleExecutionResult,
    RoleName,
    RoleStatus,
    WorkflowRequest,
)
from punto.workflow.pipeline import visual_qa_required

#: Rutas que una reparación **no** puede tocar sin Human Gate L3.
#:
#: La lista es explícita y no la puede recortar la petición: son la constitución, sus permisos, los
#: presupuestos, la frontera de política, las pruebas internas del Human Gate, la auditoría, los
#: secretos y los gates de CI. Una reparación capaz de reescribir cualquiera de ellos dejaría de ser
#: una reparación para convertirse en un cambio de las reglas del juego, y eso es precisamente lo
#: que exige una persona (L3).
PROTECTED_PATHS: Final[tuple[str, ...]] = (
    "config/constitution.yaml",
    "config/permissions.yaml",
    "config/budgets.yaml",
    "src/punto/policy/",
    "src/punto/policy/human_gate.py",
    "src/punto/audit/",
    ".env",
    ".github/",
    "src/punto/tools/security",
)

#: Códigos de fallo que describen al **entorno**, no al producto. No se reparan: se reintentan
#: cuando el proveedor o la credencial estén disponibles. Reparar código por un timeout dejaría el
#: producto peor de lo que estaba y con una excusa falsa.
INFRASTRUCTURE_CODES: Final[tuple[str, ...]] = (
    "PROVIDER_UNAVAILABLE",
    "PENDING_CREDENTIALS",
    "TIMEOUT",
    "NETWORK",
)

#: Categorías reservadas a una persona o irreversibles. Repararlas en autonomía sería el atajo que
#: la constitución prohíbe: por eso se clasifican ``NON_REPAIRABLE`` antes de mirar la gravedad.
NON_REPARABLE_CATEGORIES: Final[tuple[str, ...]] = (
    "CONSTITUTION",
    "PERMISSIONS",
    "POLICY",
    "SECRETS",
    "PAYMENTS",
    "PRODUCTION",
    "LEGAL",
    "BILLING",
    "IRREVERSIBLE_DELETE",
    "MASTER_SECRET",
    "BUSINESS_MODEL",
)

#: Gravedades de un hallazgo de seguridad que **detienen** toda reparación autónoma. Un HIGH o un
#: CRITICAL de Security es una frontera constitucional, no una tarea de mantenimiento.
_SECURITY_STOP_SEVERITIES: Final[tuple[FindingSeverity, ...]] = (
    FindingSeverity.HIGH,
    FindingSeverity.CRITICAL,
)

#: Estados de ciclo que cuentan como «intento sin avanzar» para la falta de progreso. Un ciclo
#: ``ROLLED_BACK`` o ``BLOCKED`` no es un reintento fallido del mismo plan, así que no suma.
_STALLED_CYCLE_STATUSES: Final[tuple[RepairCycleStatus, ...]] = (
    RepairCycleStatus.FAILED,
    RepairCycleStatus.NO_PROGRESS,
)

#: Cota de roles de verificación del contrato (``RepairPlan.verification_roles``). Se nombra aquí
#: para que el recorte del plan no dependa de un número suelto en medio del código.
_MAX_VERIFICATION_ROLES: Final[int] = 8

#: Cota de la evidencia que se copia de **cada** defecto al ``root_cause_summary`` del diagnóstico.
#:
#: El contrato ya acota ``RepairFinding.evidence``; esta cota es la del resumen, que reúne la de
#: todos los defectos del ciclo. Se recorta en vez de fallar por el mismo motivo que el resto del
#: módulo: un informe largo no puede impedir diagnosticar, pero tampoco puede hacer crecer el
#: contrato por encima de su propio ``max_length``.
_MAX_DIAGNOSIS_EVIDENCE_CHARS: Final[int] = 240

#: Separador entre la evidencia de dos defectos distintos dentro del diagnóstico. Explícito para que
#: el texto sea idéntico en cualquier proceso.
_DIAGNOSIS_EVIDENCE_SEPARATOR: Final[str] = " | "

#: Marca explícita de que un defecto no declaró ni código ni categoría. No es un valor inventado:
#: falta de dato, declarada como tal.
_UNCLASSIFIED_DEFECT: Final[str] = "SIN_CODIGO"


def _normalize_text(value: str) -> str:
    """Normaliza un campo de texto de un fingerprint: sin espacios en los extremos y en minúsculas.

    Solo se recortan los extremos: el contenido interior se respeta tal cual, porque una evidencia
    distinta es un defecto distinto. Mayúsculas y espacios sobrantes no cambian el defecto, así que
    no pueden cambiar su identidad.
    """
    return value.strip().lower()


def _normalize_list(values: Sequence[str]) -> list[str]:
    """Normaliza una lista de un fingerprint: minúsculas, sin repetidos y ordenada.

    El orden no es información del defecto —dos listas con los mismos archivos describen lo mismo— y
    un repetido tampoco. Los elementos vacíos se descartan porque no aportan identidad.
    """
    normalized = {_normalize_text(value) for value in values}
    normalized.discard("")
    return sorted(normalized)


def _normalize_token(value: str) -> str:
    """Normaliza un código o una categoría para compararlo con los catálogos de reglas.

    Se recortan los extremos y se pasa a mayúsculas porque los catálogos (``INFRASTRUCTURE_CODES``,
    ``NON_REPARABLE_CATEGORIES``) están en mayúsculas y quien declara el defecto no siempre respeta
    la caja. Si la comparación fuera sensible a mayúsculas, un ``"Policy"`` en vez de ``"POLICY"``
    saltaría la frontera constitucional, que es justo el fallo que no puede ocurrir.
    """
    return value.strip().upper()


def _canonical_digest(payload: Mapping[str, object]) -> str:
    """sha256 del JSON canónico de un payload.

    ``sort_keys`` y ``separators`` fijan los bytes: el mismo contenido produce siempre el mismo
    digest, en cualquier proceso y en cualquier máquina. ``ensure_ascii=False`` deja que los acentos
    viajen como UTF-8 en vez de como escapes, de modo que dos formas de escribir el mismo texto no
    generen dos identidades distintas.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repair_finding(
    *,
    role: RoleName,
    stage: TaskStatus,
    step_index: int,
    code: str,
    category: str,
    severity: FindingSeverity,
    summary: str,
    evidence: str,
    affected_files: Sequence[str],
    acceptance_criteria: Sequence[str],
) -> RepairFinding:
    """Construye un defecto reparable con su fingerprint y sus campos recortados a las cotas.

    Se recorta —en vez de fallar— porque estos datos vienen de un rol y de la petición: un mensaje
    largo no puede impedir que el defecto se registre, pero tampoco puede hacer crecer el checkpoint
    sin límite. La clasificación se deja en ``NON_REPAIRABLE`` a propósito: un defecto sin
    clasificar no se repara (fail-closed), y :func:`classify_repairability` decide después.
    """
    files = tuple(affected_files)[:MAX_REPAIR_FILES]
    criteria = tuple(acceptance_criteria)[:MAX_REPAIR_EVIDENCE]
    normalized_code = code[:60]
    normalized_category = category[:60]
    return RepairFinding(
        fingerprint=finding_fingerprint(
            source_role=role,
            code=normalized_code,
            category=normalized_category,
            affected_files=files,
            evidence=evidence,
            acceptance=criteria,
        ),
        source_role=role,
        source_stage=stage,
        source_step_index=step_index,
        category=normalized_category,
        severity=severity,
        code=normalized_code,
        summary=summary[:MAX_WORKFLOW_SUMMARY_CHARS],
        evidence=evidence[:MAX_WORKFLOW_TEXT_CHARS],
        affected_files=files,
        acceptance_criteria=criteria,
        status=RepairFindingStatus.OPEN,
        repairability=Repairability.NON_REPAIRABLE,
    )


def _autonomous_or_human(policy_allows: bool) -> Repairability:
    """Reparable en autonomía si la política lo permite; si no, hace falta una persona.

    Es la traducción de una sola regla: el motor nunca se concede a sí mismo un permiso que la
    política no le dio.
    """
    return (
        Repairability.AUTONOMOUS_REPAIRABLE if policy_allows else Repairability.HUMAN_REQUIRED
    )


def finding_fingerprint(
    *,
    source_role: RoleName,
    code: str,
    category: str,
    affected_files: Sequence[str] = (),
    evidence: str = "",
    acceptance: Sequence[str] = (),
) -> str:
    """Identidad canónica de un defecto, en hexadecimal de 64 caracteres.

    Se calcula sobre campos **estructurados** y normalizados —rol, código, categoría, archivos,
    evidencia y criterios de aceptación— y jamás sobre marcas de tiempo: dos defectos iguales
    detectados en momentos distintos tienen que compartir fingerprint para que «volvió el mismo
    defecto» sea detectable y el bucle pueda cortarse por falta de progreso.

    Args:
        source_role: Rol que detectó el defecto.
        code: Código del fallo, si lo hay.
        category: Categoría declarada del defecto.
        affected_files: Archivos afectados (el orden y los repetidos no cuentan).
        evidence: Evidencia observada.
        acceptance: Criterios de aceptación incumplidos.

    Returns:
        El sha256 hexadecimal del JSON canónico del defecto normalizado.
    """
    payload: dict[str, object] = {
        "source_role": source_role.value,
        "code": _normalize_text(code),
        "category": _normalize_text(category),
        "affected_files": _normalize_list(affected_files),
        "evidence": _normalize_text(evidence),
        "acceptance": _normalize_list(acceptance),
    }
    return _canonical_digest(payload)


def findings_from_result(
    *,
    result: RoleExecutionResult,
    stage: TaskStatus,
    step_index: int,
    acceptance_criteria: Sequence[str] = (),
    affected_files: Sequence[str] = (),
    code: str = "",
) -> tuple[RepairFinding, ...]:
    """Convierte los hallazgos **bloqueantes** de un resultado de rol en defectos reparables.

    Solo lo bloqueante entra al ciclo: un ``HIGH`` o un ``CRITICAL`` (la definición de «bloquea
    aprobar» que ya usa el motor). Un ``INFO``, ``LOW`` o ``MEDIUM`` no justifica mutar código.

    Si el rol pidió cambios sin detallar ninguno (``NEEDS_REPAIR`` sin hallazgos bloqueantes), se
    sintetiza **un** defecto ``NEEDS_REPAIR``/``REPAIR_REQUEST``: el motor no puede ignorar la
    petición de cambios, pero tampoco inventarse un diagnóstico que el rol no dio. Si además hay
    hallazgos bloqueantes, se usan esos y no se añade el sintético: el detalle real manda.

    El rol del defecto es el que **declara el hallazgo** (``WorkflowFinding.role``, «rol que lo
    encontró»): es lo que decide reglas como la parada de seguridad, y un defecto no puede dejar de
    ser de Security porque lo transporte un resultado etiquetado de otra forma. La petición
    sintética de cambios, que no viene de ningún hallazgo, usa el rol que ejecutó el resultado.

    Args:
        result: Resultado normalizado del rol.
        stage: Etapa en la que se ejecutó.
        step_index: Índice del paso, para poder volver a la traza.
        acceptance_criteria: Criterios de aceptación vigentes.
        affected_files: Archivos afectados declarados por quien llama.
        code: Código de fallo del resultado, si lo hay. El hallazgo normalizado no trae uno propio.

    Returns:
        Los defectos, en el mismo orden en que los devolvió el rol. Vacío si no hay nada que
        reparar. Ninguno viene clasificado: eso lo hace :func:`classify_repairability`.
    """
    blocking = result.blocking_findings
    if blocking:
        return tuple(
            _repair_finding(
                role=finding.role,
                stage=stage,
                step_index=step_index,
                code=code,
                category=finding.category,
                severity=finding.severity,
                summary=finding.message,
                evidence=finding.evidence,
                affected_files=affected_files,
                acceptance_criteria=acceptance_criteria,
            )
            for finding in blocking
        )
    if result.status is not RoleStatus.NEEDS_REPAIR:
        return ()
    detail = result.summary or result.error_detail
    return (
        _repair_finding(
            role=result.role,
            stage=stage,
            step_index=step_index,
            code="NEEDS_REPAIR",
            category="REPAIR_REQUEST",
            # La gravedad es la del contrato por defecto: el rol no dio ninguna, y subirla sería
            # inventar una urgencia que nadie declaró.
            severity=FindingSeverity.MEDIUM,
            summary=detail or "el rol pidió cambios sin detallar el defecto",
            evidence=detail,
            affected_files=affected_files,
            acceptance_criteria=acceptance_criteria,
        ),
    )


def _authorized_files(target_files: Sequence[str]) -> tuple[str, ...]:
    """Archivos autorizados por la petición, sin repetidos, sin vacíos y en su orden declarado.

    Es el mismo conjunto que el plan declara como ``target_files``: la autorización de escritura la
    fija la petición, y el diagnóstico solo puede situar el defecto donde esa autorización alcanza.
    Normalizar aquí (recorte de extremos y deduplicación estable) evita que ``" src/a.py"`` y
    ``"src/a.py"`` cuenten como dos archivos distintos según quién los declaró.
    """
    declared: dict[str, str] = {}
    for path in target_files:
        cleaned = path.strip()
        if cleaned:
            declared.setdefault(cleaned, cleaned)
    return tuple(declared.values())


def _located_files(finding: RepairFinding, authorized: Sequence[str]) -> tuple[str, ...]:
    """Archivos del defecto que están **dentro** de la autorización, en su orden, sin repetidos.

    Un archivo afectado que la petición no autorizó no se recorta ni se sustituye: queda fuera, y si
    no queda ninguno el diagnóstico no se puede sostener (``()``). Situar el defecto en un archivo
    que la reparación no puede tocar sería un diagnóstico que ninguna mutación autorizada podría
    cumplir.
    """
    allowed = set(authorized)
    seen: dict[str, str] = {}
    for path in finding.affected_files:
        cleaned = path.strip()
        if cleaned and cleaned in allowed:
            seen.setdefault(cleaned, cleaned)
    return tuple(seen.values())


def _evidence_names_file(finding: RepairFinding) -> bool:
    """True si la **evidencia** del informe nombra alguno de los archivos afectados.

    Es una comprobación literal —ruta completa o nombre de fichero, en minúsculas— sobre el texto
    que el rol ya había declarado, no una interpretación: si aparece, el informe es autocontenido
    para situar el defecto, y eso es lo que permite declarar confianza ``HIGH`` en vez de
    ``MEDIUM``.
    """
    text = finding.evidence.strip().lower()
    if not text:
        return False
    for path in finding.affected_files:
        cleaned = path.strip().lower()
        if not cleaned:
            continue
        basename = cleaned.rsplit("/", 1)[-1]
        if cleaned in text or basename in text:
            return True
    return False


def build_repair_diagnosis(
    *,
    findings: Sequence[RepairFinding],
    request: WorkflowRequest,
    target_files: Sequence[str],
    origin_stage: TaskStatus,
    detection_refs: Sequence[ArtifactReference] = (),
) -> RepairDiagnosis | None:
    """Traduce a ``RepairDiagnosis`` lo que los informes **demuestran**; ``None`` si no alcanza.

    Esta función es la frontera de honestidad del diagnóstico (F611-02) y por eso su contrato es
    estrecho a propósito. Lo que hace, exactamente:

    - **No infiere mecanismos.** No lee código, no reproduce el fallo, no llama a ningún modelo y no
      adivina una causa. Copia del defecto ya registrado: el código y la categoría (qué criterio
      falló), los archivos autorizados en los que el propio informe lo sitúa y la evidencia textual
      que el informe aportó. El ``root_cause_summary`` es esa ubicación con esa evidencia, no una
      narración de por qué ocurre.
    - **Devuelve ``None`` sin evidencia suficiente.** Si algún defecto del ciclo no trae evidencia,
      si no sitúa el defecto en ningún archivo autorizado, o si no hay defectos, no hay diagnóstico:
      quien llama bloquea con ``BLOCKED_EVIDENCE`` y con
      ``WORKFLOW_REPAIR_EVIDENCE_INCOMPLETE``, sin mutar nada. Ese ``None`` es la respuesta
      correcta, no un fallo: preferir un bloqueo declarado a una reparación especulativa es
      exactamente lo que esta frontera existe para hacer.
    - **No llena el contrato con una tautología.** Un diagnóstico cuyo ``root_cause_summary`` dijera
      «el defecto falló» o «hay que arreglar el defecto» no aportaría nada y aun así autorizaría una
      mutación; aquí no se produce ninguno así, porque el texto se construye con hechos del informe
      (rol, código, categoría, archivos) y con su evidencia.
    - **Declara lo que no sabe.** ``unknowns`` dice, en texto fijo y verificable, que la causa
      mecánica (línea, símbolo, expresión) no consta, que el fallo no se ha reproducido en este
      proceso y, cuando corresponde, que el paso que detectó el defecto no dejó ninguna referencia
      durable de su informe. Un ``unknowns`` vacío se leería como certeza total, que no es el caso.
    - **Es de PUNTO, no de un modelo.** ``model_proposed=False``: la conclusión la produjo este
      diagnoser determinista, y un lector del artefacto puede distinguirlo de una propuesta de
      modelo sin inspeccionar nada más.

    Sin razonamiento privado: el contrato ``RepairDiagnosis`` no tiene ningún campo de
    *chain-of-thought* y esta función no inventa uno. Lo único textual que copia es la evidencia que
    el informe del rol ya declaró como dato estructurado (``RepairFinding.evidence``), que es
    evidencia observable y no razonamiento del modelo.

    Args:
        findings: Defectos que el ciclo va a reparar, en el orden en que se conocieron.
        request: Petición del workflow. Aporta los criterios de aceptación vigentes.
        target_files: Archivos que la petición autoriza. Es la autorización de escritura, y solo
            dentro de ella se puede situar el defecto.
        origin_stage: Etapa de la que salió el ciclo; se conserva como dato del ciclo.
        detection_refs: Referencias durables de los informes que detectaron los defectos, si los
            publicaron. Se copian tal cual —nunca se fabrican— y son lo que permite a un proceso
            nuevo leer el informe original.

    Returns:
        El diagnóstico demostrable, o ``None`` si la evidencia no alcanza para afirmar nada.
    """
    if not findings:
        return None
    authorized = _authorized_files(target_files)
    located: list[tuple[RepairFinding, tuple[str, ...]]] = []
    for finding in findings:
        if not finding.evidence.strip():
            # Sin evidencia observada no se diagnostica: el resumen dice qué falla, pero afirmar una
            # causa con él sería inferir. Es el mismo criterio que ``classify_repairability``.
            return None
        files = _located_files(finding, authorized)
        if not files:
            # El informe no sitúa el defecto en ningún archivo que la reparación pueda tocar: no hay
            # diagnóstico honesto que hacer, solo uno inventado.
            return None
        located.append((finding, files))
    suspected: dict[str, str] = {}
    for _finding, files in located:
        for path in files:
            suspected.setdefault(path, path)
    codes = sorted(
        {finding.code or finding.category or _UNCLASSIFIED_DEFECT for finding, _ in located}
    )
    roles = sorted({finding.source_role.value for finding, _ in located})
    evidence = _DIAGNOSIS_EVIDENCE_SEPARATOR.join(
        finding.evidence.strip()[:_MAX_DIAGNOSIS_EVIDENCE_CHARS]
        for finding, _ in located
        if finding.evidence.strip()
    )
    return RepairDiagnosis(
        finding_ids=tuple(finding.finding_id for finding, _ in located)[:MAX_REPAIR_FINDINGS],
        # El texto afirma **dónde** y **qué criterio** falla, con la evidencia que lo sostiene. No
        # afirma por qué: eso no lo demuestra ningún dato del motor.
        root_cause_summary=(
            f"el informe de {', '.join(roles)} sitúa el defecto ({', '.join(codes)}) en "
            f"{', '.join(suspected)} con la evidencia: {evidence}"
        )[:MAX_WORKFLOW_SUMMARY_CHARS],
        evidence_refs=tuple(detection_refs)[:MAX_REPAIR_EVIDENCE],
        suspected_files=tuple(suspected)[:MAX_REPAIR_FILES],
        constraints=_diagnosis_constraints(suspected, request),
        proposed_strategy=(
            f"corregir {', '.join(codes)} en {', '.join(suspected)} con la mínima modificación y "
            f"volver a verificar desde {restart_stage(origin_stage).value}"
        )[:MAX_WORKFLOW_SUMMARY_CHARS],
        # ``MEDIUM`` es el mínimo de un diagnóstico que sí se emite: la evidencia existe y la
        # ubicación está declarada. ``HIGH`` solo cuando la propia evidencia nombra el archivo, que
        # es lo único que hace al informe autocontenido.
        confidence=(
            RepairConfidence.HIGH
            if all(_evidence_names_file(finding) for finding, _ in located)
            else RepairConfidence.MEDIUM
        ),
        unknowns=_diagnosis_unknowns(located, detection_refs),
        model_proposed=False,
    )


def _diagnosis_constraints(
    suspected: Mapping[str, str], request: WorkflowRequest
) -> tuple[str, ...]:
    """Restricciones del diagnóstico: hechos del contrato, no consejos del diagnoser.

    Las dos primeras son la autorización —qué se puede tocar y qué no— y la tercera son los
    criterios vigentes, que son contra los que después se verifica. Se nombran porque un diagnóstico
    que no dijera su alcance dejaría al Developer adivinando hasta dónde puede llegar.
    """
    items = [
        f"tocar solo los archivos autorizados: {', '.join(suspected)}",
        f"prohibido escribir en: {', '.join(PROTECTED_PATHS)}",
    ]
    criteria = tuple(
        criterion.strip() for criterion in request.acceptance_criteria if criterion.strip()
    )
    if criteria:
        items.append(f"criterios vigentes: {', '.join(criteria)[:MAX_WORKFLOW_SUMMARY_CHARS]}")
    return tuple(items)


def _diagnosis_unknowns(
    located: Sequence[tuple[RepairFinding, tuple[str, ...]]],
    detection_refs: Sequence[ArtifactReference],
) -> tuple[str, ...]:
    """Lo que el diagnóstico **no** puede demostrar, dicho de forma explícita y verificable.

    No es relleno: cada frase es un hecho comprobable sobre el propio diagnóstico, y su ausencia
    convertiría el artefacto en una afirmación de certeza que nadie ha demostrado.
    """
    unknowns = [
        "la causa mecánica (línea, símbolo o expresión concreta) no consta en el informe del rol",
        "el fallo no se ha reproducido en este proceso: el diagnóstico se apoya en el informe",
    ]
    if not detection_refs:
        unknowns.append(
            "el paso que detectó el defecto no publicó ninguna referencia durable de su informe"
        )
    if not all(_evidence_names_file(finding) for finding, _ in located):
        unknowns.append(
            "la evidencia no nombra el archivo: la ubicación es la que el defecto declara"
        )
    return tuple(unknowns)


def classify_repairability(
    *,
    finding: RepairFinding,
    request: WorkflowRequest,
    policy_allows: bool,
    evidence_sufficient: bool = True,
    infrastructure: bool = False,
) -> Repairability:
    """Clasifica un defecto con reglas deterministas, en este orden exacto.

    El orden **es** la política: el primer caso que aplica gana. Las reglas, una a una y con su
    porqué:

    a) **Sin evidencia no se repara** (``BLOCKED_EVIDENCE``). Una reparación especulativa es peor
       que un bloqueo declarado: no se toca código para «ver si era eso». Va primero porque ninguna
       otra regla puede autorizar lo que la falta de evidencia prohíbe.
    b) **Infraestructura** (``infrastructure`` o un ``code`` de :data:`INFRASTRUCTURE_CODES`) es
       ``RETRYABLE_INFRASTRUCTURE``: el defecto no está en el producto, así que se reintenta, no se
       repara. Va antes de las categorías porque un timeout no es un problema de constitución.
    c) **Categorías reservadas o irreversibles** (``NON_REPARABLE_CATEGORIES``) son
       ``NON_REPAIRABLE``: constitución, permisos, política, secretos, pagos, producción, legal,
       facturación, borrado irreversible, secreto maestro y modelo de negocio no se tocan solos.
    d) **Seguridad**: un hallazgo del rol Security con gravedad ``HIGH`` o ``CRITICAL`` es
       ``SECURITY_STOP`` (una frontera, no una tarea); con ``MEDIUM`` o ``LOW`` es reparable en
       autonomía **solo** si la política lo permite, y si no, ``HUMAN_REQUIRED``. La clasificación
       se decide por el rol que lo encontró: es Security quien tiene autoridad para declarar un
       problema de seguridad como tal.
    e) **El resto** (QA, Reviewer, auditoría cruzada, verificación visual y Developer): reparable en
       autonomía si la política lo permite; si no, ``HUMAN_REQUIRED``.

    Args:
        finding: Defecto a clasificar.
        request: Petición del workflow. Hoy la decisión no la necesita —``policy_allows`` ya la
            resume— pero se recibe para que el llamante no tenga que recalcular nada y para que una
            regla futura dependiente de la petición no cambie el contrato.
        policy_allows: True si la política vigente autoriza reparar en autonomía.
        evidence_sufficient: False si no hay evidencia bastante para reparar sin adivinar.
        infrastructure: True si quien llama ya sabe que el fallo es del entorno.

    Returns:
        La clasificación del defecto.
    """
    if not evidence_sufficient:
        return Repairability.BLOCKED_EVIDENCE
    if infrastructure or _normalize_token(finding.code) in INFRASTRUCTURE_CODES:
        return Repairability.RETRYABLE_INFRASTRUCTURE
    if _normalize_token(finding.category) in NON_REPARABLE_CATEGORIES:
        return Repairability.NON_REPAIRABLE
    if finding.source_role is RoleName.SECURITY:
        if finding.severity in _SECURITY_STOP_SEVERITIES:
            return Repairability.SECURITY_STOP
        return _autonomous_or_human(policy_allows)
    return _autonomous_or_human(policy_allows)


def restart_stage(origin_stage: TaskStatus) -> TaskStatus:
    """Etapa por la que se reanuda la verificación: siempre ``QA``.

    Si la reparación tocó código, el trabajo cambió, y lo que estaba verificado ya no lo está: la
    verificación vuelve a empezar por QA. No se reanuda en la etapa donde apareció el defecto
    porque eso dejaría sin comprobar el resto del camino limpio anterior a esa etapa, y una
    reparación puede romper algo que ya había pasado.

    ``origin_stage`` se recibe para que la traza conserve de dónde venía el ciclo; la regla de
    reanudación no depende de él a propósito, para que no haya forma de «reanudar más adelante»
    pidiéndolo desde una etapa tardía.
    """
    return TaskStatus.QA


def verification_chain(
    *, origin_stage: TaskStatus, request: WorkflowRequest
) -> tuple[RoleName, ...]:
    """Cadena de roles que hay que volver a pasar tras reparar, en orden.

    QA siempre entra, porque hubo mutación de código; después van Security y Reviewer, y al final
    las verificaciones independientes que la petición exija (auditoría cruzada y verificación
    visual). Ninguna gate se salta por decisión de un modelo: lo único que recorta la cadena es lo
    que la petición no exige, y quien lo decide es
    :func:`punto.workflow.pipeline.visual_qa_required` y el campo ``cross_audit_required``, no la
    reparación.
    """
    roles: list[RoleName] = [RoleName.QA, RoleName.SECURITY, RoleName.REVIEWER]
    if request.cross_audit_required:
        roles.append(RoleName.CROSS_AUDIT)
    if visual_qa_required(request):
        roles.append(RoleName.VISUAL_QA)
    return tuple(roles)


def plan_fingerprint(
    *, finding_fingerprints: Sequence[str], target_files: Sequence[str], strategy: str
) -> str:
    """Identidad canónica de un plan de reparación, en hexadecimal de 64 caracteres.

    Es lo que permite saber si el ciclo que se va a intentar es **el mismo** que ya falló: mismos
    defectos, mismos archivos y misma estrategia. Sin esta identidad, la detección de falta de
    progreso tendría que comparar planes «a ojo» y el bucle podría gastar el presupuesto repitiendo
    exactamente lo mismo.
    """
    payload: dict[str, object] = {
        "finding_fingerprints": _normalize_list(finding_fingerprints),
        "target_files": _normalize_list(target_files),
        "strategy": _normalize_text(strategy),
    }
    return _canonical_digest(payload)


def build_repair_decision(
    *,
    findings: Sequence[RepairFinding],
    request: WorkflowRequest,
    repairability: Repairability,
    policy_decision_id: UUID | None,
    origin_stage: TaskStatus,
    max_allowed_attempts: int,
    reason: str,
    effective_risk: RiskLevel | None = None,
    effective_authority: AuthorityLevel | None = None,
) -> RepairDecision:
    """Decide qué se hace con un conjunto de defectos, con la autoridad efectiva.

    ``requires_human`` se marca si la clasificación exige persona (``HUMAN_REQUIRED``) o si **no**
    es una reparación autónoma: ante cualquier clasificación que no sea ``AUTONOMOUS_REPAIRABLE``
    el defecto no lo arregla el motor, y decirlo explícitamente evita que un llamante trate
    ``NON_REPAIRABLE`` o ``BLOCKED_EVIDENCE`` como un permiso para intentarlo.

    El riesgo y la autoridad efectivos son los que recibe el kernel del Policy Engine; los
    declarados en la petición solo se usan si no hay otros, porque declarar menos no rebaja una
    acción L3.
    """
    resolved_risk = request.risk if effective_risk is None else effective_risk
    resolved_authority = request.authority if effective_authority is None else effective_authority
    requires_human = (
        repairability is Repairability.HUMAN_REQUIRED
        or not repairability.allows_autonomous_repair
    )
    return RepairDecision(
        finding_ids=tuple(finding.finding_id for finding in findings)[:MAX_REPAIR_FINDINGS],
        repairability=repairability,
        effective_risk=resolved_risk,
        effective_authority=resolved_authority,
        requires_human=requires_human,
        reason=reason[:MAX_WORKFLOW_SUMMARY_CHARS],
        policy_decision_id=policy_decision_id,
        max_allowed_attempts=max_allowed_attempts,
        verification_plan=verification_chain(origin_stage=origin_stage, request=request),
        origin_stage=origin_stage,
        restart_stage=restart_stage(origin_stage),
    )


def build_repair_plan(
    *,
    workflow_id: UUID,
    cycle: int,
    findings: Sequence[RepairFinding],
    diagnosis_id: UUID | None,
    target_files: Sequence[str],
    allowed_file_globs: Sequence[str] = (),
    forbidden_files: Sequence[str] = PROTECTED_PATHS,
    expected_changes: Sequence[str],
    acceptance_criteria: Sequence[str],
    verification_roles: Sequence[RoleName],
    risk: RiskLevel,
    authority: AuthorityLevel,
    policy_decision_id: UUID | None,
    budget_model_calls: int,
    budget_total_tokens: int,
    idempotency_key: str,
    strategy: str,
) -> RepairPlan:
    """Materializa el plan de una reparación: la autorización de escritura del ciclo.

    El plan es un contrato, no una sugerencia: fija qué archivos se pueden tocar, cuáles no, qué
    cambios se esperan y qué roles vuelven a verificar. Las colecciones se recortan a las cotas del
    contrato para que el checkpoint no pueda crecer porque un llamante pase de más. El
    ``plan_fingerprint`` se calcula aquí, con los mismos defectos y archivos que el plan declara,
    para que «mismo plan» sea una identidad y no una impresión.
    """
    files = tuple(target_files)[:MAX_REPAIR_FILES]
    return RepairPlan(
        workflow_id=workflow_id,
        cycle=cycle,
        finding_ids=tuple(finding.finding_id for finding in findings)[:MAX_REPAIR_FINDINGS],
        diagnosis_id=diagnosis_id,
        target_files=files,
        allowed_file_globs=tuple(allowed_file_globs)[:MAX_REPAIR_FILES],
        forbidden_files=tuple(forbidden_files)[:MAX_REPAIR_FILES],
        expected_changes=tuple(expected_changes)[:MAX_REPAIR_EVIDENCE],
        acceptance_criteria=tuple(acceptance_criteria)[:MAX_REPAIR_EVIDENCE],
        verification_roles=tuple(verification_roles)[:_MAX_VERIFICATION_ROLES],
        risk=risk,
        authority=authority,
        policy_decision_id=policy_decision_id,
        budget_model_calls=budget_model_calls,
        budget_total_tokens=budget_total_tokens,
        idempotency_key=idempotency_key,
        plan_fingerprint=plan_fingerprint(
            finding_fingerprints=tuple(finding.fingerprint for finding in findings),
            target_files=files,
            strategy=strategy,
        ),
    )


def no_progress(
    *,
    history: Sequence[RepairCycle],
    plan_fingerprint_value: str,
    max_identical: int = MAX_IDENTICAL_REPAIR_FAILURES,
) -> bool:
    """True si el plan indicado ya falló sin avanzar las veces permitidas.

    Esta es la frontera que impide gastar todas las reparaciones repitiendo lo mismo: se cuentan
    los ciclos de la historia con **ese mismo** ``plan_fingerprint`` y estado ``FAILED`` o
    ``NO_PROGRESS`` —intentos iguales que no movieron el defecto— y se corta al llegar al máximo. Un
    ciclo ``ROLLED_BACK`` o ``BLOCKED`` no cuenta: no fue un reintento del mismo plan.

    Args:
        history: Ciclos ya registrados del workflow.
        plan_fingerprint_value: Fingerprint del plan que se quiere intentar.
        max_identical: Repeticiones idénticas permitidas antes de cortar.

    Returns:
        True si ya se agotaron las repeticiones idénticas; en ese caso el plan no debe ejecutarse.
    """
    wanted = _normalize_text(plan_fingerprint_value)
    stalled = sum(
        1
        for cycle in history
        if cycle.status in _STALLED_CYCLE_STATUSES
        and _normalize_text(cycle.plan_fingerprint) == wanted
    )
    return stalled >= max_identical


def next_cycle_status(
    *, resolved: Sequence[RepairFinding], previous: Sequence[RepairFinding]
) -> RepairCycleStatus:
    """Estado del ciclo a partir de lo que quedó abierto después de verificar.

    Las cuatro ramas, en orden, porque el orden decide los empates:

    1. ``RESOLVED``: todos los defectos que estaban abiertos quedaron ``RESOLVED`` y no queda
       ninguno abierto. Va primero a propósito: sin abiertos, el conjunto abierto es idéntico al
       anterior (los dos vacíos) y sin esta prioridad un ciclo perfecto se leería como «sin
       progreso».
    2. ``NO_PROGRESS``: el conjunto de fingerprints abiertos es **idéntico** al anterior. El mismo
       defecto sigue ahí: el intento no movió nada.
    3. ``FAILED``: hay fingerprints abiertos que antes no estaban. El intento no solo no resolvió:
       introdujo un defecto nuevo, que es peor que no haber hecho nada.
    4. ``BLOCKED``: cualquier otro caso. Ocurre cuando un defecto anterior desapareció del
       conjunto sin quedar ``RESOLVED``: no hay evidencia de que se arreglara, así que no se declara
       resuelto ni se sigue adelante; se bloquea para que una persona mire qué pasó.
    """
    previous_open = {finding.fingerprint for finding in previous if finding.is_open}
    current_open = {finding.fingerprint for finding in resolved if finding.is_open}
    resolved_fingerprints = {
        finding.fingerprint
        for finding in resolved
        if finding.status is RepairFindingStatus.RESOLVED
    }
    if not current_open and previous_open <= resolved_fingerprints:
        return RepairCycleStatus.RESOLVED
    if current_open == previous_open:
        return RepairCycleStatus.NO_PROGRESS
    if current_open - previous_open:
        return RepairCycleStatus.FAILED
    return RepairCycleStatus.BLOCKED


__all__ = [
    "INFRASTRUCTURE_CODES",
    "NON_REPARABLE_CATEGORIES",
    "PROTECTED_PATHS",
    "build_repair_decision",
    "build_repair_diagnosis",
    "build_repair_plan",
    "classify_repairability",
    "finding_fingerprint",
    "findings_from_result",
    "next_cycle_status",
    "no_progress",
    "plan_fingerprint",
    "restart_stage",
    "verification_chain",
]
