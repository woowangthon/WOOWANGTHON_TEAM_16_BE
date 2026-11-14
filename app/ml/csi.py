"""ESP32 CSI 파싱.

ESP32는 서브캐리어마다 [허수, 실수] 순서의 부호 있는 바이트 두 개를 내보낸다
(ESP-IDF Wi-Fi CSI 문서). 우리는 진폭만 쓰므로 순서가 바뀌어도 결과는 같지만,
위상을 쓰게 될 때를 대비해 순서를 지켜 둔다.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class CsiFormatError(ValueError):
    pass


def iq_to_complex(raw: Sequence[int] | np.ndarray) -> np.ndarray:
    a = np.asarray(raw, dtype=np.float64)
    if a.ndim != 1 or a.size == 0:
        raise CsiFormatError("CSI 배열이 비어 있거나 1차원이 아닙니다.")
    if a.size % 2:
        raise CsiFormatError(f"CSI 배열 길이가 홀수입니다({a.size}).")
    if not np.all(np.isfinite(a)):
        raise CsiFormatError("CSI 배열에 숫자가 아닌 값이 있습니다.")
    return a[1::2] + 1j * a[0::2]


def iq_to_amplitude(raw: Sequence[int] | np.ndarray) -> np.ndarray:
    return np.abs(iq_to_complex(raw))


@dataclass
class Frame:
    t: float  # 초 단위 시각(수집 PC 기준 epoch)
    rssi: float
    amp: np.ndarray  # 서브캐리어별 진폭


def _parse_int_list(text: str) -> list[int]:
    text = text.strip().strip('"').strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    sep = "," if "," in text else None
    return [int(v) for v in text.split(sep) if v.strip()]


# esp-csi csi_recv 예제(2026-04 커밋 8633d67 기준)가 데이터 앞에 찍는 필드 이름.
# ESP32·S3·C3 형식과 C5·C6·C61 형식이 다르고, 둘 다 마지막 두 필드가 len, first_word다.
ESP_FIELDS_S3 = ("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,"
                 "sgi,noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word").split(",")
ESP_FIELDS_C6 = "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word".split(",")


@dataclass
class CsiRecord:
    """CSI_DATA 한 줄에서 꺼낸 값. raw는 [허수, 실수, 허수, 실수, ...]."""

    rssi: float | None
    ts: float | None  # 보드의 local_timestamp(µs)
    raw: list[int]
    seq: int | None = None  # 송신 보드가 패킷에 넣은 번호
    mac: str | None = None
    channel: int | None = None


def _known_fields(fields: list[str], header: list[str] | None) -> list[str] | None:
    if header and "rssi" in header:
        return header
    if len(fields) == len(ESP_FIELDS_S3):
        return ESP_FIELDS_S3
    if len(fields) == len(ESP_FIELDS_C6):
        return ESP_FIELDS_C6
    return None


def parse_esp_csi_record(line: str, header: list[str] | None = None) -> CsiRecord | None:
    """esp-csi csi_recv 예제의 'CSI_DATA,...' 한 줄을 CsiRecord로 바꾼다. 깨진 줄이면 None.

    - 헤더 줄을 받았으면 이름으로 찾고, 없으면 필드 개수로 형식(S3 또는 C6)을 알아낸다.
      둘 다 아니면 모든 형식에서 공통인 4번째 필드(rssi)만 쓴다.
    - 'len' 필드와 실제 값 개수가 다르면 시리얼 전송 중에 잘린 줄로 보고 버린다.
    - first_word가 1이면 앞 4개 값이 하드웨어 한계로 무효라서 0으로 바꾼다(ESP-IDF 문서).
    - 줄 앞에 로그 색 코드나 다른 글자가 붙어도 'CSI_DATA'부터 읽는다.
    """
    k = line.find("CSI_DATA")
    if k < 0:
        return None
    line = line[k:].strip()
    lb, rb = line.rfind("["), line.rfind("]")
    if lb < 0 or rb < lb:
        return None
    try:
        raw = _parse_int_list(line[lb : rb + 1])
    except ValueError:
        return None
    head = line[:lb].rstrip().rstrip('"').rstrip(",")
    fields = next(csv.reader([head]))
    names = _known_fields(fields, header)

    def get(name: str, cast):
        if names is None or name not in names:
            return None
        i = names.index(name)
        if i >= len(fields):
            return None
        try:
            return cast(fields[i])
        except ValueError:
            return None

    if names is not None:
        n = get("len", int)
        if n is not None and n != len(raw):
            return None
        if get("first_word", int) == 1 and len(raw) >= 4:
            raw[:4] = [0, 0, 0, 0]
        rssi = get("rssi", float)
    else:
        try:
            rssi = float(fields[3]) if len(fields) > 3 else None
        except ValueError:
            rssi = None
    seq = get("id", int)
    if seq is None:
        seq = get("seq", int)
    return CsiRecord(rssi=rssi, ts=get("local_timestamp", float), raw=raw, seq=seq,
                     mac=get("mac", str), channel=get("channel", int))


def parse_esp_csi_line(line: str, header: list[str] | None = None) -> tuple[float | None, float | None, list[int]] | None:
    """parse_esp_csi_record의 간단한 형태: (rssi, local_timestamp_us, raw)."""
    rec = parse_esp_csi_record(line, header)
    if rec is None:
        return None
    return rec.rssi, rec.ts, rec.raw


def read_session_csv(path: str | Path, nominal_fps: float = 50.0) -> list[Frame]:
    """세션 파일을 읽는다.

    1) 수집 도구가 쓰는 형식: 헤더 't,rssi,data', data는 "[...]"
    2) esp-csi 로그를 그대로 저장한 파일: 'CSI_DATA,...' 줄. 이 경우 시각은
       local_timestamp(µs)를 쓰고, 그것도 없으면 nominal_fps로 균일 간격을 가정한다.
    """
    path = Path(path)
    frames: list[Frame] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
        fh.seek(0)
        if first.lower().startswith("t,rssi,data"):
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    raw = _parse_int_list(row["data"])
                    amp = iq_to_amplitude(raw)
                    frames.append(Frame(float(row["t"]), float(row.get("rssi") or "nan"), amp))
                except (CsiFormatError, ValueError, KeyError):
                    continue
            return frames

        header: list[str] | None = None
        times: list[float | None] = []
        parsed: list[tuple[float | None, np.ndarray]] = []
        for line in fh:
            if line.startswith("type,"):
                header = [h.strip() for h in line.strip().split(",")]
                continue
            res = parse_esp_csi_line(line, header)
            if res is None:
                continue
            rssi, ts, raw = res
            try:
                amp = iq_to_amplitude(raw)
            except CsiFormatError:
                continue
            times.append(ts)
            parsed.append((rssi, amp))
    if not parsed:
        return frames
    if all(t is not None for t in times):
        t0 = float(times[0])  # type: ignore[arg-type]
        ts_sec = [(float(t) - t0) / 1e6 for t in times]  # type: ignore[arg-type]
    else:
        ts_sec = [i / nominal_fps for i in range(len(parsed))]
    for t, (rssi, amp) in zip(ts_sec, parsed):
        frames.append(Frame(t, float("nan") if rssi is None else rssi, amp))
    return frames


def pack_session_csv(csv_path: str | Path, npz_path: str | Path) -> tuple[int, int]:
    """수집 도구 형식 CSV(t,rssi,data)를 묶음 파일(.npz)로 옮겨 적는다. (옮긴 줄 수, 버린 줄 수)를 돌려준다.

    값은 바꾸지 않는다. t는 float64, rssi는 float32, data는 보드가 보낸 정수 그대로 int16으로 담는다
    (이득 보정을 거친 값이라 한 바이트 범위를 넘는다).
    글자 CSV는 수십 MB를 한 줄씩 풀어야 해서 작은 서버에서 재생을 시작하는 데 몇 분이 걸린다.
    길이가 다른 줄은 frames_to_arrays가 어차피 버리므로 여기서도 가장 많은 길이만 남긴다.
    """
    ts: list[float] = []
    rssis: list[float] = []
    rows: list[list[int]] = []
    with Path(csv_path).open("r", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            try:
                raw = _parse_int_list(row["data"])
                t = float(row["t"])
                rssi = float(row.get("rssi") or "nan")
            except (ValueError, KeyError, TypeError):
                continue
            if not raw or len(raw) % 2 or min(raw) < -32768 or max(raw) > 32767:
                continue
            ts.append(t)
            rssis.append(rssi)
            rows.append(raw)
    if not rows:
        raise CsiFormatError(f"{csv_path}: 옮길 프레임이 없습니다.")
    lengths, counts = np.unique([len(r) for r in rows], return_counts=True)
    width = int(lengths[np.argmax(counts)])
    keep = [i for i, r in enumerate(rows) if len(r) == width]
    np.savez_compressed(
        npz_path,
        t=np.array([ts[i] for i in keep], dtype=np.float64),
        rssi=np.array([rssis[i] for i in keep], dtype=np.float32),
        data=np.array([rows[i] for i in keep], dtype=np.int16),
    )
    return len(keep), len(rows) - len(keep)


def read_session_npz(path: str | Path) -> list[Frame]:
    """pack_session_csv가 만든 묶음 파일을 읽는다. 진폭 계산은 iq_to_amplitude와 같은 식이다."""
    with np.load(Path(path)) as z:
        t = np.asarray(z["t"], dtype=np.float64)
        rssi = np.asarray(z["rssi"], dtype=np.float64)
        raw = np.asarray(z["data"], dtype=np.float64)
    if raw.ndim != 2 or raw.shape[0] != t.size or rssi.size != t.size or raw.shape[1] == 0 or raw.shape[1] % 2:
        raise CsiFormatError(f"{path}: 묶음 파일의 모양이 맞지 않습니다.")
    amp = np.abs(raw[:, 1::2] + 1j * raw[:, 0::2])
    return [Frame(float(t[i]), float(rssi[i]), amp[i]) for i in range(t.size)]


def read_session(path: str | Path) -> list[Frame]:
    """확장자를 보고 묶음 파일(.npz)이나 CSV를 읽는다."""
    path = Path(path)
    return read_session_npz(path) if path.suffix.lower() == ".npz" else read_session_csv(path)


def frames_to_arrays(frames: Iterable[Frame], n_subcarriers: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """프레임 목록을 (t, rssi, amp[T,S]) 배열로. 서브캐리어 수가 다른 프레임은 버린다."""
    frames = list(frames)
    if not frames:
        return np.empty(0), np.empty(0), np.empty((0, n_subcarriers or 0))
    if n_subcarriers is None:
        lengths, counts = np.unique([f.amp.size for f in frames], return_counts=True)
        n_subcarriers = int(lengths[np.argmax(counts)])
    keep = [f for f in frames if f.amp.size == n_subcarriers]
    keep.sort(key=lambda f: f.t)
    t = np.array([f.t for f in keep], dtype=np.float64)
    rssi = np.array([f.rssi for f in keep], dtype=np.float64)
    amp = np.vstack([f.amp for f in keep]) if keep else np.empty((0, n_subcarriers))
    return t, rssi, amp


def frame_from_payload(item: dict) -> Frame:
    """API로 들어온 프레임 하나({t, rssi, data})를 Frame으로."""
    raw = item.get("data")
    if isinstance(raw, str):
        raw = json.loads(raw)
    amp = iq_to_amplitude(raw)
    rssi = item.get("rssi")
    return Frame(float(item["t"]), float("nan") if rssi is None else float(rssi), amp)
