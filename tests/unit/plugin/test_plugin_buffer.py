import asyncio
import contextlib
import errno
import io
import json
import os
import threading
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from plugin_helpers import FakeStore

from mint_litellm.plugin.buffer import (
    BufferWriteError,
    ConsumeRequest,
    ConsumptionLost,
    EvidenceRecorder,
    LocalBuffer,
)
from mint_litellm.types.events import (
    Event,
    EventKind,
    Evidence,
    call_source_key,
    control_source_key,
)
from mint_litellm.types.ids import new_uuid7

try:
    import fcntl
except ImportError:
    fcntl = None


def make_event(n: int = 0, *, payload: dict | None = None) -> Event:
    invocation = UUID(int=(0x0192F7A07C1E7A108000 << 48) + n)
    return Event(
        instance_id="inst",
        inserted_at=datetime(2026, 11, 3, 9, 0, n % 60, tzinfo=UTC),
        kind=EventKind.DECISION_EVALUATED,
        source_key=call_source_key(invocation, 1, EventKind.DECISION_EVALUATED),
        invocation_id=invocation,
        attempt_id=1,
        payload=payload or {"n": n},
    )


def recorder(tmp_path, store, *, stderr=None, max_bytes=1024 * 1024, interval=0.0):
    buffer = LocalBuffer(tmp_path / "buf", max_bytes)
    rec = EvidenceRecorder(
        store,
        buffer,
        budget_seconds=1.0,
        instance_id="inst",
        stderr=stderr if stderr is not None else io.StringIO(),
        replay_interval_seconds=interval,
    )
    return rec, buffer


def files(directory):
    """버퍼 디렉터리의 데이터 파일(프로세스 사이 잠금 파일은 제외)."""
    return sorted(p.name for p in directory.iterdir() if p.name != ".lock")


def closed_files(buffer: LocalBuffer):
    buffer.close()
    return sorted(buffer._dir.glob("*.jsonl"))


class TestLocalBuffer:
    def test_events_are_appended_as_json_lines_and_read_back(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        events = [make_event(i) for i in range(3)]
        for event in events:
            buffer.append(event)
        (path,) = closed_files(buffer)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3 and all(
            json.loads(line)["kind"] == "decision_evaluated" for line in lines
        )
        read, unreadable = LocalBuffer.read(path)
        assert read == events and unreadable == 0

    def test_a_truncated_last_line_is_counted_not_fatal(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        buffer.append(make_event(1))
        (path,) = closed_files(buffer)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "decision_ev')
        read, unreadable = LocalBuffer.read(path)
        assert len(read) == 1 and unreadable == 1

    def test_claiming_closes_the_segment_being_written(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        assert not buffer.has_pending()
        buffer.append(make_event(1))
        assert buffer.has_pending()
        (candidate,) = buffer.pending()
        claimed = buffer.take(candidate)
        assert [e.source_key for e in LocalBuffer.read(claimed)[0]] == [make_event(1).source_key]
        assert not buffer.has_pending()
        buffer.append(make_event(2))
        assert buffer.has_pending()

    def test_claim_renames_and_release_gives_the_file_back(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        buffer.append(make_event(1))
        buffer.close()
        (candidate,) = buffer.pending()
        claimed = buffer.take(candidate)
        assert ".replaying." in claimed.name and not buffer.has_pending()
        buffer.release(claimed)
        assert buffer.has_pending()
        claimed = buffer.take(buffer.pending()[0])
        buffer.finish(claimed)
        assert files(tmp_path) == []

    def test_the_size_cap_deletes_the_oldest_closed_files_and_counts_what_was_lost(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 8 * 1024)
        big = "x" * 900
        for i in range(30):
            buffer.append(make_event(i, payload={"pad": big}))
        total = sum(p.stat().st_size for p in tmp_path.iterdir() if p.name != ".lock")
        assert total <= 8 * 1024
        overflow = buffer.take_overflow()
        assert overflow is not None and overflow.dropped_files >= 1
        kept = 0
        for path in closed_files(buffer):
            kept += len(LocalBuffer.read(path)[0])
        assert kept + overflow.dropped_events == 30
        assert buffer.take_overflow() is None

    def test_an_event_larger_than_the_cap_cannot_be_buffered(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024)
        with pytest.raises(BufferWriteError):
            buffer.append(make_event(1, payload={"pad": "x" * 4000}))

    def test_an_unwritable_location_raises_a_buffer_error(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        buffer = LocalBuffer(blocker / "inner", 1024 * 1024)
        with pytest.raises(BufferWriteError):
            buffer.append(make_event(1))

    def test_concurrent_appends_and_rotations_all_land_in_files(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 8 * 1024 * 1024)
        buffer._segment_bytes = 1024
        errors: list[Exception] = []

        def work(worker: int) -> None:
            for i in range(40):
                try:
                    buffer.append(make_event(worker * 1000 + i, payload={"pad": "x" * 150}))
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        keys: set[str] = set()
        for path in closed_files(buffer):
            events, unreadable = LocalBuffer.read(path)
            assert unreadable == 0
            keys |= {e.source_key for e in events}
        assert len(keys) == 8 * 40
        assert len(closed_files(buffer)) > 8

    def test_buffers_sharing_a_directory_trim_it_without_failing(self, tmp_path):
        cap = 16 * 1024
        sizes = self.fill_shared_directory(tmp_path, cap)
        assert sizes["errors"] == []
        if fcntl is not None:
            assert sizes["largest"] <= cap and sizes["final"] <= cap

    @staticmethod
    def fill_shared_directory(tmp_path, cap):
        """한 디렉터리를 쓰는 버퍼 넷이 상한을 넘겨 쓰는 동안 디렉터리 크기를 잰다."""
        buffers = [LocalBuffer(tmp_path, cap) for _ in range(4)]
        errors: list[Exception] = []
        largest = [0]
        done = threading.Event()

        def work(buffer: LocalBuffer, worker: int) -> None:
            for i in range(60):
                try:
                    buffer.append(make_event(worker * 1000 + i, payload={"pad": "x" * 400}))
                except BufferWriteError:
                    pass
                except Exception as exc:
                    errors.append(exc)

        def watch() -> None:
            while not done.is_set():
                with contextlib.suppress(OSError):
                    total = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
                    largest[0] = max(largest[0], total)
                time.sleep(0.002)

        threads = [threading.Thread(target=work, args=(b, w)) for w, b in enumerate(buffers)]
        watcher = threading.Thread(target=watch)
        watcher.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        done.set()
        watcher.join()
        final = sum(p.stat().st_size for p in tmp_path.iterdir() if p.is_file())
        return {"errors": errors, "largest": largest[0], "final": final}

    @pytest.mark.skipif(os.name != "posix", reason="needs directory fsync")
    def test_a_failed_directory_fsync_is_a_write_failure(self, tmp_path, monkeypatch):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)

        def failing(fd):
            raise OSError(errno.EIO, "input/output error")

        monkeypatch.setattr(os, "fsync", failing)
        with pytest.raises(BufferWriteError):
            buffer.append(make_event(1))

    @pytest.mark.skipif(os.name != "posix", reason="needs directory fsync")
    def test_a_filesystem_without_directory_fsync_still_buffers(self, tmp_path, monkeypatch):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        real = os.fsync

        def unsupported_for_directories(fd):
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError(errno.EINVAL, "invalid argument")
            real(fd)

        monkeypatch.setattr(os, "fsync", unsupported_for_directories)
        buffer.append(make_event(1))
        (path,) = closed_files(buffer)
        assert len(LocalBuffer.read(path)[0]) == 1

    def test_the_segment_size_counts_bytes_not_characters(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        buffer._segment_bytes = 2048
        for i in range(12):
            buffer.append(make_event(i, payload={"pad": "한" * 150}))
        sizes = [p.stat().st_size for p in closed_files(buffer)]
        assert len(sizes) > 1 and all(size <= 2048 for size in sizes)

    @pytest.mark.skipif(fcntl is None, reason="needs flock")
    def test_an_open_file_without_a_live_writer_is_adopted(self, tmp_path):
        dead = tmp_path / ("deadbeef-000001.open")
        dead.write_text(make_event(5).to_json_line() + "\n", encoding="utf-8")
        live = LocalBuffer(tmp_path, 1024 * 1024)
        assert live.has_pending()
        other = LocalBuffer(tmp_path, 1024 * 1024)
        other.append(make_event(6))
        names = [p.name for p in live.pending()]
        assert dead.name in names and not any(n.startswith(other._owner) for n in names)

    def test_files_are_taken_one_at_a_time(self, tmp_path):
        buffer = LocalBuffer(tmp_path, 1024 * 1024)
        buffer._segment_bytes = 1024
        for i in range(3):
            buffer.append(make_event(i, payload={"pad": "x" * 700}))
        candidates = buffer.pending()
        assert len(candidates) == 3
        taken = buffer.take(candidates[0])
        names = files(tmp_path)
        assert (
            sum(".replaying." in n for n in names) == 1
            and sum(n.endswith(".jsonl") for n in names) == 2
        )
        assert buffer.take(candidates[0]) is None
        buffer.release(taken)
        assert not any(".replaying." in n for n in files(tmp_path))

    @pytest.mark.skipif(fcntl is None, reason="needs flock")
    def test_a_replay_left_behind_by_a_dead_process_is_recovered(self, tmp_path):
        dead = LocalBuffer(tmp_path, 1024 * 1024)
        dead.append(make_event(7))
        (taken,) = [dead.take(p) for p in dead.pending()]
        other = LocalBuffer(tmp_path, 1024 * 1024)
        assert other.pending() == []
        dead._held.pop(taken).close()
        (recovered,) = other.pending()
        assert recovered.name == taken.name
        mine = other.take(recovered)
        assert mine is not None and other._owner in mine.name
        assert [e.source_key for e in LocalBuffer.read(mine)[0]] == [make_event(7).source_key]
        other.release(mine)
        assert [p.name.endswith(".jsonl") for p in other.pending()] == [True]

    @pytest.mark.skipif(fcntl is None, reason="needs flock")
    def test_a_file_held_by_a_live_writer_is_not_adopted(self, tmp_path):
        writer = LocalBuffer(tmp_path, 1024 * 1024)
        writer.append(make_event(1))
        reader = LocalBuffer(tmp_path, 1024 * 1024)
        assert not reader.has_pending()


class TestEvidenceRecorder:
    def test_database_first(self, tmp_path):
        store = FakeStore()
        rec, buffer = recorder(tmp_path, store)
        assert asyncio.run(rec.record(make_event(1))) is Evidence.DB
        assert len(store.events) == 1 and not buffer.has_pending()

    def test_a_database_failure_falls_back_to_the_buffer_once(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store)
        assert asyncio.run(rec.record(make_event(1))) is Evidence.BUFFER
        (path,) = closed_files(buffer)
        (event,), _ = LocalBuffer.read(path)
        assert event.evidence is Evidence.BUFFER and store.events == {}

    def test_buffer_failure_falls_back_to_one_stderr_line(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        stderr = io.StringIO()
        rec, buffer = recorder(tmp_path, store, stderr=stderr)
        blocker = tmp_path / "file"
        blocker.write_text("x")
        buffer._dir = blocker / "inner"
        assert asyncio.run(rec.record(make_event(1))) is Evidence.STDERR
        record = json.loads(stderr.getvalue())
        assert record["evidence"] == "stderr" and record["source_key"].startswith("call:")

    def test_consumption_lost_is_reported_and_nothing_is_stored(self, tmp_path):
        store = FakeStore()
        store.lose_consumption = True
        rec, _ = recorder(tmp_path, store)
        consume = ConsumeRequest(
            call_approval_id=new_uuid7(),
            invocation_id=new_uuid7(),
            principal_type="user",
            principal_id="u1",
            server_id="s",
            tool="t",
            args_fp="0" * 64,
            policy_version_id="pv",
            state_version=1,
        )
        result = asyncio.run(rec.try_database(make_event(1), consume))
        assert result.consumption_lost and not result.stored and store.events == {}

    def test_overflow_is_recorded_as_a_system_event(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store, max_bytes=8 * 1024)

        async def fill():
            for i in range(30):
                await rec.record(make_event(i, payload={"pad": "x" * 900}))

        asyncio.run(fill())
        kinds = []
        for path in closed_files(buffer):
            kinds += [e.kind for e in LocalBuffer.read(path)[0]]
        assert EventKind.BUFFER_OVERFLOW in kinds

    def test_system_events_of_one_process_take_consecutive_sequence_numbers(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store, max_bytes=8 * 1024)

        async def scenario():
            for i in range(30):
                await rec.record(make_event(i, payload={"pad": "x" * 900}))
            buffer.close()
            store.fail = None
            await rec.replay()

        asyncio.run(scenario())
        system = [
            e.source_key.split(":")
            for e in store.events.values()
            if e.kind in (EventKind.BUFFER_OVERFLOW, EventKind.BUFFER_REPLAYED)
        ]
        assert len(system) >= 2
        assert {parts[0] for parts in system} == {"buffer"}
        assert len({parts[1] for parts in system}) == 1
        sequence = sorted(int(parts[2]) for parts in system)
        assert sequence[0] >= 1 and len(set(sequence)) == len(sequence)
        (report,) = store.of_kind("buffer_replayed")
        assert int(report.source_key.split(":")[2]) == sequence[-1]

    def test_system_events_of_two_processes_keep_apart_at_the_same_sequence_number(self, tmp_path):
        store = FakeStore()

        async def replay_one(directory):
            rec, buffer = recorder(directory, store)
            store.fail = ConnectionError("down")
            await rec.record(make_event(1))
            buffer.close()
            store.fail = None
            await rec.replay()

        asyncio.run(replay_one(tmp_path / "a"))
        asyncio.run(replay_one(tmp_path / "b"))
        reports = store.of_kind("buffer_replayed")
        assert len(reports) == 2
        assert all(e.source_key.endswith(":1:buffer_replayed") for e in reports)
        assert len({e.source_key for e in reports}) == 2

    @staticmethod
    def refuse_system_events(buffer):
        """시스템 사건만 버퍼에 쓰지 못하게 하고 버퍼에 닿은 호출을 센다."""
        original = buffer.append
        calls: list[EventKind] = []

        def append(event):
            calls.append(event.kind)
            if event.kind in (EventKind.BUFFER_OVERFLOW, EventKind.BUFFER_REPLAYED):
                raise BufferWriteError("full")
            original(event)

        buffer.append = append
        return calls

    def test_an_overflow_report_that_fits_nowhere_reaches_standard_error(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        stderr = io.StringIO()
        rec, buffer = recorder(tmp_path, store, stderr=stderr)
        calls = self.refuse_system_events(buffer)
        buffer._dropped_events, buffer._dropped_files = 5, 2
        assert asyncio.run(rec.fallback(make_event(1))) is Evidence.BUFFER
        (line,) = stderr.getvalue().splitlines()
        record = json.loads(line)
        assert record["kind"] == "buffer_overflow" and record["evidence"] == "stderr"
        assert record["payload"] == {"dropped_events": 5, "dropped_files": 2}
        assert record["source_key"].startswith("buffer:")
        assert calls == [EventKind.DECISION_EVALUATED, EventKind.BUFFER_OVERFLOW]

    def test_a_replay_report_that_fits_nowhere_reaches_standard_error(self, tmp_path):
        class ReportsFail(FakeStore):
            async def insert_event(self, event, consume=None):
                if event.kind is EventKind.BUFFER_REPLAYED:
                    raise ConnectionError("down")
                return await super().insert_event(event, consume)

        store = ReportsFail()
        store.fail = ConnectionError("down")
        stderr = io.StringIO()
        rec, buffer = recorder(tmp_path, store, stderr=stderr)
        asyncio.run(rec.record(make_event(1)))
        buffer.close()
        store.fail = None
        self.refuse_system_events(buffer)
        asyncio.run(rec.replay())
        (line,) = stderr.getvalue().splitlines()
        record = json.loads(line)
        assert record["kind"] == "buffer_replayed" and record["evidence"] == "stderr"
        assert record["payload"] == {"replayed": 1, "duplicates": 0, "unreadable": 0}

    def test_a_system_event_that_cannot_be_stored_is_not_reported_again(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store)
        calls = self.refuse_system_events(buffer)
        buffer._dropped_events, buffer._dropped_files = 1, 1
        asyncio.run(rec.fallback(make_event(1)))
        assert calls.count(EventKind.BUFFER_OVERFLOW) == 1
        assert buffer.take_overflow() is None

    def test_replay_loads_the_buffer_and_records_the_count(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store)

        async def scenario():
            for i in range(3):
                await rec.record(make_event(i))
            buffer.close()
            store.fail = None
            await rec.replay()

        asyncio.run(scenario())
        assert len(store.of_kind("decision_evaluated")) == 3
        (report,) = store.of_kind("buffer_replayed")
        assert report.payload == {"replayed": 3, "duplicates": 0, "unreadable": 0}
        assert not buffer.has_pending()

    @staticmethod
    def three_segments(tmp_path, store):
        """사건 하나씩 담은 닫힌 세그먼트 3개가 있는 기록기."""
        rec, buffer = recorder(tmp_path, store)
        buffer._segment_bytes = 1024
        store.fail = ConnectionError("down")

        async def fill():
            for i in range(3):
                await rec.record(make_event(i, payload={"n": i, "pad": "x" * 700}))

        asyncio.run(fill())
        buffer.close()
        store.fail = None
        return rec, buffer

    def test_a_failure_on_the_first_file_leaves_every_file_for_the_next_pass(self, tmp_path):
        store = FakeStore()
        rec, buffer = self.three_segments(tmp_path, store)
        store.fail = ConnectionError("down")
        asyncio.run(rec.replay())
        names = files(buffer._dir)
        assert len(names) == 3 and all(n.endswith(".jsonl") for n in names)
        store.fail = None
        asyncio.run(rec.replay())
        assert len(store.of_kind("decision_evaluated")) == 3 and files(buffer._dir) == []

    def test_a_failure_on_a_later_file_keeps_the_loaded_ones_and_the_rest_waiting(self, tmp_path):
        class DiesOnThird(FakeStore):
            async def insert_event(self, event, consume=None):
                if event.payload.get("n") == 2:
                    raise ConnectionError("down")
                return await super().insert_event(event, consume)

        store = DiesOnThird()
        rec, buffer = self.three_segments(tmp_path, store)
        asyncio.run(rec.replay())
        assert len(store.of_kind("decision_evaluated")) == 2
        names = files(buffer._dir)
        assert len(names) == 1 and names[0].endswith(".jsonl")

    def test_cancelling_a_replay_gives_the_current_file_back(self, tmp_path):
        class Stuck(FakeStore):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()

            async def insert_event(self, event, consume=None):
                self.entered.set()
                await asyncio.sleep(60)

        store = FakeStore()
        rec, buffer = self.three_segments(tmp_path, store)
        rec._store = Stuck()

        async def scenario():
            task = asyncio.create_task(rec.replay())
            await rec._store.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        names = files(buffer._dir)
        assert len(names) == 3 and all(n.endswith(".jsonl") for n in names)

    @pytest.mark.skipif(fcntl is None, reason="needs flock")
    def test_a_restarted_process_replays_what_a_dead_one_had_taken(self, tmp_path):
        store = FakeStore()
        _, dead = self.three_segments(tmp_path, store)
        for path in dead.pending():
            dead.take(path)
        for handle in list(dead._held.values()):
            handle.close()
        rec, restarted = recorder(tmp_path, store)
        asyncio.run(rec.replay())
        assert len(store.of_kind("decision_evaluated")) == 3
        assert files(restarted._dir) == []

    def test_commit_then_lost_response_converges_to_one_row_and_counts_the_duplicate(
        self, tmp_path
    ):
        class LostResponse(FakeStore):
            armed = True

            async def insert_event(self, event, consume=None):
                inserted = await super().insert_event(event, consume)
                if self.armed:
                    self.armed = False
                    raise ConnectionResetError("response lost")
                return inserted

        store = LostResponse()
        rec, buffer = recorder(tmp_path, store)

        async def scenario():
            assert await rec.record(make_event(1)) is Evidence.BUFFER
            buffer.close()
            await rec.replay()

        asyncio.run(scenario())
        assert len(store.of_kind("decision_evaluated")) == 1
        (report,) = store.of_kind("buffer_replayed")
        assert report.payload == {"replayed": 0, "duplicates": 1, "unreadable": 0}

    def test_replay_stops_and_returns_the_file_when_the_database_fails(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store)

        async def scenario():
            await rec.record(make_event(1))
            buffer.close()
            await rec.replay()

        asyncio.run(scenario())
        assert buffer.has_pending() and store.events == {}

    def test_replaying_system_events_does_not_chain_new_reports(self, tmp_path):
        store = FakeStore()
        rec, buffer = recorder(tmp_path, store)
        report = Event(
            instance_id="inst",
            inserted_at=datetime(2026, 11, 3, tzinfo=UTC),
            kind=EventKind.BUFFER_REPLAYED,
            source_key=control_source_key(EventKind.BUFFER_REPLAYED, str(new_uuid7()), 1),
            payload={"replayed": 1, "duplicates": 0, "unreadable": 0},
        )
        buffer.append(report)
        buffer.close()
        asyncio.run(rec.replay())
        assert len(store.events) == 1 and not buffer.has_pending()

    def test_a_successful_write_starts_a_replay_of_waiting_events(self, tmp_path):
        store = FakeStore()
        store.fail = ConnectionError("down")
        rec, buffer = recorder(tmp_path, store)

        async def scenario():
            await rec.record(make_event(1))
            store.fail = None
            await rec.record(make_event(2))
            await asyncio.sleep(0.2)

        asyncio.run(scenario())
        assert len(store.of_kind("decision_evaluated")) == 2
        assert not buffer.has_pending()

    def test_a_plugin_without_a_database_still_records_to_the_buffer(self, tmp_path):
        rec, buffer = recorder(tmp_path, None)
        assert asyncio.run(rec.record(make_event(1))) is Evidence.BUFFER


def test_buffer_directory_is_created_on_first_use(tmp_path):
    target = tmp_path / "a" / "b"
    LocalBuffer(target, 1024 * 1024).append(make_event(1))
    assert target.is_dir() and os.listdir(target)


def test_consumption_lost_is_an_exception_type():
    assert issubclass(ConsumptionLost, Exception)
