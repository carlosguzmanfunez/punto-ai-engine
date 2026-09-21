# Pendientes operativos

## Abiertos

1. **Dashboard: botón «Ejecutar de nuevo».** Añadir al Dashboard el llamador de
   `POST /console/tasks/{id}/run` para reanudar una Task durable bloqueada o fallida como nuevo intento
   (misma Task, `runs` +1, historial intacto). Hoy el endpoint existe pero solo se invoca a mano.
2. **Impedir `/run` concurrente sobre la misma Task.** `run_console_task` solo rechaza las Tasks
   `REJECTED`: un segundo `POST` mientras el ciclo corre lanzaría un segundo ciclo sobre el mismo
   repositorio. Propuesta: 409 si hay un intento vivo en este proceso (`attempt_started_at` abierto, o
   etapa `DEVELOPING`/`PUBLISHING` en una Task no recuperada); una Task recuperada de un reinicio
   (sin nada corriendo) sigue siendo reanudable.

## Contexto — Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`

- Intento 7 aplicó cambios y quedó bloqueado (`EVIDENCE_REQUIRED`) antes del commit.
- Intento 8 los encontró ya presentes (`CHANGE_WITHOUT_EFFECT` / `CHANGES_EMPTY`); el BUILDER lo ejecutó
  Claude por failover (DeepSeek `CREDITS_EXHAUSTED`).
- Los cambios se consolidaron en un commit local del destino (`64c8bc3`, trailer `Task-Id`) y el
  `baseline_sha` local del destino se re-ancló a ese commit. El estado durable de la Task no se tocó:
  el motor no tiene un mecanismo para adjuntar un commit externo a una Task; el vínculo queda en el
  trailer del commit.
- Sin push, deploy ni producción. El siguiente intento (`/run`) está pendiente de decisión.
