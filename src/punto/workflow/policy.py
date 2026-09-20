"""Gobierno de política y Human Gate del kernel de workflow (ENGINE-6.0.1).

Este módulo **no** es una segunda constitución: es la frontera por la que el kernel de workflow
consulta la autoridad que ya existe. Aquí no se decide nada por segunda vez.

- El **Policy Engine** (``punto.policy.policy_engine``) sigue siendo el único que emite una
  :class:`PolicyDecision`. Este módulo construye la :class:`ActionRequest` y le entrega el
  veredicto al kernel tal cual.
- El **Human Gate** (``punto.policy.human_gate``) sigue siendo el único que emite una
  :class:`HumanApprovalProof`. Este módulo **nunca** la construye: solo la verifica.
- ``config/permissions.yaml`` sigue siendo el único catálogo de acciones y niveles de autoridad.

Lo que añade este módulo son las tres garantías que el kernel necesita:

1. **La acción es explícita y obligatoria** (V60-01). La :class:`ActionRequest` se construye con
   ``action=request.action``, ``risk_level=request.risk`` y ``files_changed=request.changed_files``,
   y con las banderas de impacto **derivadas del nombre de la acción** —técnica, reversible,
   producción, legal, negocio, secreto maestro—, nunca de lo que declare el llamante. Una acción
   que no está en la tabla de impacto **no llega al motor**: se resuelve como *default deny*.
2. **El impacto manda sobre la declaración** (V60-01). Una acción de la tabla L3 —producción,
   pagos/financiero, borrado irreversible, secreto maestro, legal, cambio de modelo de negocio, y
   la modificación de ``constitution.yaml``/``permissions.yaml``— permanece L3 aunque la petición
   declare ``risk=LOW`` o ``authority=L0``: la tabla fuerza ``technical=False`` y
   ``reversible=False``, y el riesgo efectivo se eleva con los escaladores del Risk Engine. El
   resultado efectivo (``decision.outcome``, ``decision.authority_level``,
   ``decision.effective_risk``) sale siempre del motor.
3. **Salir de ``HUMAN_APPROVAL`` exige una prueba real** (V60-02). El kernel no puede aprobarse a
   sí mismo: la :class:`HumanApprovalProof` la emite ``HumanGate.authorize_resume`` y aquí solo se
   **verifica** contra el workflow. Sin prueba, con prueba de otra tarea, de otra decisión de
   política, de otro gate, con otro estado de reanudación o ya consumida, la reanudación se rechaza
   con un código estable.
4. **La reparación se evalúa contra su propia acción** (F611-03). El ciclo de reparación muta
   archivos, así que no se juzga con ``run.request.action`` —que describe el trabajo original— sino
   con la acción canónica de escritura y con los ``target_files`` reales del plan:
   :meth:`WorkflowPolicy.evaluate_repair_action`. El riesgo se eleva por los archivos que el plan
   toca (rutas protegidas y familias de mayor riesgo) y la reversibilidad se comprueba contra el
   snapshot del ciclo, nunca se supone.

Sobre ``swing`` en :meth:`WorkflowPolicy.request_human_gate`: es el descriptor del cambio de estado
que motiva el gate (``from_status``/``to_status`` o ``current_state``/``proposed_next_state``). Se
usa únicamente para enriquecer el motivo auditable que se registra en el Human Gate y **no** decide
nada: el estado de reanudación autorizado sale siempre de ``human_gate_resume_status``. Es opcional
(``None`` por defecto) para que la frontera funcione aunque el llamante no disponga de él.

Códigos de fallo usados:

- ``WORKFLOW_APPROVAL_PROOF_INVALID``: la autorización presentada no es válida, y también —por
  coherencia— una solicitud de gate que no declara su ``policy_decision_id``: sin la decisión del
  motor que la motiva, la aprobación no podría autorizar nada nunca (``HumanGate.authorize_resume``
  se niega a emitir una prueba sin decisión), así que se rechaza al entrar, no al salir.
- ``WORKFLOW_HUMAN_APPROVAL_REQUIRED``: se intentó reanudar desde ``HUMAN_APPROVAL`` sin prueba.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final
from uuid import UUID

from punto.common import basename_of, normalize_path
from punto.policy.human_gate import (
    RESUMABLE_STATUSES,
    HumanApprovalProof,
    HumanGate,
)
from punto.policy.policy_engine import PolicyEngine, PolicyEvaluationContext
from punto.schemas.decision import ActionRequest, HumanApprovalRequest
from punto.schemas.enums import AuthorityLevel, RiskLevel, TaskStatus
from punto.schemas.policy import PolicyDecision, PolicyOutcome
from punto.schemas.repair import RepairPlan
from punto.schemas.workflow import (
    MAX_WORKFLOW_SUMMARY_CHARS,
    HumanGateRequest,
    RoleName,
    WorkflowFailureCode,
    WorkflowRequest,
    WorkflowRun,
)
from punto.workflow.errors import WorkflowApprovalProofInvalidError, WorkflowError

#: Resultados que habilitan la ejecución. Es el mapeo fijo del encargo: el ``outcome`` del motor
#: es la fuente única de verdad de si la acción puede continuar.
_ALLOWED_OUTCOMES: Final[frozenset[PolicyOutcome]] = frozenset(
    {PolicyOutcome.ALLOW, PolicyOutcome.ALLOW_WITH_REVIEW}
)


@dataclass(frozen=True, slots=True)
class ActionImpact:
    """Impacto determinista derivado del nombre de la acción (no del llamante)."""

    technical: bool
    reversible: bool
    production: bool
    legal: bool
    business: bool
    master_secret: bool


@dataclass(frozen=True, slots=True)
class PolicyGate:
    """Veredicto de política listo para el kernel: qué se decidió y con qué autoridad.

    Es una vista **resumida** e inmutable de la :class:`PolicyDecision` del motor: la decisión
    completa viaja en ``decision`` para auditoría y para vincular un Human Gate a ella.
    """

    outcome: PolicyOutcome
    decision: PolicyDecision
    authority: AuthorityLevel
    risk: RiskLevel
    requires_review: bool
    requires_human: bool
    allowed: bool
    reason: str

    @property
    def policy_decision_id(self) -> UUID:
        """Identificador de la decisión del motor que ampara este veredicto.

        Es el valor que el kernel guarda en un artefacto derivado —``RepairPlan.policy_decision_id``
        en el ciclo de reparación, el Human Gate en la parada humana— y la única vía admitida para
        recuperar *esa* decisión del historial del motor (``PolicyEngine.decision_by_id``): la
        posición en el historial mezclaría tareas concurrentes.
        """
        return self.decision.id


#: Impacto base del trabajo autónomo: técnico, reversible y sin impacto externo declarado.
_AUTONOMOUS_IMPACT: Final[ActionImpact] = ActionImpact(
    technical=True,
    reversible=True,
    production=False,
    legal=False,
    business=False,
    master_secret=False,
)


def _l3_impact(
    *,
    production: bool = False,
    legal: bool = False,
    business: bool = False,
    master_secret: bool = False,
) -> ActionImpact:
    """Impacto de una acción L3: nunca técnica y nunca reversible, con su bandera de impacto.

    ``technical=False`` y ``reversible=False`` son el **piso** de toda acción reservada a decisión
    humana: aunque la petición las declarara a favor, el motor nunca las verá como tales, y el
    escalador de irreversibilidad del Risk Engine eleva el riesgo efectivo como mínimo a ``HIGH``.
    """
    return ActionImpact(
        technical=False,
        reversible=False,
        production=production,
        legal=legal,
        business=business,
        master_secret=master_secret,
    )


#: Tabla de impacto: **espejo** del catálogo real de ``config/permissions.yaml``.
#:
#: Cada acción catalogada tiene aquí su impacto. La cobertura se comprueba en las pruebas contra el
#: catálogo real del motor, de modo que añadir una acción al catálogo sin declarar su impacto no
#: pase inadvertido: una acción catalogada pero ausente de esta tabla caería en *default deny*.
#:
#: Nota sobre ``master_secret``: la :class:`ActionRequest` del motor no tiene una bandera de
#: secreto, así que este campo es declarativo. Se marca también ``high_security_risk`` porque el
#: contrato de seis campos no tiene bandera de seguridad y agruparla con lo sensible a secretos es
#: la lectura conservadora (nunca más permisiva).
_ACTION_IMPACTS: Final[MappingProxyType[str, ActionImpact]] = MappingProxyType(
    {
        # ---------------------------------------------------------- LEVEL 0 ----
        "create_file": _AUTONOMOUS_IMPACT,
        "modify_file": _AUTONOMOUS_IMPACT,
        "refactor_code": _AUTONOMOUS_IMPACT,
        "run_tests": _AUTONOMOUS_IMPACT,
        "fix_bug": _AUTONOMOUS_IMPACT,
        "create_branch": _AUTONOMOUS_IMPACT,
        "create_commit": _AUTONOMOUS_IMPACT,
        # PILOT-05: borrar o mover un fichero local del proyecto autorizado. Está catalogada
        # porque el riesgo lo decide el sobre adaptativo: borrar algo preexistente exige
        # autorización humana y borrar lo que el propio ciclo creó es deshacer
        # (`irreversible_delete` sigue sin ser autónoma nunca).
        "delete_file": _AUTONOMOUS_IMPACT,
        "create_documentation": _AUTONOMOUS_IMPACT,
        "simulate_failure": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 1 ----
        "install_dependency": _AUTONOMOUS_IMPACT,
        "modify_secondary_api": _AUTONOMOUS_IMPACT,
        "modify_dev_schema": _AUTONOMOUS_IMPACT,
        "modify_major_component": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 2 ----
        "replace_library": _AUTONOMOUS_IMPACT,
        "secondary_architecture_change": _AUTONOMOUS_IMPACT,
        "remove_noncritical_module": _AUTONOMOUS_IMPACT,
        "major_refactor": _AUTONOMOUS_IMPACT,
        # ---------------------------------------------------------- LEVEL 3 ----
        # Producción.
        "deploy_production": _l3_impact(production=True),
        "production_database_delete": _l3_impact(production=True),
        # Borrado irreversible.
        "irreversible_delete": _l3_impact(),
        # Pagos / financiero. El contrato no tiene bandera financiera propia: el impacto económico
        # es impacto de negocio, que es lo que la regla de autonomía constitucional prohíbe.
        "payment": _l3_impact(business=True),
        "financial_action": _l3_impact(business=True),
        # Legal y modelo de negocio.
        "legal_change": _l3_impact(legal=True),
        "business_model_change": _l3_impact(business=True),
        # Secretos maestros y seguridad elevada.
        "master_secret_change": _l3_impact(master_secret=True),
        "high_security_risk": _l3_impact(master_secret=True),
        # ------------------------------------- DB AUTHORITY v0 (hallazgo F-9) ----
        # Las acciones de base de datos entraron en el catálogo con DB AUTHORITY EXECUTOR v0 y no se
        # declararon aquí: el espejo quedó incompleto y una acción catalogada caía en *default deny*
        # al calcular su impacto. Su nivel lo sigue fijando el catálogo revisado por un humano
        # (``config/permissions.yaml``); aquí solo se declara su impacto, que es lo que esta tabla
        # representa. Nada de esto concede autoridad: el nivel y las reglas de Human Gate mandan.
        #
        # Nivel 0: conectividad, introspección y lectura en la base de **desarrollo**.
        "db_connect_check": _AUTONOMOUS_IMPACT,
        "db_introspect": _AUTONOMOUS_IMPACT,
        "db_safe_read": _AUTONOMOUS_IMPACT,
        # Nivel 1: cambio de esquema no destructivo y seed, con revisión posterior obligatoria.
        "db_migration_apply": _AUTONOMOUS_IMPACT,
        "db_seed": _AUTONOMOUS_IMPACT,
        # Nivel 3: lo destructivo, lo masivo y la creación de recursos externos. Nunca autónomo.
        "db_destructive_apply": _l3_impact(),
        "db_mass_data_change": _l3_impact(),
        # Crear un recurso externo (proyecto, branch, rol) puede tener coste: es impacto de negocio.
        "external_resource_create": _l3_impact(business=True),
    }
)


def action_impact(action: str) -> ActionImpact | None:
    """Impacto declarado de una acción, o ``None`` si la acción es desconocida.

    La búsqueda normaliza el nombre (espacios y mayúsculas) igual que el catálogo de autoridad, de
    modo que ``"  DEPLOY_PRODUCTION  "`` y ``"deploy_production"`` son la misma acción.
    """
    return _ACTION_IMPACTS.get(action.strip().lower())


def known_actions() -> tuple[str, ...]:
    """Acciones con impacto declarado, en orden alfabético determinista."""
    return tuple(sorted(_ACTION_IMPACTS))


# ------------------------------------------------------------------ reparación
#: Acciones del catálogo que expresan la **mutación** de una reparación, en orden de preferencia.
#:
#: ``fix_bug`` es la acción canónica de «corregir un defecto reproducido por pruebas», que es
#: exactamente la reparación; ``modify_file`` es el respaldo para un catálogo que no la declare. Las
#: dos existen ya en ``config/permissions.yaml``: esta frontera **no** añade entradas al catálogo ni
#: inventa una acción de reparación propia, porque el nivel de autoridad de una reparación tiene que
#: salir de la tabla que un humano revisó.
REPAIR_ACTION_PREFERENCES: Final[tuple[str, ...]] = ("fix_bug", "modify_file")

#: Rol y etapa que ejecutan la reparación. Se fijan aquí porque la mutación de reparación la lleva a
#: cabo el Developer **dentro** de ``REPAIRING``: lo único que la reparación no hereda es la
#: **acción** del trabajo original, no el contexto en el que se ejecuta.
_REPAIR_ROLE: Final[RoleName] = RoleName.DEVELOPER
_REPAIR_STAGE: Final[TaskStatus] = TaskStatus.REPAIRING

#: Piso de riesgo de una reparación que toca una **ruta protegida** de la configuración
#: (``PolicyEngine.protected_paths``: constitución, permisos, reglas de riesgo, presupuestos y
#: entornos). Es el escalador ``files_changed_on_protected_path: CRITICAL`` que
#: ``config/risk-rules.yaml`` declara y el Policy Engine no aplica —lo deja anotado como «se
#: gestiona en el Policy Engine»—: aquí se aplica de verdad para la mutación de reparación.
_PROTECTED_TARGET_RISK: Final[RiskLevel] = RiskLevel.CRITICAL

#: Piso de riesgo de una reparación que toca una familia de **mayor riesgo** por su nombre o su
#: ubicación: autenticación, seguridad, frontera de política, secretos y credenciales, gates de CI y
#: configuración. No son rutas constitucionalmente protegidas, pero una reparación que las escribe
#: cambia la superficie de confianza del producto y no se autoriza sola.
_HIGH_RISK_TARGET_RISK: Final[RiskLevel] = RiskLevel.HIGH

#: Prefijos de ruta que sitúan un archivo en una familia de mayor riesgo. Se comparan sobre la ruta
#: normalizada (posix, sin ``./``, en minúsculas), de modo que ``.\\src\\auth.py`` y ``src/auth.py``
#: son la misma ruta.
_HIGH_RISK_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "src/punto/policy/",
    "src/punto/tools/security",
    ".github/",
    ".env",
    "config/",
)

#: Piezas de un nombre de archivo que lo sitúan en una familia de mayor riesgo. La comparación es
#: por **pieza completa** (los segmentos de la ruta partidos por todo lo que no sea alfanumérico),
#: no por subcadena: así ``src/auth.py`` y ``src/auth/session.py`` son de riesgo alto, y
#: ``src/author.py`` o ``src/tokenizer.py`` no lo son por un parecido accidental.
_HIGH_RISK_PATH_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "constitution",
        "credential",
        "credentials",
        "password",
        "permission",
        "permissions",
        "policy",
        "secret",
        "secrets",
        "security",
        "token",
        "tokens",
    }
)


class WorkflowPolicy:
    """Frontera de autoridad del kernel: el Policy Engine decide y el Human Gate autoriza."""

    def __init__(self, *, engine: PolicyEngine, gate: HumanGate) -> None:
        self._engine = engine
        self._gate = gate

    @property
    def engine(self) -> PolicyEngine:
        """Policy Engine real en uso. Es el único emisor de decisiones."""
        return self._engine

    @property
    def gate(self) -> HumanGate:
        """Human Gate real en uso. Es el único emisor de autorizaciones."""
        return self._gate

    # ------------------------------------------------------------- evaluación
    def evaluate_action(
        self, *, request: WorkflowRequest, role: RoleName, stage: TaskStatus
    ) -> PolicyGate:
        """Evalúa la acción de una petición y devuelve el veredicto del Policy Engine.

        La acción se toma **de la petición** y su impacto de la tabla derivada del nombre, nunca de
        lo que declare el llamante. ``role`` y ``stage`` solo se registran en el motivo auditable.

        Una acción desconocida no se consulta al motor: se resuelve como *default deny* con la
        misma forma que la rama 1 de ``PolicyEngine.evaluate``.
        """
        impact = action_impact(request.action)
        if impact is None:
            denied = _default_deny_decision(request.action)
            return _gate_from_decision(denied, role=role, stage=stage)

        action_request = ActionRequest(
            action=request.action,
            technical=impact.technical,
            reversible=impact.reversible,
            production_impact=impact.production,
            legal_impact=impact.legal,
            business_impact=impact.business,
            risk_level=request.risk,
            files_changed=list(request.changed_files),
            description=request.objective,
            task_id=str(request.task_id),
        )
        decision = self._engine.evaluate(
            action_request, PolicyEvaluationContext(actor=role.value)
        )
        return _gate_from_decision(decision, role=role, stage=stage)

    # --------------------------------------------------- evaluación de reparación
    def evaluate_repair_action(self, *, run: WorkflowRun, plan: RepairPlan) -> PolicyGate:
        """Evalúa la **mutación de reparación**, no la acción original del workflow.

        Por qué la reparación puede pesar más que el trabajo original
        ------------------------------------------------------------
        Una reparación no es la continuación de la acción que pidió el humano: es **otra** acción.
        ``run.request.action`` describe el trabajo original —``run_tests``, ``create_file``,
        ``deploy_production``—, y la reparación casi siempre **escribe archivos**. Reutilizar aquel
        nombre evaluaría una acción que no se va a ejecutar: dejaría sin juzgar los archivos que la
        reparación va a tocar (una reparación dentro de un workflow de ``run_tests`` podría
        modificar ``src/auth.py`` sin que el motor viera esa ruta) y, en el otro sentido, heredaría
        el nivel de autoridad de una acción distinta. Además, la reparación muta un árbol que ya
        estaba verificado, con un presupuesto de ciclos acotado, y puede tocar la frontera de
        política, la seguridad o la configuración protegida: por eso su riesgo puede ser **mayor**
        que el del trabajo original aunque el objetivo sea «más pequeño».

        Por qué no se hereda la decisión previa
        ---------------------------------------
        Una :class:`PolicyDecision` autoriza **una** acción sobre **una** lista de archivos. La
        aprobación de ``run_tests`` no dice nada sobre escribir ``src/auth.py``, y una decisión
        anterior favorable no puede convertirse en un permiso permanente sobre cualquier mutación
        posterior: la autoridad se pide por acción, no por workflow. Por eso aquí se construye una
        petición nueva y se consulta al motor otra vez, en lugar de reutilizar el veredicto que ya
        viaja en el checkpoint.

        Qué se evalúa
        -------------
        - **Acción**: la canónica del catálogo apropiada a modificar archivos —
          :data:`REPAIR_ACTION_PREFERENCES`—. No se añade ninguna entrada a ``permissions.yaml``: si
          ninguna de esas acciones estuviera catalogada, la reparación no se evalúa y se resuelve
          como *default deny*.
        - **Recursos**: los ``target_files`` reales del plan, sin globs y sin nada que venga de un
          diagnóstico: son los archivos que el plan autoriza a escribir.
        - **Riesgo**: el del plan, elevado al piso que imponen los archivos que toca —
          :data:`_PROTECTED_TARGET_RISK` para una ruta protegida de la configuración (y la
          modificación de ``constitution.yaml``/``permissions.yaml`` la rechaza el propio motor
          antes de llegar al riesgo), :data:`_HIGH_RISK_TARGET_RISK` para autenticación, seguridad,
          frontera de política, secretos, CI y configuración—. Un riesgo ``HIGH`` o ``CRITICAL``
          exige Human Gate por defecto, así que el veredicto sube de autoridad efectiva por la vía
          del motor: aquí no se inventa un nivel de catálogo para la acción de reparación.
        - **Reversibilidad**: real, no supuesta. La reparación es reversible solo si el snapshot del
          ciclo la cubre: sin archivos declarados no hay nada que un snapshot pueda cubrir, y un
          snapshot registrado que no cubra todos los objetivos deja la mutación sin deshacer. En
          ambos casos ``reversible=False`` y el escalador de irreversibilidad del Risk Engine la
          lleva como mínimo a ``HIGH``. Si todavía no hay snapshot, la cobertura la garantiza el
          contrato del ciclo: el kernel captura exactamente ``plan.target_files`` **antes** de mutar
          y bloquea el ciclo si no puede capturarlo.
        - **Descripción**: acotada a los defectos del plan (código/categoría y resumen) y a los
          cambios esperados. Nunca razonamiento del modelo ni el objetivo del workflow.
        - **Tarea**: ``run.task_id``, para que la decisión quede ligada a este workflow.

        Mapeo del veredicto, que decide el motor y aquí no se reinterpreta: ``ALLOW`` ⇒ se puede
        reparar; ``ALLOW_WITH_REVIEW`` ⇒ se repara y la verificación completa vuelve a pasar;
        ``REQUIRE_HUMAN`` ⇒ Human Gate; ``REJECT`` ⇒ no se muta nada.

        El plan puede traer ``policy_decision_id=None`` durante esta evaluación: es el kernel quien
        guarda después el identificador devuelto —:attr:`PolicyGate.policy_decision_id`— en
        ``RepairPlan.policy_decision_id``. Un identificador que ya viniera en el plan **no** se
        reutiliza: la decisión que se devuelve es siempre la de esta evaluación.

        Returns:
            El :class:`PolicyGate` **tal cual** lo emite el Policy Engine.
        """
        action = self._repair_action()
        impact = action_impact(action) if action is not None else None
        if action is None or impact is None:
            denied = _default_deny_decision(action or REPAIR_ACTION_PREFERENCES[0])
            return _gate_from_decision(denied, role=_REPAIR_ROLE, stage=_REPAIR_STAGE)

        declared = _declared_paths(plan.target_files)
        protected = _protected_targets(self._engine.protected_paths, declared)
        high_risk = _high_risk_targets(declared)
        risk_floor = _repair_risk_floor(plan.risk, protected=protected, high_risk=high_risk)
        reversible = impact.reversible and _repair_is_reversible(
            run, declared=_normalized_paths(declared)
        )
        action_request = ActionRequest(
            action=action,
            technical=impact.technical,
            # La reversibilidad de la reparación no la declara el plan: la decide el snapshot. Una
            # acción irreversible no se vuelve reversible por un snapshot, de ahí el ``and``.
            reversible=reversible,
            production_impact=impact.production,
            legal_impact=impact.legal,
            business_impact=impact.business,
            risk_level=risk_floor,
            files_changed=list(declared),
            description=_repair_description(run, plan),
            task_id=str(run.task_id),
        )
        decision = self._engine.evaluate(
            action_request, PolicyEvaluationContext(actor=_REPAIR_ROLE.value)
        )
        return _gate_from_decision(decision, role=_REPAIR_ROLE, stage=_REPAIR_STAGE)

    def _repair_action(self) -> str | None:
        """Acción catalogada que expresa la mutación de reparación, o ``None`` si no hay ninguna.

        La preferencia es :data:`REPAIR_ACTION_PREFERENCES` y el catálogo real es el del motor: una
        acción que no esté en él no se puede usar, porque el nivel de autoridad de la reparación
        tiene que salir de la tabla que un humano revisó y no de una suposición de esta frontera.
        """
        for candidate in REPAIR_ACTION_PREFERENCES:
            if self._engine.catalog.has(candidate):
                return candidate
        return None

    # ------------------------------------------------------------ human gate
    def request_human_gate(
        self,
        *,
        request: WorkflowRequest,
        gate_request: HumanGateRequest,
        swing: object | None = None,
    ) -> HumanApprovalRequest:
        """Registra la solicitud de aprobación en el Human Gate **real**.

        Sin ``policy_decision_id`` no se registra nada: una aprobación sin la decisión que la motiva
        no podría autorizar ninguna reanudación, así que se rechaza al entrar.

        El ``resume_status`` es ``gate_request.human_gate_resume_status``, **sin sustituciones**: si
        no coincide con ``proposed_next_state`` o no es un destino de reanudación autorizado, no se
        registra ninguna aprobación (hallazgo V602-01).

        Returns:
            La solicitud creada en el Human Gate, en estado ``PENDING``.

        Raises:
            WorkflowApprovalProofInvalidError: si el destino declarado y el propuesto no coinciden o
                el declarado no es reanudable.
        """
        decision_id = gate_request.policy_decision_id
        if decision_id is None:
            raise WorkflowError(
                WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
                (
                    "el Human Gate no declara 'policy_decision_id': sin la decisión del Policy "
                    "Engine que lo motiva no se registra una aprobación ni se puede autorizar "
                    "ninguna reanudación"
                ),
            )

        detail = gate_request.context_summary.strip() or request.objective.strip()
        reason = f"{gate_request.reason_code.value}: {detail}"
        note = _swing_note(swing)
        if note:
            reason = f"{reason} ({note})"

        return self._gate.request(
            task_id=gate_request.task_id,
            action=gate_request.requested_action,
            risk=gate_request.risk,
            reason=reason,
            resume_status=_resume_status_for(gate_request),
            policy_outcome=gate_request.policy_outcome,
            policy_decision_id=decision_id,
        )

    def authorize_resume(self, approval_id: UUID, *, task_id: UUID) -> HumanApprovalProof:
        """Emite la autorización de reanudación delegando en el Human Gate real.

        Este módulo **no** construye la prueba: el único emisor es
        :meth:`punto.policy.human_gate.HumanGate.authorize_resume`, que exige una solicitud
        ``APPROVED`` con decisión de política y un estado de reanudación autorizado. Los errores del
        gate (``HumanGateError``) se propagan sin reescribir: son su taxonomía, no la del kernel.
        """
        return self._gate.authorize_resume(approval_id, task_id=task_id)

    def verify_proof(self, proof: HumanApprovalProof | None, *, run: WorkflowRun) -> None:
        """Verifica que la prueba autoriza **esta** reanudación concreta.

        Comprueba, en orden: que el workflow está en ``HUMAN_APPROVAL``, que tiene Human Gate, que
        la prueba no está consumida (``run.human_gate_approved``) y que la prueba corresponde a la
        tarea, a la solicitud del gate, a la decisión de política y al estado de reanudación
        registrados en el workflow.

        Raises:
            WorkflowError: con ``WORKFLOW_HUMAN_APPROVAL_REQUIRED`` si no hay prueba, y con
                ``WORKFLOW_APPROVAL_PROOF_INVALID`` ante cualquier prueba que no encaje.
        """
        if proof is None:
            raise WorkflowError(
                WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED,
                (
                    f"el workflow {run.workflow_id} está en {run.status.value} y no se presentó "
                    "ninguna HumanApprovalProof: sin la autorización emitida por el Human Gate no "
                    "se reanuda"
                ),
            )

        if run.status is not TaskStatus.HUMAN_APPROVAL:
            raise _invalid_proof(
                f"el workflow está en {run.status.value}, no en {TaskStatus.HUMAN_APPROVAL.value}"
            )

        gate = run.human_gate
        if gate is None:
            raise _invalid_proof("el workflow no tiene ningún Human Gate registrado")

        if run.human_gate_approved:
            raise _invalid_proof(
                f"la prueba de la solicitud {proof.approval_id} ya se consumió: cada aprobación "
                "autoriza una sola reanudación"
            )

        if proof.task_id != run.task_id:
            raise _invalid_proof(
                f"la prueba pertenece a la tarea {proof.task_id} y no a {run.task_id}"
            )

        if gate.approval_id is None or proof.approval_id != gate.approval_id:
            raise _invalid_proof(
                f"la prueba es de la solicitud {proof.approval_id} y el gate del workflow es "
                f"{gate.approval_id}"
            )

        if gate.policy_decision_id is None or proof.policy_decision_id != gate.policy_decision_id:
            raise _invalid_proof(
                f"la prueba cita la decisión {proof.policy_decision_id} y el gate del workflow "
                f"cita {gate.policy_decision_id}"
            )

        # Coherencia del destino: el estado que el gate propone aplicar, el que declara autorizar y
        # el que la prueba autoriza tienen que ser el mismo. Sin esta comprobación una prueba que
        # autoriza ``IN_PROGRESS`` podía reanudar a ``ANALYZING`` (hallazgo V602-01).
        if gate.proposed_next_state != gate.human_gate_resume_status:
            raise _invalid_proof(
                f"el gate propone {gate.proposed_next_state.value} y declara autorizar "
                f"{gate.human_gate_resume_status.value}: autorización y destino no coinciden"
            )

        if proof.resume_status != gate.human_gate_resume_status:
            raise _invalid_proof(
                f"la prueba autoriza reanudar a {proof.resume_status.value} y el gate declaró "
                f"{gate.human_gate_resume_status.value}"
            )

        if proof.resume_status != gate.proposed_next_state:
            raise _invalid_proof(
                f"la prueba autoriza reanudar a {proof.resume_status.value} y el destino real del "
                f"workflow es {gate.proposed_next_state.value}"
            )


# --------------------------------------------------------------------- interno
def _gate_from_decision(
    decision: PolicyDecision, *, role: RoleName, stage: TaskStatus
) -> PolicyGate:
    """Vista del kernel sobre la decisión del motor, sin reinterpretar su veredicto.

    La autoridad, el riesgo y el resultado salen del motor tal cual; el módulo solo deriva las dos
    banderas que el kernel consulta (``requires_review``/``requires_human``) y añade la traza de
    quién pidió la evaluación y en qué etapa.
    """
    return PolicyGate(
        outcome=decision.outcome,
        decision=decision,
        authority=decision.authority_level,
        risk=decision.effective_risk,
        requires_review=(
            decision.requires_review or decision.outcome is PolicyOutcome.ALLOW_WITH_REVIEW
        ),
        requires_human=(
            decision.requires_human or decision.outcome is PolicyOutcome.REQUIRE_HUMAN
        ),
        allowed=decision.outcome in _ALLOWED_OUTCOMES,
        reason=_reason_for(decision.reason, role=role, stage=stage),
    )


def _declared_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Rutas declaradas por el plan: sin vacíos, sin repetidos y en su orden original.

    Se conserva la forma que declaró el plan (es lo que se envía al motor como recurso y lo que
    queda en la traza); la normalización se reserva para las comparaciones.
    """
    seen: dict[str, str] = {}
    for path in paths:
        cleaned = path.strip()
        if cleaned:
            seen.setdefault(normalize_path(cleaned), cleaned)
    return tuple(seen.values())


def _normalized_paths(paths: Sequence[str]) -> frozenset[str]:
    """Conjunto normalizado de rutas, para comparar coberturas y protecciones."""
    return frozenset(normalize_path(path) for path in paths if path.strip())


def _protected_targets(
    protected: Sequence[str], declared: Sequence[str]
) -> tuple[str, ...]:
    """Rutas del plan que son **protegidas** para el motor, ordenadas y sin duplicados.

    La lista de protegidas sale del propio Policy Engine (``protected_paths``: el piso en código más
    la configuración explícita), no de una tabla paralela de esta frontera: si la protección crece
    en la configuración, esta comprobación la ve sin cambios. La comparación es conservadora —ruta
    exacta, sufijo de ruta y nombre base, igual que la protección constitucional— porque un falso
    positivo solo eleva el riesgo, nunca lo rebaja.
    """
    known = tuple(normalize_path(path) for path in protected)
    bases = frozenset(basename_of(path) for path in known if path)
    found = {
        target
        for target in _normalized_paths(declared)
        if target in known or target.endswith(known) or basename_of(target) in bases
    }
    return tuple(sorted(found))


def _high_risk_targets(declared: Sequence[str]) -> tuple[str, ...]:
    """Rutas del plan que pertenecen a una familia de **mayor riesgo**, ordenadas.

    Ni la autenticación ni la seguridad ni la frontera de política son rutas protegidas de la
    configuración, pero una reparación que las escribe cambia la superficie de confianza del
    producto: se elevan a :data:`_HIGH_RISK_TARGET_RISK` para que no se autoricen en autonomía.
    """
    found = {
        target
        for target in _normalized_paths(declared)
        if target.startswith(_HIGH_RISK_PATH_PREFIXES)
        or bool(_path_tokens(target) & _HIGH_RISK_PATH_TOKENS)
    }
    return tuple(sorted(found))


def _path_tokens(path: str) -> frozenset[str]:
    """Piezas alfanuméricas de una ruta normalizada, para comparar familias de riesgo."""
    return frozenset(part for part in re.split(r"[^a-z0-9]+", normalize_path(path)) if part)


def _repair_risk_floor(
    declared: RiskLevel, *, protected: Sequence[str], high_risk: Sequence[str]
) -> RiskLevel:
    """Riesgo declarado de la reparación, elevado por lo que el plan va a tocar.

    El riesgo nunca baja: se parte del que declara el plan y solo se sube. Un archivo protegido pesa
    más que uno de familia sensible, de modo que el orden de los pisos es
    ``HIGH`` < ``CRITICAL``.
    """
    floor = declared
    if high_risk:
        floor = max(floor, _HIGH_RISK_TARGET_RISK)
    if protected:
        floor = max(floor, _PROTECTED_TARGET_RISK)
    return floor


def _repair_is_reversible(run: WorkflowRun, *, declared: frozenset[str]) -> bool:
    """True si el snapshot del ciclo cubre la mutación que el plan autoriza.

    Tres casos, y solo el primero permite reparar en autonomía:

    1. **Sin snapshot registrado**: la cobertura la garantiza el contrato del ciclo —el kernel
       captura exactamente ``plan.target_files`` antes de mutar y bloquea el ciclo si no puede
       (``WORKFLOW_REPAIR_SNAPSHOT_INVALID``)—, así que la mutación es reversible siempre que el
       plan declare archivos que capturar.
    2. **Con snapshot registrado**: tiene que cubrir **todos** los objetivos. Si deja alguno fuera,
       esa parte de la mutación no tiene estado previo con el que deshacerse y la reparación no es
       reversible.
    3. **Sin archivos declarados**: no hay nada que un snapshot pueda cubrir. Una reparación sin
       alcance declarado no es acotada ni reversible, y por tanto no se autoriza sola.
    """
    if not declared:
        return False
    snapshot = run.active_repair_snapshot
    if snapshot is None:
        return True
    covered = {normalize_path(entry.path) for entry in snapshot.entries if entry.path.strip()}
    return declared <= covered


def _repair_description(run: WorkflowRun, plan: RepairPlan) -> str:
    """Descripción acotada de la reparación para el motivo auditable del motor.

    Solo entra dato **estructurado**: los defectos que el plan declara reparar (código o categoría y
    su resumen) y los cambios esperados del plan. Ni el objetivo del workflow ni el diagnóstico ni
    razonamiento privado de un modelo: el motor tiene que poder auditar qué se autorizó sin leer
    prosa que nadie puede verificar. El texto se recorta a la cota del contrato para que un llamante
    verboso no haga crecer el checkpoint.
    """
    by_id = {finding.finding_id: finding for finding in run.repair_findings}
    details: list[str] = []
    for finding_id in plan.finding_ids:
        finding = by_id.get(finding_id)
        if finding is None:
            continue
        label = finding.code or finding.category or "defecto"
        details.append(f"{label}: {finding.summary}" if finding.summary else label)
    parts = [f"reparación del ciclo {plan.cycle}"]
    if details:
        parts.append("defectos: " + "; ".join(details))
    if plan.expected_changes:
        parts.append("cambios esperados: " + "; ".join(plan.expected_changes))
    return " | ".join(parts)[:MAX_WORKFLOW_SUMMARY_CHARS]


def _default_deny_decision(action: str) -> PolicyDecision:
    """Decisión de *default deny* para una acción ausente de la tabla de impacto.

    Reproduce el veredicto de la rama 1 de ``PolicyEngine.evaluate`` (acción no catalogada) para
    que el kernel vea **una** semántica de *default deny*, sin consultar al motor: no hay impacto
    que declarar, así que no hay :class:`ActionRequest` que enviar. La decisión no entra en el
    historial del motor (el motor no ha evaluado nada) y, al ser un ``REJECT``, no habilita ningún
    Human Gate.
    """
    return PolicyDecision(
        allowed=False,
        authority_level=AuthorityLevel.LEVEL_3_HUMAN,
        requires_review=False,
        requires_human=True,
        reason=(
            f"DEFAULT DENY: la acción '{action}' no está catalogada. "
            "No se concede autoridad autónoma a acciones desconocidas."
        ),
        outcome=PolicyOutcome.REJECT,
        effective_risk=RiskLevel.CRITICAL,
        action=action,
        reasons=(f"acción desconocida: {action}",),
    )


def _invalid_proof(detail: str) -> WorkflowError:
    """Error estable de autorización de reanudación inválida."""
    return WorkflowError(
        WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID,
        f"autorización de reanudación rechazada: {detail}",
    )


def _reason_for(reason: str, *, role: RoleName, stage: TaskStatus) -> str:
    """Motivo del motor con la traza de quién lo pidió y en qué etapa.

    El veredicto del motor se conserva **íntegro** al principio del texto: el sufijo solo añade el
    contexto del kernel y nunca lo sustituye.
    """
    return f"{reason} (rol {role.value}, etapa {stage.value})"


def _resume_status_for(gate_request: HumanGateRequest) -> TaskStatus:
    """Estado de reanudación declarado por el gate, sin sustituirlo por otro.

    Antes se caía a ``IN_PROGRESS`` cuando el estado declarado no era reanudable, y esa
    sustitución era el defecto V602-01: el gate acababa autorizando un destino distinto del que el
    workflow iba a aplicar. Ahora la función **exige** coherencia: el estado declarado tiene que ser
    un destino de reanudación autorizado y tiene que coincidir con el destino propuesto. Si no
    encaja, no se registra ninguna aprobación, porque una autorización que no describe la
    reanudación real no autoriza nada.

    Raises:
        WorkflowApprovalProofInvalidError: si el estado declarado no es reanudable o no coincide
            con ``proposed_next_state``.
    """
    declared = gate_request.human_gate_resume_status
    proposed = gate_request.proposed_next_state
    if declared != proposed:
        raise WorkflowApprovalProofInvalidError(
            f"el Human Gate declara reanudar a {declared.value} y propone {proposed.value}: el "
            "destino autorizado y el destino aplicado tienen que ser el mismo estado"
        )
    if declared not in RESUMABLE_STATUSES:
        raise WorkflowApprovalProofInvalidError(
            f"{declared.value} no es un destino de reanudación autorizado: una aprobación hacia "
            "ese estado no reanudaría el workflow"
        )
    return declared


def _swing_note(swing: object | None) -> str:
    """Resumen auditable del cambio de estado que motiva el gate, si el descriptor lo declara."""
    if swing is None:
        return ""
    origin = _state_of(swing, "from_status", "current_state")
    target = _state_of(swing, "to_status", "proposed_next_state")
    if origin is None or target is None:
        return ""
    return f"cambio de estado {origin.value} -> {target.value}"


def _state_of(swing: object, *attributes: str) -> TaskStatus | None:
    """Primer atributo del descriptor que sea un estado del workflow, o ``None``."""
    for attribute in attributes:
        value = getattr(swing, attribute, None)
        if isinstance(value, TaskStatus):
            return value
        if isinstance(value, str):
            try:
                return TaskStatus(value)
            except ValueError:
                continue
    return None


__all__ = [
    "REPAIR_ACTION_PREFERENCES",
    "ActionImpact",
    "PolicyGate",
    "WorkflowPolicy",
    "action_impact",
    "known_actions",
]
