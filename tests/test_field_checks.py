"""첫 수집 전 점검 기능: 걷기 시험 장면, 비교표, 노드 이름표 파일, 송신 보드 전원 경고."""

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import collector  # noqa: E402

from app.ml.compare import compare_sessions, format_report  # noqa: E402
from app.ml.sessions import load_sessions  # noqa: E402
from tests.synthetic import SyntheticRoom, write_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 걷기 시험 장면

def test_try_plan_has_walking_check_for_every_zone():
    plan, nodes = collector._load_plan(ROOT / "docs/plans/try_plan.csv")
    assert plan[0]["scene"] == "start_empty" and set(plan[0]["labels"].values()) == {"empty"}
    walking = [p for p in plan if p["scene"].startswith("walking_")]
    # 구역마다 걷기 장면이 하나씩: 그 구역 노드는 walking, 나머지는 neighbor
    assert sorted(n for p in walking for n, lab in p["labels"].items() if lab == "walking") == sorted(nodes)
    assert all(list(p["labels"].values()).count("walking") == 1 for p in walking)
    assert sum(p["seconds"] for p in plan) <= 300  # 시험은 5분 안에 끝난다


# ---------------------------------------------------------------- 비교표

def _day(folder: Path, node: str, scenes: list[tuple[str, str, str, float]], room: SyntheticRoom, day="d1"):
    t0 = 1_700_000_000.0
    for i, (scene, label, condition, seconds) in enumerate(scenes):
        frames = room.frames(seconds, condition, t0=t0, seed=i + 1)
        write_session(folder, f"{day}_{node}_{scene}", frames,
                      {"node": node, "label": label, "day": day, "scene": scene})
        t0 = frames[-1][0] + 5


def test_compare_flags_good_placement_and_bad_placement(tmp_path):
    room = SyntheticRoom(seed=3)
    # RX-A: 제대로 놓인 노드. 걷기·누움에 신호가 바뀐다
    _day(tmp_path, "RX-A", [("start_empty", "empty", "empty", 60), ("walking_a", "walking", "walking", 40),
                            ("lying_a", "lying", "lying", 40), ("end_empty", "empty", "empty", 40)], room)
    # RX-B: 걷기 장면인데 신호가 빈 방과 같다(송신→수신 경로가 구역을 지나지 않는 자리)
    _day(tmp_path, "RX-B", [("start_empty", "empty", "empty", 60), ("walking_b", "walking", "empty", 40)],
         SyntheticRoom(seed=4))
    sessions, problems = load_sessions(tmp_path)
    assert not problems
    groups = {g.node: g for g in compare_sessions(sessions)}
    a = {r.scene: r for r in groups["RX-A"].rows}
    assert a["start_empty"].status == "기준선"
    assert a["walking_a"].status == "정상" and a["walking_a"].big_ratio > 0.5
    assert a["end_empty"].status == "정상" and a["end_empty"].big_ratio <= 0.2
    assert a["walking_a"].score_med > a["end_empty"].score_med
    b = {r.scene: r for r in groups["RX-B"].rows}
    assert b["walking_b"].status == "확인" and "보드 자리" in b["walking_b"].message
    text = format_report(list(groups.values()))
    assert "■ RX-A · d1 · 기준선 start_empty" in text and "확인할 것:" in text and "RX-B d1 walking_b" in text


def test_compare_without_empty_session_explains(tmp_path):
    room = SyntheticRoom(seed=5)
    _day(tmp_path, "RX-A", [("lying_a", "lying", "lying", 30)], room)
    sessions, _ = load_sessions(tmp_path)
    [g] = compare_sessions(sessions)
    assert "빈 방" in g.note and g.rows[0].windows == 0


def test_compare_command(tmp_path, capsys):
    room = SyntheticRoom(seed=6)
    _day(tmp_path, "RX-A", [("start_empty", "empty", "empty", 40), ("walking_a", "walking", "walking", 30)], room)
    collector.main(["compare", "--data", str(tmp_path)])
    out = capsys.readouterr().out
    assert "RX-A" in out and "walking_a" in out and "읽는 법" in out
    with pytest.raises(SystemExit):
        collector.main(["compare", "--data", str(tmp_path / "없는폴더")])


# ---------------------------------------------------------------- 노드 이름표 파일

@pytest.fixture
def nodes_file(tmp_path, monkeypatch):
    path = tmp_path / "nodes.csv"
    path.write_text("# 설명 줄\nnode,mac\nRX-301,90:70:69:00:00:0A\nRX-302,90-70-69-00-00-0b\nRX-COR,\n",
                    encoding="utf-8")
    monkeypatch.setitem(collector._NODES, "path", path)
    return path


def test_node_names_resolve_and_block_wrong_pairs(nodes_file):
    assert collector.load_node_names() == {"RX-301": "90:70:69:00:00:0a", "RX-302": "90:70:69:00:00:0b"}
    assert collector.resolve_gw("RX-301") == "90:70:69:00:00:0a"
    assert collector.resolve_gw("90:70:69:00:00:0C") == "90:70:69:00:00:0c"
    assert collector._parse_node_ports(["RX-301=gw", "RX-302=gw:RX-302", "RX-COR=COM6"]) == {
        "RX-301": "gw:RX-301", "RX-302": "gw:RX-302", "RX-COR": "COM6"}
    assert collector._record_targets(argparse_ns(rx=["gw:RX-301:lying"])) == [("gw:RX-301", "RX-301", "lying")]
    for bad in (["RX-301=gw:RX-302"], ["RX-301=gw:90:70:69:00:00:0b"]):  # 다른 노드 것을 붙임
        with pytest.raises(SystemExit):
            collector._parse_node_ports(bad)
    with pytest.raises(SystemExit):  # MAC을 아직 안 적은 노드
        collector.resolve_gw("RX-COR")


def test_node_names_reject_duplicate_mac(tmp_path, monkeypatch):
    path = tmp_path / "nodes.csv"
    path.write_text("node,mac\nRX-301,90:70:69:00:00:0a\nRX-302,90:70:69:00:00:0a\n", encoding="utf-8")
    monkeypatch.setitem(collector._NODES, "path", path)
    with pytest.raises(SystemExit):
        collector.load_node_names()


def test_repo_nodes_file_is_readable():
    names = collector.load_node_names(ROOT / "nodes.csv")
    assert set(names) <= {"RX-301", "RX-302", "RX-COR"}


def argparse_ns(**kw):
    import argparse

    base = dict(rx=[], port=None, node=None, label=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- 송신 보드 전원 경고

def _st(tx_span: int, seconds: float = 10.0, csi: int = 160) -> dict:
    step = max(1, tx_span // csi)
    return {"seconds": seconds, "lines": csi, "csi": csi, "bad": 0, "logs": 0, "garbage": 0, "header": True,
            "lengths": Counter({384: csi}), "rssi": [-40.0] * csi, "macs": Counter(), "channels": Counter({11: csi}),
            "seq": list(range(0, tx_span, step))[:csi]}


def test_tx_power_warning():
    ok, lines = collector._verdict(_st(tx_span=230), 921600)  # 11/1 보조배터리: 송신 약 23Hz
    assert not ok and any("보조배터리" in x for x in lines)
    ok, lines = collector._verdict(_st(tx_span=800, csi=740), 921600)  # 노트북 USB 전원: 약 74~80Hz
    assert ok and not any("보조배터리" in x for x in lines)


def _st_spread(rx: int, tx_span: int, seconds: float = 10.0) -> dict:
    """송신 번호 0..tx_span-1 중 rx개만 받은 점검 결과(번호가 고르게 빠짐)."""
    st = _st(tx_span=tx_span, seconds=seconds, csi=rx)
    st["seq"] = [int(x) for x in np.linspace(0, tx_span - 1, rx)]
    return st


def test_check_flags_node_that_hears_part_of_tx():
    # 11/3 R06 RX-302: 무선 손실 0.2%였지만 송신 91Hz 중 52Hz만 받았다
    ok, lines = collector._verdict(_st_spread(rx=524, tx_span=910), 921600)
    assert not ok and any("보낸 것의 58%만" in x for x in lines)
    # 11/3 R09 실제 배치: 송신 약 91Hz 중 83~93Hz
    ok, lines = collector._verdict(_st_spread(rx=850, tx_span=910), 921600)
    assert ok and any("송신 대비 받은 비율 93%" in x for x in lines)
