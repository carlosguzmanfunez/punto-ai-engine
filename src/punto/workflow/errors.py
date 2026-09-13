"""Errores del kernel de workflow (ENGINE-6.0).

Porqué este módulo existe y porqué es una taxonomía y no un puñado de ``RuntimeError``: el
workflow lo consumen CAMUS, la auditoría y el Human Gate, y los tres necesitan reaccionar de
forma distinta al mismo hecho sin leer mensajes de texto. Por eso cada error lleva un
:class:`WorkflowFailureCode` estable, y ese código es la API pública: el kernel **no** expone
excepciones genéricas ni códigos inventados en el lugar del fallo.

Dos decisiones concretas:

- **El mensaje empieza por el código.** Un log truncado, una línea de auditoría recortada o
  una consola estrecha siguen siendo útiles si lo primero que se lee es
  ``WORKFLOW_BUDGET_EXCEEDED``. El detalle viene después, en lenguaje natural, para que un
  humano entienda qué pasó sin consultar la tabla.
- **Una clase por código relevante, con el código ya fijado.** Quien captura
  ``WorkflowBudgetExceededError`` sabe qué capturó sin comparar cadenas, y quien lo lanza no
  puede equivocarse de código: la subclase solo acepta el detalle.

El módulo es hoja respecto de ``punto.workflow``: solo depende de los esquemas congelados, de
modo que cualquiera de los otros módulos del kernel puede importarlo sin arriesgar un ciclo.
"""

from __future__ import annotations

from punto.schemas.workflow import WorkflowFailureCode


class WorkflowError(RuntimeError):
    """Error del kernel con código estable. Es la API pública de errores.

    Args:
        code: Código estable del fallo, tomado de :class:`WorkflowFailureCode`.
        detail: Explicación legible. No debe contener credenciales, volcados de código ni
            cadenas de razonamiento: este texto acaba en auditoría y en el Human Gate.
    """

    code: WorkflowFailureCode
    detail: str

    def __init__(self, code: WorkflowFailureCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


class WorkflowInvalidTransitionError(WorkflowError):
    """La tabla del workflow no permite el paso solicitado.

    Porqué es un error y no un ajuste silencioso: la tabla de transiciones **es** el contrato
    del ciclo de vida. «Corregir» la transición para que encaje convertiría el estado del
    workflow en una sugerencia y dejaría la traza de auditoría sin valor probatorio.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_INVALID_TRANSITION, detail)


class WorkflowTerminalError(WorkflowError):
    """Se intentó avanzar un workflow que ya está en un estado terminal.
    Reanudar un workflow terminado no es continuarlo: es otro workflow. Se falla en vez de
    reabrir el anterior para que un ``COMPLETED`` o un ``FAILED`` no puedan cambiar de
    significado después de haberse reportado.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_TERMINAL, detail)


class WorkflowBudgetExceededError(WorkflowError):
    """Se superó un límite del presupuesto declarado.

    El presupuesto lo calcula PUNTO a partir del consumo real, nunca el modelo. Un límite
    excedido detiene el avance: no se negocia ni se amplía en caliente.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_BUDGET_EXCEEDED, detail)


class WorkflowLoopDetectedError(WorkflowError):
    """El workflow volvería a entrar en un estado más veces de las permitidas.

    Es la protección contra bucles y la decide el contador de visitas de PUNTO, no una
    impresión del modelo de que «ya está casi». Ningún modelo puede autorizar una visita más.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_LOOP_DETECTED, detail)


class WorkflowProviderUnavailableError(WorkflowError):
    """El proveedor que necesita un rol no está disponible o no tiene credencial.

    Nunca se traduce en «usar otro proveedor»: aquí no hay fallback. Si el rol pedía
    DeepSeek y DeepSeek no está, el rol no se ejecuta —o queda pendiente de credenciales— y
    se dice explícitamente, en vez de responder con otro proveedor como si nada.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_PROVIDER_UNAVAILABLE, detail)


class WorkflowRoleFailedError(WorkflowError):
    """La ejecución de un rol terminó en fallo y el workflow no puede seguir con él.

    Distinguirlo de un fallo del kernel importa: aquí el workflow está sano y lo que falló
    fue un rol concreto, con su ``role`` y su ``step_index`` en la traza.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_ROLE_FAILED, detail)


class WorkflowHumanApprovalRequiredError(WorkflowError):
    """El workflow requiere aprobación humana antes de continuar.

    No es un fallo del motor: es una pausa deliberada y auditable. Se representa como error
    de flujo para que ningún camino de código pueda «seguir un poco más» y saltarse el
    Human Gate por descuido.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_HUMAN_APPROVAL_REQUIRED, detail)


class WorkflowCheckpointInvalidError(WorkflowError):
    """Un checkpoint no se puede leer o no supera su comprobación de integridad.

    Un checkpoint corrupto no se repara adivinando: se declara inválido. Reconstruir el
    estado a partir de contenido dudoso sería reanudar un workflow que quizá nunca existió.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_CHECKPOINT_INVALID, detail)


class WorkflowResumeFailedError(WorkflowError):
    """La reanudación no pudo continuar desde el estado persistido.

    Cubre lo que el checkpoint es válido pero el mundo no: revisión que no encaja, estado
    terminal, o una reanudación sin la autorización que la hacía legítima.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_RESUME_FAILED, detail)


class WorkflowIncompleteEvidenceError(WorkflowError):
    """Falta evidencia exigida para cerrar el workflow.

    Aprobar sin la evidencia declarada convertiría la verificación en una promesa. Si el
    perfil exige QA, seguridad o auditoría cruzada, su resultado tiene que estar.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_INCOMPLETE_EVIDENCE, detail)


class WorkflowRepairDeferredError(WorkflowError):
    """El workflow llegó a ``REPAIRING`` y se detiene ahí, por diseño de esta fase.

    El ciclo de reparación completo es ENGINE-6.1. Tiene código propio para no disfrazar la
    pausa: quien lea la traza debe ver «diferido», no «falló» ni «completó».
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_REPAIR_DEFERRED, detail)


class WorkflowApprovalProofInvalidError(WorkflowError):
    """La autorización presentada para salir de un Human Gate no sirve.

    Cubre la proof de otra tarea, la de otra decisión de política, la de otro gate, la ya
    consumida y la fabricada. Un booleano no demuestra que un humano aprobara nada: solo la
    prueba emitida por ``HumanGate.authorize_resume`` lo hace, y solo para el gate que la emitió.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_APPROVAL_PROOF_INVALID, detail)


class WorkflowIdempotencyConflictError(WorkflowError):
    """La misma clave de idempotencia llegó con contenido distinto.

    No es una repetición: es otra petición disfrazada. Devolver el workflow anterior daría por
    hecho un trabajo que nadie pidió.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_IDEMPOTENCY_CONFLICT, detail)


class WorkflowPolicyRejectedError(WorkflowError):
    """El Policy Engine rechazó la acción de forma dura (default deny o recurso protegido).

    Es un fallo de política, no del producto: no se crea workflow y no se ejecuta nada.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_POLICY_REJECTED, detail)


class WorkflowEffectReconciliationError(WorkflowError):
    """Hay un efecto en vuelo cuyo resultado se desconoce.

    No se repite a ciegas: se bloquea para reconciliar. Repetir un efecto irreversible porque no
    sabemos si ocurrió es exactamente el daño que esta frontera existe para evitar.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(WorkflowFailureCode.WORKFLOW_EFFECT_RECONCILIATION_REQUIRED, detail)


__all__ = [
    "WorkflowApprovalProofInvalidError",
    "WorkflowBudgetExceededError",
    "WorkflowCheckpointInvalidError",
    "WorkflowEffectReconciliationError",
    "WorkflowError",
    "WorkflowHumanApprovalRequiredError",
    "WorkflowIdempotencyConflictError",
    "WorkflowIncompleteEvidenceError",
    "WorkflowInvalidTransitionError",
    "WorkflowLoopDetectedError",
    "WorkflowPolicyRejectedError",
    "WorkflowProviderUnavailableError",
    "WorkflowRepairDeferredError",
    "WorkflowResumeFailedError",
    "WorkflowRoleFailedError",
    "WorkflowTerminalError",
]
