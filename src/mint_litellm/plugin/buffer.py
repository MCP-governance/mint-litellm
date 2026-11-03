"""로컬 내구 버퍼와 증거 기록 순서.

사건은 같은 ``source_key``로 DB → 버퍼 → 표준 오류 순서의 한 위치에 처음 기록된다. 기록 결과가
불명인 경우(커밋 뒤 응답 유실 등)에는 다음 위치에 다시 쓰므로 같은 사건이 두 위치에 남을 수
있고, 버퍼 재적재가 DB의 ``(instance_id, source_key)`` 유일성으로 한 건에 수렴시킨다.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO
from uuid import UUID

from mint_litellm.types.events import Event, EventKind, Evidence, control_source_key
from mint_litellm.types.ids import new_uuid7

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

_OPEN = ".open"
_CLOSED = ".jsonl"
_REPLAYING = ".replaying"
_SYSTEM_KINDS = frozenset({EventKind.BUFFER_OVERFLOW, EventKind.BUFFER_REPLAYED})


class BufferWriteError(Exception):
    """버퍼에 쓰지 못했다(읽기 전용 볼륨, 가득 참, 입출력 오류)."""


class ConsumptionLost(Exception):
    """호출별 승인을 소비하지 못했다. 같은 트랜잭션의 사건 기록도 되돌려졌다."""


@dataclass(frozen=True)
class ConsumeRequest:
    """승인된 호출별 승인을 한 번 소비하는 조건. 모든 항목이 현재 호출과 일치해야 한다."""

    call_approval_id: UUID
    invocation_id: UUID
    principal_type: str
    principal_id: str
    server_id: str
    tool: str
    args_fp: str
    policy_version_id: str
    state_version: int


class EventStore(Protocol):
    async def insert_event(self, event: Event, consume: ConsumeRequest | None = None) -> bool:
        """사건을 넣는다. 같은 배치에 같은 ``source_key``가 이미 있으면 ``False``."""
        ...


@dataclass(frozen=True)
class Overflow:
    dropped_events: int
    dropped_files: int


class LocalBuffer:
    """JSONL 추가 쓰기 + fsync. 상한을 넘으면 가장 오래된 닫힌 파일부터 지운다."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self._dir = directory
        self._max_bytes = max_bytes
        self._segment_bytes = max(1024, min(4 * 1024 * 1024, max_bytes // 4))
        self._owner = new_uuid7().hex
        self._seq = 0
        self._handle: TextIO | None = None
        self._path: Path | None = None
        self._size = 0
        self._dropped_events = 0
        self._dropped_files = 0
        self._lock = threading.RLock()

    def append(self, event: Event) -> None:
        line = event.to_json_line()
        if "\n" in line:
            raise BufferWriteError("event line contains a line break")
        data = line + "\n"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._make_room(len(data.encode("utf-8")))
            if self._handle is None or self._size + len(data) > self._segment_bytes:
                self._rotate()
            assert self._handle is not None
            self._handle.write(data)
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._size += len(data)
        except BufferWriteError:
            raise
        except OSError as exc:
            self._abandon()
            raise BufferWriteError(f"{type(exc).__name__}") from exc

    def take_overflow(self) -> Overflow | None:
        with self._lock:
            if not self._dropped_files:
                return None
            report = Overflow(self._dropped_events, self._dropped_files)
            self._dropped_events = self._dropped_files = 0
            return report

    def close(self) -> None:
        with self._lock:
            self._close_current()

    def has_pending(self) -> bool:
        with self._lock:
            if self._handle is not None and self._size > 0:
                return True
            try:
                return any(self._claimable())
            except OSError:
                return False

    def claim(self) -> list[Path]:
        """다시 적재할 파일을 가져온다(이름을 바꿔 소유한다).

        지금 쓰고 있는 파일도 닫아서 포함한다. 닫지 않으면 쓰는 프로세스가 살아 있는 동안 그
        파일의 사건은 다시 적재되지 않는다. 주인이 없어진 열린 파일도 가져온다.
        """
        with self._lock:
            self._close_current()
            claimed: list[Path] = []
            for path in self._claimable():
                target = path.with_name(f"{path.name}{_REPLAYING}.{self._owner}")
                try:
                    os.rename(path, target)
                except OSError:
                    continue
                claimed.append(target)
            return claimed

    def release(self, path: Path) -> None:
        """재적재를 마치지 못한 파일을 다시 닫힌 파일로 돌려놓는다."""
        original = path.name.split(_REPLAYING)[0]
        name = original if original.endswith(_CLOSED) else original.removesuffix(_OPEN) + _CLOSED
        with contextlib.suppress(OSError):
            os.rename(path, path.with_name(name))

    @staticmethod
    def read(path: Path) -> tuple[list[Event], int]:
        events: list[Event] = []
        unreadable = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    events.append(Event.from_json_line(raw))
                except ValueError:
                    unreadable += 1
        return events, unreadable

    @staticmethod
    def discard(path: Path) -> None:
        with contextlib.suppress(OSError):
            path.unlink()

    def _claimable(self):
        if not self._dir.is_dir():
            return
        for entry in sorted(self._dir.iterdir()):
            name = entry.name
            if name.endswith(_CLOSED):
                yield entry
            elif (
                name.endswith(_OPEN) and not name.startswith(self._owner) and self._orphaned(entry)
            ):
                yield entry

    @staticmethod
    def _orphaned(path: Path) -> bool:
        """쓰는 프로세스가 없는 열린 파일. 쓰는 쪽이 건 배타 잠금을 얻을 수 있으면 주인이 없다."""
        if fcntl is None:
            return False
        try:
            with path.open("r") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            return False
        return True

    def _rotate(self) -> None:
        self._close_current()
        self._seq += 1
        path = self._dir / f"{self._owner}-{self._seq:06d}{_OPEN}"
        handle = path.open("a", encoding="utf-8", newline="\n")
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._handle, self._path, self._size = handle, path, 0
        self._fsync_directory()

    def _close_current(self) -> None:
        handle, path = self._handle, self._path
        self._handle = self._path = None
        self._size = 0
        if handle is None or path is None:
            return
        with contextlib.suppress(OSError):
            handle.close()
        with contextlib.suppress(OSError):
            os.rename(path, path.with_name(path.name.removesuffix(_OPEN) + _CLOSED))
            self._fsync_directory()

    def _abandon(self) -> None:
        """쓰기에 실패한 열린 파일을 닫아 다음 호출이 새 파일에서 다시 시도하게 한다."""
        handle = self._handle
        self._handle = self._path = None
        self._size = 0
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()

    def _fsync_directory(self) -> None:
        if os.name != "posix":
            return
        with contextlib.suppress(OSError):
            fd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._dir.iterdir() if p.is_file())

    def _make_room(self, incoming: int) -> None:
        while self._total_bytes() + incoming > self._max_bytes:
            closed = sorted(p for p in self._dir.iterdir() if p.name.endswith(_CLOSED))
            if not closed:
                if self._handle is not None and self._size > 0:
                    self._close_current()
                    continue
                raise BufferWriteError("buffer is full")
            victim = closed[0]
            events, unreadable = self.read(victim)
            self._dropped_events += len(events) + unreadable
            self._dropped_files += 1
            victim.unlink()


class Recorded(Protocol):
    evidence: Evidence


@dataclass(frozen=True)
class DatabaseResult:
    stored: bool
    consumption_lost: bool = False


class EvidenceRecorder:
    """DB → 버퍼 → 표준 오류 순서로 사건을 남기고, DB가 돌아오면 버퍼를 다시 적재한다."""

    def __init__(
        self,
        store: EventStore | None,
        buffer: LocalBuffer,
        *,
        budget_seconds: float,
        instance_id: str,
        stderr: TextIO | None = None,
        replay_interval_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._buffer = buffer
        self._budget = budget_seconds
        self._instance_id = instance_id
        self._stderr = stderr
        self._replay_interval = replay_interval_seconds
        self._last_replay = 0.0
        self._replaying: asyncio.Task[None] | None = None

    async def try_database(
        self,
        event: Event,
        consume: ConsumeRequest | None = None,
        *,
        timeout: float | None = None,
    ) -> DatabaseResult:
        """DB에 쓴다. 시간 초과는 커밋 여부가 불명이라 실패로 다루고, 다음 위치에 다시 쓴다."""
        if self._store is None:
            return DatabaseResult(stored=False)
        try:
            async with asyncio.timeout(self._budget if timeout is None else timeout):
                inserted = await self._store.insert_event(event, consume)
        except ConsumptionLost:
            return DatabaseResult(stored=False, consumption_lost=True)
        except Exception:
            return DatabaseResult(stored=False)
        if consume is not None and not inserted:
            return DatabaseResult(stored=False)
        self._schedule_replay()
        return DatabaseResult(stored=True)

    async def fallback(self, event: Event) -> Evidence:
        """DB에 쓰지 못한 사건을 버퍼에, 그것도 안 되면 표준 오류에 남긴다."""
        try:
            await asyncio.to_thread(self._buffer.append, event.with_evidence(Evidence.BUFFER))
        except Exception:
            self._write_stderr(event.with_evidence(Evidence.STDERR))
            return Evidence.STDERR
        overflow = self._buffer.take_overflow()
        if overflow is not None:
            await self._append_system(
                EventKind.BUFFER_OVERFLOW,
                {
                    "dropped_events": overflow.dropped_events,
                    "dropped_files": overflow.dropped_files,
                },
            )
        return Evidence.BUFFER

    async def record(self, event: Event) -> Evidence:
        if (await self.try_database(event)).stored:
            return Evidence.DB
        return await self.fallback(event)

    async def replay(self) -> None:
        """버퍼에 쌓인 사건을 DB에 적재한다. 중복은 세어 ``buffer_replayed``에 남긴다."""
        store = self._store
        if store is None:
            return
        replayed = duplicates = unreadable = 0
        for path in await asyncio.to_thread(self._buffer.claim):
            events, bad = await asyncio.to_thread(self._buffer.read, path)
            try:
                for event in events:
                    async with asyncio.timeout(self._budget):
                        inserted = await store.insert_event(event.with_evidence(Evidence.DB))
                    if event.kind in _SYSTEM_KINDS:
                        continue
                    if inserted:
                        replayed += 1
                    else:
                        duplicates += 1
            except Exception:
                self._buffer.release(path)
                break
            unreadable += bad
            self._buffer.discard(path)
        if replayed or duplicates or unreadable:
            await self._append_system(
                EventKind.BUFFER_REPLAYED,
                {"replayed": replayed, "duplicates": duplicates, "unreadable": unreadable},
            )

    def _schedule_replay(self) -> None:
        now = time.monotonic()
        if (
            self._replaying is not None and not self._replaying.done()
        ) or now - self._last_replay < self._replay_interval:
            return
        self._last_replay = now
        if not self._buffer.has_pending():
            return
        try:
            self._replaying = asyncio.get_running_loop().create_task(self.replay())
        except RuntimeError:
            return

    async def _append_system(self, kind: EventKind, payload: dict[str, int]) -> None:
        batch = new_uuid7()
        event = Event(
            instance_id=self._instance_id,
            inserted_at=_utcnow(),
            kind=kind,
            source_key=control_source_key(kind, str(batch), 1),
            payload=payload,
        )
        if self._store is not None:
            try:
                async with asyncio.timeout(self._budget):
                    await self._store.insert_event(event)
                return
            except Exception:
                pass
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._buffer.append, event.with_evidence(Evidence.BUFFER))

    def _write_stderr(self, event: Event) -> None:
        stream = self._stderr if self._stderr is not None else sys.stderr
        with contextlib.suppress(Exception):
            stream.write(event.to_json_line() + "\n")
            stream.flush()


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)


Handler = Callable[[Event], Awaitable[None]]
