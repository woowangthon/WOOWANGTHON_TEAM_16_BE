"""11/7 현장 측정(1~4차)을 팀원에게 보낼 학습용 묶음으로 정리한다.

원본 폴더(..\\data)는 읽기만 하고 복사한다. 회차·제외 목록은 docs/field_tests/2026-11-07의 CSV를 그대로 따른다.

묶음 구성:
  README.md              회차별 정리(docs/field_tests/2026-11-07/README.md)
  manifest.csv           장면 파일마다 회차, 장면, 초당 프레임, 학습 사용 여부, 특수 상황
  묶음요약.txt            회차별 파일 수와 경고
  학습용/<회차>_<day>/     학습에 쓰는 장면 파일(.csv + .json). 학습은 이 폴더만 넣는다
  제외/<묶음>/            학습에서 뺀 장면 파일(.json이 있으니 학습용에 섞지 않는다)
  모델/                   학습한 모델(.joblib)과 모델 카드(.card.json)
  장면목록/               녹화에 쓴 장면 목록 CSV
  기록/<회차>_<day>/       session_log.csv, 화면 장면별 판단(judge_*.csv), --extra로 준 파일
  측정정리/               rounds.csv, events.csv, excluded.csv, training.csv

실행(C:\\wm\\watchmate-be, 표준 라이브러리만 씀):
  python tools/pack_field_data.py --data ..\\data --out ..\\watchmate_data_1107_all --zip
  python tools/pack_field_data.py --dry-run          # 복사하지 않고 파일 수만 확인

  --with-raw   연속 원본(_raw, 준비·이동 시간 포함)도 기록/에 넣는다. 용량이 크다
  --model      넣을 모델 파일(.joblib). 없으면 presence_model_1107_all, 그것도 없으면 models 폴더 전체
  --extra      기록/화면에 함께 넣을 파일(예: 화면에서 내려받은 기록 CSV). 여러 번 쓸 수 있다
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve()
BE = HERE.parents[1]
DOCS = BE / "docs" / "field_tests" / "2026-11-07"
DEFAULT_MODEL = "presence_model_1107_all"
MANIFEST_COLS = ["round", "pack_path", "file", "day", "node", "label", "scene_no", "scene", "description",
                 "started_at", "seconds", "frames", "hz", "used_for_training", "reason_or_event", "notes"]


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def split_list(value: str) -> set[str]:
    return {v.strip() for v in value.split(";") if v.strip()}


def plan_order(plan: Path) -> dict[str, int]:
    if not plan.exists():
        return {}
    return {r["scene"]: i for i, r in enumerate(read_csv(plan), 1)}


def local_time(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def scene_files(folder: Path) -> list[tuple[Path, Path, dict]]:
    """폴더 바로 아래 장면 파일(.json + 같은 이름 .csv). 연속 원본(_raw)은 보지 않는다."""
    out = []
    for meta_path in sorted(folder.glob("*.json")):
        csv_path = meta_path.with_suffix(".csv")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or "node" not in meta or "label" not in meta:
            continue  # 모델 카드 같은 다른 JSON
        if csv_path.exists():
            out.append((meta_path, csv_path, meta))
    return out


def events_for(events: list[dict], folder: str, scene: str, node: str) -> list[str]:
    hits = []
    for e in events:
        if e["folder"] != folder:
            continue
        if e["scene"] and e["scene"] != scene:
            continue
        if e["nodes"] and node not in split_list(e["nodes"]):
            continue
        hits.append(f"{e['what']} → {e['action']}" if e["action"] else e["what"])
    return hits


def manifest_row(round_name: str, pack_path: str, csv_path: Path, meta: dict, order: dict[str, int],
                 used: bool, reason: str) -> dict:
    frames = meta.get("frames")
    seconds = meta.get("seconds")
    hz = ""
    if isinstance(frames, (int, float)) and isinstance(seconds, (int, float)) and seconds > 0:
        hz = f"{frames / seconds:.1f}"
    scene = str(meta.get("scene", ""))
    return {
        "round": round_name, "pack_path": pack_path, "file": csv_path.stem, "day": meta.get("day", ""),
        "node": meta.get("node", ""), "label": meta.get("label", ""), "scene_no": order.get(scene, ""),
        "scene": scene, "description": meta.get("description", ""), "started_at": local_time(meta.get("started_at")),
        "seconds": f"{seconds:.1f}" if isinstance(seconds, (int, float)) else "", "frames": frames if frames is not None else "",
        "hz": hz, "used_for_training": "예" if used else "아니오", "reason_or_event": reason, "notes": meta.get("notes", ""),
    }


def copy(src: Path, dst_dir: Path, dry: bool) -> int:
    size = src.stat().st_size
    if not dry:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / src.name)
    return size


def mb(n: int) -> str:
    return f"{n / 1e6:,.0f}MB"


def make_zip(folder: Path) -> Path:
    target = folder.with_suffix(".zip")
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    done, last = 0, 0.0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p in files:
            z.write(p, Path(folder.name) / p.relative_to(folder))
            done += p.stat().st_size
            if time.monotonic() - last > 2:
                print(f"  압축 중 {done / max(total, 1):5.1%}  ({mb(done)} / {mb(total)})", end="\r")
                last = time.monotonic()
    print(f"  압축 끝: {target} ({mb(target.stat().st_size)})            ")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="11/7 1~4차 측정을 학습용 묶음으로 정리")
    ap.add_argument("--data", default=str(BE.parent / "data"), help="sessions·excluded·models가 있는 폴더(기본 ..\\data)")
    ap.add_argument("--out", default=str(BE.parent / "watchmate_data_1107_all"), help="만들 묶음 폴더(없거나 빈 폴더)")
    ap.add_argument("--zip", action="store_true", help="다 만든 뒤 같은 이름의 .zip도 만든다")
    ap.add_argument("--with-raw", action="store_true", help="연속 원본(_raw)도 기록/에 넣는다")
    ap.add_argument("--model", action="append", default=[], help="넣을 모델 .joblib(여러 번 가능)")
    ap.add_argument("--extra", action="append", default=[], help="기록/화면에 넣을 파일(여러 번 가능)")
    ap.add_argument("--dry-run", action="store_true", help="복사하지 않고 파일 수와 경고만 본다")
    args = ap.parse_args(argv)

    data, out, dry = Path(args.data).resolve(), Path(args.out).resolve(), args.dry_run
    if not (data / "sessions").is_dir():
        print(f"{data}\\sessions 폴더가 없습니다. --data로 측정 데이터 폴더를 알려 주세요.")
        return 2
    if not dry and out.exists() and any(out.iterdir()):
        print(f"{out} 폴더가 이미 있고 비어 있지 않습니다. 다른 --out을 주거나 그 폴더를 직접 치운 뒤 다시 실행하세요.")
        return 2
    sessions = data / "sessions"
    if out == sessions or sessions in out.parents:
        print("묶음 폴더를 sessions 안에 만들면 다음 학습에 섞입니다. --out을 sessions 밖으로 주세요.")
        return 2

    rounds = read_csv(DOCS / "rounds.csv")
    excluded = read_csv(DOCS / "excluded.csv")
    events = read_csv(DOCS / "events.csv")
    manifest: list[dict] = []
    summary: list[str] = []
    warnings: list[str] = []
    total = 0

    # 1) 학습용: 회차 폴더의 장면 파일
    for r in rounds:
        src = data / Path(r["folder"])
        order = plan_order(DOCS / r["plan"])
        dst = out / "학습용" / r["pack_dir"]
        if not src.is_dir():
            warnings.append(f"{r['round']}: {src} 폴더가 없습니다.")
            continue
        found = scene_files(src)
        for meta_path, csv_path, meta in found:
            if str(meta.get("day", "")) != r["day"]:
                warnings.append(f"{r['round']}: {meta_path.name}의 day가 {meta.get('day')}입니다({r['day']}가 아님).")
            total += copy(csv_path, dst, dry) + copy(meta_path, dst, dry)
            ev = events_for(events, r["folder"], str(meta.get("scene", "")), str(meta.get("node", "")))
            manifest.append(manifest_row(r["round"], f"학습용/{r['pack_dir']}", csv_path, meta, order, True, " / ".join(ev)))
        want = int(r["expected_scene_json"] or 0)
        line = f"{r['pack_dir']:<26} 장면 파일 {len(found)}쌍"
        if want and len(found) != want:
            line += f"  ← 예상 {want}쌍과 다름"
            hint = "제외할 파일이 아직 이 폴더에 있는지" if len(found) > want else "옮기다 빠진 파일이 있는지"
            warnings.append(f"{r['round']}: 장면 파일 {len(found)}쌍(예상 {want}쌍). {hint} 확인하세요.")
        summary.append(line)
        logs = [p for p in [src / "session_log.csv", *sorted(src.glob("judge_*.csv"))] if p.exists()]
        for p in logs:
            total += copy(p, out / "기록" / r["pack_dir"], dry)
        if args.with_raw and (src / "_raw").is_dir():
            for p in sorted((src / "_raw").rglob("*")):
                if p.is_file():
                    total += copy(p, out / "기록" / r["pack_dir"] / p.parent.relative_to(src), dry)

    # 2) 제외: 따로 옮겨 둔 묶음
    for x in excluded:
        src = data / Path(x["folder"])
        name = Path(x["folder"]).name
        if not src.is_dir():
            warnings.append(f"제외 {name}: {src} 폴더가 없습니다.")
            continue
        found = scene_files(src)
        order = {}
        for r in rounds:
            if r["day"] == x["day"]:
                order = plan_order(DOCS / r["plan"])
        for meta_path, csv_path, meta in found:
            total += copy(csv_path, out / "제외" / name, dry) + copy(meta_path, out / "제외" / name, dry)
            manifest.append(manifest_row(x["round"], f"제외/{name}", csv_path, meta, order, False, x["reason"]))
        want = int(x["expected_files"] or 0)
        line = f"제외/{name:<23} 파일 {len(found) * 2}개"
        if want and len(found) * 2 != want:
            line += f"  ← 예상 {want}개와 다름"
            warnings.append(f"제외 {name}: 파일 {len(found) * 2}개(예상 {want}개).")
        summary.append(line)

    # 3) 모델, 장면 목록, 측정 정리, README
    models_dir = data / "models"
    picked = [Path(m) for m in args.model]
    if not picked and models_dir.is_dir():
        picked = sorted(models_dir.glob(f"{DEFAULT_MODEL}*.joblib")) or sorted(models_dir.glob("*.joblib"))
    for m in picked:
        if not m.exists():
            warnings.append(f"모델 {m}이 없습니다.")
            continue
        total += copy(m, out / "모델", dry)
        card = m.with_suffix(".card.json")
        if card.exists():
            total += copy(card, out / "모델", dry)
        else:
            warnings.append(f"모델 카드 {card.name}이 없습니다.")
    if not picked:
        warnings.append("넣을 모델이 없습니다(--model로 지정 가능).")
    for p in sorted((DOCS / "plans").glob("*.csv")):
        total += copy(p, out / "장면목록", dry)
    for name in ("rounds.csv", "events.csv", "excluded.csv", "training.csv"):
        total += copy(DOCS / name, out / "측정정리", dry)
    total += copy(DOCS / "README.md", out, dry)
    for e in args.extra:
        p = Path(e)
        if p.is_file():
            total += copy(p, out / "기록" / "화면", dry)
        else:
            warnings.append(f"--extra {p}가 없습니다.")

    used = sum(1 for m in manifest if m["used_for_training"] == "예")
    head = [f"와치메이트 11/7 측정 묶음 · 만든 시각 {datetime.now():%Y-%m-%d %H:%M}",
            f"학습용 장면 파일 {used}쌍, 제외 {len(manifest) - used}쌍, 전체 {mb(total)}",
            "학습: python -m app.ml.train --data <이 폴더>/학습용 --out models/presence_model.joblib", ""]
    text = "\n".join(head + summary + ([""] + ["경고: " + w for w in warnings] if warnings else [])) + "\n"
    print(text)
    if dry:
        print("(--dry-run: 아무것도 복사하지 않았습니다)")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    (out / "묶음요약.txt").write_text(text, encoding="utf-8-sig")
    with (out / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, MANIFEST_COLS)
        w.writeheader()
        w.writerows(manifest)
    print(f"묶음을 만들었습니다: {out}")
    if args.zip:
        make_zip(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
