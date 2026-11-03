"""S4-1동 2층 평면도: 피난안내도 사진의 벽 위치(픽셀)를 223호 실측값으로 미터로 바꿔 그린다."""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
for f in font_manager.findSystemFonts():
    if "wqy-zenhei" in f.lower():
        font_manager.fontManager.addfont(f)
plt.rcParams["font.family"] = "WenQuanYi Zen Hei"

S = 7.35 / 171          # m/px: 223호 가로 7.35m = 171px (세로 5.80m = 135px, 같은 눈금)
X0 = 92
def X(px): return (px - X0) * S

BOT, COR, TOP = 4.80, 2.25, 5.80        # 아래 줄 깊이, 복도 폭, 위 줄 깊이(m)
YB0, YC0, YT0 = 0.0, BOT, BOT + COR
YT1 = YT0 + TOP

top = [(158, 407, "201"), (407, 468, "202"), (468, 590, "203"), (590, 838, "204"), (838, 1009, "223"),
       (1009, 1255, "205"), (1255, 1503, "206"), (1503, 1635, "207"), (1635, 1704, "208")]
bot = [(116, 297, "222"), (297, 478, "221"), (478, 540, "220"), (540, 596, "219"), (596, 658, "218"),
       (658, 779, "217"), (779, 838, "남\n화장실"),
       (1004, 1124, "224"), (1124, 1184, "216"), (1184, 1246, "215"), (1246, 1307, "214"), (1307, 1369, "213"),
       (1369, 1428, "212"), (1428, 1554, "211"), (1554, 1617, "210"), (1617, 1742, "209")]
LOBBY_TOP = 3.12                         # 계단·여자화장실 칸 윗벽(복도가 넓어지는 곳)
LOBBY_BOT = -2.0                         # 계단 칸이 건물 밖으로 나온 끝

ROOM, WALL, TXT = "#e9ebec", "#2b2f33", "#1c1f23"
HI = {"223": "#f4c2bd", "224": "#f7dcae"}


def draw(ax, annotate: bool):
    def room(x0, x1, y0, y1, name, color=ROOM, fs=11):
        ax.add_patch(Rectangle((X(x0), y0), X(x1) - X(x0), y1 - y0, facecolor=color, edgecolor=WALL, lw=1.6))
        ax.text((X(x0) + X(x1)) / 2, (y0 + y1) / 2, name, ha="center", va="center", fontsize=fs, color=TXT)
    # 복도
    ax.add_patch(Rectangle((X(116), YC0), X(1765) - X(116), COR, facecolor="#fbfbfa", edgecolor="none"))
    ax.add_patch(Rectangle((X(838), LOBBY_TOP), X(1004) - X(838), YC0 - LOBBY_TOP, facecolor="#fbfbfa", edgecolor="none"))
    for x0, x1, n in top:
        room(x0, x1, YT0, YT1, n, HI.get(n, ROOM) if annotate else ROOM, 12 if len(n) == 3 else 10)
    for x0, x1, n in bot:
        room(x0, x1, YB0, YC0, n, HI.get(n, ROOM) if annotate else ROOM, 12 if len(n) == 3 else 9)
    room(838, 903, LOBBY_BOT, LOBBY_TOP, "여\n화장실", ROOM, 9)
    room(903, 1004, LOBBY_BOT, LOBBY_TOP, "계단", "#d9eadf", 11)
    room(92, 158, YT0 + 0.3, YT1 - 0.6, "계단", "#d9eadf", 10)
    room(1704, 1765, YT0 + 0.3, YT1 - 0.6, "계단", "#d9eadf", 10)
    ax.text(X(400), YC0 + COR / 2, "복도", ha="center", va="center", fontsize=11, color="#5d636b")
    # 축척 막대 5m
    ax.plot([X(140), X(140) + 5], [-3.2, -3.2], color=WALL, lw=3)
    ax.text(X(140) + 2.5, -3.75, "5 m", ha="center", va="top", fontsize=10, color=TXT)
    ax.text(X(1765), -3.75, "S4-1동 2층 · 피난안내도를 223호 실측(7.35×5.80m) 눈금으로 환산, 오차 ±5%",
            ha="right", va="top", fontsize=9, color="#5d636b")

    if annotate:
        # 치수
        ax.annotate("", xy=(X(838), YT1 + 0.45), xytext=(X(1009), YT1 + 0.45),
                    arrowprops=dict(arrowstyle="<->", color=WALL, lw=1))
        ax.text((X(838) + X(1009)) / 2, YT1 + 0.6, "7.35 m(실측)", ha="center", va="bottom", fontsize=9)
        ax.annotate("", xy=(X(1004), -0.45), xytext=(X(1124), -0.45), arrowprops=dict(arrowstyle="<->", color=WALL, lw=1))
        ax.text((X(1004) + X(1124)) / 2, -0.6, "≈5.2 m", ha="center", va="top", fontsize=9)
        ax.text(X(1124) - 0.15, YB0 + BOT / 2, "≈4.7 m", ha="right", va="center", fontsize=8, rotation=90)
        ax.text(X(1009) + 0.2, YT0 + TOP / 2, "5.80 m(실측)", ha="left", va="center", fontsize=8, rotation=90)
        ax.annotate("", xy=(X(1670), YC0), xytext=(X(1670), YT0), arrowprops=dict(arrowstyle="<->", color=WALL, lw=1))
        ax.text(X(1670) - 0.25, YC0 + COR / 2, "복도 ≈2.25 m", ha="right", va="center", fontsize=9)
        # 배치 예시
        tx = (X(1006), YC0 + COR / 2)
        nodes = {"RX-301": ((X(838) + X(1009)) / 2 - 1.2, YT0 + 1.7), "RX-302": ((X(1004) + X(1124)) / 2, 1.3),
                 "RX-COR": (X(1006) - 7.5, YC0 + COR / 2)}
        for name, (x, y) in nodes.items():
            ax.plot([tx[0], x], [tx[1], y], ls="--", color="#c8281c", lw=1)
            ax.plot(x, y, "o", ms=9, color=TXT)
            ax.text(x, y - 0.45, name, ha="center", va="top", fontsize=9, fontweight="bold")
        ax.plot(*tx, "D", ms=10, color="#c8281c")
        ax.text(tx[0] + 0.3, tx[1] + 0.35, "TX-1", ha="left", va="bottom", fontsize=9, fontweight="bold", color="#c8281c")
        gw = (X(1390), YC0 + COR / 2)
        ax.plot(*gw, "s", ms=9, color="#2557b8")
        ax.text(gw[0], gw[1] - 0.45, "GW·노트북\n(구역 밖)", ha="center", va="top", fontsize=8, color="#2557b8")
        ax.text(X(838), YT1 + 1.6, "배치 예시: TX-1 복도(223·224 문 사이), RX-301=223호, RX-302=224호, RX-COR=복도 7~8m 떨어진 곳. "
                "문 위치를 보고 조정", ha="left", va="bottom", fontsize=9, color="#5d636b")


for name, annotate in (("s4-1_2f_plan.png", False), ("s4-1_2f_plan_annotated.png", True)):
    fig, ax = plt.subplots(figsize=(24, 7.2 if annotate else 6.4), dpi=110)
    draw(ax, annotate)
    ax.set_xlim(-1, X(1765) + 1)
    ax.set_ylim(-4.6, YT1 + (2.6 if annotate else 0.8))
    ax.set_aspect("equal")
    ax.axis("off")
    fig.savefig(OUT / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)

rows = [("위 줄", n, X(x1) - X(x0), TOP) for x0, x1, n in top] + \
       [("아래 줄", n.replace("\n", " "), X(x1) - X(x0), BOT) for x0, x1, n in bot]
for side, n, w, d in rows:
    print(f"{side}\t{n}\t{w:.2f}\t{d:.2f}")
print("length", round(X(1742) - X(116), 1), round(X(1765) - X(92), 1))
