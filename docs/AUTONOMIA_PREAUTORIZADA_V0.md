# Autonomía preautorizada V0

Autorización **persistente del propietario**, general para todos los destinos registrados
(`config/autonomy.yaml` + `punto.policy.autonomy`). Un riesgo `HIGH` por sí solo **no** abre un
Human Gate ni concede nada: decide la **operación + recurso + alcance + reversibilidad + autoridad
configurada**.

## Qué es autónomo (dentro del alcance del destino registrado y reversible)

Planificar; editar código; crear, modificar y borrar ficheros del proyecto (el borrado, si Git puede
restaurarlo); refactors; tests, lint, typecheck, build; QA de navegador/visual; commits locales y
ramas de trabajo; reintentos y reparación automática; migraciones locales no destructivas ya
gobernadas; uso y failover de los proveedores configurados.

## Qué sigue exigiendo persona (fronteras de autoridad)

Secretos nuevos o su exposición · pagos y coste fuera del presupuesto · borrado irreversible de datos
de producción · migraciones destructivas · infraestructura fuera del sobre · destino/repositorio/
cuenta no autorizado · ampliar la propia autoridad de PUNTO (incluido `config/autonomy.yaml`) ·
reescribir historial remoto · actos legales o contractuales · evidencia factual que sigue sin
resolverse · exceder el sobre (techo de ficheros, coste, tiempo) · recursos de identidad/autenticación
(configurable) · recursos de clase desconocida.

**Push, deploy y producción** no se conceden aquí: requieren además que el destino declare un sobre
explícito para esa clase (`targets.local.yaml → authority`).

## Cómo se decide

| Capa | Uso |
|---|---|
| `AutonomyPolicy.evaluate(query, context)` | única fuente de la decisión; devuelve **todas** las fronteras cruzadas |
| `PolicyEngine.evaluate(..., PolicyEvaluationContext(autonomy=...))` | acciones del catálogo; solo levanta `HIGH` (nunca `CRITICAL` ni nivel 3) |
| `AdaptiveAuthorityEnvelope` | operaciones del ciclo; `git-reversible-delete` |
| `GovernedRepository._autonomy_context` | hechos comprobados: destino registrado, alcance, recuperable por Git |

## Anti-autoelevación

- Piso en código (`FLOOR_GATED_CLASSES`, `FLOOR_NEVER_OPERATIONS`, `RELEASE_OPERATIONS`): el YAML
  solo puede **sumar** fronteras.
- El contexto lo fija código de PUNTO; `ActionRequest` no admite campos de autoridad.
- Sin contexto o sin `autonomy.yaml`: comportamiento previo (fail-closed).
- `config/autonomy.yaml` está en `protected_files` (constitución, permisos y piso en código).
- Los gates históricos se conservan como auditoría; esta política no los reabre ni los resuelve.

Pruebas: `tests/test_preauthorized_autonomy.py`.
