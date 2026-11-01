/*
 * 와치메이트 게이트웨이(csi_gateway).
 *
 * 수신 노드(csi_node)들이 ESP-NOW로 보낸 CSI를 받아 USB(UART, CH343 포트)로 노트북에 넘긴다.
 * 노드가 게이트웨이를 찾을 수 있도록 0.5초마다 "WMGW" 알림을 브로드캐스트한다.
 * 노트북 쪽은 tools/collector.py가 이 묶음을 풀어 esp-csi csi_recv와 같은 CSI_DATA 줄로 되돌린다.
 *
 * UART 묶음 형식은 firmware/common/wm_proto.h에 있다. 측정은 하지 않는다.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

#include "nvs_flash.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"

#include "wm_proto.h"

#if !CONFIG_IDF_TARGET_ESP32S3
#error "csi_gateway는 ESP32-S3(idf.py set-target esp32s3)용으로만 확인했다"
#endif

#define GW_UART               UART_NUM_0
#define BEACON_PERIOD_MS      500
#define STATUS_PERIOD_MS      2000
#define ITEM_HEAD             8          /* 노드 MAC(6) + 게이트웨이에서 잰 rssi(1) + 예약(1) */

static const uint8_t BROADCAST_MAC[] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
static const char *TAG = "csi_gateway";

static RingbufHandle_t s_rb;
static volatile uint32_t s_rx_csi = 0;     /* 노드에서 받은 CSI 수 */
static volatile uint32_t s_rb_drop = 0;    /* 링버퍼가 차서 버린 수(USB가 못 따라감) */
static volatile uint32_t s_beacon_fail = 0;

/* ------------------------------------------------------------------ Wi-Fi: 노드·TX-1과 같은 채널 11, HT40 */

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_init());
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT40));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(WM_CHANNEL, WIFI_SECOND_CHAN_BELOW));
}

/* ------------------------------------------------------------------ ESP-NOW 받기 */

static void espnow_recv_cb(const esp_now_recv_info_t *info, const uint8_t *data, int len)
{
    if (len < (int)sizeof(wm_csi_pkt_t) || memcmp(data, WM_CSI_MAGIC, 4) != 0) {
        return;  /* TX-1의 4바이트 번호 패킷, 다른 게이트웨이 알림 등은 무시 */
    }
    if (len > WM_UART_MAX_PAYLOAD - ITEM_HEAD) {
        return;
    }
    uint8_t *item = NULL;
    if (xRingbufferSendAcquire(s_rb, (void **)&item, ITEM_HEAD + len, 0) != pdTRUE) {
        s_rb_drop++;
        return;
    }
    memcpy(item, info->src_addr, 6);
    item[6] = (uint8_t)(info->rx_ctrl ? info->rx_ctrl->rssi : 0);
    item[7] = 0;
    memcpy(item + ITEM_HEAD, data, len);
    xRingbufferSendComplete(s_rb, item);
    s_rx_csi++;
}

static void espnow_init(void)
{
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_set_pmk((uint8_t *)"pmk1234567890123"));
    esp_now_peer_info_t peer = {
        .channel = WM_CHANNEL,
        .ifidx   = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, BROADCAST_MAC, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    ESP_ERROR_CHECK(esp_now_register_recv_cb(espnow_recv_cb));
}

/* ------------------------------------------------------------------ UART 묶음 */

static uint16_t crc16_ccitt_false(const uint8_t *p, size_t n, uint16_t crc)
{
    while (n--) {
        crc ^= (uint16_t)(*p++) << 8;
        for (int i = 0; i < 8; i++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static uint8_t s_frame[5 + WM_UART_MAX_PAYLOAD + 2];

/* 한 묶음을 uart_write_bytes 한 번으로 쓴다. 이 함수는 uart_task에서만 부른다. */
static void write_frame(uint8_t type, const uint8_t *payload, size_t len)
{
    if (len > WM_UART_MAX_PAYLOAD) {
        return;
    }
    s_frame[0] = WM_UART_SYNC0;
    s_frame[1] = WM_UART_SYNC1;
    s_frame[2] = type;
    s_frame[3] = (uint8_t)(len & 0xff);
    s_frame[4] = (uint8_t)(len >> 8);
    memcpy(s_frame + 5, payload, len);
    uint16_t crc = crc16_ccitt_false(s_frame + 2, 3 + len, 0xFFFF);
    s_frame[5 + len] = (uint8_t)(crc & 0xff);
    s_frame[6 + len] = (uint8_t)(crc >> 8);
    uart_write_bytes(GW_UART, s_frame, 7 + len);
}

static void write_status(void)
{
    static char text[200];
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    int n = snprintf(text, sizeof(text),
                     "gateway " MACSTR " up %" PRIu32 "s csi %" PRIu32 " usb_drop %" PRIu32 " beacon_fail %" PRIu32 " baud %d",
                     MAC2STR(mac), (uint32_t)(esp_timer_get_time() / 1000000), s_rx_csi, s_rb_drop, s_beacon_fail,
                     CONFIG_WM_GW_BAUD);
    if (n > 0) {
        write_frame(WM_UART_TYPE_STATUS, (const uint8_t *)text, (size_t)n < sizeof(text) ? (size_t)n : sizeof(text) - 1);
    }
}

static void uart_task(void *arg)
{
    int64_t next_status = 0;
    for (;;) {
        size_t size = 0;
        uint8_t *item = (uint8_t *)xRingbufferReceive(s_rb, &size, pdMS_TO_TICKS(200));
        if (item) {
            write_frame(WM_UART_TYPE_CSI, item, size);
            vRingbufferReturnItem(s_rb, item);
        }
        int64_t now = esp_timer_get_time();
        if (now >= next_status) {
            write_status();
            next_status = now + (int64_t)STATUS_PERIOD_MS * 1000;
        }
    }
}

/* ------------------------------------------------------------------ 알림(노드가 게이트웨이를 찾게) */

static void beacon_task(void *arg)
{
    wm_beacon_t beacon = {0};
    memcpy(beacon.magic, WM_BEACON_MAGIC, 4);
    beacon.version = WM_BEACON_VERSION;
    for (;;) {
        if (esp_now_send(BROADCAST_MAC, (const uint8_t *)&beacon, sizeof(beacon)) != ESP_OK) {
            s_beacon_fail++;
        }
        vTaskDelay(pdMS_TO_TICKS(BEACON_PERIOD_MS));
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

    s_rb = xRingbufferCreate(CONFIG_WM_GW_RINGBUF_KB * 1024, RINGBUF_TYPE_NOSPLIT);
    assert(s_rb);

    wifi_init();
    espnow_init();

    uint8_t mac[6];
    ESP_ERROR_CHECK(esp_wifi_get_mac(WIFI_IF_STA, mac));
    ESP_LOGI(TAG, "================ WATCHMATE GATEWAY ================");
    ESP_LOGI(TAG, "게이트웨이 MAC " MACSTR ", 채널 %d(HT40-), USB %d bps로 묶음을 보냅니다",
             MAC2STR(mac), WM_CHANNEL, CONFIG_WM_GW_BAUD);

    /* 이후로는 UART 드라이버로만 쓴다. 글자 로그가 묶음 중간에 끼지 않게 경고 이상만 남긴다. */
    ESP_ERROR_CHECK(uart_driver_install(GW_UART, 256, CONFIG_WM_GW_UART_TX_KB * 1024, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_set_baudrate(GW_UART, CONFIG_WM_GW_BAUD));
    uart_vfs_dev_use_driver(GW_UART);
    esp_log_level_set("*", ESP_LOG_WARN);

    xTaskCreatePinnedToCore(uart_task, "wm_uart", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(beacon_task, "wm_beacon", 3072, NULL, 4, NULL, 1);
}
