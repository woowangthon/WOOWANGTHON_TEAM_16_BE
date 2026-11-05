"""ESP32(esp-csi csi_recv 예제) 시리얼 출력을 받아 점검·저장하거나 서버로 보내는 수집 도구.

수신 보드를 USB(시리얼 변환 칩 CH343 쪽 포트)로 노트북에 꽂고, 저장소 루트에서 실행한다.

  # 연결된 시리얼 포트 보기
  python tools/collector.py ports

  # 보드 점검: 10초 동안 받아서 초당 프레임, CSI 길이, 신호 세기를 보고 판정한다
  python tools/collector.py check --port COM5 --port COM6 --port COM7

  # 수집 날 진행: 장면 목록(docs/plans/day_plan.csv)을 따라 노드 전체를 장면마다 녹화한다
  python tools/collector.py session --plan docs/plans/day_plan.csv --day 2026-11-01 \
      --out ../data/sessions/2026-11-01 --node RX-301=COM5 --node RX-302=COM6 --node RX-COR=COM7

  # 장면 하나만 직접 녹화. 라벨은 app/ml/sessions.py의 LABELS 중 하나이고,
  # '그 노드 구역에서 일어난 일'로 노드마다 따로 붙인다(docs/DATA_COLLECTION.md)
  python tools/collector.py record --day 2026-11-01 --seconds 150 --out ../data/sessions/2026-11-01 \
      --rx COM5:RX-301:lying --rx COM6:RX-302:neighbor --rx COM7:RX-COR:neighbor

  # 실시간 전송(화재 상황·보정 때). 노드 토큰은 건물 설정 화면에서 발급
  python tools/collector.py live --port COM5 --api http://localhost:8000 --token <노드토큰>

  # 녹화 파일을 노드인 것처럼 서버에 다시 보내기(개발용)
  python tools/collector.py send --file ../data/sessions/2026-11-01/xxx.csv --api http://localhost:8000 --token <노드토큰>

무선 노드(firmware/csi_node)를 게이트웨이(firmware/csi_gateway)로 받을 때는 포트 자리에 gw:<노드 MAC>을 쓰고
--gateway로 게이트웨이의 USB 포트를 알려 준다. USB로 꽂은 노드(COM5 등)와 섞어 써도 된다.

  # 게이트웨이에 들어오는 노드 목록과 무선 손실 보기
  python tools/collector.py gateway --gateway COM7

  # nodes.csv에 이름표와 MAC을 적어 두면 MAC 대신 이름을 쓴다(gw:RX-301, 또는 --node RX-301=gw)
  python tools/collector.py check --gateway COM7 --port gw:RX-301 --port gw:RX-302 --port gw:RX-COR

  # 비교표: 녹화 폴더의 장면마다 그날 빈 방 대비 변화(보드 자리·기준선 점검)
  python tools/collector.py compare --data ../data/sessions/2026-11-04

  python tools/collector.py check --gateway COM7 --port gw:90:70:69:f7:fb:ec --port gw:90:70:69:aa:bb:cc
  python tools/collector.py session --gateway COM7 --plan docs/plans/day_plan.csv --day 2026-11-04 \
      --out ../data/sessions/2026-11-04 --node RX-301=gw:90:70:69:f7:fb:ec --node RX-302=gw:... --node RX-COR=gw:...
  python tools/collector.py live --gateway COM7 --api https://<서버> \
      --port gw:<RX-301 MAC> --token <RX-301 토큰> --port gw:<RX-302 MAC> --token <RX-302 토큰>

필요 패키지: pyserial (requirements-dev.txt에 있다). 나머지는 backend requirements와 같다.
"""

from __future__ import annotations

import argparse
import binascii
import csv
import json
import os
import queue as queue_mod
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ml.csi import parse_esp_csi_record  # noqa: E402
from app.ml.sessions import LABELS  # noqa: E402
from tools.recording_archive import RecordingArchive, RecordingError  # noqa: E402

DEFAULT_BAUD = 921600  # csi_recv 예제의 sdkconfig.defaults(CONFIG_ESP_CONSOLE_UART_BAUDRATE)
SEND_MAC = "1a:00:00:00:00:00"  # csi_send 예제가 쓰는 송신 MAC. csi_recv는 이 MAC의 패킷만 출력한다
MIN_HZ = 10.0
TX_MIN_HZ = 50.0  # TX-1은 초당 100개를 보낸다. 11/1 보조배터리로 켰을 때 약 23개로 떨어졌다
# 받은 수 ÷ 송신 수. 무선 손실 숫자는 노드가 보내기 직전부터만 세서, 노드 안에서 빠진 것은 여기서만 보인다.
# 11/3 R06 RX-302는 손실 0.2%였지만 송신 91Hz 중 52Hz만 받았다. 프레임이 줄면 특징이 커져 빈 방도 감지로 바뀐다
RX_TX_MIN_RATIO = 0.7
NODES_FILE = Path(__file__).resolve().parent.parent / "nodes.csv"


def _serial_factory(port: str, baud: int, timeout: float):
    """실제 시리얼 포트를 연다. 테스트에서 가짜 포트로 바꿔 끼운다."""
    try:
        import serial  # type: ignore
    except ImportError:
        sys.exit("pyserial이 필요합니다: pip install pyserial")
    try:
        return serial.Serial(port, baud, timeout=timeout)
    except serial.SerialException as exc:  # type: ignore[attr-defined]
        sys.exit(f"{port}를 열 수 없습니다: {exc}\n"
                 "포트 이름이 맞는지, idf.py monitor나 다른 터미널이 이 포트를 쓰고 있지 않은지 확인하세요.")


def _open_serial(port: str, baud: int):
    """USB로 꽂은 보드(COM5, /dev/ttyUSB0)나 게이트웨이를 거친 무선 노드(gw:<MAC>)를 연다.

    둘 다 readline()이 csi_recv와 같은 CSI_DATA 줄을 돌려주므로, 점검·녹화·실시간 전송 코드는 구분하지 않는다.
    """
    if port.lower().startswith("gw:"):
        return _gateway_node_port(port[3:])
    return _serial_factory(port, baud, 1)


# ---------------------------------------------------------------- 게이트웨이(무선 노드)
# firmware/common/wm_proto.h와 같아야 한다.

GW_BAUD = 2_000_000
GW_SYNC = b"\xa5\x5a"
GW_TYPE_CSI = 0x01
GW_TYPE_STATUS = 0x02
GW_MAX_PAYLOAD = 1600
GW_ITEM_HEAD = 8  # 노드 MAC(6) + 게이트웨이에서 잰 rssi(1) + 예약(1)
GW_CSI_PKT = struct.Struct("<4sHHIIfbbBBBBBB")  # wm_csi_pkt_t 헤더 28바이트
GW_CSI_MAGIC = b"WMC1"
GW_SEQ_RESET = 1000  # 노드 번호가 이만큼 안쪽으로 뒤로 가면 이미 받은 것(중복·순서 바뀜)으로 보고 세지 않는다
# 노드가 다시 켜지면 번호와 보드 시각(local_ts, 켜진 뒤 µs)이 함께 처음으로 돌아간다. 시각만 보고 판단하면
# 약 72분마다 오는 시각 되감김을, 번호만 보고 판단하면 오래 끊겼다 돌아온 것을 재시작으로 잘못 센다.
GW_BOOT_TS_MAX = 600_000_000  # 다시 켜진 노드의 첫 패킷은 켜진 지 10분 안의 시각이어야 한다

_GATEWAY = {"port": os.environ.get("WATCHMATE_GATEWAY", ""), "baud": GW_BAUD}


def gw_crc(data: bytes) -> int:
    """CRC16 CCITT-FALSE(다항식 0x1021, 초기값 0xFFFF)."""
    return binascii.crc_hqx(data, 0xFFFF)


def gw_frame(ftype: int, payload: bytes) -> bytes:
    """게이트웨이가 USB로 보내는 묶음을 만든다(테스트와 문서용; 게이트웨이 펌웨어와 같은 형식)."""
    head = bytes([ftype]) + struct.pack("<H", len(payload))
    return GW_SYNC + head + payload + struct.pack("<H", gw_crc(head + payload))


class GatewayFrameReader:
    """바이트를 받아 (type, payload) 묶음을 꺼낸다. 깨진 묶음은 건너뛰고 다음 동기 바이트부터 다시 찾는다."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_crc = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out: list[tuple[int, bytes]] = []
        buf = self.buf
        i = 0
        while True:
            j = buf.find(GW_SYNC, i)
            if j < 0:
                i = max(len(buf) - 1, i)  # 마지막 바이트가 동기의 앞 절반일 수 있다
                break
            if len(buf) - j < 5:
                i = j
                break
            n = buf[j + 3] | (buf[j + 4] << 8)
            if n > GW_MAX_PAYLOAD:
                i = j + 1
                continue
            end = j + 5 + n + 2
            if len(buf) < end:
                i = j
                break
            body = bytes(buf[j + 2 : j + 5 + n])
            crc = buf[end - 2] | (buf[end - 1] << 8)
            if gw_crc(body) != crc:
                self.bad_crc += 1
                i = j + 1
                continue
            out.append((body[0], body[3:]))
            i = end
        del buf[:i]
        return out


def _norm_mac(text: str) -> str:
    hexs = "".join(ch for ch in text.lower() if ch in "0123456789abcdef")
    if len(hexs) != 12:
        sys.exit(f"MAC 주소 형식이 아닙니다: {text} (예: 90:70:69:f7:fb:ec)")
    return ":".join(hexs[k : k + 2] for k in range(0, 12, 2))


def gw_decode_csi(payload: bytes) -> dict | None:
    """게이트웨이 CSI 묶음 → 노드 MAC, 노드 패킷 번호, csi_recv와 같은 값의 CSI 배열."""
    import numpy as np

    if len(payload) < GW_ITEM_HEAD + GW_CSI_PKT.size:
        return None
    mac = ":".join(f"{b:02x}" for b in payload[:6])
    gw_rssi = struct.unpack("<b", payload[6:7])[0]
    (magic, node_seq, n, tx_id, local_ts, gain, rssi, noise_floor, channel, secondary, cwb, sig_mode,
     first_word, _res) = GW_CSI_PKT.unpack_from(payload, GW_ITEM_HEAD)
    if magic != GW_CSI_MAGIC:
        return None
    data = payload[GW_ITEM_HEAD + GW_CSI_PKT.size :]
    if len(data) != n:
        return None
    # csi_recv의 (int16_t)(compensate_gain * info->buf[i])와 같게: float32 곱셈 후 0 쪽으로 자르기
    raw = np.frombuffer(data, dtype=np.int8).astype(np.float32) * np.float32(gain)
    values = raw.astype(np.int16).tolist()
    return {"mac": mac, "gw_rssi": gw_rssi, "node_seq": node_seq, "tx_id": tx_id, "local_ts": local_ts,
            "rssi": rssi, "noise_floor": noise_floor, "channel": channel, "secondary": secondary, "cwb": cwb,
            "sig_mode": sig_mode, "first_word": first_word, "values": values}


def gw_csi_line(d: dict) -> str:
    """esp-csi csi_recv(ESP32-S3)가 USB로 찍는 것과 같은 필드 순서의 CSI_DATA 줄."""
    head = [d["tx_id"], SEND_MAC, d["rssi"], 0, d["sig_mode"], 0, d["cwb"], 0, 0, 0, 0, 0, 0,
            d["noise_floor"], 0, d["channel"], d["secondary"], d["local_ts"], 0, 0, d["sig_mode"],
            len(d["values"]), d["first_word"]]
    return "CSI_DATA," + ",".join(map(str, head)) + ',"[' + ",".join(map(str, d["values"])) + ']"'


class _NodeStats:
    def __init__(self) -> None:
        self.frames = 0
        self.lost = 0
        self.resets = 0  # 노드가 다시 켜진 횟수. 이득 보정 기준도 다시 잡힌다
        self.last_seq: int | None = None
        self.last_ts = 0
        self.gw_rssi: list[int] = []
        self.lengths: Counter = Counter()
        self.first_t = time.time()
        self.last_t = 0.0

    def add(self, d: dict) -> None:
        seq, ts = d["node_seq"], d["local_ts"]
        if self.last_seq is not None:
            gap = (seq - self.last_seq - 1) % 65536
            # 시각이 처음으로 돌아갔고, 번호도 이어지지 않거나 시각 되감김 직전이 아니었으면 다시 켜진 것
            if ts < self.last_ts and ts < GW_BOOT_TS_MAX and (
                    gap >= GW_SEQ_RESET or self.last_ts < 2**32 - GW_BOOT_TS_MAX):
                self.resets += 1
            elif gap >= 65536 - GW_SEQ_RESET:
                return  # 이미 받은 번호
            else:
                self.lost += gap  # 오래 끊겼다 돌아와도 다시 켜진 것이 아니면 잃은 것으로 센다
        self.last_seq, self.last_ts = seq, ts
        self.frames += 1
        self.gw_rssi.append(d["gw_rssi"])
        del self.gw_rssi[:-500]
        self.lengths[len(d["values"])] += 1
        self.last_t = time.time()


class _GatewayHub:
    """게이트웨이 USB 포트 하나를 읽어 노드(MAC)별로 CSI_DATA 줄을 나눠 준다."""

    hubs: dict[str, "_GatewayHub"] = {}
    lock = threading.Lock()

    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.ser = _serial_factory(port, baud, 0.2)
        self.reader = GatewayFrameReader()
        self.subs: dict[str, list["_GatewayNodePort"]] = {}
        self.stats: dict[str, _NodeStats] = {}
        self.status = ""
        self.frames_total = 0
        self.users = 0
        self.stop = threading.Event()
        self._sub_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @classmethod
    def get(cls, port: str, baud: int) -> "_GatewayHub":
        with cls.lock:
            hub = cls.hubs.get(port)
            if hub is None:
                hub = cls.hubs[port] = cls(port, baud)
            hub.users += 1
            return hub

    def release(self) -> None:
        with _GatewayHub.lock:
            self.users -= 1
            if self.users > 0:
                return
            _GatewayHub.hubs.pop(self.port, None)
        self.stop.set()
        self.thread.join(timeout=2)
        try:
            self.ser.close()
        except Exception:
            pass

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                chunk = self.ser.read(4096)
            except Exception as exc:  # 케이블이 빠지는 등
                self.status = f"게이트웨이 포트를 읽지 못했습니다: {exc}"
                time.sleep(0.5)
                continue
            if not chunk:
                continue
            for ftype, payload in self.reader.feed(chunk):
                if ftype == GW_TYPE_STATUS:
                    self.status = payload.decode("utf-8", errors="replace")
                    continue
                if ftype != GW_TYPE_CSI:
                    continue
                d = gw_decode_csi(payload)
                if d is None:
                    continue
                self.frames_total += 1
                self.stats.setdefault(d["mac"], _NodeStats()).add(d)
                with self._sub_lock:
                    targets = list(self.subs.get(d["mac"], ()))
                if targets:
                    line = (gw_csi_line(d) + "\n").encode()
                    for t in targets:
                        t.offer(line)

    def subscribe(self, port: "_GatewayNodePort") -> None:
        with self._sub_lock:
            self.subs.setdefault(port.mac, []).append(port)

    def unsubscribe(self, port: "_GatewayNodePort") -> None:
        with self._sub_lock:
            lst = self.subs.get(port.mac, [])
            if port in lst:
                lst.remove(port)


class _GatewayNodePort:
    """무선 노드 하나를 시리얼 포트처럼 쓰게 해 준다(readline, reset_input_buffer, close)."""

    def __init__(self, hub: _GatewayHub, mac: str) -> None:
        self.hub, self.mac = hub, mac
        self.q: "queue_mod.Queue[bytes]" = queue_mod.Queue(maxsize=5000)
        self.closed = False
        self.dropped = 0
        self._capture_callback = None
        self._capture_lock = threading.Lock()
        self._resets_at_open = self._resets()
        hub.subscribe(self)

    def offer(self, line: bytes) -> None:
        """허브 스레드가 부른다. 기다리지 않는다: 큐가 차면 가장 오래된 줄을 버리고 넣는다.

        session이 장면 사이에 Enter를 기다리는 동안 아무도 큐를 읽지 않는다. 여기서 허브가 기다리면
        게이트웨이 포트 읽기가 통째로 멈추고, 다음 장면 앞에 오래된 프레임이 지금 시각으로 섞인다.
        """
        with self._capture_lock:
            capture = self._capture_callback
        if capture is not None:
            # record/session 원본은 버림 가능한 표시용 대기줄을 거치기 전에 받는다.
            # 콜백은 디스크에 쓰지 않고 전용 writer로 넘기므로 허브를 막지 않는다.
            capture(line)
            return
        while True:
            try:
                self.q.put_nowait(line)
                return
            except queue_mod.Full:
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                except queue_mod.Empty:
                    pass

    def set_capture(self, callback) -> None:
        """녹화 명령의 연속 원본 수신기를 연결한다. 실제 COM 포트를 다시 열지 않는다."""
        with self._capture_lock:
            self._capture_callback = callback
        self.reset_input_buffer()

    def _resets(self) -> int:
        st = self.hub.stats.get(self.mac)
        return st.resets if st else 0

    def restarts_since_open(self) -> int:
        """이 포트를 연 뒤 노드가 다시 켜진 횟수."""
        return self._resets() - self._resets_at_open

    def readline(self) -> bytes:
        try:
            return self.q.get(timeout=1)
        except queue_mod.Empty:
            return b""

    def reset_input_buffer(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue_mod.Empty:
                return

    def link_counts(self) -> tuple[int, int]:
        st = self.hub.stats.get(self.mac)
        return (st.frames, st.lost) if st else (0, 0)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.hub.unsubscribe(self)
            self.hub.release()


_NODES = {"path": Path(os.environ.get("WATCHMATE_NODES", str(NODES_FILE)))}


def _is_mac(text: str) -> bool:
    hexs = "".join(ch for ch in text.lower() if ch in "0123456789abcdef")
    return len(hexs) == 12 and all(ch in "0123456789abcdefABCDEF:-" for ch in text)


def load_node_names(path: Path | None = None) -> dict[str, str]:
    """nodes.csv(node,mac)를 읽어 {이름표: MAC}을 돌려준다. MAC이 빈 줄은 아직 안 적은 것으로 본다.

    #으로 시작하는 줄은 설명이다. 같은 이름이나 같은 MAC이 두 번 나오면 멈춘다(라벨이 엉뚱한 노드에 붙는 것을 막는다).
    """
    path = Path(path or _NODES["path"])
    if not path.exists():
        return {}
    names: dict[str, str] = {}
    lines = [ln for ln in _read_text(path).splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for i, row in enumerate(csv.DictReader(lines), 2):
        name = (row.get("node") or "").strip()
        mac = (row.get("mac") or "").strip()
        if not name:
            continue
        if name in names:
            sys.exit(f"{path.name}에 {name}가 두 번 있습니다.")
        if not mac:
            continue
        if not _is_mac(mac):
            sys.exit(f"{path.name}의 {name} MAC '{mac}'이 MAC 형식이 아닙니다(예: 90:70:69:f7:fb:ec).")
        mac = _norm_mac(mac)
        if mac in names.values():
            other = next(n for n, m in names.items() if m == mac)
            sys.exit(f"{path.name}에서 {other}와 {name}의 MAC이 같습니다({mac}). 이름표를 다시 확인하세요.")
        names[name] = mac
    return names


def resolve_gw(target: str) -> str:
    """gw: 뒤의 글자(MAC 또는 nodes.csv의 이름표)를 MAC으로 바꾼다."""
    if _is_mac(target):
        return _norm_mac(target)
    names = load_node_names()
    if target in names:
        return names[target]
    path = _NODES["path"]
    if not path.exists():
        sys.exit(f"gw:{target}의 MAC을 찾을 nodes.csv가 없습니다({path}). MAC을 직접 쓰거나 nodes.csv를 만드세요.")
    sys.exit(f"{path.name}에 {target}의 MAC이 없습니다. 노드에 펌웨어를 올릴 때 나온 MAC을 적어 주세요.")


def node_port(node: str, port: str) -> str:
    """--node 노드=포트의 포트를 정리한다. 'gw'만 쓰면 그 노드 이름표로 찾는다.

    gw:<다른 이름표>나 nodes.csv에서 다른 이름표의 MAC을 주면 멈춘다. 장면 라벨이 엉뚱한 노드 파일에 붙는 것을 막는다.
    """
    if port.lower() == "gw":
        return f"gw:{node}"
    if port.lower().startswith("gw:"):
        target = port[3:]
        if _is_mac(target):
            mac = _norm_mac(target)
            owner = next((n for n, m in load_node_names().items() if m == mac), None)
            if owner is not None and owner != node:
                sys.exit(f"{node}에 {owner}의 MAC({mac})을 붙였습니다. nodes.csv와 명령의 이름표를 확인하세요.")
        elif target != node:
            sys.exit(f"{node}에 gw:{target}을 붙였습니다. 같은 이름표를 쓰거나 --node {node}=gw 로 쓰세요.")
    return port


def _gateway_node_port(target: str) -> _GatewayNodePort:
    if not _GATEWAY["port"]:
        sys.exit("무선 노드(gw:...)를 쓰려면 게이트웨이의 USB 포트를 --gateway로 알려 주세요(예: --gateway COM7).")
    mac = resolve_gw(target)
    hub = _GatewayHub.get(_GATEWAY["port"], _GATEWAY["baud"])
    return _GatewayNodePort(hub, mac)


def _close_all_gateways() -> None:
    for hub in list(_GatewayHub.hubs.values()):
        hub.users = 1
        hub.release()


def _write_frame(writer, t: float, rssi, raw: list[int]) -> None:
    writer.writerow([f"{t:.4f}", "" if rssi is None else rssi, "[" + ",".join(map(str, raw)) + "]"])


def _lines(ser, deadline: float | None = None, stop: threading.Event | None = None):
    """시리얼에서 CSI 줄만 골라 (수신 시각, rssi, raw)를 낸다. 헤더 줄이 오면 기억해 둔다.

    deadline(time.time() 기준)이 지나거나 stop이 켜지면 끝낸다.
    보드가 멈춰 줄이 안 와도 readline 타임아웃(1초)마다 확인한다.
    """
    header = None
    while True:
        if deadline is not None and time.time() >= deadline:
            return
        if stop is not None and stop.is_set():
            return
        raw_line = ser.readline()
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="ignore").strip()
        if line.startswith("type,"):
            header = [h.strip() for h in line.split(",")]
            continue
        rec = parse_esp_csi_record(line, header)
        if rec is None or len(rec.raw) < 2 or len(rec.raw) % 2:
            continue
        yield time.time(), rec.rssi, rec.raw


def _post(api: str, token: str, frames: list[dict], source: str = "live", restarted: bool = False) -> dict:
    payload: dict = {"frames": frames, "source": source}
    if restarted:
        payload["restarted"] = True  # 지난 묶음 뒤 노드가 다시 켜졌다
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        api.rstrip("/") + "/api/ingest/csi", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Node-Token": token},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def cmd_ports(_args) -> None:
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        sys.exit("pyserial이 필요합니다: pip install pyserial")
    ports = list(list_ports.comports())
    if not ports:
        print("연결된 시리얼 포트가 없습니다. 데이터 케이블인지, 드라이버(CH343)가 깔렸는지 확인하세요.")
    for p in ports:
        # 윈도우 기본 드라이버는 CH343을 'USB 직렬 장치'로 보여 주므로 제조사 번호(WCH 1A86)로도 알아본다
        is_ch34 = "CH34" in (p.description or "") or getattr(p, "vid", None) == 0x1A86
        hint = "  ← 보드의 CH343 포트(수집·게이트웨이에 쓰는 쪽)" if is_ch34 else ""
        print(f"{p.device}\t{p.description}{hint}")


# ---------------------------------------------------------------- 점검(check)

def _scan(ser, seconds: float) -> dict:
    """seconds 동안 줄을 읽어 종류별로 센다. 판정은 _verdict가 한다."""
    st = {"seconds": seconds, "lines": 0, "csi": 0, "bad": 0, "logs": 0, "garbage": 0, "header": False,
          "lengths": Counter(), "rssi": [], "macs": Counter(), "channels": Counter(), "seq": []}
    header = None
    ser.reset_input_buffer()
    deadline = time.time() + seconds
    while time.time() < deadline:
        b = ser.readline()
        if not b:
            continue
        st["lines"] += 1
        text = b.decode("utf-8", errors="replace")
        if text.count("�") > max(3, len(text) // 20):
            st["garbage"] += 1
            continue
        line = text.strip()
        if line.startswith("type,"):
            header = [h.strip() for h in line.split(",")]
            st["header"] = True
            continue
        if "CSI_DATA" not in line:
            st["logs"] += 1
            continue
        rec = parse_esp_csi_record(line, header)
        if rec is None or len(rec.raw) < 2 or len(rec.raw) % 2:
            st["bad"] += 1
            continue
        st["csi"] += 1
        st["lengths"][len(rec.raw)] += 1
        if rec.rssi is not None:
            st["rssi"].append(rec.rssi)
        if rec.mac:
            st["macs"][rec.mac.lower()] += 1
        if rec.channel is not None:
            st["channels"][rec.channel] += 1
        if rec.seq is not None:
            st["seq"].append(rec.seq)
    return st


def _verdict(st: dict, baud: int) -> tuple[bool, list[str]]:
    """점검 결과를 사람이 읽을 줄로 바꾼다. 첫 값은 수집에 써도 되는지."""
    out: list[str] = []
    if st["csi"] == 0:
        if st["garbage"]:
            out.append(f"글자가 깨져 들어옵니다. 보드레이트가 다를 수 있습니다(지금 {baud}, csi_recv는 921600).")
        elif st["lines"] == 0:
            out.append("아무 출력이 없습니다. CH343 포트가 맞는지, csi_recv를 올렸는지 보고 RST 버튼을 한 번 눌러 보세요.")
        else:
            out.append("보드는 켜져 있지만 CSI_DATA 줄이 없습니다. 송신 보드(TX-1)가 켜져 있는지, "
                       "이 보드에 csi_send가 아니라 csi_recv가 올라갔는지 확인하세요.")
        return False, out
    ok = True
    hz = st["csi"] / st["seconds"]
    tx = ""
    if len(st["seq"]) > 1:
        span = max(st["seq"]) - min(st["seq"]) + 1
        tx = f" (송신 보드 약 {span / st['seconds']:.0f} Hz)"
    out.append(f"CSI {st['csi']}줄 / {st['seconds']:.0f}초 = {hz:.1f} Hz{tx}")
    if len(st["seq"]) > 10:
        tx_hz = (max(st["seq"]) - min(st["seq"]) + 1) / st["seconds"]
        if tx_hz < TX_MIN_HZ:
            ok = False
            out.append(f"  경고: 송신 보드(TX-1)가 초당 약 {tx_hz:.0f}개만 보내고 있습니다(정상 약 100). "
                       "TX-1을 보조배터리로 켰다면 충전기나 노트북 USB로 바꾸고 다시 점검하세요.")
        else:
            ratio = hz / tx_hz
            out.append(f"송신 대비 받은 비율 {ratio:.0%}")
            if ratio < RX_TX_MIN_RATIO:
                ok = False
                out.append(f"  경고: 송신 보드가 보낸 것의 {ratio:.0%}만 받고 있습니다(기준 {RX_TX_MIN_RATIO:.0%}). "
                           "무선 손실 숫자에는 안 잡히는 누락입니다. 게이트웨이를 노드 쪽으로 옮기거나 "
                           "그 노드만 USB 케이블로 꽂아 다시 점검하세요.")
    if hz < MIN_HZ:
        ok = False
        out.append(f"  경고: 초당 {MIN_HZ:.0f}줄보다 적습니다. 송신 보드 전원과 거리, USB 허브를 확인하세요.")
    length, n = st["lengths"].most_common(1)[0]
    share = n / st["csi"]
    out.append(f"값 개수 {length}(서브캐리어 {length // 2})" + (" — 모든 줄 같음" if share == 1 else f" — {share:.0%}가 같음"))
    if share < 0.95:
        ok = False
        out.append("  경고: 길이가 다른 줄이 섞였습니다. 같은 커밋·같은 설정으로 펌웨어를 다시 올리세요.")
    if st["rssi"]:
        mean = statistics.fmean(st["rssi"])
        out.append(f"RSSI 평균 {mean:.0f} dBm (최저 {min(st['rssi']):.0f}, 최고 {max(st['rssi']):.0f})")
        if mean < -75:
            ok = False
            out.append("  경고: 신호가 약합니다. 거리를 줄이고 안테나 쪽을 손이나 금속으로 가리지 마세요.")
    if st["channels"]:
        out.append(f"채널 {st['channels'].most_common(1)[0][0]}")
    if st["bad"] > 0.05 * (st["csi"] + st["bad"]):
        ok = False
        out.append(f"  경고: 잘린 줄이 {st['bad']}개입니다. USB 허브를 빼고 노트북에 바로 꽂거나 다른 프로그램을 닫으세요.")
    elif st["bad"]:
        out.append(f"잘린 줄 {st['bad']}개(버림)")
    return ok, out


def cmd_check(args) -> None:
    ports = args.port
    if not ports:
        sys.exit("--port로 점검할 포트를 하나 이상 주세요(예: --port COM5 --port COM6).")
    sers = {p: _open_serial(p, args.baud) for p in ports}
    print(f"{len(ports)}개 포트를 {args.seconds:.0f}초 동안 읽습니다. 송신 보드(TX-1)는 켜 두세요.")
    results: dict[str, dict] = {}

    def worker(port: str) -> None:
        ser = sers[port]
        before = ser.link_counts() if hasattr(ser, "link_counts") else None
        results[port] = _scan(ser, args.seconds)
        if before is not None:
            got, lost = (a - b for a, b in zip(ser.link_counts(), before))
            results[port]["link"] = (got, lost)

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in ports]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    for ser in sers.values():
        ser.close()
    all_ok = True
    for p in ports:
        ok, lines = _verdict(results[p], args.baud)
        if "link" in results[p]:
            got, lost = results[p]["link"]
            rate = lost / max(got + lost, 1)
            lines.append(f"무선 전송: 게이트웨이가 {got}건 받음, 잃은 것 {lost}건({rate:.1%})")
            if got + lost and rate > 0.05:
                ok = False
                lines.append("  경고: 무선으로 5% 넘게 잃었습니다. 게이트웨이를 노드 쪽으로 옮기거나, "
                             "그 노드만 USB 케이블로 꽂아 포트를 COM 번호로 바꾸세요.")
            if results[p]["csi"] == 0 and got == 0:
                lines.append("  게이트웨이가 이 노드 MAC을 못 받고 있습니다. gateway 명령으로 들어오는 MAC 목록을 확인하세요.")
        all_ok &= ok
        print(f"\n[{p}] {'정상' if ok else '확인 필요'}")
        for line in lines:
            print(f"  {line}")
    if not all_ok:
        sys.exit(1)


# ---------------------------------------------------------------- 녹화(record, session)

def _parse_rx(spec: str) -> tuple[str, str, str]:
    """'포트:노드:라벨' (예: COM5:RX-301:lying) → (포트, 노드, 라벨)."""
    parts = spec.rsplit(":", 2)
    if len(parts) != 3 or not all(p.strip() for p in parts):
        sys.exit(f"--rx는 포트:노드:라벨 형식입니다(예: COM5:RX-301:lying). 받은 값: {spec}")
    port, node, label = (p.strip() for p in parts)
    return port, node, label


def _record_targets(args) -> list[tuple[str, str, str]]:
    if args.rx:
        if args.port or args.node or args.label:
            sys.exit("--rx를 쓸 때는 --port/--node/--label을 함께 쓰지 않습니다.")
        targets = [_parse_rx(s) for s in args.rx]
    else:
        if not (args.port and args.node and args.label):
            sys.exit("--port, --node, --label을 모두 주거나, --rx 포트:노드:라벨 을 노드마다 하나씩 주세요.")
        targets = [(args.port, args.node, args.label)]
    targets = [(node_port(node, port), node, label) for port, node, label in targets]
    for _, node, label in targets:
        if label not in LABELS:
            sys.exit(f"{node}의 라벨 '{label}'은 쓸 수 없습니다. 다음 중 하나: {', '.join(sorted(LABELS))}")
    nodes = [n for _, n, _ in targets]
    if len(set(nodes)) != len(nodes):
        sys.exit("같은 노드 이름이 두 번 들어갔습니다.")
    return targets


def _record_scene(sers: dict, labels: dict[str, str], seconds: float, countdown: int, out: Path, day: str,
                  meta_extra: dict, capture: RecordingArchive) -> dict[str, tuple[Path, int, float]]:
    """연속 원본에서 지금 장면 구간만 기존 학습 CSV로 함께 기록한다."""
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S") + "_" + uuid4().hex[:8]
    nodes = list(labels)
    desc = ", ".join(f"{n}={labels[n]}" for n in nodes)
    print(f"[{desc}] {countdown}초 뒤 {seconds:.0f}초 동안 녹화합니다. 자세를 잡아 주세요.")
    capture.mark("countdown", labels=labels, seconds=countdown)
    capture.wait(countdown)
    saved: dict[str, tuple[Path, int, float]] = {}
    sids = {n: f"{day}_{n}_{labels[n]}_{stamp}" for n in nodes}
    paths = {n: out / f"{sids[n]}.csv" for n in nodes}
    started = capture.start_scene(sids, labels, paths, seconds)
    mono_start = time.monotonic()
    try:
        while (elapsed := time.monotonic() - mono_start) < seconds:
            counts = capture.counts
            status = "  ".join(f"{n} {counts.get(n, 0)}개({counts.get(n, 0) / max(elapsed, 1e-6):.0f}Hz)" for n in nodes)
            print(f"  {min(elapsed, seconds):5.1f}초  {status}", end="\r")
            capture.wait(min(0.5, seconds - elapsed))
        counts, took = capture.finish_scene()
    except BaseException:
        files_closed = False
        try:
            capture.finish_scene(cancelled=True)
            files_closed = True
        except RecordingError:
            pass
        for sid in sids.values():
            # 실패한 writer가 아직 파일을 소유할 수 있다. 메타데이터가 없으면 학습에 들어가지 않는다.
            if files_closed:
                (out / f"{sid}.csv").unlink(missing_ok=True)
            (out / f"{sid}.json").unlink(missing_ok=True)
        print("\n녹화를 멈췄습니다. 미완성 장면은 학습에서 제외하며 연속 원본은 남깁니다.")
        raise
    print()
    for node in nodes:
        sid, path, n = sids[node], paths[node], counts[node]
        restarts = sers[node].restarts_since_open() if hasattr(sers[node], "restarts_since_open") else 0
        meta = {"session_id": sid, "node": node, "label": labels[node], "day": day, "started_at": started,
                "frames": n, "seconds": took, "recorded_with": nodes, "node_restarts": restarts,
                "raw_archive": str(capture.path.relative_to(out)), **meta_extra}
        path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        saved[node] = (path, n, took)
        print(f"저장: {path.name} ({n}프레임, {n / max(took, 1e-6):.0f}Hz)")
        if n < seconds * MIN_HZ:
            print(f"  경고: {node}의 초당 프레임이 {MIN_HZ:.0f}개보다 적습니다. 송신 보드·거리·USB 연결을 확인하세요.")
        if restarts:
            print(f"  경고: {node}가 이 수집을 시작한 뒤 다시 켜졌습니다(전원이 잠깐 빠졌을 수 있음). "
                  "보드가 신호 크기 기준을 다시 잡으므로, 명령을 다시 시작해 그날 start_empty부터 다시 찍으세요.")
    return saved


def cmd_record(args) -> None:
    """장면 하나 녹화. 수신 노드 여러 개를 한 번에 같은 시각으로 녹화할 수 있다.

    라벨은 노드마다 '그 노드 구역에서 일어난 일'로 붙인다. 301호에 사람이 누워 있으면
    RX-301은 lying, 옆 구역 노드들은 neighbor다.
    """
    targets = _record_targets(args)
    sers = {}
    labels = {node: label for _, node, label in targets}
    zone = args.zone if len(targets) == 1 else ""
    try:
        for port, node, _ in targets:
            sers[node] = _open_serial(port, args.baud)
        with RecordingArchive(sers, Path(args.out), args.day, parse_esp_csi_record, _write_frame) as capture:
            print(f"연속 원본: {capture.path} (준비 시간 포함, 학습에서는 제외)")
            _record_scene(sers, labels, args.seconds, args.countdown, Path(args.out), args.day,
                          {"zone": zone, "people": args.people, "notes": args.notes}, capture)
    finally:
        for ser in sers.values():
            ser.close()


PLAN_FIXED = ("scene", "description", "people", "seconds")


def _read_text(path: Path) -> str:
    """Excel에서 저장한 CSV(CP949)도 읽는다."""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    sys.exit(f"{path}의 글자 인코딩을 읽을 수 없습니다. UTF-8로 저장해 주세요.")


def _load_plan(path: Path) -> tuple[list[dict], list[str]]:
    """장면 목록 CSV: scene, description, people, seconds, 그리고 노드 이름 열(값은 그 노드의 라벨)."""
    if not path.exists():
        sys.exit(f"장면 목록이 없습니다: {path}")
    rows = list(csv.DictReader(_read_text(path).splitlines()))
    if not rows:
        sys.exit(f"장면 목록이 비어 있습니다: {path}")
    cols = [c.strip() for c in rows[0].keys()]
    missing = [c for c in PLAN_FIXED if c not in cols]
    if missing:
        sys.exit(f"장면 목록에 {', '.join(missing)} 열이 없습니다.")
    nodes = [c for c in cols if c not in PLAN_FIXED]
    if not nodes:
        sys.exit("장면 목록에 노드 열(예: RX-301)이 없습니다.")
    plan, names = [], set()
    for i, r in enumerate(rows, 1):
        r = {k.strip(): (v or "").strip() for k, v in r.items()}
        scene = r["scene"]
        if not scene or scene in names:
            sys.exit(f"{i}번째 장면 이름이 비었거나 겹칩니다: '{scene}'")
        names.add(scene)
        labels = {n: r[n] for n in nodes}
        for n, lab in labels.items():
            if lab not in LABELS:
                sys.exit(f"장면 '{scene}'의 {n} 라벨 '{lab}'은 쓸 수 없습니다. 다음 중 하나: {', '.join(sorted(LABELS))}")
        try:
            seconds, people = float(r["seconds"]), int(r["people"] or 0)
        except ValueError:
            sys.exit(f"장면 '{scene}'의 seconds나 people이 숫자가 아닙니다.")
        if seconds <= 0:
            sys.exit(f"장면 '{scene}'의 seconds는 0보다 커야 합니다.")
        plan.append({"scene": scene, "description": r["description"], "people": people,
                     "seconds": seconds, "labels": labels})
    return plan, nodes


def _parse_node_ports(specs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in specs:
        node, sep, port = s.partition("=")
        if not sep or not node.strip() or not port.strip():
            sys.exit(f"--node는 노드=포트 형식입니다(예: --node RX-301=COM5). 받은 값: {s}")
        if node.strip() in out:
            sys.exit(f"{node.strip()}가 두 번 들어갔습니다.")
        out[node.strip()] = node_port(node.strip(), port.strip())
    return out


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def _ask_during_capture(prompt: str, capture: RecordingArchive) -> str:
    """Enter를 기다리는 동안에도 원본 저장 실패를 주 스레드에 전달한다."""
    ready = threading.Event()
    result = {}

    def read_answer():
        try:
            result["answer"] = _ask(prompt)
        except BaseException as exc:
            result["error"] = exc
        finally:
            ready.set()

    threading.Thread(target=read_answer, daemon=True, name="collection-input").start()
    while not ready.wait(0.05):
        capture.check()
    capture.check()
    if "error" in result:
        raise result["error"]
    return result["answer"]


def cmd_session(args) -> None:
    """수집 날 진행. 장면 목록을 차례로 보여 주고, 장면마다 모든 수신 노드를 같은 시각에 녹화한다.

    장면이 끝나면 Enter(다음), r(다시 찍기), q(끝내기)를 고른다. --yes면 묻지 않고 끝까지 간다.
    """
    plan, plan_nodes = _load_plan(Path(args.plan))
    ports = _parse_node_ports(args.node)
    if set(ports) != set(plan_nodes):
        sys.exit(f"장면 목록의 노드({', '.join(plan_nodes)})와 --node로 준 노드({', '.join(ports) or '없음'})가 같아야 합니다.")
    start = 0
    if args.start:
        names = [p["scene"] for p in plan]
        if args.start in names:
            start = names.index(args.start)
        elif args.start.isdigit() and 1 <= int(args.start) <= len(plan):
            start = int(args.start) - 1
        else:
            sys.exit(f"--start '{args.start}'에 해당하는 장면이 없습니다. 장면 이름이나 1~{len(plan)} 번호를 주세요.")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "session_log.csv"
    new_log = not log_path.exists()
    sers = {}
    total = sum(p["seconds"] for p in plan[start:])
    print(f"장면 {len(plan) - start}개, 녹화만 약 {total / 60:.0f}분입니다. 노드: "
          + ", ".join(f"{n}={ports[n]}" for n in plan_nodes))
    done, i, resume = 0, start, None
    stopped = False
    try:
        for node in plan_nodes:
            sers[node] = _open_serial(ports[node], args.baud)
        with RecordingArchive(sers, out, args.day, parse_esp_csi_record, _write_frame) as capture:
            print(f"연속 원본: {capture.path} (준비·대기·재촬영 포함, 학습에서는 제외)")
            try:
                with log_path.open("a", newline="", encoding="utf-8-sig") as logf:
                    log = csv.writer(logf)
                    if new_log:
                        log.writerow(["시각", "장면", "설명", "노드", "라벨", "인원", "초", "프레임", "Hz", "파일"])
                    for i in range(start, len(plan)):
                        sc = plan[i]
                        resume = sc["scene"]
                        print(f"\n[{i + 1}/{len(plan)}] {sc['scene']}  {sc['seconds']:.0f}초  장면 전체 인원 {sc['people']}명")
                        print(f"  {sc['description']}")
                        print("  라벨: " + ", ".join(f"{n}={lab}" for n, lab in sc["labels"].items()))
                        saved = None
                        while True:
                            capture.check()
                            if not args.yes:
                                capture.mark("waiting", scene=sc["scene"], phase="prepare")
                                ans = _ask_during_capture("  준비되면 Enter, 이 장면 건너뛰기 s, 끝내기 q: ", capture)
                                capture.check()
                                if ans == "q":
                                    raise _Stop
                                if ans == "s":
                                    capture.mark("scene_skipped", scene=sc["scene"])
                                    break
                            saved = _record_scene(sers, sc["labels"], sc["seconds"], args.countdown, out, args.day,
                                                  {"zone": "", "people": sc["people"], "notes": args.notes,
                                                   "scene": sc["scene"], "description": sc["description"]}, capture)
                            if args.yes:
                                break
                            capture.mark("waiting", scene=sc["scene"], phase="review")
                            ans = _ask_during_capture("  Enter 다음 장면 / r 다시 찍기 / q 끝내기: ", capture)
                            capture.check()
                            if ans == "r":
                                capture.discard_scene({n: path.stem for n, (path, _, _) in saved.items()}, reason="retry")
                                for path, _, _ in saved.values():
                                    path.unlink(missing_ok=True)
                                    path.with_suffix(".json").unlink(missing_ok=True)
                                saved = None
                                print("  방금 장면 파일만 지웠습니다. 연속 원본은 남기고 다시 찍습니다.")
                                continue
                            if ans == "q":
                                _log_scene(log, sc, saved)
                                logf.flush()
                                done += 1
                                resume = plan[i + 1]["scene"] if i + 1 < len(plan) else None
                                raise _Stop
                            break
                        if saved:
                            _log_scene(log, sc, saved)
                            logf.flush()
                            done += 1
                    resume = None
            except _Stop:
                stopped = True
                capture.mark("command_stop", reason="q")
            except KeyboardInterrupt:
                capture.mark("command_stop", reason="keyboard_interrupt")
                raise
    except KeyboardInterrupt:
        stopped = True
    finally:
        for ser in sers.values():
            ser.close()
    if stopped:
        print(f"\n중단했습니다. 끝난 장면 {done}개와 연속 원본은 남아 있습니다."
              + (f" 이어서 하려면 --start {resume}" if resume else ""))
    if resume is None:
        print(f"\n장면 {done}개를 녹화했습니다.")
    print(f"기록: {log_path} (Notion 데이터 수집 일지에 옮겨 적습니다)")


class _Stop(Exception):
    """사용자가 q로 수집을 끝냈을 때."""


def _log_scene(log, sc: dict, saved: dict[str, tuple[Path, int, float]]) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for node, (path, n, took) in saved.items():
        log.writerow([now, sc["scene"], sc["description"], node, sc["labels"][node], sc["people"],
                      took, n, f"{n / max(took, 1e-6):.1f}", path.name])


# ---------------------------------------------------------------- 서버로 보내기(live, send)

def _sender_loop(api, token, queue, lock, stop, source, flags=None):
    """flags["restarted"]가 켜져 있으면 다음 묶음에 실어 보내고 끈다. 보내지 못하면 다시 켜 둔다."""
    flags = {} if flags is None else flags
    while not stop.is_set() or queue:
        time.sleep(0.5)
        with lock:
            batch = queue[:2000]
            del queue[:len(batch)]
            restarted = flags.pop("restarted", False) if batch else False
        if not batch:
            continue
        try:
            res = _post(api, token, batch, source, restarted)
            print(f"  전송 {len(batch):4d} → 모드 {res.get('mode')} 창 {res.get('windows', 0)}", end="\r")
        except urllib.error.HTTPError as exc:
            print(f"\n서버 오류 {exc.code}: {exc.read().decode(errors='ignore')[:200]}")
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"\n전송 실패({exc}). 다음 묶음에서 다시 시도합니다.")
            with lock:
                queue[:0] = batch[-1000:]  # 너무 오래된 것은 버리고 최근 것만 다시 보낸다
                if restarted:
                    flags["restarted"] = True


def cmd_live(args) -> None:
    """실시간 전송. --port와 --token을 같은 수만큼 주면 노드 여러 개를 한 프로세스에서 보낸다.

    게이트웨이(gw:...) 노드는 게이트웨이 USB 포트를 함께 쓰므로 여기서 한꺼번에 돌려야 한다.
    """
    ports, tokens = args.port, args.token
    if not ports or len(ports) != len(tokens):
        sys.exit("--port와 --token을 노드마다 한 쌍씩 같은 순서로 주세요(예: --port gw:<MAC> --token <토큰>).")
    stop = threading.Event()
    links = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if args.save_dir:
        Path(args.save_dir).mkdir(parents=True, exist_ok=True)
    for port, token in zip(ports, tokens):
        ser = _open_serial(port, args.baud)
        q: list[dict] = []
        lock = threading.Lock()
        flags: dict = {}
        sender = threading.Thread(target=_sender_loop, args=(args.api, token, q, lock, stop, "live", flags),
                                  daemon=True)
        save = None
        if args.save_dir:
            safe = "".join(ch if ch.isalnum() else "-" for ch in port)
            save = (Path(args.save_dir) / f"live_{safe}_{stamp}.csv").open("w", newline="", encoding="utf-8")
            csv.writer(save).writerow(["t", "rssi", "data"])

        def reader(ser=ser, q=q, lock=lock, save=save, flags=flags, port=port) -> None:
            w = csv.writer(save) if save else None
            # 게이트웨이 노드만 다시 켜진 것을 안다(노드 번호와 보드 시각). USB로 꽂은 노드는 알 수 없다
            restarts = getattr(ser, "restarts_since_open", None)
            seen = 0
            for t, rssi, raw in _lines(ser, stop=stop):
                with lock:
                    q.append({"t": t, "rssi": rssi, "data": raw})
                    if restarts is not None and restarts() > seen:
                        seen = restarts()
                        flags["restarted"] = True
                        print(f"\n{port}: 보드가 다시 켜졌습니다. 서버는 다시 보정할 때까지 이 노드를 판단에서 뺍니다.")
                if w:
                    _write_frame(w, t, rssi, raw)

        rd = threading.Thread(target=reader, daemon=True)
        links.append((ser, sender, rd, save))
    for _, sender, rd, _ in links:
        sender.start()
        rd.start()
    print(f"실시간 전송 중입니다(노드 {len(links)}개). 멈추려면 Ctrl+C.")
    try:
        while any(rd.is_alive() for _, _, rd, _ in links):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for ser, sender, rd, save in links:
            rd.join(timeout=3)
            sender.join(timeout=5)
            ser.close()
            if save:
                save.close()


# ---------------------------------------------------------------- 게이트웨이 상태(gateway)

def cmd_gateway(args) -> None:
    """게이트웨이로 들어오는 노드(MAC)마다 받은 수, 초당 수, 잃은 비율, CSI 길이, 신호 세기를 본다."""
    if not _GATEWAY["port"]:
        sys.exit("--gateway로 게이트웨이의 USB 포트를 알려 주세요(예: --gateway COM7).")
    hub = _GatewayHub.get(_GATEWAY["port"], _GATEWAY["baud"])
    print(f"{_GATEWAY['port']}에서 {args.seconds:.0f}초 동안 듣습니다. 노드와 TX-1을 켜 두세요.")
    try:
        start = {mac: (st.frames, st.lost, st.resets) for mac, st in hub.stats.items()}
        time.sleep(args.seconds)
        status, bad = hub.status, hub.reader.bad_crc
        rows = []
        for mac, st in sorted(hub.stats.items()):
            f0, l0, r0 = start.get(mac, (0, 0, 0))
            got, lost = st.frames - f0, st.lost - l0
            if got == 0:
                continue
            length = st.lengths.most_common(1)[0][0]
            rssi = statistics.fmean(st.gw_rssi) if st.gw_rssi else float("nan")
            rows.append((mac, got, got / args.seconds, lost / max(got + lost, 1), length, rssi, st.resets - r0))
    finally:
        hub.release()
    if status:
        print(f"게이트웨이: {status}")
    else:
        print("게이트웨이 상태 묶음이 오지 않았습니다. 포트가 게이트웨이(csi_gateway)인지, "
              f"속도가 {_GATEWAY['baud']}인지 확인하세요.")
    if bad:
        print(f"깨진 묶음 {bad}개(버림). 많으면 USB 허브를 빼고 노트북에 바로 꽂으세요.")
    if not rows:
        print("들어오는 노드가 없습니다. 노드에 csi_node를 올렸는지, 전원이 켜졌는지, TX-1이 켜졌는지 확인하세요.")
        sys.exit(1)
    names = {m: n for n, m in load_node_names().items()}
    print(f"\n{'노드 MAC':<19}{'이름표':<10}{'받음':>7}{'초당':>8}{'잃음':>8}{'값 개수':>8}{'게이트웨이 RSSI':>16}"
          f"{'재시작':>7}")
    for mac, got, hz, loss, length, rssi, resets in rows:
        name = names.get(mac, "?")
        print(f"{mac:<19}{name:<10}{got:>7}{hz:>8.1f}{loss:>8.1%}{length:>8}{rssi:>13.0f} dBm{resets:>7}")
    restarted = [names.get(r[0], r[0]) for r in rows if r[6]]
    if restarted:
        print(f"\n듣는 동안 다시 켜진 노드: {', '.join(restarted)}. 충전기·케이블이 헐겁거나 전원이 약한지 확인하세요.")
    unknown = [mac for mac, *_ in rows if mac not in names]
    missing = [n for n, m in sorted((n, m) for m, n in names.items()) if m not in {r[0] for r in rows}]
    if missing:
        print(f"\nnodes.csv에는 있는데 안 들어오는 노드: {', '.join(missing)} (전원·펌웨어·거리 확인)")
    if unknown:
        print(f"\nnodes.csv에 없는 MAC이 있습니다. 이름표를 확인하고 {_NODES['path'].name}에 적어 주세요:")
        for mac in unknown:
            print(f"  RX-???,{mac}")
    else:
        print("\n모든 노드가 이름표와 짝지어져 있습니다. 명령에는 --node RX-301=gw 또는 --port gw:RX-301 처럼 씁니다.")


def cmd_send(args) -> None:
    # 원래 I/Q 배열을 그대로 보낸다(서버가 라이브와 같은 경로로 처리)
    rows = []
    with open(args.file, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append({"t": float(row["t"]), "rssi": float(row["rssi"]) if row.get("rssi") else None,
                         "data": json.loads(row["data"])})
    if not rows:
        sys.exit("프레임이 없습니다. collector record로 만든 CSV인지 확인하세요.")
    rows.sort(key=lambda r: r["t"])
    t0 = rows[0]["t"]
    started = time.monotonic()
    i = 0
    while i < len(rows):
        cut = t0 + (time.monotonic() - started) * args.speed
        j = i
        while j < len(rows) and rows[j]["t"] <= cut:
            j += 1
        if j > i:
            res = _post(args.api, args.token, rows[i:j], "replay")
            print(f"  {rows[j - 1]['t'] - t0:6.1f}초 → 모드 {res.get('mode')} 창 {res.get('windows', 0)}", end="\r")
            i = j
        time.sleep(0.25)
    print("\n전송 끝")


def cmd_nodelog(args) -> None:
    """USB로 꽂은 노드(csi_node)의 상태 줄만 보여 준다. CSI_DATA 줄은 숨긴다.

    5초마다 'CSI ○Hz, 전송 ○Hz, USB ○Hz | ACK 없음, 큐 버림, 전송 버림, USB 건너뜀'이 나온다.
    ACK 없음이 늘면 무선 구간(거리·벽·간섭) 문제, 큐 버림·전송 버림이 늘면 보드 안에서 못 따라가는 것이다.
    """
    if args.port.lower().startswith("gw:"):
        sys.exit("nodelog는 노드를 USB로 꽂은 포트(COM 번호)에 씁니다.")
    ser = _open_serial(args.port, args.baud)
    print(f"{args.port}의 노드 상태를 {args.seconds:.0f}초 동안 봅니다(5초마다 한 줄).")
    deadline = time.time() + args.seconds
    shown = 0
    try:
        while time.time() < deadline:
            b = ser.readline()
            if not b:
                continue
            line = b.decode("utf-8", errors="replace").strip()
            if "CSI_DATA" in line or "csi_node" not in line:
                continue
            k = line.find("csi_node")
            print(line[k:].replace("\x1b[0m", "").strip())  # 줄 끝 색 코드만 지운다
            shown += 1
    finally:
        ser.close()
    if not shown:
        print("상태 줄이 없습니다. 이 포트가 csi_node를 올린 노드인지, 속도가 921600인지 확인하세요.")


def cmd_compare(args) -> None:
    # 비교표는 서버 설정 중 창 길이만 쓴다. 서버용 SECRET_KEY 안내가 섞여 나오지 않게 비어 있으면 채워 둔다.
    os.environ.setdefault("SECRET_KEY", "collector-compare-local")
    from app.ml.compare import main as compare_main  # 학습 코드(scikit-learn)는 이 명령에서만 읽는다

    argv = ["--data", args.data] + [x for n in args.node for x in ("--node", n)]
    code = compare_main(argv)
    if code:
        sys.exit(code)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="ESP32 CSI 수집 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    gwp = argparse.ArgumentParser(add_help=False)
    gwp.add_argument("--gateway", default=os.environ.get("WATCHMATE_GATEWAY", ""),
                     help="무선 노드(gw:<MAC>)를 받는 게이트웨이의 USB 포트(예: COM7)")
    gwp.add_argument("--gateway-baud", type=int, default=GW_BAUD)
    gwp.add_argument("--nodes", default="", help=f"이름표와 MAC 파일(기본 {NODES_FILE.name})")

    gw = sub.add_parser("gateway", parents=[gwp], help="게이트웨이로 들어오는 무선 노드 목록과 손실")
    gw.add_argument("--seconds", type=float, default=5)

    sub.add_parser("ports", help="시리얼 포트 목록")

    ck = sub.add_parser("check", parents=[gwp], help="보드 점검: 초당 프레임, CSI 길이, 신호 세기")
    ck.add_argument("--port", action="append", default=[], help="점검할 포트. 여러 개면 반복")
    ck.add_argument("--seconds", type=float, default=10)
    ck.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    ss = sub.add_parser("session", parents=[gwp], help="수집 날 진행: 장면 목록대로 노드 전체 녹화")
    ss.add_argument("--plan", default="docs/plans/day_plan.csv", help="장면 목록 CSV")
    ss.add_argument("--node", action="append", default=[], help="노드=포트. 노드마다 반복(예: --node RX-301=COM5)")
    ss.add_argument("--day", required=True, help="수집 날짜(하루 단위 교차검증의 묶음)")
    ss.add_argument("--out", required=True)
    ss.add_argument("--start", default="", help="이 장면부터 시작(이름이나 1부터 센 번호)")
    ss.add_argument("--countdown", type=int, default=5)
    ss.add_argument("--notes", default="", help="그날 메모(문 상태, 참여자 등). 모든 세션 메타데이터에 들어간다")
    ss.add_argument("--yes", action="store_true", help="묻지 않고 끝까지 진행")
    ss.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    r = sub.add_parser("record", parents=[gwp], help="장면 하나 녹화(노드 여러 개 동시 가능)")
    r.add_argument("--rx", action="append", default=[],
                   help="포트:노드:라벨. 노드마다 하나씩 반복(예: --rx COM5:RX-301:lying --rx COM6:RX-302:neighbor)")
    r.add_argument("--port", help="노드 하나만 녹화할 때 포트")
    r.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    r.add_argument("--node", help="노드 하나만 녹화할 때 수신 노드 이름(예: RX-301)")
    r.add_argument("--label", help="노드 하나만 녹화할 때 라벨")
    r.add_argument("--day", required=True, help="교차검증 묶음 단위(수집 날짜)")
    r.add_argument("--zone", default="", help="노드 하나만 녹화할 때 구역 이름")
    r.add_argument("--people", type=int, default=0, help="장면 전체 인원")
    r.add_argument("--notes", default="")
    r.add_argument("--seconds", type=float, default=150)
    r.add_argument("--countdown", type=int, default=5)
    r.add_argument("--out", required=True)

    lv = sub.add_parser("live", parents=[gwp], help="실시간 전송")
    lv.add_argument("--port", action="append", default=[], help="노드 포트. 여러 노드면 --token과 짝지어 반복")
    lv.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    lv.add_argument("--api", required=True)
    lv.add_argument("--token", action="append", default=[], help="노드 토큰. --port와 같은 순서")
    lv.add_argument("--save-dir", default="")

    nl = sub.add_parser("nodelog", help="USB로 꽂은 무선 노드의 상태 줄(전송·손실 원인) 보기")
    nl.add_argument("--port", required=True)
    nl.add_argument("--seconds", type=float, default=15)
    nl.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    cp = sub.add_parser("compare", help="비교표: 녹화 장면마다 그날 빈 방 대비 변화(보드 자리·기준선 점검)")
    cp.add_argument("--data", required=True, help="세션 폴더(예: ../data/sessions/2026-11-04)")
    cp.add_argument("--node", action="append", default=[], help="이 노드만(여러 번 가능)")

    sd = sub.add_parser("send", help="녹화 파일을 서버로 다시 보내기(개발용)")
    sd.add_argument("--file", required=True)
    sd.add_argument("--api", required=True)
    sd.add_argument("--token", required=True)
    sd.add_argument("--speed", type=float, default=1.0)

    args = ap.parse_args(argv)
    saved = dict(_GATEWAY), dict(_NODES)
    if getattr(args, "gateway", None) is not None:
        _GATEWAY["port"], _GATEWAY["baud"] = args.gateway, args.gateway_baud
    if getattr(args, "nodes", ""):
        _NODES["path"] = Path(args.nodes)
    try:
        {"ports": cmd_ports, "check": cmd_check, "session": cmd_session, "record": cmd_record,
         "live": cmd_live, "send": cmd_send, "gateway": cmd_gateway, "compare": cmd_compare,
         "nodelog": cmd_nodelog}[args.cmd](args)
    except RecordingError as exc:
        sys.exit(f"원본 저장을 완료하지 못했습니다. 이 실행은 미완료로 확인하세요: {exc}")
    finally:
        _close_all_gateways()
        _GATEWAY.update(saved[0])
        _NODES.update(saved[1])


if __name__ == "__main__":
    main()
