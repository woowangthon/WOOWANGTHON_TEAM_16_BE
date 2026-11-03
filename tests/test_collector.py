"""수집 도구: 인자 검사와, 가짜 보드(pty) 두 개를 동시에 녹화하는 경로."""

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import collector  # noqa: E402

from app.ml.sessions import load_sessions  # noqa: E402


def _args(**kw):
    base = dict(rx=[], port=None, node=None, label=None, baud=921600, day="2026-11-01", zone="", people=0,
                notes="", seconds=2.0, countdown=0, out="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_record_targets_validation():
    assert collector._record_targets(_args(rx=["COM5:RX-301:lying", "COM6:RX-302:neighbor"])) == [
        ("COM5", "RX-301", "lying"), ("COM6", "RX-302", "neighbor")]
    assert collector._record_targets(_args(port="COM5", node="RX-301", label="empty")) == [("COM5", "RX-301", "empty")]
    for bad in (_args(rx=["COM5:RX-301:sleeping"]), _args(rx=["COM5-RX-301"]), _args(port="COM5"),
                _args(rx=["COM5:RX:lying", "COM6:RX:empty"]), _args(rx=["COM5:RX:lying"], port="COM6")):
        with pytest.raises(SystemExit):
            collector._record_targets(bad)


HEADER = ("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,"
          "sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,len,first_word,data\n")


def _fake_board(master: int, seed: int, stop: threading.Event) -> None:
    rnd = random.Random(seed)
    os.write(master, b"I (12) boot: log line\n" + HEADER.encode())
    i = 0
    while not stop.is_set():
        data = ",".join(str(rnd.randint(-20, 20)) for _ in range(128))
        line = f'CSI_DATA,{i},aa:bb:cc:dd:ee:ff,-42,11,1,7,0,0,0,0,0,0,0,-95,0,6,0,{i * 20000},0,100,0,128,0,"[{data}]"\n'
        try:
            os.write(master, line.encode())
        except OSError:
            return
        i += 1
        time.sleep(0.02)


@pytest.mark.skipif(os.name != "posix", reason="가짜 시리얼 포트(pty)는 POSIX에서만 만든다")
def test_record_two_nodes_at_once(tmp_path):
    pytest.importorskip("serial")
    stop = threading.Event()
    pairs = [os.openpty() for _ in range(2)]
    threads = [threading.Thread(target=_fake_board, args=(m, k, stop), daemon=True) for k, (m, _) in enumerate(pairs)]
    for th in threads:
        th.start()
    try:
        ports = [os.ttyname(s) for _, s in pairs]
        collector.cmd_record(_args(rx=[f"{ports[0]}:RX-A:lying", f"{ports[1]}:RX-B:neighbor"], out=str(tmp_path)))
    finally:
        stop.set()
    sessions, problems = load_sessions(tmp_path)
    assert not problems
    by_node = {s.node: s for s in sessions}
    assert by_node["RX-A"].label == "lying" and by_node["RX-A"].is_person
    assert by_node["RX-B"].label == "neighbor" and not by_node["RX-B"].is_person
    assert all(s.amp.shape[0] > 30 and s.amp.shape[1] == 64 for s in sessions)
    meta = json.loads(next(tmp_path.glob("*RX-A*.json")).read_text(encoding="utf-8"))
    assert meta["recorded_with"] == ["RX-A", "RX-B"]


# ---- esp-csi csi_recv(ESP32-S3) 실제 출력 형식: 2026-04 커밋 8633d67의 app_main.c 기준 ----
S3_HEADER = ("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,"
             "sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data")


def _s3_line(seq: int, values: list[int], rssi: int = -41, ts: int = 1000, first_word: int = 0, length: int | None = None) -> str:
    n = len(values) if length is None else length
    return (f"CSI_DATA,{seq},1a:00:00:00:00:00,{rssi},11,1,0,1,0,1,0,0,0,0,-96,0,11,2,{ts},0,47,1,{n},{first_word},"
            '"[' + ",".join(map(str, values)) + ']"')


def test_parse_real_s3_lines():
    from app.ml.csi import parse_esp_csi_record
    vals = [(-1) ** k * (k % 30) for k in range(384)]
    rec = parse_esp_csi_record(_s3_line(7, vals, ts=123456), S3_HEADER.split(","))
    assert rec is not None and len(rec.raw) == 384
    assert (rec.rssi, rec.seq, rec.mac, rec.channel, rec.ts) == (-41, 7, "1a:00:00:00:00:00", 11, 123456)
    # 헤더 줄을 못 받았어도 필드 개수로 형식을 알아낸다
    assert parse_esp_csi_record(_s3_line(8, vals)).seq == 8
    # 로그 색 코드 뒤에 붙어 들어온 줄도 읽는다
    assert parse_esp_csi_record("\x1b[0m" + _s3_line(9, vals)) is not None
    # len과 실제 값 개수가 다르면(전송 중 잘림) 버린다
    assert parse_esp_csi_record(_s3_line(10, vals[:300], length=384)) is None
    # first_word가 1이면 앞 4개 값을 0으로
    assert parse_esp_csi_record(_s3_line(11, [9] * 8, first_word=1)).raw[:5] == [0, 0, 0, 0, 9]
    # 이득 보정 로그 줄은 CSI가 아니다
    assert parse_esp_csi_record("\x1b[0;32mI (5123) csi_recv: compensate_gain 1.000000, agc_gain 30, fft_gain 2\x1b[0m") is None


def _s3_board(master: int, seed: int, stop: threading.Event, hz: float = 50.0, corrupt_every: int = 0) -> None:
    """csi_recv(ESP32-S3)처럼 이득 로그 줄과 384개 값의 CSI_DATA 줄을 번갈아 낸다."""
    rnd = random.Random(seed)
    os.write(master, b"I (300) csi_recv: ================ CSI RECV ================\n" + (S3_HEADER + "\n").encode())
    i = 0
    while not stop.is_set():
        vals = [rnd.randint(-25, 25) for _ in range(384)]
        # 송신 번호: 송신 보드가 보낸 10개 중 9개를 받는 셈(11/1 R02 USB 점검: 송신 81Hz 중 74Hz)
        line = _s3_line(i + i // 9, vals, rssi=-40 - (i % 5), ts=i * 20000)
        if corrupt_every and i % corrupt_every == corrupt_every - 1:
            line = line[: len(line) // 2] + line[-3:]  # 가운데가 빠진 줄
        payload = (f"\x1b[0;32mI ({i}) csi_recv: compensate_gain 1.000000, agc_gain 30, fft_gain 2\x1b[0m\n{line}\n").encode()
        try:
            os.write(master, payload)
        except OSError:
            return
        i += 1
        time.sleep(1 / hz)


@pytest.mark.skipif(os.name != "posix", reason="가짜 시리얼 포트(pty)는 POSIX에서만 만든다")
def test_check_reports_good_and_silent_boards(capsys):
    pytest.importorskip("serial")
    stop = threading.Event()
    good, silent = os.openpty(), os.openpty()
    th = threading.Thread(target=_s3_board, args=(good[0], 1, stop, 90.0, 20), daemon=True)
    th.start()
    try:
        with pytest.raises(SystemExit) as exc:
            collector.main(["check", "--port", os.ttyname(good[1]), "--port", os.ttyname(silent[1]), "--seconds", "2"])
    finally:
        stop.set()
    assert exc.value.code == 1  # 조용한 포트 때문에 실패
    out = capsys.readouterr().out
    good_part, silent_part = out.split(f"[{os.ttyname(silent[1])}]")
    assert "정상" in good_part and "값 개수 384(서브캐리어 192)" in good_part
    import re
    tx = int(re.search(r"송신 보드 약 (\d+) Hz", good_part).group(1))
    assert 70 <= tx <= 130  # 가짜 보드는 초당 90줄을 내고 송신 번호를 10개 중 1개씩 건너뛴다(송신 약 100Hz)
    assert "송신 대비 받은 비율" in good_part and "보낸 것의" not in good_part
    assert "잘린 줄" in good_part and "채널 11" in good_part
    assert "아무 출력이 없습니다" in silent_part


@pytest.mark.skipif(os.name != "posix", reason="가짜 시리얼 포트(pty)는 POSIX에서만 만든다")
def test_session_records_every_scene_with_per_node_labels(tmp_path, capsys):
    pytest.importorskip("serial")
    plan = tmp_path / "plan.csv"
    plan.write_text("scene,description,people,seconds,RX-A,RX-B\n"
                    "start_empty,모두 밖,0,2,empty,empty\n"
                    "lying_a,A 구역에 1명 누움,1,2,lying,neighbor\n", encoding="utf-8-sig")
    stop = threading.Event()
    pairs = [os.openpty() for _ in range(2)]
    for k, (m, _) in enumerate(pairs):
        threading.Thread(target=_s3_board, args=(m, k, stop), daemon=True).start()
    out = tmp_path / "day1"
    try:
        collector.main(["session", "--plan", str(plan), "--day", "2026-11-01", "--out", str(out), "--yes",
                        "--countdown", "0", "--node", f"RX-A={os.ttyname(pairs[0][1])}",
                        "--node", f"RX-B={os.ttyname(pairs[1][1])}"])
    finally:
        stop.set()
    sessions, problems = load_sessions(out)
    assert not problems
    got = sorted((s.node, s.label, s.meta["scene"]) for s in sessions)
    assert got == [("RX-A", "empty", "start_empty"), ("RX-A", "lying", "lying_a"),
                   ("RX-B", "empty", "start_empty"), ("RX-B", "neighbor", "lying_a")]
    assert all(s.amp.shape[1] == 192 for s in sessions)
    log = (out / "session_log.csv").read_text(encoding="utf-8-sig").splitlines()
    assert log[0].startswith("시각,장면") and len(log) == 5


def test_plan_and_node_validation(tmp_path):
    plan = tmp_path / "p.csv"
    plan.write_text("scene,description,people,seconds,RX-A\ns1,x,0,5,sleeping\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        collector._load_plan(plan)
    plan.write_text("scene,description,people,seconds,RX-A\ns1,엑셀 저장,0,5,empty\n".encode("cp949").decode("cp949"),
                    encoding="cp949")
    rows, nodes = collector._load_plan(plan)
    assert nodes == ["RX-A"] and rows[0]["labels"] == {"RX-A": "empty"} and rows[0]["description"] == "엑셀 저장"
    assert collector._parse_node_ports(["RX-A=COM5", "RX-B=/dev/ttyUSB0"]) == {"RX-A": "COM5", "RX-B": "/dev/ttyUSB0"}
    for bad in (["RX-A"], ["RX-A=COM5", "RX-A=COM6"]):
        with pytest.raises(SystemExit):
            collector._parse_node_ports(bad)
    default_plan, default_nodes = collector._load_plan(Path(__file__).resolve().parent.parent / "docs/plans/day_plan.csv")
    assert default_nodes == ["RX-301", "RX-302", "RX-COR"] and len(default_plan) == 11
    assert default_plan[0]["labels"] == {"RX-301": "empty", "RX-302": "empty", "RX-COR": "empty"}
