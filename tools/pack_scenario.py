"""재생 시나리오의 CSV를 묶음 파일(.npz)로 옮겨 적는다.

    python tools/pack_scenario.py <CSV가 든 폴더> <시나리오 폴더>

- 값은 바꾸지 않는다(app/ml/csi.py의 pack_session_csv).
- 옮긴 뒤 CSV와 묶음 파일을 각각 읽어 시각·신호 세기·진폭이 같은지 확인한다. 다르면 멈춘다.
- 작은 서버는 수십 MB 글자 CSV를 푸는 데 몇 분이 걸려서, 배포용 시나리오는 묶음 파일로 둔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ml.csi import frames_to_arrays, pack_session_csv, read_session_csv, read_session_npz  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*.csv"))
    if not files:
        print(f"{src}에 CSV가 없습니다.")
        return 1
    for csv_path in files:
        npz_path = dst / (csv_path.stem + ".npz")
        kept, dropped = pack_session_csv(csv_path, npz_path)
        a = frames_to_arrays(read_session_csv(csv_path))
        b = frames_to_arrays(read_session_npz(npz_path))
        same = all(x.shape == y.shape and np.array_equal(x, y, equal_nan=True) for x, y in zip(a, b))
        print(f"{csv_path.name}: {csv_path.stat().st_size / 1e6:.1f}MB -> {npz_path.name} {npz_path.stat().st_size / 1e6:.1f}MB"
              f" · {kept}줄 · 버린 줄 {dropped} · CSV와 같은 값: {'예' if same else '아니오'}")
        if not same:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
