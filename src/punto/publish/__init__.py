"""Publicación gobernada a producción (dashboards/human console, TASK → PRODUCTION).

Este paquete publica **a producción**: no tiene nada que ver con ``punto.workflow.handoff`` (que
"publica" artefactos en el almacén del flujo). Aquí se ejecuta la única operación que puede cambiar
lo que un usuario final ve: integrar el commit ya validado en la rama de producción y comprobar que
el despliegue correcto está realmente disponible.

Frontera de autoridad, explícita porque es lo que este paquete demuestra:

- **no concede autoridad**: la operación ``deploy_production`` está catalogada en nivel 3 y en
  ``never_autonomous``; la publicación exige una ``HumanApprovalRequest``
  **aprobada** y lo comprueba
  con ``HumanGate.assert_executable``, el mismo punto único de parada que usa el resto del motor;
- **no adivina el destino**: si el destino no declara rama y URL de producción, no se publica;
- **no empuja a ciegas**: el ``argv`` se construye con una allowlist (un remoto,
  un sha, una rama; sin
  ``--force``, sin tags, sin espejo, sin borrados) y el remoto no local exige una autorización
  explícita del operador;
- **no declara éxito por el commit ni por el push**: ``PRODUCTION_VALIDATED``
  solo se alcanza cuando la
  comprobación de producción responde con lo que se esperaba.
"""

from punto.publish.production import (
    GitPublisher,
    ProductionEvidence,
    ProductionProbe,
    PublicationRecord,
    PublicationRefused,
    PublicationService,
    PublicationStage,
    PushEvidence,
    PushPlan,
    default_fetch,
)

__all__ = [
    "GitPublisher",
    "ProductionEvidence",
    "ProductionProbe",
    "PublicationRecord",
    "PublicationRefused",
    "PublicationService",
    "PublicationStage",
    "PushEvidence",
    "PushPlan",
    "default_fetch",
]
