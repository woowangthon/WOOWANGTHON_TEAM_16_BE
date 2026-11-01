# 와치메이트 백엔드 (WOOWANGTHON_TEAM_XX_BE)

화재 경보가 울리면 건물에 미리 설치한 Wi-Fi 노드(ESP32-S3)의 채널 상태 정보(CSI)를 받아,
평면도 구역마다 사람 기척이 있는지 추정해 현장지휘관 화면에 보내는 서버입니다.
2026 AI 우왕톤 팀 와치메이트의 출품작이며, 프론트엔드는 `WOOWANGTHON_TEAM_XX_FE` 저장소에 있습니다.

- AI는 구역별 기척 확률과 근거를 2초마다 갱신하고 수색 우선순위를 제시합니다.
- 진입 여부와 수색 순서는 지휘관이 정하고, 그 결정을 같은 화면에 기록합니다.
- 평상시에는 감지가 꺼져 있고 들어온 CSI는 저장하지 않습니다. 경보가 오거나 지휘관이 직접 상황을 열 때만 분석합니다.
- 감지되지 않은 구역을 "사람 없음"으로 표시하지 않습니다. 미탐지는 부재의 증거가 아닙니다.

## 바로 써 보기

- 서비스: `https://<배포 주소>` (제출 폼에 적은 주소)
- API 문서: `https://<백엔드 주소>/docs`, 상태 확인: `https://<백엔드 주소>/api/health`
- 테스트 계정: 제출 폼에 적은 아이디·비밀번호
- 확인 순서: 로그인 → 건물 목록의 "녹화 데이터로 시연"에서 실측 녹화 시나리오의 재생 시작 → 지휘 화면에서 구역 상태와 근거를 보고 구역 하나에 수색 중·구조 완료 기록 → 상황 종료 → 위쪽 메뉴의 AI 검증에서 모델 카드 확인
- 무료 서버라 처음 접속하면 깨우는 데 1분쯤 걸릴 수 있습니다.
- push할 때마다 GitHub Actions가 테스트를 돌립니다(`.github/workflows/ci.yml`).

## 이 저장소에 있는 것

```
app/
  main.py            FastAPI 앱, 시작할 때 테이블 생성·테스트 계정·모델 읽기
  config.py          환경변수 읽기(.env 지원)
  models.py          DB 테이블(건물·구역·노드·기준선·상황·추론·결정·이벤트)
  routers/           API (auth, buildings, ingest, incidents, model, demo)
  services/
    stream.py        노드별 버퍼, 보정, 2초 창 처리, 추론 저장
    state.py         구역 상태·우선순위·타임라인 계산
    replay.py        녹화 데이터 재생(심사용 시연)
    registry.py      사람 판별 모델 로딩
  ml/
    csi.py           esp-csi 출력 파싱, I/Q → 진폭
    features.py      2초 창 특징 8개
    baseline.py      노드별 빈 공간 기준선, z-점수, 1단계 변화 확률
    model.py         2단계 사람 판별 모델 저장·로딩
    sessions.py      수집 세션 읽기, 라벨 정의
    train.py         학습·검증 명령(하루 단위 교차검증 → 모델 카드)
firmware/
  csi_node/          무선 수신 노드(esp-csi csi_recv 기반, 측정값을 ESP-NOW로 게이트웨이에 전송)
  csi_gateway/       게이트웨이(노드들의 측정값을 받아 USB로 노트북에 전달)
  common/wm_proto.h  노드·게이트웨이·수집 도구가 함께 쓰는 데이터 형식
tools/
  collector.py       ESP32 시리얼·게이트웨이 → 보드 점검, 장면별 녹화, 서버 실시간 전송
  make_dev_scenario.py  화면 개발용 합성 시나리오(배포에서는 꺼짐)
models/              학습 결과(모델 파일 + 모델 카드 JSON)
sample_data/         심사용 녹화 데이터 시나리오
docs/DATA_COLLECTION.md  ESP32 데이터 수집 절차
docs/WIRELESS.md     무선 노드·게이트웨이 펌웨어 올리기와 점검
docs/plans/          수집 장면 목록(장면별 노드 라벨)
tests/               단위·API 통합 테스트
```

## 기술 선택 이유

| 영역 | 선택 | 이유 |
|---|---|---|
| 언어·프레임워크 | Python 3.13, FastAPI | 학습할 때와 서비스할 때 같은 신호처리 코드(numpy·scipy)를 쓰려고 백엔드를 Python으로 정했습니다. 요청·응답 스키마가 `/docs`에 자동으로 정리됩니다. |
| DB | SQLAlchemy 2, 로컬 SQLite / 배포 PostgreSQL(Neon) | 코드는 그대로 두고 `DATABASE_URL`만 바꿉니다. 무료 서버 디스크는 재시작 때 지워지므로 평면도 이미지도 DB에 넣습니다. |
| AI | 기준선 정규화 특징 8개 + scikit-learn 로지스틱 회귀 | 데이터가 수천 개 창 규모라 작은 모델이 더 안정적이고, 어떤 특징 때문에 판단했는지 설명할 수 있습니다. 추론이 밀리초 단위라 무료 서버에서도 돕니다. |
| 수집 | ESP32-S3 + Espressif esp-csi 예제 펌웨어 + pyserial | 펌웨어는 공식 예제를 그대로 쓰고, 우리는 수집·전송·학습 쪽을 만들었습니다. |
| 외부 AI API | 쓰지 않음 | 판단은 우리가 학습한 모델로만 합니다. 따라서 관리할 API 키가 없습니다. |

## AI가 하는 일

1. **1단계, 변화 감지** — 보정 때 저장한 노드별 빈 공간 기준선과 비교해, 지금 신호가 얼마나 다른지를 z-점수로 계산합니다. 임계값은 보정 때 빈 공간 창의 99백분위로 노드마다 자동으로 정해집니다.
2. **2단계, 사람 판별** — 기준선으로 정규화한 특징 8개를 학습된 모델에 넣어 사람일 확률을 냅니다. 선풍기·문·열기류처럼 사람이 아닌 흔들림과 쓰러져 거의 움직이지 않는 사람을 가르는 것이 목적입니다.
3. **구역 융합** — 같은 구역 노드들의 최근 결과를 묶어 구역 상태(기척 감지 0.70↑ / 가능성 있음 0.35↑ / 미감지 / 판단 불가 / 보정 필요)와 근거 문장을 만듭니다.

모델 파일이 없으면 1단계만 동작하고, 화면 문구가 "사람 기척"이 아니라 "신호 변화"로 바뀝니다.
검증하지 않은 판별을 사람이라고 부르지 않기 위해서입니다.

### 검증 방법

학습 명령이 검증까지 한 번에 하고 결과를 모델 카드(`models/presence_model.card.json`)로 저장합니다.
웹의 "AI 검증" 화면은 이 파일을 그대로 보여 줍니다. 숫자를 손으로 옮겨 적지 않습니다.

- 수집한 날 하나를 통째로 빼고 학습해 그날로 평가합니다(leave-one-day-out). 같은 날 창을 섞어 나누면 이웃한 창끼리 거의 같아서 점수가 부풀려집니다.
- 같은 데이터로 1단계만 쓴 경우, 다른 날 기준선을 그대로 쓴 경우(재보정 없음)를 함께 잽니다.
- 누워 있음·가려진 자리는 재현율을, 옆 구역에만 사람이 있을 때와 선풍기·문·열기류는 오경보율을 따로 보고합니다.

## 로컬 실행

Python 3.11 이상(3.13에서 테스트). 저장소 루트에서 실행합니다.

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env      # 필요한 값만 채운다. 비워 두면 SQLite와 임시 키로 돈다
uvicorn app.main:app --reload --port 8000
```

macOS·Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

- API 문서: http://localhost:8000/docs
- 상태 확인: http://localhost:8000/api/health
- 테스트 계정: `.env`의 `DEMO_USERNAME` / `DEMO_PASSWORD` (기본값 `demo` / `demo1234`). 서버가 시작할 때 이 계정을 만들거나 비밀번호를 맞춰 줍니다.

테스트:

```bash
python -m pytest -q
```

테스트의 합성 신호(`tests/synthetic.py`)는 코드가 끝까지 도는지 확인하는 용도로만 씁니다. 화면·영상·발표 결과에는 쓰지 않습니다.

## 환경변수

| 이름 | 설명 |
|---|---|
| `DATABASE_URL` | 비우면 `data/app.db`(SQLite). 배포 때는 Neon 주소(`postgres://`, `postgresql://` 둘 다 됨) |
| `SECRET_KEY` | 로그인 토큰 서명 키. 없으면 임시 키를 만들고 경고를 남깁니다(재시작하면 로그인이 풀림) |
| `DEMO_USERNAME`, `DEMO_PASSWORD` | 심사용 테스트 계정 |
| `CORS_ORIGINS` | 프론트 주소, 쉼표로 여러 개 |
| `MODEL_PATH` | 사람 판별 모델 경로. 기본 `models/presence_model.joblib` |
| `ALLOW_SYNTHETIC_DEMO` | 합성 시나리오 재생 허용(개발용). 배포에서는 `0` |
| `TOKEN_TTL_HOURS` | 로그인 유지 시간, 기본 12 |

비밀 값은 저장소에 올리지 않습니다(`.env`는 `.gitignore`에 들어 있음). 노드 토큰과 경보 토큰은 발급할 때 한 번만 보여 주고 DB에는 해시만 남깁니다.

## 데이터 수집과 학습

자세한 절차는 [docs/DATA_COLLECTION.md](docs/DATA_COLLECTION.md)에 있습니다. 요약:

```bash
# 수신 보드를 USB(CH343 쪽 포트)로 연결하고 포트 확인
python tools/collector.py ports

# 보드 점검: 초당 프레임, CSI 길이, 신호 세기를 보고 정상인지 판정
python tools/collector.py check --port COM6 --port COM7 --port COM8

# 수집 날 진행: 장면 목록대로 세 노드를 장면마다 같은 시각에 녹화(라벨은 목록에 노드별로 들어 있음)
python tools/collector.py session --plan docs/plans/day_plan.csv --day 2026-11-03 --out ../data/sessions/2026-11-03 \
    --node RX-301=COM6 --node RX-302=COM7 --node RX-COR=COM8

# 학습·검증 → models/presence_model.joblib + presence_model.card.json
python -m app.ml.train --data ../data/sessions --out models/presence_model.joblib --source-note "공학관 301·302호, 11/3~11/7 수집"
```

무선 노드를 게이트웨이로 받을 때는 포트 자리에 `gw:<노드 MAC>`을 쓰고 `--gateway COM7`을 붙입니다. 펌웨어 올리기와 무선 점검은 [docs/WIRELESS.md](docs/WIRELESS.md)에 있습니다.

원시 세션(`data/`)은 저장소에 올리지 않고 팀 드라이브에 둡니다. 저장소에는 학습된 모델과 모델 카드, 시연용 녹화 시나리오만 올립니다.

## 실제 노드로 쓰기

1. 웹에서 건물을 만들고 평면도·구역·노드를 등록합니다. 노드 토큰은 그때 한 번만 보입니다.
2. 건물이 빈 상태에서 "보정"을 시작하고, 각 수신 노드에서 실시간 전송을 켭니다.

   ```bash
   python tools/collector.py live --port COM5 --api https://<백엔드 주소> --token <노드 토큰>
   ```

3. 화재 수신기 쪽에서 경보 API를 부르면 감지가 시작됩니다. 수신기에 붙일 연동 장치는 아직 없어서, 시험과 시연에서는 아래 명령으로 같은 요청을 보냅니다. 지휘 화면에서 직접 열 수도 있습니다.

   ```bash
   curl -X POST https://<백엔드 주소>/api/alarm -H "X-Alarm-Token: <경보 토큰>" -H "Content-Type: application/json" -d '{"event":"fire"}'
   ```

   Windows PowerShell:

   ```powershell
   Invoke-RestMethod -Method Post -Uri https://<백엔드 주소>/api/alarm -Headers @{"X-Alarm-Token"="<경보 토큰>"} -ContentType "application/json" -Body '{"event":"fire"}'
   ```

## 녹화 데이터 시연

심사위원은 노드가 없는 곳에서 링크만 엽니다. 그래서 팀이 실제 공간에서 녹화한 CSI를 서버 안에서 다시 흘려보내
라이브와 같은 처리 경로로 보여 줍니다. 결과를 미리 정해 두지 않고, 화면에는 항상 "녹화 데이터 재생"으로 표시합니다.
시나리오 형식은 [sample_data/README.md](sample_data/README.md)에 있습니다.

## 배포 (Render + Neon)

1. Neon에서 프로젝트를 만들고 연결 문자열을 복사합니다.
2. Render에서 이 저장소로 **Blueprint**를 만들면 `render.yaml`대로 Docker 웹 서비스가 생깁니다. (직접 만들 때는 New → Web Service → Docker, Health Check Path `/api/health`)
3. 환경변수를 넣습니다: `DATABASE_URL`(Neon), `DEMO_PASSWORD`, `CORS_ORIGINS`(Vercel 주소). `SECRET_KEY`는 Blueprint가 만들어 줍니다.
4. 배포가 끝나면 `https://<서비스>.onrender.com/api/health`와 `/api/model/card`를 열어 확인합니다.

무료 인스턴스는 15분 동안 요청이 없으면 잠들고, 깨우는 데 1분 가까이 걸립니다. 프론트엔드는 이때 "서버를 깨우는 중"을 표시하고 다시 시도합니다.
창 버퍼를 메모리에 두므로 워커는 하나만 띄웁니다(`Dockerfile` 참고).

## API 요약

| 메서드·경로 | 용도 | 인증 |
|---|---|---|
| `POST /api/auth/login` | 로그인 | 없음 |
| `GET·POST /api/buildings`, `PUT /api/buildings/{id}/plan` | 건물, 평면도 | 로그인 |
| `POST·PATCH·DELETE` 구역·노드 | 구역 그리기, 노드 배치, 토큰 발급 | 로그인 |
| `POST /api/buildings/{id}/calibration/start`, `/stop` | 기준선 보정 | 로그인 |
| `POST /api/ingest/csi` | CSI 프레임 수신 | 노드 토큰 |
| `POST /api/alarm` | 화재 경보 수신 | 경보 토큰 |
| `POST /api/buildings/{id}/incidents`, `/api/incidents/{id}/close` | 상황 수동 개시·종료 | 로그인 |
| `GET /api/incidents/{id}/state` | 구역 상태, 우선순위, 타임라인 | 로그인 |
| `POST /api/incidents/{id}/decisions` | 지휘관 결정 기록 | 로그인 |
| `GET /api/incidents/{id}/report` | 상황 기록 | 로그인 |
| `GET /api/model/card` | 모델 카드 | 없음 |
| `GET /api/demo/scenarios`, `POST /api/demo/scenarios/{id}/start` | 녹화 데이터 재생 | 로그인 |

## 직접 만든 부분과 가져다 쓴 부분

직접 만든 부분: CSI 파싱과 진폭 변환, 2초 창 특징 8개, 노드별 기준선과 변화 확률, 학습·검증 명령과 모델 카드, 구역 융합과 상태 판정, API와 DB 설계, 수집 도구, 녹화 재생.

가져다 쓴 것:

- ESP32 펌웨어: Espressif [esp-csi](https://github.com/espressif/esp-csi)(Apache-2.0). 송신은 csi_send 예제를 수정 없이 쓰고, 무선 수신 노드(`firmware/csi_node`)는 csi_recv 예제의 측정 부분을 그대로 두고 전송 부분을 바꿨습니다. 게이트웨이(`firmware/csi_gateway`)와 노드·게이트웨이 데이터 형식은 직접 만들었습니다. 이득 보정은 Espressif의 esp_csi_gain_ctrl 부품을 씁니다
- FastAPI, Uvicorn, SQLAlchemy, Pydantic, psycopg, NumPy, SciPy, scikit-learn, joblib, pyserial (각 프로젝트 라이선스를 따름)
- 개발 과정에서 AI 코딩 도구를 썼습니다. 어느 부분에 어떻게 썼는지는 발표 자료 부록과 Notion "AI 활용 기록"에 적습니다.

## 알려진 한계

- 사람 판별 성능은 팀이 수집한 데이터의 범위(건물·인원·날짜)만큼만 보장됩니다. 숫자는 AI 검증 화면의 모델 카드를 기준으로 합니다.
- 실제 불꽃이 만드는 신호 흔들림은 아직 검증하지 못했습니다.
- 노드가 불에 타거나 전원이 끊기면 그 구역은 "판단 불가"가 됩니다. 노드 전원·내열 설계는 설치 단계 과제입니다.
- 화재 수신기에 붙일 연동 장치는 만들지 않았습니다. 경보 API까지만 있고, 시연에서는 이 API를 직접 부릅니다(발표 자료 부록 1).
- 서버 한 대, 워커 하나 기준입니다. 건물 여러 곳을 동시에 처리하는 규모는 고려하지 않았습니다.
