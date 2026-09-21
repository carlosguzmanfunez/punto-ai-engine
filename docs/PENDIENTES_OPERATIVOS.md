# Pendientes operativos

## Abiertos

_Ninguno de los dos anteriores sigue abierto (ver «Cerrados»)._

## Cerrados

1. **Dashboard: botón «Ejecutar de nuevo»** — cerrado. Llama a `POST /console/tasks/{id}/run` sobre la
   misma Task, se deshabilita mientras corre y representa el rechazo concurrente. Solo se ofrece si el
   motor dice que la Task se puede volver a ejecutar (`rerun.allowed`).
2. **`/run` concurrente sobre la misma Task** — cerrado. Interlock atómico en `ConsoleTask`
   (`begin_execution` / `end_execution`), solo en memoria, liberado al cerrar el intento y en `finally`;
   un segundo `/run` recibe 409 sin crear intento, sin incrementar `runs` y sin afectar a la ejecución
   viva. Tasks distintas no se estorban. Pruebas: `tests/test_console_rerun_interlock.py`.

## Contexto — Task `2e7822a0-5d67-405e-aeb6-3c07a139cbbf`

- Intento 7 aplicó cambios y quedó bloqueado (`EVIDENCE_REQUIRED`) antes del commit.
- Intento 8 los encontró ya presentes (`CHANGE_WITHOUT_EFFECT` / `CHANGES_EMPTY`); el BUILDER lo ejecutó
  Claude por failover (DeepSeek `CREDITS_EXHAUSTED`).
- Los cambios se consolidaron en un commit local del destino (`64c8bc3`, trailer `Task-Id`) y el
  `baseline_sha` local del destino se re-ancló a ese commit. El estado durable de la Task no se tocó:
  el motor no tiene un mecanismo para adjuntar un commit externo a una Task; el vínculo queda en el
  trailer del commit.
- Sin push, deploy ni producción. El siguiente intento (`/run`) está pendiente de decisión.
