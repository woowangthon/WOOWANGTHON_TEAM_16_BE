"""Continuous, unlabelled capture alongside the scene CSVs used for training.

Only the capture readers consume the ports. A bounded FIFO hands received lines to
one disk writer; overflow or disk failure stops capture with an explicit error.
Gateway callbacks never wait for disk I/O. Raw lines survive scene cancellation.
"""

from __future__ import annotations

import base64
import csv
import json
import math
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class RecordingError(RuntimeError):
    """Capture could not preserve all received data."""


@dataclass
class _Scene:
    ids: dict[str, str]
    labels: dict[str, str]
    paths: dict[str, Path]
    started: float
    mono: float
    deadline: float
    counts: dict[str, int]
    files: dict = field(default_factory=dict)
    writers: dict = field(default_factory=dict)
    elapsed: float = 0.0
    finished: bool = False


@dataclass
class _Action:
    kind: str
    payload: object = None
    done: threading.Event = field(default_factory=threading.Event)


class RecordingArchive:
    def __init__(self, ports: dict, out: Path, day: str, parse_record,
                 write_frame, queue_size: int = 20000):
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.ports = dict(ports)
        self.day = day
        self.parse_record = parse_record
        self.write_frame = write_frame
        run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "_" + uuid.uuid4().hex[:8]
        self.path = Path(out) / "_raw" / run
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._receive_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._stop = threading.Event()
        self._writer_done = threading.Event()
        self._error: RecordingError | None = None
        self._entered = False
        self._created = False
        self._closed = False
        self._accepting = False
        self._exit_incomplete = False
        self._index = 0
        self._written = {n: 0 for n in self.ports}
        self._headers = {n: None for n in self.ports}
        self._active: _Scene | None = None
        self._current: _Scene | None = None
        self._raw_files: dict = {}
        self._event_file = None
        self._readers: list[threading.Thread] = []
        self._attached: list = []
        self._writer: threading.Thread | None = None
        self._manifest = {
            "kind": "raw_capture", "status": "recording", "day": day,
            "run_id": run, "started_at": None,
            "nodes": {
                n: {"file": f"{i:02d}_{self._filename(n)}.jsonl"}
                for i, n in enumerate(self.ports, 1)
            },
        }

    @staticmethod
    def _filename(node: str) -> str:
        return re.sub(r"[^\w.-]+", "_", str(node)).strip(".")[:80] or "node"

    @property
    def counts(self) -> dict[str, int]:
        with self._receive_lock:
            return dict(self._current.counts) if self._current else {n: 0 for n in self.ports}

    def _fail(self, message: str, exc: Exception | None = None) -> None:
        with self._error_lock:
            if self._error is None:
                self._error = RecordingError(message + (f": {exc}" if exc else ""))
        self._stop.set()

    def check(self) -> None:
        if self._error is not None:
            raise self._error
        if self._entered and not self._closed and self._writer_done.is_set():
            self._fail("원시 기록 writer가 예기치 않게 종료됐습니다")
            raise self._error

    def __enter__(self):
        if self._entered:
            raise RecordingError("이미 시작한 원시 기록입니다")
        self._entered = True
        try:
            self.path.mkdir(parents=True, exist_ok=False)
            self._created = True
            self._manifest["started_at"] = time.time()
            self._save_manifest()
            for node, entry in self._manifest["nodes"].items():
                self._raw_files[node] = (self.path / entry["file"]).open("x", encoding="utf-8")
            self._event_file = (self.path / "events.jsonl").open("x", encoding="utf-8")
            self._accepting = True
            self._writer = threading.Thread(target=self._write_loop, daemon=True, name="raw-writer")
            self._writer.start()
            for node, port in self.ports.items():
                if callable(getattr(port, "set_capture", None)):
                    # The existing gateway hub remains the single COM reader.
                    self._attached.append(port)
                    port.set_capture(lambda line, n=node: self.receive(n, line))
                else:
                    reader = threading.Thread(target=self._read_usb, args=(node, port),
                                              daemon=True, name=f"raw-reader-{node}")
                    self._readers.append(reader)
                    reader.start()
            self.check()
            return self
        except BaseException as exc:
            self._exit_incomplete = True
            if isinstance(exc, Exception):
                self._fail("원시 기록을 시작하지 못했습니다", exc)
            self._shutdown()
            if self._error is not None:
                raise self._error from exc
            raise

    def _read_usb(self, node, port) -> None:
        try:
            while not self._stop.is_set():
                line = port.readline()
                if line:
                    self.receive(node, line)
                else:
                    self._stop.wait(0.005)
        except Exception as exc:
            self._fail(f"{node} 포트를 읽지 못했습니다", exc)

    def _put(self, item) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self._fail("원시 기록 큐가 가득 찼습니다. 기록을 중단합니다(일부 수신 줄이 저장되지 않았습니다)")
            return False

    def receive(self, node: str, line: bytes) -> None:
        """Port callback: assign time and scene once, enqueue without waiting."""
        with self._receive_lock:
            if not self._accepting or self._error is not None:
                return
            if node not in self.ports:
                self._fail(f"등록되지 않은 노드의 신호를 받았습니다: {node}")
                return
            if not isinstance(line, bytes):
                self._fail(f"{node}: 수신 줄은 bytes여야 합니다")
                return
            wall, mono = time.time(), time.monotonic()
            scene = self._active
            if scene is not None and mono >= scene.deadline:
                scene = None
            self._index += 1
            self._put((node, line, wall, mono, self._index, scene))

    def mark(self, kind: str, **details) -> None:
        self.check()
        with self._receive_lock:
            self._put(_Action("event", {"kind": kind, "t": time.time(),
                                        "mono": time.monotonic(), **details}))
        self.check()

    def wait(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be finite and nonnegative")
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(0.05, remaining))

    def _await(self, action: _Action) -> None:
        while not action.done.wait(0.05):
            self.check()
        self.check()

    def start_scene(self, session_ids: dict[str, str], labels: dict[str, str],
                    csv_paths: dict[str, Path], seconds: float) -> float:
        self.check()
        if not self._entered or self._closed:
            raise RecordingError("원시 기록을 먼저 시작해야 합니다")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("seconds must be finite and positive")
        if any(set(d) != set(self.ports) for d in (session_ids, labels, csv_paths)):
            raise ValueError("scene nodes must match the capture ports")
        with self._receive_lock:
            if self._active is not None:
                raise RecordingError("이미 녹화 중인 장면이 있습니다")
            wall, mono = time.time(), time.monotonic()
            scene = _Scene(dict(session_ids), dict(labels), {n: Path(p) for n, p in csv_paths.items()},
                           wall, mono, mono + seconds, {n: 0 for n in self.ports})
            action = _Action("start", scene)
            if self._put(action):
                self._active = self._current = scene
        self._await(action)
        return wall

    def finish_scene(self, cancelled: bool = False) -> tuple[dict[str, int], float]:
        if self._error is not None:
            # Failure may be published while the writer is still closing its
            # files. Give cleanup a bounded opportunity before propagating it.
            # An error return never promises that a blocked OS write has ended;
            # callers must not unlink derived files after a failed finish.
            self._writer_done.wait(2)
        self.check()
        with self._receive_lock:
            scene = self._active
            if scene is None:
                return self.counts_unlocked(), self._current.elapsed if self._current else 0.0
            self._active = None
            wall, mono = time.time(), time.monotonic()
            scene.elapsed = max(0.0, min(mono, scene.deadline) - scene.mono)
            action = _Action("finish", (scene, cancelled, wall, mono))
            self._put(action)
        self._await(action)
        return self.counts, scene.elapsed

    def counts_unlocked(self) -> dict[str, int]:
        return dict(self._current.counts) if self._current else {n: 0 for n in self.ports}

    def discard_scene(self, session_ids: dict[str, str], reason: str = "retry") -> None:
        self.mark("scene_discarded", session_ids=dict(session_ids), reason=reason)

    def _event(self, event: dict) -> None:
        self._event_file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _write_line(self, item) -> None:
        node, line, wall, mono, index, scene = item
        record = {"index": index, "node": node, "t": wall, "mono": mono,
                  "scene_id": None if scene is None else scene.ids[node]}
        try:
            text = line.decode("utf-8")
            record["line"] = text
        except UnicodeDecodeError:
            record["line_base64"] = base64.b64encode(line).decode("ascii")
            text = line.decode("utf-8", errors="replace")
        # Always preserve the original before trying to parse or label it.
        self._raw_files[node].write(json.dumps(record, ensure_ascii=False) + "\n")
        self._written[node] += 1
        text = text.strip()
        if text.startswith("type,"):
            self._headers[node] = [part.strip() for part in text.split(",")]
            return
        if scene is None or self._error is not None:
            return
        try:
            rec = self.parse_record(text, self._headers[node])
        except (ValueError, TypeError, csv.Error):
            return  # Malformed lines remain in the raw archive.
        if rec is None or len(rec.raw) < 2 or len(rec.raw) % 2:
            return
        self.write_frame(scene.writers[node], wall, rec.rssi, rec.raw)
        with self._receive_lock:
            scene.counts[node] += 1

    def _start_files(self, scene: _Scene) -> None:
        for node, path in scene.paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("x", newline="", encoding="utf-8")
            scene.files[node] = fh
            writer = scene.writers[node] = csv.writer(fh)
            writer.writerow(["t", "rssi", "data"])
        self._event({"kind": "scene_start", "t": scene.started, "mono": scene.mono,
                     "session_ids": scene.ids, "labels": scene.labels,
                     "seconds": scene.deadline - scene.mono})

    @staticmethod
    def _close_scene_files(scene: _Scene) -> None:
        first_error = None
        for fh in scene.files.values():
            try:
                fh.flush()
                fh.close()
            except Exception as exc:
                first_error = first_error or exc
                try:
                    fh.close()
                except Exception:
                    pass
        scene.files.clear()
        scene.writers.clear()
        scene.finished = True
        if first_error is not None:
            raise first_error

    def _flush(self) -> None:
        for fh in self._raw_files.values():
            fh.flush()
        if self._event_file is not None:
            self._event_file.flush()

    def _write_loop(self) -> None:
        opened: _Scene | None = None
        last_flush = time.monotonic()
        try:
            while True:
                if self._error is not None and opened is not None:
                    # Queue overflow is fatal, but previously queued raw lines
                    # can still be saved. Stop touching derived scene files.
                    self._close_scene_files(opened)
                    opened = None
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    self._flush()
                    if self._error is not None:
                        break
                    continue
                if isinstance(item, _Action):
                    try:
                        if item.kind == "stop":
                            break
                        if item.kind == "event":
                            self._event(item.payload)
                        elif item.kind == "start":
                            opened = item.payload
                            self._start_files(opened)
                        elif item.kind == "finish":
                            scene, cancelled, wall, mono = item.payload
                            self._close_scene_files(scene)
                            opened = None
                            self._event({"kind": "scene_cancelled" if cancelled else "scene_end",
                                         "t": wall, "mono": mono,
                                         "session_ids": scene.ids, "counts": dict(scene.counts),
                                         "elapsed_seconds": scene.elapsed})
                            self._flush()
                    except Exception as exc:
                        # Publish failure before waking the caller waiting on this
                        # action; otherwise it could briefly observe success.
                        self._fail("원시 기록을 저장하지 못했습니다", exc)
                        raise
                    finally:
                        item.done.set()
                else:
                    self._write_line(item)
                if time.monotonic() - last_flush >= 1.0:
                    self._flush()
                    last_flush = time.monotonic()
        except Exception as exc:
            self._fail("원시 기록을 저장하지 못했습니다", exc)
        finally:
            if opened is not None:
                try:
                    self._close_scene_files(opened)
                except Exception as exc:
                    self._fail("장면 파일을 닫지 못했습니다", exc)
            for fh in [*self._raw_files.values(), self._event_file]:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception as exc:
                        self._fail("원시 기록 파일을 닫지 못했습니다", exc)
            try:
                self._manifest.update(
                    status="incomplete" if self._error or self._exit_incomplete else "complete",
                    finished_at=time.time(), received_lines=self._index,
                    written_lines=dict(self._written), error=None if self._error is None else str(self._error),
                )
                self._save_manifest()
            except Exception as exc:
                self._fail("원시 기록 manifest를 저장하지 못했습니다", exc)
            self._writer_done.set()

    def _save_manifest(self) -> None:
        # A timed-out writer may later finish. Serialize manifest writes so its
        # final update cannot interleave with the caller's failure update.
        with self._manifest_lock:
            (self.path / "manifest.json").write_text(
                json.dumps(dict(self._manifest), ensure_ascii=False, indent=2), encoding="utf-8")

    def _shutdown(self) -> None:
        self._stop.set()
        with self._receive_lock:
            self._accepting = False
        for port in self._attached:
            try:
                port.set_capture(None)
            except Exception as exc:
                self._fail("게이트웨이 원시 기록을 해제하지 못했습니다", exc)
        self._attached.clear()
        for reader in self._readers:
            reader.join(timeout=2)
            if reader.is_alive():
                self._fail("수신 reader가 종료되지 않았습니다")
        if self._writer is not None and self._writer.is_alive():
            action = _Action("stop")
            for _ in range(50):
                if not self._writer.is_alive():
                    break
                try:
                    self._queue.put(action, timeout=0.1)
                    break
                except queue.Full:
                    continue
            self._writer.join(timeout=5)
            if self._writer.is_alive():
                # Python cannot safely cancel a thread inside an OS write.
                # Leave its handles alone and report an incomplete archive.
                self._fail("원시 기록 writer가 종료되지 않았습니다. 디스크 응답을 기다리고 있어 파일을 삭제하면 안 됩니다")
                self._manifest.update(status="incomplete", error=str(self._error))
                try:
                    self._save_manifest()
                except Exception:
                    pass
        elif self._writer is None:
            # Startup can fail before a writer exists.
            for fh in [*self._raw_files.values(), self._event_file]:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            if self._created:
                self._manifest.update(status="incomplete", error=str(self._error))
                try:
                    self._save_manifest()
                except Exception:
                    pass
        self._closed = True

    def __exit__(self, exc_type, exc, traceback):
        self._exit_incomplete = exc_type is not None
        try:
            if self._active is not None and self._error is None:
                self.finish_scene(cancelled=True)
        finally:
            self._shutdown()
        if self._error is not None:
            raise self._error
        return False
