# sample_data — 녹화 데이터 시연 시나리오

심사위원은 ESP32 노드가 없는 곳에서 배포 링크만 연다. 그래서 팀이 실제 공간에서 녹화한 CSI를
서버 안에서 다시 흘려보내 라이브와 같은 처리 경로(기준선 → 특징 → 판단 → 구역 상태)를 보여 준다.

- 결과를 미리 정해 두는 기능이 아니다. 녹화된 CSI가 그 자리에서 처리된다.
- 화면에는 항상 "녹화 데이터 재생"으로 표시된다. 발표 부록에도 사용 범위를 적는다.
- `"synthetic": true`인 시나리오(합성 신호)는 서버가 `ALLOW_SYNTHETIC_DEMO=1`일 때만 재생한다. 배포에서는 끈다.

## 폴더 구성

```
sample_data/
  lab-2rooms-1105/          ← 시나리오 하나
    scenario.json
    plan.png                ← 선택. 평면도
    RX-301_cal.csv          ← 빈 방 구간(기준선용, 40초 이상)
    RX-301_stream.csv       ← 재생할 구간
    RX-302_cal.csv
    RX-302_stream.csv
```

CSV는 수집 도구(`tools/collector.py record`)가 만드는 형식(`t,rssi,data`)이다.
기준선용 파일과 재생용 파일은 같은 날, 같은 노드 배치에서 녹화해야 한다.

## scenario.json 예시

```json
{
  "id": "lab-2rooms-1105",
  "title": "공학관 실습실 두 곳과 복도",
  "description": "11/5 실측. 302호에 1명이 들어가 바닥에 눕는다.",
  "synthetic": false,
  "recorded_at": "2026-11-05",
  "plan_image": "plan.png",
  "plan_size": [1000, 640],
  "duration_seconds": 90,
  "zones": [
    {"name": "301호", "polygon": [[0.04, 0.06], [0.47, 0.06], [0.47, 0.6], [0.04, 0.6]]},
    {"name": "302호", "polygon": [[0.53, 0.06], [0.96, 0.06], [0.96, 0.6], [0.53, 0.6]]}
  ],
  "nodes": [
    {"name": "TX-1", "role": "tx", "x": 0.5, "y": 0.63},
    {"name": "RX-301", "role": "rx", "x": 0.25, "y": 0.33, "zone": "301호",
     "calibration": "RX-301_cal.csv", "stream": "RX-301_stream.csv"},
    {"name": "RX-302", "role": "rx", "x": 0.75, "y": 0.33, "zone": "302호",
     "calibration": "RX-302_cal.csv", "stream": "RX-302_stream.csv"}
  ],
  "timeline": [
    {"at": 0, "text": "두 방 모두 비어 있음"},
    {"at": 35, "text": "302호에 1명 입실, 바닥에 누움"}
  ]
}
```

`timeline`은 녹화 당시 실제로 있었던 일(정답)이다. 화면에서 AI 판단과 나란히 보여 줘서
맞았는지 틀렸는지 그대로 드러나게 한다.

## 개발용 합성 시나리오

화면을 만들 때만 쓴다: `python tools/make_dev_scenario.py` → `sample_data/_dev_synthetic/`(저장소에 올리지 않음).
