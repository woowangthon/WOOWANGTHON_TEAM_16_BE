"""무선 노드 → 게이트웨이 → 수집 도구 경로.

가짜 게이트웨이는 firmware/csi_gateway와 같은 USB 묶음(wm_proto.h)을 만든다. pty를 쓰지 않아 윈도우에서도 돈다.
"""

import random
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import collector  # noqa: E402

from app.ml.csi import parse_esp_csi_record  # noqa: E402
from app.ml.sessions import load_sessions  # noqa: E402

MAC_A = "90:70:69:00:00:0a"
MAC_B = "90:70:69:00:00:0b"


def node_packet(node_seq: int, tx_id: int, data: list[int], gain: float = 1.0, rssi: int = -40,
                first_word: int = 0, channel: int = 11) -> bytes:
    """firmware/common/wm_proto.h의 wm_csi_pkt_t."""
    head = collector.GW_CSI_PKT.pack(b"WMC1", node_seq, len(data), tx_id, tx_id * 10000, gain, rssi, -95,
                                     channel, 2, 1, 1, first_word, 0)
    return head + struct.pack(f"<{len(data)}b", *data)


def csi_frame(mac: str, pkt: bytes, gw_rssi: int = -55) -> bytes:
    item = bytes.fromhex(mac.replace(":", "")) + struct.pack("<bB", gw_rssi, 0) + pkt
    return collector.gw_frame(collector.GW_TYPE_CSI, item)


def test_frame_reader_skips_garbage_and_broken_frames():
    good1 = csi_frame(MAC_A, node_packet(1, 7, [1, -2] * 192))
    good2 = csi_frame(MAC_B, node_packet(2, 8, [3, -4] * 192))
    broken = bytearray(csi_frame(MAC_A, node_packet(3, 9, [5, 6] * 192)))
    broken[40] ^= 0xFF  # 가운데 한 바이트가 깨짐
    status = collector.gw_frame(collector.GW_TYPE_STATUS, b"gateway up 3s")
    stream = b"I (300) boot: \xa5 log text\n" + good1 + bytes(broken) + b"\x00\xa5" + good2 + status
    reader = collector.GatewayFrameReader()
    frames = []
    rnd = random.Random(0)
    i = 0
    while i < len(stream):  # USB가 아무 데서나 끊어 주는 것처럼 조각내 넣는다
        n = rnd.randint(1, 50)
        frames += reader.feed(stream[i : i + n])
        i += n
    kinds = [t for t, _ in frames]
    assert kinds == [collector.GW_TYPE_CSI, collector.GW_TYPE_CSI, collector.GW_TYPE_STATUS]
    assert reader.bad_crc >= 1
    macs = [collector.gw_decode_csi(p)["mac"] for t, p in frames if t == collector.GW_TYPE_CSI]
    assert macs == [MAC_A, MAC_B]


def test_decoded_line_matches_csi_recv_values():
    rnd = random.Random(1)
    data = [rnd.randint(-128, 127) for _ in range(384)]
    gain = 1.7
    pkt = node_packet(5, 4321, data, gain=gain, rssi=-37, first_word=1)
    d = collector.gw_decode_csi(bytes.fromhex(MAC_A.replace(":", "")) + b"\xc9\x00" + pkt)
    # 보드의 (int16_t)(compensate_gain * buf[i]): float32 곱셈 뒤 0 쪽으로 자르기
    expect = [int(np.int16(np.float32(gain) * np.float32(v))) for v in data]
    assert d["values"] == expect and d["gw_rssi"] == -55 and d["node_seq"] == 5
    assert any(v != round(gain * x) for v, x in zip(expect, data))  # 반올림이 아니라 자르기라는 것을 확인
    rec = parse_esp_csi_record(collector.gw_csi_line(d))
    assert rec is not None and len(rec.raw) == 384
    assert (rec.seq, rec.rssi, rec.channel, rec.mac) == (4321, -37, 11, collector.SEND_MAC)
    assert rec.raw[:4] == [0, 0, 0, 0] and rec.raw[4:] == expect[4:]  # first_word=1이면 앞 4개는 무효


class FakeGateway:
    """시간에 맞춰 노드 여러 개의 묶음을 내는 가짜 게이트웨이 USB 포트."""

    def __init__(self, nodes: dict[str, float], drop_every: dict[str, int] | None = None, length: int = 384):
        self.nodes, self.drop_every, self.length = nodes, drop_every or {}, length
        self.t0 = time.time()
        self.sent = {mac: 0 for mac in nodes}
        self.seq = {mac: 0 for mac in nodes}
        self.rnd = random.Random(2)
        self.closed = False
        self.last_status = 0.0

    def read(self, n: int) -> bytes:
        time.sleep(0.02)
        now = time.time()
        out = bytearray()
        for mac, hz in self.nodes.items():
            due = int((now - self.t0) * hz)
            while self.sent[mac] < due:
                self.sent[mac] += 1
                seq = self.seq[mac]
                self.seq[mac] = (seq + 1) % 65536
                k = self.drop_every.get(mac, 0)
                if k and seq % k == k - 1:
                    continue  # 무선에서 잃은 패킷: 번호만 건너뛴다
                data = [self.rnd.randint(-30, 30) for _ in range(self.length)]
                out += csi_frame(mac, node_packet(seq, seq * 2, data, rssi=-40 - seq % 4))
        if now - self.last_status > 0.5:
            self.last_status = now
            out += collector.gw_frame(collector.GW_TYPE_STATUS, b"gateway 90:70:69:00:00:99 up 5s csi 10 usb_drop 0")
        return bytes(out)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_gateway(monkeypatch):
    holder = {}

    def factory(port, baud, timeout):
        assert port == "FAKEGW" and baud == collector.GW_BAUD
        holder["gw"] = FakeGateway(holder["nodes"], holder.get("drop"))
        return holder["gw"]

    monkeypatch.setattr(collector, "_serial_factory", factory)
    yield holder
    collector._close_all_gateways()
    collector._GATEWAY["port"] = ""


def test_check_over_gateway_reports_rate_loss_and_missing_node(fake_gateway, capsys):
    fake_gateway["nodes"] = {MAC_A: 60.0, MAC_B: 60.0}
    fake_gateway["drop"] = {MAC_B: 4}  # B는 25%를 잃는다
    missing = "90:70:69:00:00:0c"
    with pytest.raises(SystemExit) as exc:
        collector.main(["check", "--gateway", "FAKEGW", "--seconds", "2", "--port", f"gw:{MAC_A}",
                        "--port", "gw:90-70-69-00-00-0B", "--port", f"gw:{missing}"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    part_a = out.split(f"[gw:{MAC_A}]")[1].split("[gw:")[0]
    part_b = out.split("[gw:90-70-69-00-00-0B]")[1].split("[gw:")[0]
    part_c = out.split(f"[gw:{missing}]")[1]
    assert "정상" in part_a and "값 개수 384(서브캐리어 192)" in part_a and "잃은 것 0건" in part_a
    assert "확인 필요" in part_b and "5% 넘게" in part_b
    assert "아무 출력이 없습니다" in part_c and "MAC을 못 받고" in part_c
    assert fake_gateway["gw"].closed  # 명령이 끝나면 게이트웨이 포트를 닫는다


def test_gateway_command_lists_nodes(fake_gateway, capsys):
    fake_gateway["nodes"] = {MAC_A: 40.0, MAC_B: 40.0}
    fake_gateway["drop"] = {MAC_B: 10}
    collector.main(["gateway", "--gateway", "FAKEGW", "--seconds", "1.5"])
    out = capsys.readouterr().out
    assert "gateway 90:70:69:00:00:99" in out
    row_a = next(line for line in out.splitlines() if line.startswith(MAC_A))
    row_b = next(line for line in out.splitlines() if line.startswith(MAC_B))
    assert "0.0%" in row_a and "384" in row_a
    loss_b = float(row_b.split()[4].rstrip("%"))  # MAC, 이름표, 받음, 초당, 잃음
    assert 5 <= loss_b <= 15
    assert f"RX-???,{MAC_A}" in out  # nodes.csv에 적을 줄을 알려 준다


def test_gateway_command_shows_names_from_nodes_file(fake_gateway, tmp_path, capsys):
    fake_gateway["nodes"] = {MAC_A: 40.0}
    nodes = tmp_path / "nodes.csv"
    nodes.write_text(f"node,mac\nRX-301,{MAC_A}\nRX-302,{MAC_B}\n", encoding="utf-8")
    collector.main(["gateway", "--gateway", "FAKEGW", "--nodes", str(nodes), "--seconds", "1"])
    out = capsys.readouterr().out
    row_a = next(line for line in out.splitlines() if line.startswith(MAC_A))
    assert "RX-301" in row_a
    assert "안 들어오는 노드: RX-302" in out


def test_session_over_gateway_records_each_node(fake_gateway, tmp_path):
    fake_gateway["nodes"] = {MAC_A: 50.0, MAC_B: 50.0}
    plan = tmp_path / "plan.csv"
    plan.write_text("scene,description,people,seconds,RX-A,RX-B\n"
                    "start_empty,모두 밖,0,2,empty,empty\n"
                    "lying_a,A 구역에 1명 누움,1,2,lying,neighbor\n", encoding="utf-8-sig")
    out = tmp_path / "day"
    collector.main(["session", "--gateway", "FAKEGW", "--plan", str(plan), "--day", "2026-11-04", "--out", str(out),
                    "--yes", "--countdown", "0", "--node", f"RX-A=gw:{MAC_A}", "--node", f"RX-B=gw:{MAC_B}"])
    sessions, problems = load_sessions(out)
    assert not problems
    got = sorted((s.node, s.label) for s in sessions)
    assert got == [("RX-A", "empty"), ("RX-A", "lying"), ("RX-B", "empty"), ("RX-B", "neighbor")]
    assert all(s.amp.shape[1] == 192 and s.amp.shape[0] > 50 for s in sessions)


def test_gateway_port_needs_gateway_option():
    collector._GATEWAY["port"] = ""
    with pytest.raises(SystemExit):
        collector._open_serial("gw:90:70:69:00:00:0a", 921600)
    with pytest.raises(SystemExit):
        collector._norm_mac("90:70:69")


def test_live_needs_port_token_pairs():
    with pytest.raises(SystemExit):
        collector.main(["live", "--api", "http://x", "--port", "COM5", "--port", "COM6", "--token", "t1"])


def test_live_sends_each_gateway_node_with_its_token(fake_gateway, monkeypatch):
    fake_gateway["nodes"] = {MAC_A: 50.0, MAC_B: 50.0}
    posted: dict[str, int] = {}
    lock = threading.Lock()

    def fake_post(api, token, frames, source="live"):
        assert all(len(f["data"]) == 384 for f in frames)
        with lock:
            posted[token] = posted.get(token, 0) + len(frames)
        return {"mode": "idle", "windows": 0}

    monkeypatch.setattr(collector, "_post", fake_post)

    def stop_soon():
        time.sleep(2.0)
        import _thread
        _thread.interrupt_main()

    threading.Thread(target=stop_soon, daemon=True).start()
    collector.main(["live", "--gateway", "FAKEGW", "--api", "http://x",
                    "--port", f"gw:{MAC_A}", "--token", "tok-a", "--port", f"gw:{MAC_B}", "--token", "tok-b"])
    assert posted.get("tok-a", 0) > 30 and posted.get("tok-b", 0) > 30
