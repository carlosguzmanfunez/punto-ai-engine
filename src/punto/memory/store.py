"""Memoria práctica de experiencia, en JSONL local y determinista (PELL-0).

El almacén es un fichero de líneas JSON: sobrevive reinicios, se puede leer y auditar a mano y no
necesita ninguna infraestructura. Su API es deliberadamente pequeña:

- ``record`` / ``add`` — guardar conocimiento (con consolidación);
- ``get`` / ``list`` — leer;
- ``update_status`` / ``verify`` / ``mark_failed`` — mover el estado con evidencia;
- ``search`` — recuperar conocimiento relevante antes de resolver un problema nuevo.

La memoria **aconseja**, nunca ejecuta: no concede capabilities, no salta el Human Gate, no toca
ningún ``ResourceSet`` y no cambia políticas. La autoridad es del motor.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from punto.memory.experience import (
    EXPERIENCE_SCHEMA_VERSION,
    ExperienceMemory,
    ExperienceResult,
    ExperienceSchemaError,
    ExperienceStatus,
    normalize,
    problem_fingerprint,
    tokens,
)

#: Orden de presentación: el conocimiento demostrado primero y el superado al final.
_STATUS_RANK: Final[dict[ExperienceStatus, int]] = {
    ExperienceStatus.VERIFIED: 0,
    ExperienceStatus.CANDIDATE: 1,
    ExperienceStatus.FAILED: 2,
    ExperienceStatus.SUPERSEDED: 3,
}

#: Peso de cada coincidencia en la puntuación (determinista y sin ranking sofisticado).
_TAG_WEIGHT: Final[int] = 2
_TEXT_WEIGHT: Final[int] = 1

#: Puntuación mínima para considerar relevante una experiencia.
MIN_SCORE: Final[int] = 1


def default_memory_path() -> Path:
    """Ruta por defecto de la memoria local (configurable con ``PUNTO_PELL_PATH``)."""
    configured = os.environ.get("PUNTO_PELL_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path.cwd() / ".punto-memory" / "experiences.jsonl"


def _parse_line(line: str, source: Path) -> ExperienceMemory:
    """Interpreta una línea del almacén con el esquema vigente.

    Raises:
        ExperienceSchemaError: si la línea no es JSON válido, no encaja con el esquema o declara una
            versión distinta. Un registro ilegible **no** se ignora en silencio.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ExperienceSchemaError(f"{source}: línea ilegible en la memoria: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExperienceSchemaError(f"{source}: una experiencia tiene que ser un objeto JSON")
    version = payload.get("schema_version")
    if version != EXPERIENCE_SCHEMA_VERSION:
        raise ExperienceSchemaError(
            f"{source}: esquema de experiencia desconocido: {version!r} "
            f"(vigente: {EXPERIENCE_SCHEMA_VERSION})"
        )
    try:
        return ExperienceMemory.model_validate(payload)
    except ExperienceSchemaError:
        raise
    except Exception as exc:
        raise ExperienceSchemaError(f"{source}: experiencia inválida: {exc}") from exc


def _score(record: ExperienceMemory, query: set[str]) -> int:
    """Puntuación determinista de una experiencia contra los tokens de la consulta."""
    if not query:
        return 0
    score = 0
    tag_tokens = {token for tag in record.tags for token in tokens(tag)}
    score += _TAG_WEIGHT * len(tag_tokens & query)
    score += _TEXT_WEIGHT * len(set(tokens(record.problem)) & query)
    score += _TEXT_WEIGHT * len(set(tokens(record.context)) & query)
    score += _TEXT_WEIGHT * len(set(tokens(record.solution)) & query)
    for step in record.procedure:
        score += _TEXT_WEIGHT * len(set(tokens(step)) & query)
    return score


class ExperienceStore:
    """Almacén JSONL de experiencias, con consolidación y búsqueda determinista."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_memory_path()

    @property
    def path(self) -> Path:
        """Fichero donde vive la memoria."""
        return self._path

    # ------------------------------------------------------------------ lectura
    def list(self, *, status: ExperienceStatus | None = None) -> tuple[ExperienceMemory, ...]:
        """Experiencias guardadas, en orden de creación, filtradas por estado si se pide."""
        if not self._path.is_file():
            return ()
        records: list[ExperienceMemory] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(_parse_line(line, self._path))
        if status is not None:
            records = [record for record in records if record.status is status]
        return tuple(records)

    def get(self, experience_id: str) -> ExperienceMemory | None:
        """Experiencia por identidad, o ``None`` si no está."""
        for record in self.list():
            if record.id == experience_id:
                return record
        return None

    # ------------------------------------------------------------------ escritura
    def _write_all(self, records: Sequence[ExperienceMemory]) -> None:
        """Reescribe el almacén de forma atómica (temporal + ``os.replace``)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(f"{record.as_json_line()}\n" for record in records)
        handle, temporary = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _append(self, record: ExperienceMemory) -> None:
        """Añade una experiencia al final del almacén."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{record.as_json_line()}\n")

    def add(self, experience: ExperienceMemory) -> ExperienceMemory:
        """Guarda una experiencia consolidando lo que ya se sabía.

        La consolidación es determinista: misma huella de problema **y** mismo estado ⇒ se fusionan
        (evidencia nueva incluida) en el registro existente; mismo problema con estado distinto ⇒ se
        conservan **ambas** experiencias, porque «esto falló» y «esto funcionó» son dos hechos
        distintos y los dos sirven para no repetir trabajo.

        Returns:
            La experiencia almacenada: la nueva, o la existente fusionada.
        """
        key = (experience.fingerprint, experience.status)
        for index, existing in enumerate(self.list()):
            if (existing.fingerprint, existing.status) != key:
                continue
            merged = _merge(existing, experience)
            records = list(self.list())
            records[index] = merged
            self._write_all(records)
            return merged
        self._append(experience)
        return experience

    def record(
        self,
        *,
        problem: str,
        context: str = "",
        attempts: Iterable[str] = (),
        failure_reason: str = "",
        solution: str = "",
        procedure: Iterable[str] = (),
        result: ExperienceResult = ExperienceResult.SUCCESS,
        verification: Iterable[str] = (),
        tags: Iterable[str] = (),
        status: ExperienceStatus = ExperienceStatus.CANDIDATE,
    ) -> ExperienceMemory:
        """Registra conocimiento nuevo y lo devuelve ya consolidado."""
        return self.add(
            ExperienceMemory(
                problem=problem,
                context=context,
                attempts=tuple(attempts),
                failure_reason=failure_reason,
                solution=solution,
                procedure=tuple(procedure),
                result=result,
                verification=tuple(verification),
                tags=tuple(tags),
                status=status,
            )
        )

    def update_status(
        self,
        experience_id: str,
        status: ExperienceStatus,
        *,
        verification: Iterable[str] = (),
        failure_reason: str = "",
    ) -> ExperienceMemory:
        """Cambia el estado de una experiencia, conservando la evidencia nueva.

        Raises:
            ExperienceSchemaError: si la experiencia no existe o el cambio no cumple las reglas
                (``VERIFIED`` sin evidencia, ``FAILED`` sin causa).
        """
        records = list(self.list())
        for index, record in enumerate(records):
            if record.id != experience_id:
                continue
            updated = ExperienceMemory.model_validate(
                {
                    **record.model_dump(),
                    "status": status,
                    "verification": (*record.verification, *tuple(verification)),
                    "failure_reason": failure_reason or record.failure_reason,
                    "result": (
                        ExperienceResult.FAILED
                        if status is ExperienceStatus.FAILED
                        else record.result
                    ),
                }
            )
            records[index] = updated
            self._write_all(records)
            return updated
        raise ExperienceSchemaError(f"no existe la experiencia {experience_id!r} en la memoria")

    def verify(self, experience_id: str, *, evidence: Sequence[str]) -> ExperienceMemory:
        """Marca una experiencia como ``VERIFIED`` con la evidencia que la demuestra."""
        if not tuple(evidence):
            raise ExperienceSchemaError(
                "no se puede verificar sin evidencia: tests, E2E, auditoría o resultado verificable"
            )
        return self.update_status(
            experience_id, ExperienceStatus.VERIFIED, verification=evidence
        )

    def mark_failed(self, experience_id: str, *, reason: str) -> ExperienceMemory:
        """Marca una experiencia como ``FAILED`` conservando el motivo del fracaso."""
        if not reason.strip():
            raise ExperienceSchemaError("una experiencia FAILED exige el motivo por el que falló")
        return self.update_status(
            experience_id, ExperienceStatus.FAILED, failure_reason=reason
        )

    # ------------------------------------------------------------------ búsqueda
    def search(
        self,
        problem: str,
        *,
        context: str | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 5,
        statuses: Sequence[ExperienceStatus] | None = None,
    ) -> tuple[ExperienceMemory, ...]:
        """Recupera experiencias relevantes para un problema nuevo.

        La puntuación es determinista: etiquetas (peso 2) y palabras del problema, del contexto,
        de la solución y del procedimiento (peso 1). Se devuelven primero las ``VERIFIED``
        —conocimiento demostrado— y después los ``FAILED`` relevantes, que recuerdan qué caminos no
        repetir. Las ``SUPERSEDED`` van al final.

        Args:
            problem: problema nuevo que se va a resolver.
            context: contexto donde aplica, si se conoce.
            tags: etiquetas que se esperan de la experiencia.
            limit: número máximo de experiencias devueltas.
            statuses: filtra por estado si se quiere algo concreto (p. ej. solo ``VERIFIED``).

        Returns:
            Las experiencias ordenadas por estado, puntuación y antigüedad.
        """
        query = set(tokens(problem))
        query |= set(tokens(context or ""))
        for tag in tags or ():
            query |= set(tokens(tag))
        allowed = set(statuses) if statuses is not None else None
        scored: list[tuple[int, int, float, str, ExperienceMemory]] = []
        for record in self.list():
            if allowed is not None and record.status not in allowed:
                continue
            score = _score(record, query)
            if score < MIN_SCORE:
                continue
            created = record.created_at.timestamp()
            scored.append((_STATUS_RANK[record.status], -score, -created, record.id, record))
        scored.sort()
        return tuple(item[-1] for item in scored[: max(0, limit)])


def _merge(existing: ExperienceMemory, new: ExperienceMemory) -> ExperienceMemory:
    """Fusiona dos experiencias con la misma huella y el mismo estado, conservando la evidencia."""
    return ExperienceMemory.model_validate(
        {
            **existing.model_dump(),
            "problem": existing.problem or new.problem,
            "context": existing.context or new.context,
            "attempts": _union(existing.attempts, new.attempts),
            "procedure": _union(existing.procedure, new.procedure),
            "verification": _union(existing.verification, new.verification),
            "tags": _union(existing.tags, new.tags),
            "failure_reason": existing.failure_reason or new.failure_reason,
            "solution": existing.solution or new.solution,
            "created_at": _earliest(existing.created_at, new.created_at),
        }
    )


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    """Unión estable de dos listas de textos, sin repetir."""
    merged: list[str] = []
    for item in (*first, *second):
        if item not in merged:
            merged.append(item)
    return tuple(merged)


def _earliest(first: datetime, second: datetime) -> datetime:
    """La fecha más antigua de las dos: la experiencia existe desde que se supo."""
    return first if first <= second else second


def fingerprint_of(problem: str) -> str:
    """Huella de un problema, expuesta para informes y pruebas."""
    return problem_fingerprint(problem)


def normalized(text: str) -> str:
    """Texto normalizado, expuesto para informes y pruebas."""
    return normalize(text)
