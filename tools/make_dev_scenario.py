"""화면 개발용 합성 신호 시나리오를 만든다.

실제 측정값이 아니다. 대시보드를 만들고 고칠 때 화면이 어떻게 바뀌는지 보려는 용도다.
- 시나리오에 "synthetic": true가 붙고, 서버는 ALLOW_SYNTHETIC_DEMO=1일 때만 재생한다.
- 화면에는 '합성 신호(개발용)'로 표시된다. 시연 영상·발표·배포에는 쓰지 않는다.
- 결과 폴더(sample_data/_dev_synthetic)는 .gitignore에 들어 있다.

backend 폴더에서:  python tools/make_dev_scenario.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from synthetic import SyntheticRoom, write_session  # noqa: E402

OUT = ROOT / "sample_data" / "_dev_synthetic"
W, H = 1000, 640

ZONES = {
    "301호": [[0.04, 0.06], [0.47, 0.06], [0.47, 0.6], [0.04, 0.6]],
    "302호": [[0.53, 0.06], [0.96, 0.06], [0.96, 0.6], [0.53, 0.6]],
    "복도": [[0.04, 0.66], [0.96, 0.66], [0.96, 0.94], [0.04, 0.94]],
}


def draw_plan(path: Path) -> None:
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    fig.patch.set_facecolor("white")
    for name, poly in ZONES.items():
        x0, y0 = poly[0][0] * W, poly[0][1] * H
        x1, y1 = poly[2][0] * W, poly[2][1] * H
        # 구역 이름은 화면이 평면도 위에 따로 그린다(서버 글꼴에 한글이 없을 수 있어 이미지에는 넣지 않음)
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, linewidth=4, edgecolor="#333"))
    # 문
    for x in (0.2, 0.75):
        ax.plot([x * W - 30, x * W + 30], [0.6 * H, 0.6 * H], color="white", linewidth=6)
    fig.savefig(path, dpi=100)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    draw_plan(OUT / "plan.png")
    t0 = 1_700_300_000.0
    plan = {
        "RX-301": ("301호", 41, [("empty", 20), ("lying", 50)]),
        "RX-302": ("302호", 42, [("empty", 70)]),
        "RX-COR": ("복도", 43, [("empty", 10), ("walking", 15), ("empty", 45)]),
    }
    nodes = [{"name": "TX-1", "role": "tx", "x": 0.5, "y": 0.63}]
    positions = {"RX-301": (0.25, 0.33), "RX-302": (0.75, 0.33), "RX-COR": (0.8, 0.8)}
    for name, (zone, seed, parts) in plan.items():
        room = SyntheticRoom(seed=seed)
        write_session(OUT, f"{name}_cal", room.frames(40, "empty", seed=seed, t0=t0 - 200),
                      {"node": name, "label": "empty", "day": "dev"})
        frames, t = [], t0
        for k, (cond, secs) in enumerate(parts):
            chunk = room.frames(secs, cond, seed=seed * 10 + k, t0=t)
            frames.extend(chunk)
            t = chunk[-1][0] + 0.02
        write_session(OUT, f"{name}_stream", frames, {"node": name, "label": "mixed", "day": "dev"})
        x, y = positions[name]
        nodes.append({"name": name, "role": "rx", "x": x, "y": y, "zone": zone,
                      "calibration": f"{name}_cal.csv", "stream": f"{name}_stream.csv"})
    scenario = {
        "id": "dev-synthetic",
        "title": "합성 신호(개발용) · 두 방과 복도",
        "description": "화면 개발용 합성 신호입니다. 실제 측정값이 아니며 시연·발표에 쓰지 않습니다.",
        "synthetic": True,
        "recorded_at": "",
        "plan_image": "plan.png",
        "plan_size": [W, H],
        "duration_seconds": 70,
        "zones": [{"name": k, "polygon": v} for k, v in ZONES.items()],
        "nodes": nodes,
        "timeline": [
            {"at": 0, "text": "세 구역 모두 비어 있음"},
            {"at": 10, "text": "복도로 1명 지나감(15초)"},
            {"at": 20, "text": "301호에 1명 쓰러져 누워 있음"},
        ],
    }
    (OUT / "scenario.json").write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"만들었습니다: {OUT}\n서버를 ALLOW_SYNTHETIC_DEMO=1로 켜야 재생할 수 있습니다.")


if __name__ == "__main__":
    main()
