/*
 * 와치메이트 무선 노드 ↔ 게이트웨이 약속(프로토콜).
 *
 * 수신 노드(csi_node)는 TX-1 패킷의 CSI를 재고, 그 결과를 ESP-NOW로 게이트웨이(csi_gateway)에 보낸다.
 * 게이트웨이는 받은 것을 USB(UART)로 노트북에 넘기고, 노트북의 tools/collector.py가 이것을
 * esp-csi csi_recv 예제와 같은 CSI_DATA 줄로 되돌려 기존 점검·녹화·실시간 전송 경로에 넣는다.
 *
 * 이 파일의 구조와 tools/collector.py의 GW_* 상수는 반드시 같이 바꾼다.
 * SPDX-License-Identifier: Apache-2.0
 */
#pragma once

#include <stdint.h>

#define WM_CHANNEL            11      /* TX-1(csi_send)과 같은 채널. HT40, 보조 채널은 아래(7번 쪽) */

/* 게이트웨이 → 노드: "내가 게이트웨이다" 알림(ESP-NOW 브로드캐스트, 0.5초마다) */
#define WM_BEACON_MAGIC       "WMGW"
#define WM_BEACON_VERSION     1

typedef struct __attribute__((packed)) {
    char     magic[4];        /* "WMGW" */
    uint8_t  version;
    uint8_t  reserved[3];
} wm_beacon_t;

/* 노드 → 게이트웨이: CSI 한 건(ESP-NOW 유니캐스트) */
#define WM_CSI_MAGIC          "WMC1"
#define WM_CSI_MAX_LEN        612     /* ESP32-S3 CSI 최대 길이(ESP-IDF 문서: 128~612바이트) */

typedef struct __attribute__((packed)) {
    char     magic[4];        /* "WMC1" */
    uint16_t node_seq;        /* 노드가 보낸 패킷 번호. 게이트웨이까지 오는 길에서 잃은 수를 센다 */
    uint16_t len;             /* data의 값 개수(info->len) */
    uint32_t tx_id;           /* TX-1이 패킷에 넣은 번호(csi_recv가 찍는 id와 같음) */
    uint32_t local_ts;        /* rx_ctrl->timestamp(µs) */
    float    gain;            /* csi_recv와 같은 이득 보정값. 값 = (int16)(gain * data[i]) */
    int8_t   rssi;
    int8_t   noise_floor;
    uint8_t  channel;
    uint8_t  secondary;
    uint8_t  cwb;             /* 0: 20MHz, 1: 40MHz */
    uint8_t  sig_mode;
    uint8_t  first_word_invalid;
    uint8_t  reserved;
    int8_t   data[];          /* CSI 원본 바이트(len개) */
} wm_csi_pkt_t;

_Static_assert(sizeof(wm_csi_pkt_t) == 28, "wm_csi_pkt_t 헤더는 28바이트여야 collector.py와 맞는다");

/*
 * 게이트웨이 → 노트북(UART) 한 묶음:
 *   0xA5 0x5A | type(1) | len(2, little endian) | payload(len) | crc16(2, little endian)
 * crc16은 CCITT-FALSE(다항식 0x1021, 초기값 0xFFFF)를 type·len·payload에 대해 계산한다.
 */
#define WM_UART_SYNC0         0xA5
#define WM_UART_SYNC1         0x5A
#define WM_UART_TYPE_CSI      0x01    /* payload: 보낸 노드 MAC(6) + 게이트웨이에서 잰 rssi(1) + 예약(1) + wm_csi_pkt_t */
#define WM_UART_TYPE_STATUS   0x02    /* payload: 게이트웨이 상태 글자(UTF-8) */
#define WM_UART_MAX_PAYLOAD   1600
