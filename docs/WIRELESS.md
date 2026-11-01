# 무선 노드와 게이트웨이

수신 보드(RX-301, RX-302, RX-COR)는 각 방 콘센트에서 전원만 받고, 잰 값을 무선(ESP-NOW)으로 게이트웨이에 보냅니다. 게이트웨이(SPARE 보드) 하나만 노트북에 USB로 꽂습니다. 왜 이렇게 나눴는지는 팀원용 설명문에 있습니다.

```
TX-1(csi_send) ──측정용 신호──> RX 3개(csi_node) ──ESP-NOW, 측정값 전체──> 게이트웨이(csi_gateway) ──USB──> 노트북(collector.py) ──> 서버
```

| 보드 | 펌웨어 | 폴더 | 전원·연결 |
|---|---|---|---|
| TX-1 | csi_send (Espressif 예제 그대로) | `C:\Espressif\esp-csi\examples\get-started\csi_send` | 콘센트 충전기 |
| RX-301·302·COR | csi_node | `firmware/csi_node` | 콘센트 충전기 |
| SPARE → 게이트웨이 | csi_gateway | `firmware/csi_gateway` | 노트북 USB(CH343 쪽 포트) |

## 무엇을 보내나

노드는 TX-1 패킷을 하나 잴 때마다 한 건씩 보냅니다. 요약이 아니라 CSI 원본 바이트 전체와 이득 보정값, RSSI, TX-1 패킷 번호, 보드 시각을 보냅니다(`firmware/common/wm_proto.h`). 수집 도구가 이것을 esp-csi `csi_recv`가 USB로 찍던 것과 같은 `CSI_DATA` 줄로 되돌리므로, 학습 명령과 서버는 USB 때와 같은 값을 받습니다(`tests/test_gateway.py`가 값이 같은지 확인합니다).

- 측정 설정(채널 11, HT40, CSI 설정, 이득 보정, TX-1 MAC 필터)은 원본 `csi_recv`와 같습니다.
- 원본과 다른 점: 수신 보드의 MAC을 TX-1 주소로 바꾸지 않습니다. 노드마다 공장 MAC을 써야 게이트웨이가 노드를 구분하고, 노드끼리의 전송이 TX-1 패킷으로 잘못 측정되지 않습니다.
- 노드는 무선으로 보내면서 USB로도 늘 `CSI_DATA` 줄을 찍습니다. 그래서 케이블만 꽂으면 펌웨어를 바꾸지 않고 바로 유선 수집이 됩니다. USB 출력 속도가 초당 측정 수의 상한(약 70)이 되어, 유선과 무선의 초당 수도 같습니다.
- 패킷이 빠질 수는 있어도 값이 바뀌지는 않습니다. 노드가 붙이는 번호로 잃은 수를 세서 `check`와 `gateway` 명령이 보여 줍니다.

## 처음 한 번: 코드 받기

경로가 짧아야 ESP-IDF 빌드가 안전하므로 `C:\wm`에 받습니다. 일반 PowerShell에서:

```powershell
git clone -b wireless-nodes https://github.com/noob-03/watchmate-be.git C:\wm\watchmate-be
cd C:\wm\watchmate-be
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
python -m pytest -q
```

이후 바뀐 코드는 `git pull`로 받습니다. 수집 도구 명령은 모두 이 폴더(`C:\wm\watchmate-be`, `.venv` 켠 일반 PowerShell)에서 실행합니다.

## 펌웨어 올리기

ESP-IDF PowerShell에서 합니다. 보드는 한 번에 하나씩 CH343 쪽 포트로 꽂고, 꽂을 때마다 일반 PowerShell에서 `python tools/collector.py ports`로 COM 번호를 확인합니다(CH343 포트에는 `← 보드의 CH343 포트`가 붙습니다).

### 1. 게이트웨이(SPARE)

```powershell
cd C:\wm\watchmate-be\firmware\csi_gateway
idf.py set-target esp32s3
idf.py build
idf.py -p COM7 flash
```

- 첫 빌드는 필요한 부품을 내려받습니다.
- `monitor`는 하지 않습니다. 게이트웨이는 2Mbps 이진 묶음을 보내서 글자로 읽을 수 없습니다.
- 이름표를 `GW`로 바꿔 붙입니다.

### 2. 수신 노드 3개

```powershell
cd C:\wm\watchmate-be\firmware\csi_node
idf.py set-target esp32s3
idf.py build
idf.py -p COM4 flash monitor
```

- 로그에 `노드 MAC 90:70:69:...`가 나오면 그 MAC을 이름표 옆에 적고 `Ctrl+]`로 닫습니다.
- 나머지 두 보드는 같은 폴더에서 `idf.py -p COM? flash monitor`만 합니다(다시 빌드할 필요 없음).
- 게이트웨이가 꺼져 있으면 `게이트웨이를 찾는 중`과 `CSI_DATA` 줄이 같이 나옵니다. 정상입니다.

| 이름표 | 노드 MAC |
|---|---|
| RX-301 | |
| RX-302 | |
| RX-COR | |

TX-1은 바꾸지 않습니다.

## 무선 점검

1. TX-1과 노드 3개를 충전기로 켜고, 게이트웨이를 노트북에 꽂습니다. 보조배터리는 쓰지 않습니다(11/1 점검에서 TX-1을 보조배터리로 켜자 초당 16개, USB 전원으로 바꾸자 74개였습니다).
2. 게이트웨이에 들어오는 노드를 봅니다.

   ```powershell
   python tools/collector.py gateway --gateway COM7
   ```

   노드 MAC마다 받은 수, 초당 수, 잃은 비율, 값 개수, 게이트웨이에서 잰 신호 세기가 나옵니다.
3. 수신 노드 점검(USB 때와 같은 판정에 무선 손실이 더해집니다):

   ```powershell
   python tools/collector.py check --gateway COM7 --port gw:<RX-301 MAC> --port gw:<RX-302 MAC> --port gw:<RX-COR MAC>
   ```

4. 유선과 비교: 노드 하나를 USB로도 꽂고 같은 노드를 두 길로 동시에 점검합니다. 값 개수, RSSI 평균, 초당 수가 거의 같아야 합니다.

   ```powershell
   python tools/collector.py check --gateway COM7 --port gw:<RX-301 MAC> --port COM4
   ```

5. 케이블 전환: 무선으로 도는 중에 다른 노드에 케이블을 꽂고 `check --port COM?`이 바로 `정상`인지 봅니다.

통과 기준(노드마다):

| 항목 | 기준 |
|---|---|
| 판정 | `정상` |
| 초당 CSI | 10 이상(USB 때 약 70) |
| 값 개수 | 384, 세 노드 같음 |
| 무선 잃은 비율 | 5% 이하 |
| RSSI | 같은 자리에서 USB로 쟀을 때와 비슷 |

## 수집과 실시간 전송

포트 자리에 `gw:<MAC>`을 쓰고 `--gateway`를 붙이는 것 말고는 USB 때와 같습니다. USB로 꽂은 노드(`COM6`)와 섞어도 됩니다.

```powershell
python tools/collector.py session --gateway COM7 --plan docs/plans/day_plan.csv --day 2026-11-04 `
  --out ..\data\sessions\2026-11-04 --node RX-301=gw:<MAC> --node RX-302=gw:<MAC> --node RX-COR=gw:<MAC>

python tools/collector.py live --gateway COM7 --api https://<서버> `
  --port gw:<RX-301 MAC> --token <RX-301 토큰> --port gw:<RX-302 MAC> --token <RX-302 토큰> --port gw:<RX-COR MAC> --token <RX-COR 토큰>
```

`--gateway`를 매번 쓰기 번거로우면 `$env:WATCHMATE_GATEWAY="COM7"`을 한 번 실행해 둡니다.

수집 도중 노드를 무선에서 유선으로 바꾸면 그날 `start_empty`부터 다시 찍습니다.

## 막힐 때

| 증상 | 먼저 볼 것 |
|---|---|
| `gateway`: 게이트웨이 상태 묶음이 오지 않음 | 포트가 게이트웨이 보드인지, csi_gateway를 올렸는지. 윈도우 기본 드라이버가 2Mbps를 못 받으면 WCH 공식 사이트의 CH343SER 드라이버를 설치 |
| `gateway`: 들어오는 노드가 없음 | 노드 전원, 노드에 csi_node를 올렸는지, 노드 USB 로그에 `게이트웨이 연결`이 나오는지 |
| 노드 로그가 계속 `게이트웨이를 찾는 중` | 게이트웨이가 켜졌는지, 거리. 게이트웨이를 노드 쪽으로 옮겨 봅니다 |
| 초당 CSI는 있는데 잃은 비율이 높음 | 게이트웨이 위치(벽 여러 개 너머인지). 그 노드만 USB로 꽂아 `COM` 포트로 바꿔 수집 |
| 초당 CSI가 10 미만 | TX-1 전원(보조배터리면 충전기로), TX-1과의 거리 |
| `깨진 묶음`이 계속 늘어남 | USB 허브를 빼고 노트북에 바로 꽂기 |

## 확인한 것과 아직 못 한 것

- 2026-11-01, ESP-IDF v5.5.6으로 csi_node와 csi_gateway를 ESP32-S3용으로 빌드했습니다(경고 없음). 이 작업 환경에서는 Espressif 부품 저장소에 접속할 수 없어서, 이득 보정 라이브러리(esp_csi_gain_ctrl) 자리에 같은 함수 이름의 빈 껍데기를 넣어 컴파일과 링크만 확인했습니다. 팀 PC에서는 `idf.py build`가 진짜 라이브러리를 받아 씁니다.
- 수집 도구의 게이트웨이 경로는 가짜 게이트웨이로 시험했습니다(묶음 해석, 깨진 묶음 건너뛰기, 값이 csi_recv와 같은지, 손실 계산, check·gateway·session·live).
- 실제 보드에서 무선 전송, 초당 수, 손실, 유선과의 비교는 아직 못 했습니다. 위 무선 점검의 결과를 수집 일지에 적고, 기준과 다르면 펌웨어와 이 문서를 고칩니다.
