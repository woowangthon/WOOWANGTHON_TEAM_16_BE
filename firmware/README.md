# 펌웨어

| 폴더 | 보드 | 하는 일 |
|---|---|---|
| `csi_node` | RX-301, RX-302, RX-COR | TX-1 패킷의 CSI를 재고, 측정값 전체를 ESP-NOW로 게이트웨이에 보냄. USB로도 늘 `CSI_DATA` 줄을 찍음 |
| `csi_gateway` | 게이트웨이(SPARE) | 노드들의 측정값을 받아 USB(2Mbps)로 노트북에 넘김 |
| `common/wm_proto.h` | | 노드·게이트웨이·`tools/collector.py`가 함께 쓰는 형식 |

송신(TX-1)은 Espressif esp-csi의 `csi_send` 예제를 그대로 씁니다. 빌드·올리기·점검 순서는 [docs/WIRELESS.md](../docs/WIRELESS.md)에 있습니다. ESP-IDF v5.5.6, ESP32-S3 기준입니다.

`csi_node`는 Espressif esp-csi(Apache-2.0) `examples/get-started/csi_recv`(커밋 8633d67)를 바탕으로 했습니다.
