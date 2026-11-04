"""Continuous raw capture must survive preparation, scene boundaries, and retries."""

import base64
import csv
import json
import queue
import threading
from pathlib import Path

import pytest

from app.ml.csi import parse_esp_csi_record
from app.ml.sessions import load_sessions
from tools import collector, recording_archive
from tools.recording_archive import RecordingArchive, RecordingError


class CapturePort:
    """Gateway tap: the archive must not also consume the normal node queue."""

    def __init__(self):
        self.capture = None

    def set_capture(self, callback):
        self.capture = callback

    def readline(self):
        raise AssertionError("A gateway capture tap must not get a second reader")

    def emit(self, line):
        assert self.capture is not None
        self.capture(line)


class UsbPort:
    def __init__(self):
        self.lines = queue.Queue()
        self.returned = 0
        self.drained = threading.Event()
        self.closed = False

    def readline(self):
        if self.returned >= 2:
            # The reader has returned from receive() for both preceding lines.
            self.drained.set()
        try:
            value = self.lines.get(timeout=0.02)
            self.returned += 1
            return value
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.wall = 1_700_000_000.0
        self.mono = 1_000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def line(value):
    return f'CSI_DATA,{value},1a:00:00:00:00:00,-40,"[{value},2,{value},4]"\n'.encode()


def write_frame(writer, t, rssi, raw):
    # Keep full receipt precision so a second timestamp assignment cannot be hidden by rounding.
    writer.writerow([repr(t), rssi, json.dumps(raw)])


def archive(out, ports, **kwargs):
    return RecordingArchive(ports, out, "2026-11-04", parse_esp_csi_record, write_frame, **kwargs)


def records(path):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    rows = {}
    for node, info in manifest["nodes"].items():
        rows[node] = [json.loads(s) for s in (path / info["file"]).read_text(encoding="utf-8").splitlines()]
    return manifest, rows


def events(path):
    return [json.loads(s) for s in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def scene_rows(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(recording_archive.time, "time", lambda: value.wall)
    monkeypatch.setattr(recording_archive.time, "monotonic", lambda: value.mono)
    return value


def test_raw_preserves_preparation_and_scene_boundaries(tmp_path, clock):
    port = CapturePort()
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    with archive(tmp_path, {"RX-A": port}) as rec:
        rec.mark("waiting", scene="first")
        port.emit(line(10))
        clock.advance(0.125)
        rec.mark("countdown", seconds=5)
        port.emit(b"boot message\n")
        clock.advance(0.125)
        rec.start_scene({"RX-A": "first"}, {"RX-A": "walking"}, {"RX-A": first}, 2)
        port.emit(line(11))
        clock.advance(1)
        port.emit(line(12))
        # A late finish call must not extend the labelled interval past its deadline.
        clock.advance(1.5)
        port.emit(line(13))
        counts, elapsed = rec.finish_scene()
        assert counts == {"RX-A": 2}
        assert elapsed >= 2
        rec.mark("waiting", scene="second")
        port.emit(line(14))
        clock.advance(1)
        rec.start_scene({"RX-A": "second"}, {"RX-A": "lying"}, {"RX-A": second}, 2)
        port.emit(line(15))
        rec.finish_scene()
        port.emit(b"\xff\xfe\x00\n")
        path = rec.path

    manifest, raw = records(path)
    assert manifest["kind"] == "raw_capture" and manifest["status"] == "complete"
    assert len(raw["RX-A"]) == 8
    assert [r["scene_id"] for r in raw["RX-A"]] == [None, None, "first", "first", None, None, "second", None]
    assert [r["t"] for r in raw["RX-A"]] == [
        1_700_000_000.0 + offset for offset in (0, 0.125, 0.25, 1.25, 2.75, 2.75, 3.75, 3.75)
    ]
    assert [r["index"] for r in raw["RX-A"]] == sorted({r["index"] for r in raw["RX-A"]})
    assert base64.b64decode(raw["RX-A"][-1]["line_base64"]) == b"\xff\xfe\x00\n"
    for csv_path, sid, values in ((first, "first", [11, 12]), (second, "second", [15])):
        rows = scene_rows(csv_path)
        expected = [r for r in raw["RX-A"] if r["scene_id"] == sid]
        assert [json.loads(r["data"])[0] for r in rows] == values
        assert [float(r["t"]) for r in rows] == [r["t"] for r in expected]
    assert port.capture is None


def test_cancel_and_retry_keep_raw_and_events(tmp_path, clock):
    port = CapturePort()
    with archive(tmp_path, {"RX-A": port}) as rec:
        rec.start_scene({"RX-A": "retry"}, {"RX-A": "walking"}, {"RX-A": tmp_path / "retry.csv"}, 2)
        port.emit(line(20))
        rec.finish_scene(cancelled=True)
        rec.discard_scene({"RX-A": "retry"}, reason="retry")
        clock.advance(1)
        rec.start_scene({"RX-A": "accepted"}, {"RX-A": "walking"}, {"RX-A": tmp_path / "accepted.csv"}, 2)
        port.emit(line(21))
        rec.finish_scene()
        rec.mark("command_stop", reason="user")
        path = rec.path
    _, raw = records(path)
    assert [r["scene_id"] for r in raw["RX-A"]] == ["retry", "accepted"]
    kinds = {e.get("kind", e.get("event")) for e in events(path)}
    assert {"scene_cancelled", "scene_discarded", "scene_start", "scene_end", "command_stop"} <= kinds


def test_raw_capture_is_not_loaded_as_a_labelled_training_session(tmp_path, clock):
    port = CapturePort()
    target = tmp_path / "accepted.csv"
    with archive(tmp_path, {"RX-A": port}) as rec:
        for i in range(25):
            port.emit(line(i))
            clock.advance(0.01)
        rec.start_scene({"RX-A": "accepted"}, {"RX-A": "walking"}, {"RX-A": target}, 2)
        for i in range(25, 50):
            port.emit(line(i))
            clock.advance(0.01)
        rec.finish_scene()
    target.with_suffix(".json").write_text(json.dumps({
        "session_id": "accepted", "node": "RX-A", "label": "walking", "day": "2026-11-04",
    }), encoding="utf-8")
    loaded, problems = load_sessions(tmp_path)
    assert not problems
    assert [s.session_id for s in loaded] == ["accepted"]
    assert loaded[0].t.size == 25


def test_usb_reader_keeps_draining_while_waiting(tmp_path):
    usb = UsbPort()
    with archive(tmp_path, {"RX-A": usb}) as rec:
        rec.mark("waiting")
        usb.lines.put(line(30))
        usb.lines.put(line(31))
        assert usb.drained.wait(2), "Preparation must not leave the serial port unread"
        path = rec.path
    _, raw = records(path)
    assert [r["line"] for r in raw["RX-A"]] == [line(30).decode(), line(31).decode()]
    assert all(r["scene_id"] is None for r in raw["RX-A"])


def test_session_enter_waits_and_retry_preserve_raw_without_reopening_usb(tmp_path, monkeypatch, clock):
    usb, pending, opened, instances = UsbPort(), {}, [], []
    attempts, idle, answers = 0, 0, iter(["", "r", "", "q"])

    def emit(value):
        data = line(value)
        done = pending[data] = threading.Event()
        usb.lines.put(data)
        assert done.wait(2), "The one USB reader must continue during prompts and countdown"

    class ControlledArchive(RecordingArchive):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.in_scene = self.emitted = False
            instances.append(self)

        def receive(self, node, data):
            super().receive(node, data)
            done = pending.pop(data, None)
            if done is not None:
                done.set()

        def start_scene(self, *args, **kwargs):
            nonlocal attempts
            started = super().start_scene(*args, **kwargs)
            attempts += 1
            self.in_scene, self.emitted = True, False
            return started

        def finish_scene(self, *args, **kwargs):
            result = super().finish_scene(*args, **kwargs)
            self.in_scene = False
            return result

        def wait(self, seconds):
            nonlocal idle
            if self.in_scene:
                if not self.emitted:
                    emit(100 + attempts)
                    self.emitted = True
            else:
                emit(60 + idle)
                idle += 1
            clock.advance(seconds)
            self.check()

    def serial_factory(port, baud, timeout):
        opened.append(port)
        return usb

    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        emit(50 + len(prompts))
        clock.advance(0.125)
        return next(answers)

    plan = tmp_path / "plan.csv"
    plan.write_text("scene,description,people,seconds,RX-A\nwalking,one person,1,1,walking\n", encoding="utf-8")
    out = tmp_path / "sessions"
    monkeypatch.setattr(collector, "RecordingArchive", ControlledArchive)
    monkeypatch.setattr(collector, "_serial_factory", serial_factory)
    monkeypatch.setattr(collector, "_ask", ask)
    collector.main(["session", "--plan", str(plan), "--day", "2026-11-04", "--out", str(out),
                    "--node", "RX-A=COMFAKE", "--countdown", "1"])

    assert opened == ["COMFAKE"] and usb.closed
    assert len(prompts) == 4 and len(instances) == 1
    accepted = [p for p in out.glob("*.csv") if p.name != "session_log.csv"]
    assert len(accepted) == 1
    assert [json.loads(r["data"])[0] for r in scene_rows(accepted[0])] == [102]
    manifest, raw = records(instances[0].path)
    assert manifest["status"] == "complete"
    assert len(raw["RX-A"]) == 8  # both preparation prompts, countdowns, attempts, and reviews
    labelled = [r for r in raw["RX-A"] if r["scene_id"] is not None]
    assert len(labelled) == 2 and labelled[0]["scene_id"] != labelled[1]["scene_id"]
    assert not (out / (labelled[0]["scene_id"] + ".csv")).exists()
    assert labelled[1]["scene_id"] == accepted[0].stem
    kinds = {e.get("kind", e.get("event")) for e in events(instances[0].path)}
    assert {"waiting", "countdown", "scene_discarded", "command_stop"} <= kinds


class WriteHook:
    def __init__(self, fh, hook):
        self.fh, self.hook = fh, hook

    def write(self, text):
        self.hook()
        return self.fh.write(text)

    def __getattr__(self, name):
        return getattr(self.fh, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.fh.__exit__(*args)


def hook_raw_writes(monkeypatch, hook):
    real_open = Path.open

    def wrapped(path, *args, **kwargs):
        fh = real_open(path, *args, **kwargs)
        if path.suffix == ".jsonl" and path.name != "events.jsonl":
            return WriteHook(fh, hook)
        return fh

    monkeypatch.setattr(Path, "open", wrapped)


def test_raw_write_failure_is_reported_and_archive_marked_incomplete(tmp_path, monkeypatch):
    port = CapturePort()

    def fail():
        raise OSError("disk full")

    hook_raw_writes(monkeypatch, fail)
    rec = archive(tmp_path, {"RX-A": port})
    with pytest.raises(RecordingError):
        with rec:
            rec.start_scene({"RX-A": "failed"}, {"RX-A": "walking"}, {"RX-A": tmp_path / "failed.csv"}, 2)
            port.emit(line(40))
            rec.finish_scene()
            rec.check()
    manifest = json.loads((rec.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert port.capture is None


def test_bounded_archive_queue_overflow_is_reported(tmp_path, monkeypatch):
    port = CapturePort()
    entered, release = threading.Event(), threading.Event()

    def slow_disk():
        entered.set()
        assert release.wait(3), "Test must release the fake disk writer"

    hook_raw_writes(monkeypatch, slow_disk)
    rec = archive(tmp_path, {"RX-A": port}, queue_size=1)
    try:
        with pytest.raises(RecordingError):
            with rec:
                port.emit(line(50))
                assert entered.wait(2)
                port.emit(line(51))
                try:
                    port.emit(line(52))
                    rec.check()
                finally:
                    release.set()
    finally:
        release.set()
    manifest = json.loads((rec.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"


def test_gateway_capture_bypasses_full_size_normal_queue_and_detaches(tmp_path):
    class QuietHub:
        def __init__(self):
            self.stats = {}
            self.released = 0

        def subscribe(self, port):
            pass

        def unsubscribe(self, port):
            pass

        def release(self):
            self.released += 1

    hub = QuietHub()
    port = collector._GatewayNodePort(hub, "90:70:69:00:00:0a")
    count = port.q.maxsize + 3
    try:
        with archive(tmp_path, {"RX-A": port}) as rec:
            rec.mark("waiting")
            for i in range(count):
                port.offer(f"frame {i}\n".encode())
            assert port.q.qsize() == 0 and port.dropped == 0
            path = rec.path
        manifest, raw = records(path)
        assert manifest["status"] == "complete"
        assert len(raw["RX-A"]) == count
        assert [r["line"] for r in raw["RX-A"]] == [f"frame {i}\n" for i in range(count)]
        assert all(r["scene_id"] is None for r in raw["RX-A"])
        assert port._capture_callback is None
        port.offer(b"regular queue again\n")
        assert port.q.qsize() == 1 and port.readline() == b"regular queue again\n"
    finally:
        port.close()
    assert hub.released == 1


def test_enter_wait_reports_raw_disk_failure_before_user_answers(tmp_path, monkeypatch):
    port = CapturePort()
    blocked, release, finished = threading.Event(), threading.Event(), threading.Event()

    def fail():
        raise OSError("disk full during Enter wait")

    def ask(prompt):
        try:
            blocked.set()
            port.emit(line(70))
            assert release.wait(3), "Failure should interrupt the helper while input stays blocked"
            return ""
        finally:
            finished.set()

    hook_raw_writes(monkeypatch, fail)
    monkeypatch.setattr(collector, "_ask", ask)
    rec = archive(tmp_path, {"RX-A": port})
    try:
        with pytest.raises(RecordingError):
            with rec:
                try:
                    collector._ask_during_capture("Ready? ", rec)
                finally:
                    still_waiting = not finished.is_set()
                    release.set()
                    assert still_waiting, "Disk failure must surface before an answer is available"
    finally:
        release.set()
    assert blocked.is_set() and finished.wait(2)
    manifest = json.loads((rec.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
