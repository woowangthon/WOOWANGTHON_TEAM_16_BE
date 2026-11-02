# 무선 노드와 게이트웨이

수신 보드(RX-301, RX-302, RX-COR)는 각 방 콘센트에서 전원만 받고, 잰 값을 무선(ESP-NOW)으로 게이트웨이에 보냅니다. 게이트웨이(SPARE 보드) 하나만 노트북에 USB로 꽂습니다.

```
TX-1(csi_send) ──측정용 신호──> RX 3개(csi_node) ──ESP-NOW, 측정값 전체──> 게이트웨이(csi_gateway) ──USB──> 노트북(collector.py) ──> 서버
```

| 보드 | 펌웨어 | 할 일 | 전원·연결 |
|---|---|---|---|
| TX-1 | csi_send (Espressif 예제 그대로) | **없음**(11/1에 이미 올림) | 콘센트 충전기 |
| RX-301·302·COR | `firmware/csi_node` | 새로 올림 | 콘센트 충전기 |
| SPARE → GW | `firmware/csi_gateway` | 새로 올림 | 노트북 USB(CH343 쪽 포트) |

## 무엇을 보내나

노드는 TX-1 패킷을 하나 잴 때마다 한 건씩 보냅니다. 요약이 아니라 CSI 원본 바이트 전체와 이득 보정값, RSSI, TX-1 패킷 번호, 보드 시각입니다(`firmware/common/wm_proto.h`). 수집 도구가 이것을 esp-csi `csi_recv`가 USB로 찍던 것과 같은 `CSI_DATA` 줄로 되돌리므로, 학습 명령과 서버는 USB 때와 같은 값을 받습니다(`tests/test_gateway.py`가 값이 같은지 확인합니다).

- 측정 설정(채널 11, HT40, CSI 설정, 이득 보정, TX-1 MAC 필터)은 원본 `csi_recv`와 같습니다.
- 원본과 다른 점: 수신 보드의 MAC을 TX-1 주소로 바꾸지 않습니다. 노드마다 공장 MAC을 써야 게이트웨이가 노드를 구분하고, 노드끼리의 전송이 TX-1 패킷으로 잘못 측정되지 않습니다.
- 노드는 무선으로 보내면서 USB로도 늘 `CSI_DATA` 줄을 찍습니다. 케이블만 꽂으면 펌웨어를 바꾸지 않고 바로 유선 수집이 됩니다.
- 패킷이 빠질 수는 있어도 값이 바뀌지는 않습니다. 노드가 붙이는 번호로 잃은 수를 세서 `check`와 `gateway` 명령이 보여 줍니다.

## 보드 5개 올리기

준비물: ESP-IDF v5.5가 설치된 노트북, 데이터 케이블 1개, 충전기 4개, 보드 5개(이름표가 붙은 TX-1·RX-301·RX-302·RX-COR, 아직 봉투에 있는 SPARE).

터미널은 두 개를 씁니다.
- **일반 PowerShell**: `git`, `python tools/collector.py ...`
- **ESP-IDF 5.5 PowerShell**: `idf.py ...`

### 1단계. 코드 받기 (일반 PowerShell, 처음 한 번)

경로가 짧아야 ESP-IDF 빌드가 안전하므로 `C:\wm`에 받습니다.

```powershell
git clone -b wireless-nodes https://github.com/noob-03/watchmate-be.git C:\wm\watchmate-be
cd C:\wm\watchmate-be
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest -q
```

`41 passed, 3 skipped` 정도가 나오면 됩니다(건너뛴 3개는 리눅스 전용). 이후 바뀐 코드는 이 폴더에서 `git pull`로 받습니다.

### 2단계. TX-1은 그대로 둡니다

TX-1에는 11/1에 올린 `csi_send`가 그대로 있습니다. 다시 올리지 않습니다. 3~4단계 동안은 **TX-1 전원을 빼 두세요.** 노드 로그가 조용해서 MAC을 읽기 쉽습니다.

### 3단계. SPARE → 게이트웨이

1. SPARE를 봉투에서 꺼내 CH343 쪽 USB-C로 노트북에 꽂습니다.
2. 일반 PowerShell에서 포트를 확인합니다. `← 보드의 CH343 포트`가 붙은 새 번호가 SPARE입니다(예: COM7).

   ```powershell
   python tools/collector.py ports
   ```

3. ESP-IDF PowerShell에서 빌드하고 올립니다. `COM7`은 2번에서 나온 번호로 바꿉니다.

   ```powershell
   cd C:\wm\watchmate-be\firmware\csi_gateway
   idf.py set-target esp32s3
   idf.py build
   idf.py -p COM7 flash
   ```

4. `Hard resetting via RTS pin...`이 나오면 끝입니다. `monitor`는 하지 않습니다(게이트웨이는 2Mbps 이진 데이터를 보내 글자가 깨져 보입니다).
5. 이름표를 **GW**로 붙이고 뽑아 둡니다.

### 4단계. RX-301 → 무선 노드

1. RX-301을 꽂고 일반 PowerShell에서 `python tools/collector.py ports`로 번호를 봅니다(11/1에는 COM4).
2. ESP-IDF PowerShell에서 빌드하고 올립니다. 첫 빌드는 이득 보정 부품을 인터넷에서 받으니 인터넷이 연결돼 있어야 합니다.

   ```powershell
   cd C:\wm\watchmate-be\firmware\csi_node
   idf.py set-target esp32s3
   idf.py build
   idf.py -p COM4 flash monitor
   ```

3. 로그에서 `노드 MAC 90:70:69:...` 줄을 찾아 MAC을 적습니다. 5초마다 `노드 ... | 게이트웨이를 찾는 중` 줄에도 같은 MAC이 나옵니다. 올리는 중에 esptool이 찍는 `MAC:`도 같은 값입니다.
4. `Ctrl+]`로 monitor를 닫고 뽑습니다.

### 5단계. RX-302, RX-COR

같은 폴더에서 빌드 없이 올리기만 합니다. 보드마다 꽂고, 번호를 확인하고, 실행하고, MAC을 적고, `Ctrl+]`, 뽑기를 반복합니다.

```powershell
idf.py -p COM5 flash monitor
idf.py -p COM6 flash monitor
```

### 6단계. 이름표 파일에 MAC 적기 (일반 PowerShell)

```powershell
notepad nodes.csv
```

비어 있는 MAC 칸을 채우고 저장합니다.

```
node,mac
RX-301,90:70:69:xx:xx:xx
RX-302,90:70:69:xx:xx:xx
RX-COR,90:70:69:xx:xx:xx
```

이후 명령에는 MAC 대신 `gw:RX-301`이나 `--node RX-301=gw`처럼 이름표를 씁니다. 같은 MAC을 두 번 적었거나, `--node RX-301`에 RX-302의 것을 붙이면 수집 도구가 멈추고 알려 줍니다.

### 7단계. 책상 위 무선 점검

1. TX-1과 RX 3개를 충전기로 켭니다. 서로 1~2m 떨어뜨립니다. 보조배터리는 쓰지 않습니다(11/1 점검에서 TX-1을 보조배터리로 켜자 초당 16개, USB 전원으로 바꾸자 74개였습니다).
2. GW를 노트북에 꽂고 번호를 확인한 뒤, 이 PowerShell 창에서 게이트웨이 포트를 한 번 정해 둡니다.

   ```powershell
   python tools/collector.py ports
   $env:WATCHMATE_GATEWAY="COM7"
   ```

3. 세 노드가 들어오는지 봅니다. 노드마다 이름표, 받은 수, 초당 수, 잃은 비율, 값 개수가 나옵니다.

   ```powershell
   python tools/collector.py gateway
   ```

4. 노드 점검(USB 때와 같은 판정에 무선 손실과 송신 보드 전원 경고가 더해집니다):

   ```powershell
   python tools/collector.py check --port gw:RX-301 --port gw:RX-302 --port gw:RX-COR
   ```

5. 유선과 비교: RX-301을 데이터 케이블로도 노트북에 꽂고, 같은 노드를 두 길로 동시에 점검합니다. 값 개수, RSSI 평균, 초당 수가 거의 같아야 합니다.

   ```powershell
   python tools/collector.py check --port gw:RX-301 --port COM4
   ```

통과 기준(노드마다):

| 항목 | 기준 |
|---|---|
| 판정 | `정상` |
| 초당 CSI | 10 이상(USB 때 약 70) |
| 값 개수 | 384, 세 노드 같음 |
| 무선 잃은 비율 | 5% 이하 |
| 송신 보드 | 경고 없음(초당 약 50 이상) |
| RSSI | 같은 자리에서 USB로 쟀을 때와 비슷 |

### 8단계. 수집 장소에서 시험 녹화와 비교표

보드를 정한 자리에 두고(TX-1 복도 가운데, RX는 각 방 안과 복도 끝, 바닥에서 1m, GW와 노트북은 감지 구역 밖), 시험 목록을 찍습니다. 빈 방 40초 뒤에 구역마다 한 명이 40초씩 걸어 다니고, 마지막에 301호에 눕습니다(약 3분).

```powershell
python tools/collector.py session --plan docs/plans/try_plan.csv --day test --out ..\data\test `
  --node RX-301=gw --node RX-302=gw --node RX-COR=gw

python tools/collector.py compare --data ..\data\test
```

비교표에서 `walking_301`, `walking_302`, `walking_cor`가 각 구역 노드에서 `정상`이어야 본 수집으로 넘어갑니다. `확인`이 나오면 그 줄의 안내대로 보드 자리를 옮기고 시험을 다시 찍습니다.

## 수집과 실시간 전송

포트 자리에 `gw`(이름표)를 쓰는 것 말고는 USB 때와 같습니다. USB로 꽂은 노드(`COM6`)와 섞어도 됩니다.

```powershell
python tools/collector.py session --plan docs/plans/day_plan.csv --day 2026-11-04 `
  --out ..\data\sessions\2026-11-04 --node RX-301=gw --node RX-302=gw --node RX-COR=gw

python tools/collector.py compare --data ..\data\sessions\2026-11-04

python tools/collector.py live --api https://<서버> `
  --port gw:RX-301 --token <RX-301 토큰> --port gw:RX-302 --token <RX-302 토큰> --port gw:RX-COR --token <RX-COR 토큰>
```

새 PowerShell 창을 열면 `$env:WATCHMATE_GATEWAY="COM7"`을 다시 실행하거나, 명령마다 `--gateway COM7`을 붙입니다. 수집 도중 노드를 무선에서 유선으로 바꾸면 그날 `start_empty`부터 다시 찍습니다.

## 막힐 때

| 증상 | 먼저 볼 것 |
|---|---|
| `idf.py build`에서 `esp_csi_gain_ctrl`을 못 받음 | 인터넷 연결. 학교망에서 막히면 휴대폰 핫스팟으로 다시 빌드 |
| `gateway`: 게이트웨이 상태 묶음이 오지 않음 | 포트가 GW 보드인지, csi_gateway를 올렸는지. 윈도우 기본 드라이버가 2Mbps를 못 받으면 WCH 공식 사이트의 CH343SER 드라이버를 설치 |
| `gateway`: 들어오는 노드가 없음 | 노드 전원, 노드에 csi_node를 올렸는지, TX-1이 켜졌는지 |
| `nodes.csv에는 있는데 안 들어오는 노드` | 그 노드의 전원과 거리. 노드를 USB로 꽂아 `idf.py -p COM? monitor`에서 `게이트웨이 연결` 줄이 나오는지 |
| `nodes.csv에 없는 MAC이 있습니다` | 6단계 MAC을 잘못 적었는지. 나온 MAC을 이름표와 다시 맞춰 적기 |
| 잃은 비율이 5% 넘음 | 게이트웨이 위치(벽 여러 개 너머인지). 그 노드만 USB로 꽂아 `COM` 포트로 수집 |
| 송신 보드 경고 | TX-1 전원을 충전기나 노트북 USB로 |
| `깨진 묶음`이 계속 늘어남 | USB 허브를 빼고 노트북에 바로 꽂기 |
| 비교표에서 걷기가 `확인` | 송신→수신 경로가 그 구역을 지나도록 보드 자리 옮기기 |

## 확인한 것과 아직 못 한 것

- 2026-11-01, ESP-IDF v5.5.6으로 csi_node와 csi_gateway를 ESP32-S3용으로 빌드했습니다(경고 없음). 이 작업 환경에서는 Espressif 부품 저장소에 접속할 수 없어서, 이득 보정 라이브러리(esp_csi_gain_ctrl) 자리에 같은 함수 이름의 빈 함수를 넣어 컴파일과 링크만 확인했습니다. 팀 PC에서는 `idf.py build`가 진짜 라이브러리를 받아 씁니다.
- 수집 도구의 게이트웨이 경로, 이름표 파일, 송신 보드 경고, 비교표는 가짜 게이트웨이와 합성 신호로 시험했습니다(`tests/test_gateway.py`, `tests/test_field_checks.py`). 합성 신호는 코드 확인용이고 결과 수치에는 쓰지 않습니다.
- 실제 보드에서 무선 전송, 초당 수, 손실, 유선과의 비교는 아직 못 했습니다. 7단계의 결과를 수집 일지에 적고, 기준과 다르면 펌웨어와 이 문서를 고칩니다.
