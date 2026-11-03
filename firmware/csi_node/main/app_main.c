/*
 * 와치메이트 무선 수신 노드(csi_node).
 *
 * Espressif esp-csi 저장소 examples/get-started/csi_recv(커밋 8633d67, Apache-2.0)를 바탕으로 했다.
 * CSI를 재는 부분(채널 11·HT40, CSI 설정, 이득 보정, TX-1 MAC 필터)은 원본과 같게 두고,
 * 결과를 내보내는 길만 바꿨다.
 *
 *   게이트웨이가 보이면 → CSI 한 건마다 ESP-NOW로 게이트웨이에 보낸다.
 *   그리고 기본 설정(WM_UART_ALWAYS)에서는 원본 csi_recv와 같은 CSI_DATA 줄을 UART(USB)로도 찍는다.
 *   USB 출력은 무선보다 우선순위가 낮은 별도 태스크가 하므로 무선 전송을 막지 않는다.
 *   무선이 안 되는 날에는 USB 케이블만 꽂으면 기존 수집 도구가 그대로 동작한다.
 *
 * 원본과 달라진 점:
 *   - 수신 보드의 MAC을 송신 MAC(1a:00:...)으로 바꾸지 않는다. 노드마다 공장 MAC을 써야
 *     게이트웨이가 노드를 구분하고, 다른 노드가 이 노드의 전송을 TX-1 패킷으로 착각하지 않는다.
 *   - CSI 콜백은 값을 큐에 넣기만 하고, 전송은 별도 태스크가 한다.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "rom/ets_sys.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_csi_gain_ctrl.h"

#include "wm_proto.h"

/* ---- 원본 csi_recv와 같은 측정 설정 ---- */
#define CONFIG_LESS_INTERFERENCE_CHANNEL   WM_CHANNEL
#define CONFIG_WIFI_BANDWIDTH              WIFI_BW_HT40
#define CONFIG_ESP_NOW_PHYMODE             WIFI_PHY_MODE_HT40
#define CONFIG_ESP_NOW_RATE                WIFI_PHY_RATE_MCS0_LGI
#define CONFIG_FORCE_GAIN                  0
#define CONFIG_GAIN_CONTROL                1

#if !CONFIG_IDF_TARGET_ESP32S3
#error "csi_node는 ESP32-S3(idf.py set-target esp32s3)용으로만 확인했다"
#endif

static const uint8_t CONFIG_CSI_SEND_MAC[] = {0x1a, 0x00, 0x00, 0x00, 0x00, 0x00};
static const uint8_t BROADCAST_MAC[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char *TAG = "csi_node";

/* ---- 게이트웨이 상태 ---- */
#define GATEWAY_TIMEOUT_US   (3 * 1000 * 1000)   /* 알림이 3초 끊기면 게이트웨이가 없는 것으로 본다 */

static uint8_t s_gw_mac[6];
static volatile int64_t s_gw_last_seen_us = 0;    /* 마지막 알림 시각. 0이면 아직 못 봄 */
static volatile bool s_gw_new = false;            /* 처음 봤거나 MAC이 바뀜 → 송신 태스크가 peer 등록 */
static portMUX_TYPE s_gw_lock = portMUX_INITIALIZER_UNLOCKED;

/* ---- 통계(5초마다 UART로 찍는다) ---- */
static volatile uint32_t s_csi_count = 0;     /* TX-1 CSI를 받은 수 */
static volatile uint32_t s_sent = 0;          /* esp_now_send가 받아 준 수 */
static volatile uint32_t s_send_fail = 0;     /* 게이트웨이가 ACK하지 않은 수 */
static volatile uint32_t s_queue_drop = 0;    /* 큐가 차서 버린 수 */
static volatile uint32_t s_nomem_drop = 0;    /* ESP-NOW 내부 큐가 차서 버린 수 */

typedef struct {
    uint16_t size;                                  /* pkt + data 바이트 수 */
    uint8_t  buf[sizeof(wm_csi_pkt_t) + WM_CSI_MAX_LEN];
} csi_item_t;

static QueueHandle_t s_csi_queue;
static QueueHandle_t s_print_queue;
static volatile uint32_t s_print_drop = 0;    /* USB가 못 따라가 건너뛴 줄 */
static volatile uint32_t s_printed = 0;       /* USB로 찍은 줄 */

static bool gateway_alive(void)
{
    int64_t last = s_gw_last_seen_us;
    return last != 0 && (esp_timer_get_time() - last) < GATEWAY_TIMEOUT_US;
}

/* ------------------------------------------------------------------ Wi-Fi(원본과 같음, MAC 변경만 뺌) */

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, CONFIG_WIFI_BANDWIDTH));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(CONFIG_LESS_INTERFERENCE_CHANNEL, WIFI_SECOND_CHAN_BELOW));
    /* 원본은 여기서 esp_wifi_set_mac(WIFI_IF_STA, CONFIG_CSI_SEND_MAC)를 부른다. 무선 노드는 부르지 않는다(위 설명). */
}

/* ------------------------------------------------------------------ ESP-NOW: 게이트웨이 알림 받기 */

static void espnow_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (len < (int)sizeof(wm_beacon_t) || memcmp(data, WM_BEACON_MAGIC, 4) != 0) {
        return;  /* TX-1이 보내는 4바이트 번호 패킷 등은 무시 */
    }
    taskENTER_CRITICAL(&s_gw_lock);
    if (s_gw_last_seen_us == 0 || memcmp(s_gw_mac, info->src_addr, 6) != 0) {
        memcpy(s_gw_mac, info->src_addr, 6);
        s_gw_new = true;
    }
    s_gw_last_seen_us = esp_timer_get_time();
    taskEXIT_CRITICAL(&s_gw_lock);
}

static void espnow_send_cb(const esp_now_send_info_t *tx_info, esp_now_send_status_t status)
{
    if (status != ESP_NOW_SEND_SUCCESS) {
        s_send_fail++;
    }
}

static void wifi_esp_now_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_peer_info_t peer = {
        .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
        .ifidx   = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, BROADCAST_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate_config = {
        .phymode = CONFIG_ESP_NOW_PHYMODE,
        .rate = CONFIG_ESP_NOW_RATE,
        .ersu = false,
        .dcm = false
    };
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(peer.peer_addr, &rate_config));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));
    ESP_ERROR_CHECK(esp_now_register_send_cb(espnow_send_cb));
}

/* 게이트웨이를 peer로 등록한다. 짧은 거리라 HT40 MCS2(약 40Mbps)로 보내 전파 점유 시간을 줄인다. */
static void add_gateway_peer(const uint8_t mac[6])
{
    if (!esp_now_is_peer_exist(mac)) {
        esp_now_peer_info_t peer = {
            .channel = CONFIG_LESS_INTERFERENCE_CHANNEL,
            .ifidx   = WIFI_IF_STA,
            .encrypt = false,
        };
        memcpy(peer.peer_addr, mac, 6);
        esp_err_t err = esp_now_add_peer(&peer);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "게이트웨이 peer 등록 실패: %s", esp_err_to_name(err));
            return;
        }
    }
    esp_now_rate_config_t rate = {
        .phymode = WIFI_PHY_MODE_HT40,
        .rate = (wifi_phy_rate_t)CONFIG_WM_UPLINK_RATE,
        .ersu = false,
        .dcm = false
    };
    esp_err_t err = esp_now_set_peer_rate_config(mac, &rate);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "게이트웨이 전송 속도 설정 실패: %s", esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "게이트웨이 연결: " MACSTR, MAC2STR(mac));
}

/* ------------------------------------------------------------------ CSI 콜백(원본과 같은 측정·보정) */
/*
 * 콜백은 와이파이 태스크 안에서 돈다. 여기서 USB로 글자를 찍으면(921600bps에서 한 줄 약 16ms)
 * 그동안 와이파이 처리가 멈춰 ESP-NOW 전송 대기열이 넘친다(11/3 점검에서 무선 손실 20%의 원인).
 * 그래서 콜백은 측정값을 한 번 복사해 큐에 넣기만 하고, 무선 전송(uplink_task)과
 * USB 출력(print_task)은 다른 코어의 태스크가 한다. USB 출력은 무선보다 우선순위가 낮다.
 */

static void fill_item(const wifi_csi_info_t *info, uint32_t rx_id, float compensate_gain, csi_item_t *item)
{
    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    uint16_t n = info->len > WM_CSI_MAX_LEN ? WM_CSI_MAX_LEN : info->len;
    wm_csi_pkt_t *pkt = (wm_csi_pkt_t *)item->buf;
    memcpy(pkt->magic, WM_CSI_MAGIC, 4);
    pkt->node_seq = 0;  /* 송신 태스크가 채운다 */
    pkt->len = n;
    pkt->tx_id = rx_id;
    pkt->local_ts = rx_ctrl->timestamp;
    pkt->gain = compensate_gain;
    pkt->rssi = rx_ctrl->rssi;
    pkt->noise_floor = rx_ctrl->noise_floor;
    pkt->channel = rx_ctrl->channel;
    pkt->secondary = rx_ctrl->secondary_channel;
    pkt->cwb = rx_ctrl->cwb;
    pkt->sig_mode = rx_ctrl->sig_mode;
    pkt->first_word_invalid = info->first_word_invalid;
    pkt->reserved = 0;
    memcpy(pkt->data, info->buf, n);
    item->size = sizeof(wm_csi_pkt_t) + n;
}

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf) {
        return;
    }
    if (memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6)) {
        return;  /* TX-1 패킷만 쓴다 */
    }

    const wifi_pkt_rx_ctrl_t *rx_ctrl = &info->rx_ctrl;
    static uint32_t s_count = 0;
    float compensate_gain = 1.0f;
    static uint8_t agc_gain = 0;
    static int8_t fft_gain = 0;
#if CONFIG_GAIN_CONTROL
    static uint8_t agc_gain_baseline = 0;
    static int8_t fft_gain_baseline = 0;
    esp_csi_gain_ctrl_get_rx_gain(rx_ctrl, &agc_gain, &fft_gain);
    if (s_count < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc_gain, fft_gain);
    } else if (s_count == 100) {
        esp_csi_gain_ctrl_get_rx_gain_baseline(&agc_gain_baseline, &fft_gain_baseline);
#if CONFIG_FORCE_GAIN
        esp_csi_gain_ctrl_set_rx_force_gain(agc_gain_baseline, fft_gain_baseline);
#endif
    }
    esp_csi_gain_ctrl_get_gain_compensation(&compensate_gain, agc_gain, fft_gain);
#endif

    uint32_t rx_id;
    memcpy(&rx_id, info->payload + 15, sizeof(rx_id));  /* 원본과 같은 위치(ESP-NOW 본문 첫 4바이트) */
    s_count++;
    s_csi_count++;

    bool gw = gateway_alive();
#if CONFIG_WM_UART_ALWAYS
    bool print = true;          /* 케이블만 꽂으면 바로 유선 수집이 되도록 늘 USB로도 찍는다(낮은 우선순위) */
#elif CONFIG_WM_UART_FALLBACK
    bool print = !gw;           /* 게이트웨이가 없을 때만 USB로 찍는다 */
#else
    bool print = false;
#endif
    if (!gw && !print) {
        return;
    }

    static csi_item_t item;     /* 와이파이 태스크 스택을 아끼려고 정적으로 둔다(콜백은 한 번에 하나씩 불린다) */
    fill_item(info, rx_id, compensate_gain, &item);
    if (gw && xQueueSend(s_csi_queue, &item, 0) != pdTRUE) {
        s_queue_drop++;
    }
    if (print && xQueueSend(s_print_queue, &item, 0) != pdTRUE) {
        s_print_drop++;  /* USB가 못 따라가면 USB 줄만 건너뛴다. 무선은 영향 없음 */
    }
}

/* USB 출력: esp-csi csi_recv(ESP32-S3)와 같은 필드 순서와 같은 값((int16)(gain × 원본 바이트))으로 찍는다.
 * 원본에서 수집 도구가 쓰지 않는 rx_ctrl 칸(rate, mcs 등)은 0이다. 게이트웨이 경로(collector.py gw_csi_line)와 같은 줄이 된다. */
static void print_task(void *arg)
{
    static csi_item_t item;
    static char line[160 + WM_CSI_MAX_LEN * 7];
    printf("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,aggregation,stbc,fec_coding,sgi,"
           "noise_floor,ampdu_cnt,channel,secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n");
    for (;;) {
        if (xQueueReceive(s_print_queue, &item, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        const wm_csi_pkt_t *p = (const wm_csi_pkt_t *)item.buf;
        int n = snprintf(line, sizeof(line),
                         "CSI_DATA,%" PRIu32 "," MACSTR ",%d,0,%d,0,%d,0,0,0,0,0,0,%d,0,%d,%d,%" PRIu32 ",0,0,%d,%d,%d,\"[",
                         p->tx_id, MAC2STR(CONFIG_CSI_SEND_MAC), p->rssi, p->sig_mode, p->cwb, p->noise_floor,
                         p->channel, p->secondary, p->local_ts, p->sig_mode, p->len, p->first_word_invalid);
        for (int i = 0; i < p->len && n < (int)sizeof(line) - 16; i++) {
            n += snprintf(line + n, sizeof(line) - n, i ? ",%d" : "%d", (int16_t)(p->gain * p->data[i]));
        }
        n += snprintf(line + n, sizeof(line) - n, "]\"\n");
        fputs(line, stdout);  /* 한 번에 써서 상태 로그와 줄이 섞이지 않게 한다 */
        s_printed++;
    }
}

static void wifi_csi_init(void)
{
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    wifi_csi_config_t csi_config = {
        .lltf_en           = true,
        .htltf_en          = true,
        .stbc_htltf2_en    = true,
        .ltf_merge_en      = true,
        .channel_filter_en = true,
        .manu_scale        = false,
        .shift             = false,
    };
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
}

/* ------------------------------------------------------------------ 송신 태스크 */

static void uplink_task(void *arg)
{
    static csi_item_t item;
    uint16_t seq = 0;
    for (;;) {
        if (s_gw_new) {
            uint8_t mac[6];
            taskENTER_CRITICAL(&s_gw_lock);
            memcpy(mac, s_gw_mac, 6);
            s_gw_new = false;
            taskEXIT_CRITICAL(&s_gw_lock);
            add_gateway_peer(mac);
        }
        if (xQueueReceive(s_csi_queue, &item, pdMS_TO_TICKS(200)) != pdTRUE) {
            continue;
        }
        if (!gateway_alive()) {
            continue;  /* 게이트웨이가 사라졌으면 쌓인 것은 버린다(콜백이 UART로 바꿔 찍는다) */
        }
        uint8_t mac[6];
        taskENTER_CRITICAL(&s_gw_lock);
        memcpy(mac, s_gw_mac, 6);
        taskEXIT_CRITICAL(&s_gw_lock);

        ((wm_csi_pkt_t *)item.buf)->node_seq = seq++;
        esp_err_t err = ESP_FAIL;
        for (int attempt = 0; attempt < 3; attempt++) {
            err = esp_now_send(mac, item.buf, item.size);
            if (err != ESP_ERR_ESPNOW_NO_MEM) {
                break;
            }
            vTaskDelay(1);  /* ESP-NOW 내부 큐가 비기를 잠깐 기다린다 */
        }
        if (err == ESP_OK) {
            s_sent++;
        } else if (err == ESP_ERR_ESPNOW_NO_MEM) {
            s_nomem_drop++;
        } else if (err == ESP_ERR_ESPNOW_NOT_FOUND) {
            s_gw_new = true;  /* peer가 없어졌으면 다시 등록 */
        }
    }
}

/* ------------------------------------------------------------------ 상태 줄(5초마다) */

static void status_task(void *arg)
{
    uint8_t my_mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, my_mac));
    uint32_t last_csi = 0, last_sent = 0, last_printed = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5000));
        uint32_t csi = s_csi_count, sent = s_sent;
        if (gateway_alive()) {
            ESP_LOGI(TAG, "노드 " MACSTR " | 게이트웨이 " MACSTR " | CSI %.1f Hz, 전송 %.1f Hz, USB %.1f Hz | "
                     "ACK 없음 %" PRIu32 ", 큐 버림 %" PRIu32 ", 전송 버림 %" PRIu32 ", USB 건너뜀 %" PRIu32,
                     MAC2STR(my_mac), MAC2STR(s_gw_mac), (csi - last_csi) / 5.0, (sent - last_sent) / 5.0,
                     (s_printed - last_printed) / 5.0, s_send_fail, s_queue_drop, s_nomem_drop, s_print_drop);
        } else {
            ESP_LOGW(TAG, "노드 " MACSTR " | 게이트웨이를 찾는 중 | CSI %.1f Hz%s",
                     MAC2STR(my_mac), (csi - last_csi) / 5.0, " (USB로 CSI_DATA 출력 중)");
        }
        last_csi = csi;
        last_sent = sent;
        last_printed = s_printed;
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    s_csi_queue = xQueueCreate(CONFIG_WM_QUEUE_LEN, sizeof(csi_item_t));
    s_print_queue = xQueueCreate(4, sizeof(csi_item_t));
    assert(s_csi_queue && s_print_queue);

    wifi_init();
    wifi_esp_now_init();

    uint8_t my_mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, my_mac));
    ESP_LOGI(TAG, "================ WATCHMATE CSI NODE ================");
    ESP_LOGI(TAG, "노드 MAC " MACSTR ", 채널 %d(HT40-), TX-1 MAC " MACSTR,
             MAC2STR(my_mac), CONFIG_LESS_INTERFERENCE_CHANNEL, MAC2STR(CONFIG_CSI_SEND_MAC));

    xTaskCreatePinnedToCore(uplink_task, "wm_uplink", 4096, NULL, 5, NULL, 1);
    xTaskCreatePinnedToCore(print_task, "wm_print", 4096, NULL, 2, NULL, 1);   /* 무선(5)보다 낮게 */
    xTaskCreatePinnedToCore(status_task, "wm_status", 3072, NULL, 1, NULL, 1);

    wifi_csi_init();
}
