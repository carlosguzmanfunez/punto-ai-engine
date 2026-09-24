"""Ledger durable de leases con CAS exclusivo, epochs y fencing.

El ledger es la fuente de verdad. La auditoría solo observa sus resultados y un PID jamás decide
si un holder sigue vivo. Cada transición confirmada ocupa un JSON nuevo; publicar el nombre final
se hace con ``os.link`` exclusivo, no con overwrite/``os.replace``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, NoReturn, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from punto.audit.logger import AuditLogger
from punto.schemas.audit import AuditEventType
from punto.schemas.enums import AuditResult
from punto.schemas.scheduling import ExecutorReference
from punto.workspace.repository import StaleWriteError

MAX_TTL_SECONDS: Final[int] = 3_600
SAFETY_MARGIN_SECONDS: Final[int] = 5
TAKEOVER_GRACE_SECONDS: Final[int] = 2
GENESIS_DIGEST: Final[str] = "0" * 64
_RECORD_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<seq>[0-9]{8})\.json$")
_SAFE_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


class LeaseKind(StrEnum):
    """Clases de exclusión de Fase 2A."""

    TASK_WRITER = "TASK_WRITER"
    PROVIDER = "PROVIDER"


class LeaseState(StrEnum):
    """Estados persistidos de un lease."""

    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class LeaseOutcome(StrEnum):
    """Resultados causales; BUSY no es un fallo de proveedor."""

    PASS = "PASS"
    BUSY = "BUSY"
    FENCED = "FENCED"
    STALE_RELEASE = "STALE_RELEASE"
    LEDGER_CORRUPT = "LEDGER_CORRUPT"


class LeaseLedgerCorruptError(RuntimeError):
    """El ledger no puede gobernar autoridad de forma segura."""

    code = LeaseOutcome.LEDGER_CORRUPT.value

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class LeaseFencedError(StaleWriteError):
    """El token ya no representa al holder/epoch que gobierna la escritura."""

    code = LeaseOutcome.FENCED.value


class LeaseHolder(BaseModel):
    """Identidad de proceso; host/PID son informativos, nunca liveness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor_id: UUID = Field(default_factory=uuid4)
    executor_ref: ExecutorReference
    host: str = Field(default_factory=socket.gethostname, min_length=1, max_length=255)
    pid: int = Field(default_factory=os.getpid, ge=0)

    @field_validator("host")
    @classmethod
    def _host_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("host no puede estar vacío")
        return normalized


class FencingToken(BaseModel):
    """Prueba portable de holder + epoch; no concede autoridad sin releer el ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LeaseKind
    key: str = Field(min_length=1, max_length=240)
    epoch: int = Field(ge=1)
    holder_executor_id: UUID
    task_id: UUID | None = None
    task_epoch: int | None = Field(default=None, ge=1)


class LeaseRecord(BaseModel):
    """Transición inmutable y auto-verificable del ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LeaseKind
    key: str = Field(min_length=1, max_length=240)
    seq: int = Field(ge=1)
    epoch: int = Field(ge=1)
    state: LeaseState
    holder: LeaseHolder
    acquired_at: datetime
    expires_at: datetime
    ttl_seconds: int = Field(ge=SAFETY_MARGIN_SECONDS, le=MAX_TTL_SECONDS)
    supersedes_epoch: int | None = Field(default=None, ge=1)
    prev_digest: str = Field(min_length=64, max_length=64)
    digest: str = Field(min_length=64, max_length=64)
    provider_id: str = Field(default="", max_length=120)
    slot: int | None = Field(default=None, ge=0)
    task_id: UUID | None = None
    task_epoch: int | None = Field(default=None, ge=1)

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("los tiempos del lease deben incluir zona horaria")
        return value.astimezone(UTC)

    @field_validator("prev_digest", "digest")
    @classmethod
    def _digest_is_hex(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("el digest debe ser sha256 hexadecimal")
        return normalized

    @field_validator("provider_id")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def _kind_fields_match(self) -> Self:
        expected_expiry = self.acquired_at + timedelta(seconds=self.ttl_seconds)
        if self.expires_at != expected_expiry:
            raise ValueError("expires_at no coincide con acquired_at + ttl_seconds")
        provider_fields = (
            bool(self.provider_id),
            self.slot is not None,
            self.task_id is not None,
            self.task_epoch is not None,
        )
        if self.kind is LeaseKind.PROVIDER and not all(provider_fields):
            raise ValueError("un ProviderLease exige provider_id, slot, task_id y task_epoch")
        if self.kind is LeaseKind.TASK_WRITER and any(provider_fields):
            raise ValueError("un TaskWriterLease no admite campos de ProviderLease")
        return self

    def canonical_bytes(self) -> bytes:
        """Bytes estables usados por el digest propio."""
        payload = self.model_dump(mode="json", exclude={"digest"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def expected_digest(self) -> str:
        """Digest calculado del contenido, sin confiar en el campo persistido."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def token(self) -> FencingToken:
        """Token de la autoridad representada por este record."""
        return FencingToken(
            kind=self.kind,
            key=self.key,
            epoch=self.epoch,
            holder_executor_id=self.holder.executor_id,
            task_id=self.task_id,
            task_epoch=self.task_epoch,
        )


class TaskWriterLease(LeaseRecord):
    """Record especializado de exclusión de escritura por Task."""

    kind: Literal[LeaseKind.TASK_WRITER] = LeaseKind.TASK_WRITER


class ProviderLease(LeaseRecord):
    """Record especializado de un slot de proveedor subordinado a una Task."""

    kind: Literal[LeaseKind.PROVIDER] = LeaseKind.PROVIDER


class LeaseResult(BaseModel):
    """Desenlace estructurado de una operación de lease."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: LeaseOutcome
    record: LeaseRecord | None = None
    token: FencingToken | None = None
    detail: str = Field(default="", max_length=500)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LeaseLedger:
    """Ledger local append-only con CAS por enlace exclusivo."""

    def __init__(
        self,
        root: Path,
        clock: Callable[[], datetime] = _utc_now,
        *,
        audit: AuditLogger | None = None,
        actor: str = "punto-lease-ledger",
    ) -> None:
        self._root = Path(root)
        self._clock = clock
        self._audit = audit
        self._actor = actor

    @property
    def root(self) -> Path:
        return self._root

    def acquire(
        self,
        *,
        kind: LeaseKind,
        key: str,
        holder: LeaseHolder,
        ttl_seconds: int,
        operation_timeout_seconds: int = 0,
        provider_id: str = "",
        slot: int | None = None,
        task_id: UUID | None = None,
        task_epoch: int | None = None,
        task_token: FencingToken | None = None,
    ) -> LeaseResult:
        """Adquiere una key o devuelve BUSY sin consumir intentos externos."""
        self._validate_ttl(ttl_seconds, operation_timeout_seconds)
        normalized_key = self._normalize_key(kind, key, provider_id=provider_id, slot=slot)
        if kind is LeaseKind.PROVIDER:
            provider_id = normalized_key.rsplit(":", maxsplit=1)[0]
            if slot != 0:
                raise ValueError("provider_concurrency=1: el único slot válido es 0")
            if task_id is None or task_epoch is None or task_token is None:
                raise ValueError("ProviderLease exige TaskWriterLease previo y su fencing token")
            try:
                self.assert_fenced(task_token)
            except LeaseFencedError:
                return LeaseResult(
                    outcome=LeaseOutcome.FENCED,
                    detail="el TaskWriterLease previo perdió autoridad",
                )
            if (
                task_token.kind is not LeaseKind.TASK_WRITER
                or task_token.key != str(task_id)
                or task_token.epoch != task_epoch
                or task_token.holder_executor_id != holder.executor_id
            ):
                return LeaseResult(
                    outcome=LeaseOutcome.FENCED,
                    detail="el ProviderLease no está subordinado al TaskWriterLease",
                )
            busy = self._provider_for_task(task_id, exclude_key=normalized_key)
            if busy is not None:
                return self._result(LeaseOutcome.BUSY, busy, "la Task ya posee un ProviderLease")

        directory = self._directory(kind, normalized_key, provider_id=provider_id, slot=slot)
        for attempt in range(2):
            records = self._read(directory)
            head = records[-1] if records else None
            now = self._now()
            if head is not None and head.state is LeaseState.ACTIVE:
                if self._is_expired(head, now):
                    expired = self._transition(head, LeaseState.EXPIRED, now)
                    if not self._append(directory, expired):
                        if attempt == 0:
                            continue
                        return self._result(LeaseOutcome.BUSY, self._head(directory), "CAS perdido")
                    self._audit_record(AuditEventType.LEASE_EXPIRED, expired)
                    records = (*records, expired)
                    head = expired
                elif head.holder.executor_id == holder.executor_id:
                    if kind is LeaseKind.PROVIDER and (
                        head.task_id != task_id or head.task_epoch != task_epoch
                    ):
                        return self._result(
                            LeaseOutcome.BUSY,
                            head,
                            "el holder ya usa este slot para otra Task",
                        )
                    return self._result(LeaseOutcome.PASS, head, "acquire idempotente")
                else:
                    self._audit_record(AuditEventType.LEASE_BUSY, head, result=AuditResult.DENIED)
                    return self._result(LeaseOutcome.BUSY, head, "lease vigente de otro holder")
            epoch = 1 if head is None else head.epoch + 1
            record = self._new_record(
                kind=kind,
                key=normalized_key,
                seq=1 if head is None else head.seq + 1,
                epoch=epoch,
                state=LeaseState.ACTIVE,
                holder=holder,
                now=now,
                ttl_seconds=ttl_seconds,
                supersedes_epoch=None if head is None else head.epoch,
                prev_digest=GENESIS_DIGEST if head is None else head.digest,
                provider_id=provider_id,
                slot=slot,
                task_id=task_id,
                task_epoch=task_epoch,
            )
            if self._append(directory, record):
                self._audit_record(AuditEventType.LEASE_ACQUIRED, record)
                return self._result(LeaseOutcome.PASS, record)
            if attempt == 0:
                continue
        return self._result(LeaseOutcome.BUSY, self._head(directory), "CAS perdido")

    def renew(
        self,
        token: FencingToken,
        *,
        ttl_seconds: int,
        operation_timeout_seconds: int = 0,
    ) -> LeaseResult:
        """Renueva solo el holder y epoch que siguen en la cabeza."""
        self._validate_ttl(ttl_seconds, operation_timeout_seconds)
        directory = self._directory_from_token(token)
        if token.kind is LeaseKind.PROVIDER:
            try:
                self._assert_parent_task(token)
            except LeaseFencedError:
                return self._fenced(self._head(directory), "el TaskWriterLease perdió autoridad")
        for attempt in range(2):
            head = self._head(directory)
            now = self._now()
            if head is None or not self._token_matches(head, token):
                return self._fenced(head, "renew con holder o epoch obsoleto")
            if head.state is not LeaseState.ACTIVE or self._is_expired(head, now):
                return self._fenced(head, "renew sobre lease no activo")
            record = self._new_record(
                kind=head.kind,
                key=head.key,
                seq=head.seq + 1,
                epoch=head.epoch,
                state=LeaseState.ACTIVE,
                holder=head.holder,
                now=now,
                ttl_seconds=ttl_seconds,
                supersedes_epoch=head.supersedes_epoch,
                prev_digest=head.digest,
                provider_id=head.provider_id,
                slot=head.slot,
                task_id=head.task_id,
                task_epoch=head.task_epoch,
            )
            if self._append(directory, record):
                self._audit_record(AuditEventType.LEASE_RENEWED, record)
                return self._result(LeaseOutcome.PASS, record)
            if attempt == 0:
                continue
        return self._fenced(self._head(directory), "renew perdió el CAS")

    def release(self, token: FencingToken) -> LeaseResult:
        """Libera el lease actual; repetir el release es no-op y uno viejo no muta nada."""
        directory = self._directory_from_token(token)
        for attempt in range(2):
            head = self._head(directory)
            if head is None or not self._token_matches(head, token):
                return self._result(LeaseOutcome.STALE_RELEASE, head, "release obsoleto")
            if head.state is LeaseState.RELEASED:
                return self._result(LeaseOutcome.PASS, head, "release idempotente")
            if head.state is not LeaseState.ACTIVE or self._is_expired(head, self._now()):
                return self._result(LeaseOutcome.STALE_RELEASE, head, "lease ya no está activo")
            record = self._transition(head, LeaseState.RELEASED, self._now())
            if self._append(directory, record):
                self._audit_record(AuditEventType.LEASE_RELEASED, record)
                return self._result(LeaseOutcome.PASS, record)
            if attempt == 0:
                continue
        return self._result(LeaseOutcome.STALE_RELEASE, self._head(directory), "release perdió CAS")

    def assert_fenced(self, token: FencingToken) -> LeaseRecord:
        """Relee la cabeza y exige ACTIVE + holder + epoch + TTL vigente."""
        head = self._head(self._directory_from_token(token))
        if (
            head is None
            or head.state is not LeaseState.ACTIVE
            or self._is_expired(head, self._now())
            or not self._token_matches(head, token)
        ):
            if head is not None:
                self._audit_record(AuditEventType.LEASE_FENCED, head, result=AuditResult.DENIED)
            raise LeaseFencedError(
                f"token {token.kind.value}/{token.key}@{token.epoch} perdió autoridad"
            )
        if token.kind is LeaseKind.PROVIDER:
            self._assert_parent_task(token)
        return head

    def _assert_parent_task(self, token: FencingToken) -> None:
        if token.task_id is None or token.task_epoch is None:
            raise LeaseFencedError("Provider token sin TaskWriter epoch")
        task_token = FencingToken(
            kind=LeaseKind.TASK_WRITER,
            key=str(token.task_id),
            epoch=token.task_epoch,
            holder_executor_id=token.holder_executor_id,
        )
        self.assert_fenced(task_token)

    def reconcile(self, holder: LeaseHolder) -> tuple[LeaseResult, ...]:
        """Expira por reloj durable y reporta leases ajenos vigentes como BUSY."""
        results: list[LeaseResult] = []
        for directory in self._ledger_directories():
            for attempt in range(2):
                head = self._head(directory)
                if head is None or head.state is not LeaseState.ACTIVE:
                    break
                now = self._now()
                if self._is_expired(head, now):
                    expired = self._transition(head, LeaseState.EXPIRED, now)
                    if self._append(directory, expired):
                        self._audit_record(AuditEventType.LEASE_EXPIRED, expired)
                        results.append(self._result(LeaseOutcome.PASS, expired))
                        break
                    if attempt == 0:
                        continue
                    results.append(
                        self._result(LeaseOutcome.BUSY, self._head(directory), "CAS perdido")
                    )
                    break
                if head.holder.executor_id != holder.executor_id:
                    self._audit_record(AuditEventType.LEASE_BUSY, head, result=AuditResult.DENIED)
                    results.append(self._result(LeaseOutcome.BUSY, head, "lease vigente ajeno"))
                else:
                    results.append(self._result(LeaseOutcome.PASS, head, "lease vigente propio"))
                break
        return tuple(results)

    def head(
        self, *, kind: LeaseKind, key: str, provider_id: str = "", slot: int | None = None
    ) -> LeaseRecord | None:
        """Cabeza validada para diagnóstico y tests; corrupción nunca se oculta."""
        normalized = self._normalize_key(kind, key, provider_id=provider_id, slot=slot)
        return self._head(self._directory(kind, normalized, provider_id=provider_id, slot=slot))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("el clock del ledger debe devolver datetime con zona horaria")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_ttl(ttl_seconds: int, operation_timeout_seconds: int) -> None:
        if ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds supera MAX_TTL_SECONDS={MAX_TTL_SECONDS}")
        minimum = operation_timeout_seconds + SAFETY_MARGIN_SECONDS
        if ttl_seconds < minimum:
            raise ValueError(f"ttl_seconds debe ser >= timeout + margen ({minimum})")

    @staticmethod
    def _normalize_key(kind: LeaseKind, key: str, *, provider_id: str, slot: int | None) -> str:
        if kind is LeaseKind.TASK_WRITER:
            try:
                return str(UUID(str(key)))
            except ValueError as exc:
                raise ValueError("TaskWriterLease exige task_id UUID canónico") from exc
        provider = provider_id.strip().lower()
        if not _SAFE_COMPONENT_RE.fullmatch(provider):
            raise ValueError("provider_id no es un componente de ruta seguro")
        if slot is None or slot < 0:
            raise ValueError("ProviderLease exige slot no negativo")
        expected = f"{provider}:{slot}"
        if key and key != expected:
            raise ValueError(f"la key de ProviderLease debe ser {expected!r}")
        return expected

    def _directory(
        self,
        kind: LeaseKind,
        key: str,
        *,
        provider_id: str = "",
        slot: int | None = None,
    ) -> Path:
        if kind is LeaseKind.TASK_WRITER:
            return self._root / "leases" / "task" / key
        if not provider_id:
            provider_id, raw_slot = key.rsplit(":", maxsplit=1)
            slot = int(raw_slot)
        assert slot is not None
        return self._root / "leases" / "provider" / provider_id / str(slot)

    def _directory_from_token(self, token: FencingToken) -> Path:
        if token.kind is LeaseKind.TASK_WRITER:
            key = self._normalize_key(token.kind, token.key, provider_id="", slot=None)
            return self._directory(token.kind, key)
        provider_id, raw_slot = token.key.rsplit(":", maxsplit=1)
        key = self._normalize_key(
            token.kind, token.key, provider_id=provider_id, slot=int(raw_slot)
        )
        return self._directory(token.kind, key, provider_id=provider_id, slot=int(raw_slot))

    def _read(self, directory: Path) -> tuple[LeaseRecord, ...]:
        if not directory.exists():
            return ()
        if not directory.is_dir():
            self._corrupt(f"{directory} no es un directorio de ledger")
        entries: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.name.startswith(".lease-") and path.name.endswith(".tmp"):
                try:
                    status = path.lstat()
                except FileNotFoundError:
                    # Temporal de un append concurrente que ya se publicó o descartó entre el
                    # listado y esta lectura: es un write legítimo en vuelo, no corrupción.
                    continue
                if not stat.S_ISREG(status.st_mode):
                    self._corrupt(f"temporal ambiguo en ledger: {path}")
                continue
            match = _RECORD_RE.fullmatch(path.name)
            if match is None or not path.is_file() or path.is_symlink():
                self._corrupt(f"entrada inesperada en ledger: {path}")
            entries.append((int(match.group("seq")), path))
        entries.sort()
        records: list[LeaseRecord] = []
        expected_prev = GENESIS_DIGEST
        for expected_seq, (sequence, path) in enumerate(entries, start=1):
            if sequence != expected_seq:
                self._corrupt(f"hueco o secuencia imposible en {directory}: {sequence}")
            try:
                payload = json.loads(path.read_bytes())
                record_type: type[LeaseRecord] = (
                    ProviderLease
                    if isinstance(payload, dict) and payload.get("kind") == LeaseKind.PROVIDER.value
                    else TaskWriterLease
                )
                record = record_type.model_validate(payload)
            except Exception as exc:
                self._corrupt(f"registro ilegible {path}: {exc}")
            if record.seq != sequence:
                self._corrupt(f"{path} declara seq={record.seq}")
            if record.prev_digest != expected_prev:
                self._corrupt(f"digest encadenado inválido en {path}")
            if record.digest != record.expected_digest():
                self._corrupt(f"digest propio inválido en {path}")
            if record.acquired_at > self._now() + timedelta(seconds=SAFETY_MARGIN_SECONDS):
                self._corrupt(f"fecha futura imposible en {path}")
            if records:
                previous = records[-1]
                if record.kind is not previous.kind or record.key != previous.key:
                    self._corrupt(f"identidad del lease cambió en {path}")
                if record.epoch < previous.epoch or record.epoch > previous.epoch + 1:
                    self._corrupt(f"epoch inválido en {path}")
                if record.epoch == previous.epoch + 1 and record.state is not LeaseState.ACTIVE:
                    self._corrupt(f"solo acquire puede incrementar epoch en {path}")
                if record.epoch == previous.epoch and record.holder != previous.holder:
                    self._corrupt(f"el holder cambió sin incrementar epoch en {path}")
                if record.epoch == previous.epoch and (
                    record.provider_id != previous.provider_id
                    or record.slot != previous.slot
                    or record.task_id != previous.task_id
                    or record.task_epoch != previous.task_epoch
                ):
                    self._corrupt(f"el contexto del lease cambió sin incrementar epoch en {path}")
                if record.acquired_at < previous.acquired_at:
                    self._corrupt(f"el reloj durable retrocede en {path}")
                if record.epoch == previous.epoch + 1:
                    if previous.state is LeaseState.ACTIVE:
                        self._corrupt(f"acquire sobre lease todavía activo en {path}")
                    if record.supersedes_epoch != previous.epoch:
                        self._corrupt(f"supersedes_epoch inválido en {path}")
                elif previous.state is not LeaseState.ACTIVE:
                    self._corrupt(f"transición posterior a estado terminal sin acquire en {path}")
            elif (
                record.epoch != 1
                or record.state is not LeaseState.ACTIVE
                or record.supersedes_epoch is not None
            ):
                self._corrupt(f"la génesis del lease es inválida en {path}")
            records.append(record)
            expected_prev = record.digest
        return tuple(records)

    def _head(self, directory: Path) -> LeaseRecord | None:
        records = self._read(directory)
        return records[-1] if records else None

    def _append(self, directory: Path, record: LeaseRecord) -> bool:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{record.seq:08d}.json"
        handle, temporary_name = tempfile.mkstemp(
            dir=str(directory), prefix=".lease-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            payload = record.model_dump_json().encode("utf-8")
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return False
            except OSError as exc:
                self._corrupt(f"el filesystem no ofrece CAS exclusivo con os.link: {exc}")
            self._fsync_directory(directory)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        try:
            descriptor = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _transition(self, head: LeaseRecord, state: LeaseState, now: datetime) -> LeaseRecord:
        return self._new_record(
            kind=head.kind,
            key=head.key,
            seq=head.seq + 1,
            epoch=head.epoch,
            state=state,
            holder=head.holder,
            now=now,
            acquired_at=now,
            expires_at=now + timedelta(seconds=head.ttl_seconds),
            ttl_seconds=head.ttl_seconds,
            supersedes_epoch=head.supersedes_epoch,
            prev_digest=head.digest,
            provider_id=head.provider_id,
            slot=head.slot,
            task_id=head.task_id,
            task_epoch=head.task_epoch,
        )

    def _new_record(
        self,
        *,
        kind: LeaseKind,
        key: str,
        seq: int,
        epoch: int,
        state: LeaseState,
        holder: LeaseHolder,
        now: datetime | None = None,
        acquired_at: datetime | None = None,
        expires_at: datetime | None = None,
        ttl_seconds: int,
        supersedes_epoch: int | None,
        prev_digest: str,
        provider_id: str,
        slot: int | None,
        task_id: UUID | None,
        task_epoch: int | None,
    ) -> LeaseRecord:
        acquired = acquired_at or now
        if acquired is None:
            raise ValueError("un record exige acquired_at")
        expiry = expires_at or acquired + timedelta(seconds=ttl_seconds)
        record_type: type[LeaseRecord] = (
            ProviderLease if kind is LeaseKind.PROVIDER else TaskWriterLease
        )
        provisional = record_type(
            kind=kind,
            key=key,
            seq=seq,
            epoch=epoch,
            state=state,
            holder=holder,
            acquired_at=acquired,
            expires_at=expiry,
            ttl_seconds=ttl_seconds,
            supersedes_epoch=supersedes_epoch,
            prev_digest=prev_digest,
            digest=GENESIS_DIGEST,
            provider_id=provider_id,
            slot=slot,
            task_id=task_id,
            task_epoch=task_epoch,
        )
        return provisional.model_copy(update={"digest": provisional.expected_digest()})

    @staticmethod
    def _token_matches(record: LeaseRecord, token: FencingToken) -> bool:
        return (
            record.kind is token.kind
            and record.key == token.key
            and record.epoch == token.epoch
            and record.holder.executor_id == token.holder_executor_id
            and record.task_id == token.task_id
            and record.task_epoch == token.task_epoch
        )

    @staticmethod
    def _is_expired(record: LeaseRecord, now: datetime) -> bool:
        return now >= record.expires_at + timedelta(seconds=TAKEOVER_GRACE_SECONDS)

    def _provider_for_task(self, task_id: UUID, *, exclude_key: str) -> LeaseRecord | None:
        provider_root = self._root / "leases" / "provider"
        if not provider_root.is_dir():
            return None
        for provider_directory in sorted(provider_root.iterdir()):
            if not provider_directory.is_dir():
                self._corrupt(f"entrada inesperada en provider ledger: {provider_directory}")
            for slot_directory in sorted(provider_directory.iterdir()):
                head = self._head(slot_directory)
                if (
                    head is not None
                    and head.state is LeaseState.ACTIVE
                    and self._is_expired(head, self._now())
                ):
                    expired = self._transition(head, LeaseState.EXPIRED, self._now())
                    if self._append(slot_directory, expired):
                        self._audit_record(AuditEventType.LEASE_EXPIRED, expired)
                        head = expired
                    else:
                        head = self._head(slot_directory)
                if (
                    head is not None
                    and head.key != exclude_key
                    and head.task_id == task_id
                    and head.state is LeaseState.ACTIVE
                    and not self._is_expired(head, self._now())
                ):
                    return head
        return None

    def _ledger_directories(self) -> Iterator[Path]:
        task_root = self._root / "leases" / "task"
        if task_root.is_dir():
            for directory in sorted(task_root.iterdir()):
                if not directory.is_dir():
                    self._corrupt(f"entrada inesperada en task ledger: {directory}")
                yield directory
        provider_root = self._root / "leases" / "provider"
        if provider_root.is_dir():
            for provider_directory in sorted(provider_root.iterdir()):
                if not provider_directory.is_dir():
                    self._corrupt(f"entrada inesperada en provider ledger: {provider_directory}")
                for directory in sorted(provider_directory.iterdir()):
                    if not directory.is_dir():
                        self._corrupt(f"entrada inesperada en provider ledger: {directory}")
                    yield directory

    @staticmethod
    def _result(outcome: LeaseOutcome, record: LeaseRecord | None, detail: str = "") -> LeaseResult:
        token = record.token() if record is not None and record.state is LeaseState.ACTIVE else None
        return LeaseResult(outcome=outcome, record=record, token=token, detail=detail)

    def _fenced(self, record: LeaseRecord | None, detail: str) -> LeaseResult:
        if record is not None:
            self._audit_record(AuditEventType.LEASE_FENCED, record, result=AuditResult.DENIED)
        return self._result(LeaseOutcome.FENCED, record, detail)

    def _corrupt(self, detail: str) -> NoReturn:
        if self._audit is not None:
            self._audit.record(
                AuditEventType.LEASE_LEDGER_CORRUPT,
                action="lease_ledger_corrupt",
                resource="lease",
                result=AuditResult.FAILURE,
                actor=self._actor,
                metadata={"detail": detail[:500]},
            )
        raise LeaseLedgerCorruptError(detail)

    def _audit_record(
        self,
        event_type: AuditEventType,
        record: LeaseRecord,
        *,
        result: AuditResult = AuditResult.SUCCESS,
    ) -> None:
        if self._audit is None:
            return
        self._audit.log_lease_event(
            event_type=event_type,
            kind=record.kind.value,
            key=record.key,
            epoch=record.epoch,
            seq=record.seq,
            state=record.state.value,
            holder_id=record.holder.executor_id,
            task_id=record.task_id,
            result=result,
            actor=self._actor,
        )


__all__ = [
    "GENESIS_DIGEST",
    "MAX_TTL_SECONDS",
    "SAFETY_MARGIN_SECONDS",
    "TAKEOVER_GRACE_SECONDS",
    "FencingToken",
    "LeaseFencedError",
    "LeaseHolder",
    "LeaseKind",
    "LeaseLedger",
    "LeaseLedgerCorruptError",
    "LeaseOutcome",
    "LeaseRecord",
    "LeaseResult",
    "LeaseState",
    "ProviderLease",
    "TaskWriterLease",
]
