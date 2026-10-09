#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <errno.h>
#include <inttypes.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_system.h"
#include "esp_idf_version.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include "mqtt_client.h"
#include "cJSON.h"

#include "lwip/inet.h"
#include "lwip/sockets.h"

#include "ota_update.h"
#include "battery_monitor.h"


#define WIFI_CONNECTED_BIT         BIT0
#define LINK_REFRESH_BIT           BIT1
#define CSI_READY_BIT              BIT2

#define WS_UDP_PORT                5005
#define WS_PROBE_PACKET_SIZE       32
#define WS_PROBE_MAGIC_0           'W'
#define WS_PROBE_MAGIC_1           'S'
#define WS_PROBE_MAGIC_2           'E'
#define WS_PROBE_MAGIC_3           'N'
#define WS_PROBE_VERSION           1

#define WS_MAX_TARGET_PACKETS      4096

#define WS_CSI_MAX_LEN             256
#define WS_CSI_QUEUE_DEPTH         128

#define WS_CSI_MAGIC_0             'C'
#define WS_CSI_MAGIC_1             'S'
#define WS_CSI_MAGIC_2             'I'
#define WS_CSI_MAGIC_3             '2'
#define WS_CSI_VERSION             2
#define WS_CSI_HEADER_LEN          43


#define WS_CSI_FORCE_LLTF          1

#define WS_CAPTURE_FINAL_DELAY_MS  100
#define WS_QUEUE_DRAIN_TIMEOUT_MS  500

static const char *TAG = "WS_RX_V2";


static EventGroupHandle_t s_wifi_event_group = NULL;
static esp_netif_t *s_sta_netif = NULL;

static QueueHandle_t s_config_queue = NULL;
static QueueHandle_t s_csi_queue = NULL;
static TaskHandle_t s_capture_finalize_task_handle = NULL;

static esp_mqtt_client_handle_t s_mqtt_client = NULL;
static volatile bool s_mqtt_connected = false;
static volatile bool s_restart_pending = false;

static uint8_t s_ap_bssid[6] = {0};
static volatile bool s_ap_bssid_valid = false;
static uint32_t s_csi_server_ip_addr = 0;
static uint32_t s_link_generation = 0;
static bool s_csi_initialized = false;


static battery_status_t s_battery_status = {0};
static portMUX_TYPE s_battery_mux = portMUX_INITIALIZER_UNLOCKED;

static uint64_t s_applied_config_id = 0;
static uint32_t s_config_target_packets = 200;
static uint32_t s_config_max_capture_ms = 2500;
static uint32_t s_config_settle_time_ms = 1000;


static volatile bool s_udp_armed = false;
static volatile bool s_csi_capture_active = false;

static uint64_t s_capture_config_id = 0;
static uint32_t s_capture_block_id = 0;
static uint32_t s_capture_generation = 0;
static char s_capture_session_id[128] = {0};
static uint32_t s_capture_target_packets = 0;

static uint8_t s_udp_seen[(WS_MAX_TARGET_PACKETS + 7) / 8];
static uint8_t s_csi_probe_seen[(WS_MAX_TARGET_PACKETS + 7) / 8];

static uint32_t s_udp_unique = 0;
static uint32_t s_udp_duplicates = 0;
static uint32_t s_udp_invalid = 0;

static uint32_t s_csi_callbacks = 0;
static uint32_t s_csi_source_matches = 0;
static uint32_t s_csi_probe_matches = 0;
static uint32_t s_csi_enqueued = 0;
static uint32_t s_csi_queue_drops = 0;
static uint32_t s_csi_oversize = 0;
static uint32_t s_csi_invalid = 0;
static uint32_t s_csi_first_invalid = 0;
static uint32_t s_csi_estimate_invalid = 0;
static uint32_t s_csi_transport_generated = 0;
static uint32_t s_csi_udp_sent = 0;
static uint32_t s_csi_udp_errors = 0;

static portMUX_TYPE s_capture_mux = portMUX_INITIALIZER_UNLOCKED;


typedef enum {
    CAPTURE_NONE = 0,
    CAPTURE_TARGET_REACHED,
    CAPTURE_TIMEOUT,
} capture_completion_reason_t;

static capture_completion_reason_t s_completion_reason = CAPTURE_NONE;
static uint32_t s_final_udp_unique = 0;
static uint32_t s_final_udp_duplicates = 0;
static uint32_t s_final_udp_invalid = 0;


static char s_topic_config[160];
static char s_topic_arm[160];
static char s_topic_status[160];
static char s_topic_info[160];
static char s_topic_ota[160];
static char s_topic_restart[160];
static char s_topic_csi_reinit[160];


typedef struct {
    uint64_t config_id;
    char band[8];
    char phy[16];
    int bandwidth_mhz;
    int expected_channel;
    int probe_rate_hz;
    int blocks;
    int target_packets;
    int nominal_duration_ms;
    int max_capture_ms;
    int settle_time_ms;
} experiment_config_t;

typedef struct {
    uint64_t config_id;
    uint32_t block_id;
    uint32_t probe_sequence;
    uint32_t transport_sequence;
    uint32_t timestamp_us;
    int8_t rssi;
    uint8_t flags;
    uint8_t channel;
    uint16_t rx_seq;
    uint16_t csi_len;
    int8_t csi[WS_CSI_MAX_LEN];
} csi_sample_t;

typedef struct {
    uint64_t config_id;
    uint32_t block_id;
    uint32_t generation;
    uint32_t timeout_ms;
} capture_timeout_ctx_t;


static uint32_t read_be32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) |
           ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) |
           ((uint32_t)p[3]);
}

static uint64_t read_be64(const uint8_t *p)
{
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v = (v << 8) | p[i];
    }
    return v;
}

static void write_be16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static void write_be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static void write_be64(uint8_t *p, uint64_t v)
{
    for (int i = 0; i < 8; ++i) {
        p[i] = (uint8_t)(v >> (56 - 8 * i));
    }
}

static bool bitmap_test(const uint8_t *bitmap, uint32_t seq)
{
    return (bitmap[seq >> 3] & (1U << (seq & 7U))) != 0;
}

static void bitmap_set(uint8_t *bitmap, uint32_t seq)
{
    bitmap[seq >> 3] |= (uint8_t)(1U << (seq & 7U));
}

static bool topic_equals(const char *topic, int topic_len, const char *expected)
{
    size_t expected_len = strlen(expected);
    return topic_len == (int)expected_len &&
           memcmp(topic, expected, expected_len) == 0;
}

static const char *completion_to_string(capture_completion_reason_t reason)
{
    switch (reason) {
        case CAPTURE_TARGET_REACHED: return "target_reached";
        case CAPTURE_TIMEOUT: return "timeout";
        default: return "unknown";
    }
}


static const uint8_t *find_probe_payload(const uint8_t *payload, uint16_t payload_len)
{
    if (payload == NULL || payload_len < WS_PROBE_PACKET_SIZE) {
        return NULL;
    }

    for (uint16_t i = 0; i + WS_PROBE_PACKET_SIZE <= payload_len; ++i) {
        const uint8_t *c = &payload[i];

        if (c[0] == WS_PROBE_MAGIC_0 &&
            c[1] == WS_PROBE_MAGIC_1 &&
            c[2] == WS_PROBE_MAGIC_2 &&
            c[3] == WS_PROBE_MAGIC_3 &&
            c[4] == WS_PROBE_VERSION) {
            return c;
        }
    }

    return NULL;
}


static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;

    if (!s_csi_capture_active) {
        return;
    }

    s_csi_callbacks++;

    if (info == NULL || info->buf == NULL || info->len == 0) {
        s_csi_invalid++;
        return;
    }

    if (s_ap_bssid_valid &&
        memcmp(info->mac, s_ap_bssid, sizeof(s_ap_bssid)) != 0) {
        return;
    }

    s_csi_source_matches++;

    const uint8_t *probe = find_probe_payload(
        info->payload,
        info->payload_len
    );

    if (probe == NULL) {
        return;
    }

    const uint64_t config_id = read_be64(&probe[8]);
    const uint32_t block_id = read_be32(&probe[16]);
    const uint32_t probe_sequence = read_be32(&probe[20]);

    if (config_id != s_capture_config_id ||
        block_id != s_capture_block_id ||
        probe_sequence >= s_capture_target_packets ||
        probe_sequence >= WS_MAX_TARGET_PACKETS) {
        s_csi_invalid++;
        return;
    }

    s_csi_probe_matches++;

    if (bitmap_test(s_csi_probe_seen, probe_sequence)) {
        return;
    }

    if (info->len > WS_CSI_MAX_LEN) {
        s_csi_oversize++;
        return;
    }

    csi_sample_t sample = {0};

    sample.config_id = config_id;
    sample.block_id = block_id;
    sample.probe_sequence = probe_sequence;
    sample.timestamp_us = info->rx_ctrl.timestamp;
    sample.rssi = (int8_t)info->rx_ctrl.rssi;
    sample.channel = (uint8_t)info->rx_ctrl.channel;
    sample.rx_seq = info->rx_seq;
    sample.csi_len = info->len;

    if (info->first_word_invalid) {
        sample.flags |= 0x01;
        s_csi_first_invalid++;
    }

    if (info->rx_ctrl.rx_channel_estimate_info_vld) {
        sample.flags |= 0x02;
    } else {
        s_csi_estimate_invalid++;
    }

    sample.transport_sequence = ++s_csi_transport_generated;

    memcpy(sample.csi, info->buf, info->len);

    if (xQueueSend(s_csi_queue, &sample, 0) != pdTRUE) {
        s_csi_queue_drops++;
        return;
    }

    bitmap_set(s_csi_probe_seen, probe_sequence);
    s_csi_enqueued++;
}


static void encode_csi2_header(uint8_t *out, const csi_sample_t *sample)
{
    out[0] = WS_CSI_MAGIC_0;
    out[1] = WS_CSI_MAGIC_1;
    out[2] = WS_CSI_MAGIC_2;
    out[3] = WS_CSI_MAGIC_3;
    out[4] = WS_CSI_VERSION;


    out[5] = 0;
    out[6] = 0;
    out[7] = 0;

    memset(&out[8], 0, 4);
    size_t id_len = strlen(CONFIG_WS_DEVICE_ID);
    if (id_len > 4) {
        id_len = 4;
    }
    memcpy(&out[8], CONFIG_WS_DEVICE_ID, id_len);

    write_be64(&out[12], sample->config_id);
    write_be32(&out[20], sample->block_id);
    write_be32(&out[24], sample->probe_sequence);
    write_be32(&out[28], sample->transport_sequence);
    write_be32(&out[32], sample->timestamp_us);

    out[36] = (uint8_t)sample->rssi;
    out[37] = sample->flags;
    out[38] = sample->channel;

    write_be16(&out[39], sample->rx_seq);
    write_be16(&out[41], sample->csi_len);
}

static void csi_udp_sender_task(void *arg)
{
    (void)arg;

    uint8_t packet[WS_CSI_HEADER_LEN + WS_CSI_MAX_LEN];
    csi_sample_t sample;

    int sock = -1;

    while (true) {
        if (sock < 0) {
            sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);

            if (sock < 0) {
                ESP_LOGE(TAG, "CSI2 socket() errno=%d", errno);
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }

            ESP_LOGI(
                TAG,
                "CSI2 UDP data plane listo -> gateway:%d",
                CONFIG_WS_CSI_UDP_PORT
            );
        }

        if (xQueueReceive(s_csi_queue, &sample, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        const uint32_t server_ip = s_csi_server_ip_addr;

        if (server_ip == 0) {
            s_csi_udp_errors++;
            continue;
        }

        struct sockaddr_in dest = {0};
        dest.sin_family = AF_INET;
        dest.sin_port = htons(CONFIG_WS_CSI_UDP_PORT);
        dest.sin_addr.s_addr = server_ip;

        encode_csi2_header(packet, &sample);
        memcpy(
            &packet[WS_CSI_HEADER_LEN],
            sample.csi,
            sample.csi_len
        );

        const size_t packet_len = WS_CSI_HEADER_LEN + sample.csi_len;

        int sent = sendto(
            sock,
            packet,
            packet_len,
            0,
            (struct sockaddr *)&dest,
            sizeof(dest)
        );

        if (sent == (int)packet_len) {
            s_csi_udp_sent++;
        } else {
            s_csi_udp_errors++;

            if (sent < 0 &&
                (errno == ENETDOWN ||
                 errno == ENETUNREACH ||
                 errno == EHOSTUNREACH ||
                 errno == EBADF)) {
                close(sock);
                sock = -1;
            }
        }
    }
}


static esp_err_t configure_radio_auto(void)
{
    ESP_ERROR_CHECK(
        esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO)
    );

    wifi_protocols_t protocols = {
        .ghz_2g =
            WIFI_PROTOCOL_11B |
            WIFI_PROTOCOL_11G |
            WIFI_PROTOCOL_11N,
        .ghz_5g =
            WIFI_PROTOCOL_11A |
            WIFI_PROTOCOL_11N,
    };

    ESP_ERROR_CHECK(
        esp_wifi_set_protocols(WIFI_IF_STA, &protocols)
    );

    wifi_bandwidths_t bandwidths = {
        .ghz_2g = WIFI_BW20,
        .ghz_5g = WIFI_BW20,
    };

    ESP_ERROR_CHECK(
        esp_wifi_set_bandwidths(WIFI_IF_STA, &bandwidths)
    );

    ESP_ERROR_CHECK(
        esp_wifi_set_ps(WIFI_PS_NONE)
    );

#if CONFIG_WS_CSI_PROMISCUOUS
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    const char *promiscuous_text = "ON";
#else
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(false));
    const char *promiscuous_text = "OFF";
#endif

    ESP_LOGI(
        TAG,
        "Radio: AUTO / 2.4=bgn / 5=an / HT20 / PS=OFF / promiscuous=%s",
        promiscuous_text
    );

    return ESP_OK;
}

static esp_err_t csi_reconfigure_current_link(void)
{
    if (s_csi_capture_active || s_udp_armed || ws_ota_is_in_progress()) {
        return ESP_ERR_INVALID_STATE;
    }

    wifi_ap_record_t ap = {0};
    esp_err_t err = esp_wifi_sta_get_ap_info(&ap);
    if (err != ESP_OK) {
        return err;
    }

    if (s_csi_initialized) {
        err = esp_wifi_set_csi(false);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "set_csi(false): %s", esp_err_to_name(err));
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    memcpy(s_ap_bssid, ap.bssid, sizeof(s_ap_bssid));
    s_ap_bssid_valid = true;

    esp_netif_ip_info_t ip_info = {0};
    if (s_sta_netif != NULL &&
        esp_netif_get_ip_info(s_sta_netif, &ip_info) == ESP_OK) {
        s_csi_server_ip_addr = ip_info.gw.addr;
    }
    const bool is_5ghz = (ap.primary > 14);


    wifi_csi_config_t csi_cfg = {
        .enable = true,


        .acquire_csi_legacy = is_5ghz ? false : true,
        .acquire_csi_force_lltf =
            is_5ghz ? false : WS_CSI_FORCE_LLTF,

        .acquire_csi_ht20 = true,
        .acquire_csi_ht40 = false,
        .acquire_csi_vht = false,
        .acquire_csi_su = false,
        .acquire_csi_mu = false,
        .acquire_csi_dcm = false,
        .acquire_csi_beamformed = false,

        .acquire_csi_he_stbc_mode = 2,
        .val_scale_cfg = 0,
        .dump_ack_en = false,

        .lltf_bit_mode = is_5ghz ? 0 : 1,

        .reserved = false,
    };

    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(
        TAG,
        "CSI config: band=%s channel=%u legacy=%s force_lltf=%s lltf_bit_mode=%d",
        is_5ghz ? "5g" : "2.4g",
        ap.primary,
        csi_cfg.acquire_csi_legacy ? "true" : "false",
        csi_cfg.acquire_csi_force_lltf ? "true" : "false",
        csi_cfg.lltf_bit_mode
    );

    s_csi_initialized = true;
    s_link_generation++;

    xEventGroupSetBits(
        s_wifi_event_group,
        CSI_READY_BIT
    );

    ESP_LOGI(
        TAG,
        "CSI_ENGINE_READY gen=%" PRIu32
        " channel=%u rssi=%d bssid=" MACSTR,
        s_link_generation,
        ap.primary,
        ap.rssi,
        MAC2STR(ap.bssid)
    );

    return ESP_OK;
}

static void link_manager_task(void *arg)
{
    (void)arg;

    while (true) {
        xEventGroupWaitBits(
            s_wifi_event_group,
            LINK_REFRESH_BIT,
            pdTRUE,
            pdFALSE,
            portMAX_DELAY
        );

        xEventGroupClearBits(
            s_wifi_event_group,
            CSI_READY_BIT
        );

        while (s_csi_capture_active || s_udp_armed || ws_ota_is_in_progress()) {
            vTaskDelay(pdMS_TO_TICKS(50));
        }

        vTaskDelay(pdMS_TO_TICKS(100));

        esp_err_t err = csi_reconfigure_current_link();

        if (err != ESP_OK) {
            ESP_LOGE(
                TAG,
                "CSI lifecycle: no se pudo reconfigurar link: %s",
                esp_err_to_name(err)
            );
        }
    }
}

static void wifi_event_handler(
    void *arg,
    esp_event_base_t event_base,
    int32_t event_id,
    void *event_data
)
{
    (void)arg;
    (void)event_data;

    if (event_base == WIFI_EVENT &&
        event_id == WIFI_EVENT_STA_DISCONNECTED) {

        ESP_LOGW(TAG, "WiFi desconectado; reconectando");

        xEventGroupClearBits(
            s_wifi_event_group,
            WIFI_CONNECTED_BIT | CSI_READY_BIT
        );

        s_ap_bssid_valid = false;
        s_csi_server_ip_addr = 0;

        esp_wifi_connect();
        return;
    }

    if (event_base == IP_EVENT &&
        event_id == IP_EVENT_STA_GOT_IP) {

        xEventGroupSetBits(
            s_wifi_event_group,
            WIFI_CONNECTED_BIT | LINK_REFRESH_BIT
        );
    }
}

static void wifi_init(void)
{
    s_wifi_event_group = xEventGroupCreate();
    if (s_wifi_event_group == NULL) {
        abort();
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    s_sta_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    ESP_ERROR_CHECK(
        esp_event_handler_register(
            WIFI_EVENT,
            ESP_EVENT_ANY_ID,
            wifi_event_handler,
            NULL
        )
    );

    ESP_ERROR_CHECK(
        esp_event_handler_register(
            IP_EVENT,
            IP_EVENT_STA_GOT_IP,
            wifi_event_handler,
            NULL
        )
    );

    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(configure_radio_auto());
}

static esp_err_t connect_wifi_auto(void)
{
    if (strcmp(CONFIG_WS_WIFI_SSID_24, CONFIG_WS_WIFI_SSID_5) != 0 ||
        strcmp(CONFIG_WS_WIFI_PASS_24, CONFIG_WS_WIFI_PASS_5) != 0) {
        ESP_LOGE(TAG, "SSID/password 2.4 y 5 deben coincidir");
        return ESP_ERR_INVALID_STATE;
    }

    wifi_config_t cfg = {0};

    strlcpy(
        (char *)cfg.sta.ssid,
        CONFIG_WS_WIFI_SSID_24,
        sizeof(cfg.sta.ssid)
    );

    strlcpy(
        (char *)cfg.sta.password,
        CONFIG_WS_WIFI_PASS_24,
        sizeof(cfg.sta.password)
    );

    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(
        esp_wifi_set_config(WIFI_IF_STA, &cfg)
    );

    ESP_LOGI(TAG, "Conectando a %s", CONFIG_WS_WIFI_SSID_24);

    ESP_ERROR_CHECK(esp_wifi_connect());

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT,
        pdFALSE,
        pdTRUE,
        pdMS_TO_TICKS(30000)
    );

    if (!(bits & WIFI_CONNECTED_BIT)) {
        return ESP_ERR_TIMEOUT;
    }

    bits = xEventGroupWaitBits(
        s_wifi_event_group,
        CSI_READY_BIT,
        pdFALSE,
        pdTRUE,
        pdMS_TO_TICKS(5000)
    );

    if (!(bits & CSI_READY_BIT)) {
        return ESP_ERR_TIMEOUT;
    }

    return ESP_OK;
}

static const char *get_current_band(const wifi_ap_record_t *ap)
{
    return ap->primary <= 14 ? "2.4g" : "5g";
}

static bool wifi_matches_config(const experiment_config_t *cfg)
{
    wifi_ap_record_t ap = {0};

    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        return false;
    }

    if (strcmp(get_current_band(&ap), cfg->band) != 0) {
        return false;
    }

    if (cfg->expected_channel > 0 &&
        ap.primary != cfg->expected_channel) {
        return false;
    }

    wifi_bandwidths_t bws = {0};
    if (esp_wifi_get_bandwidths(WIFI_IF_STA, &bws) != ESP_OK) {
        return false;
    }

    wifi_bandwidth_t bw =
        ap.primary <= 14 ? bws.ghz_2g : bws.ghz_5g;

    if (cfg->bandwidth_mhz != 20 || bw != WIFI_BW20) {
        return false;
    }

    return strcmp(cfg->phy, "802.11n") == 0;
}

static void battery_status_store(const battery_status_t *status)
{
    if (status == NULL) {
        return;
    }

    taskENTER_CRITICAL(&s_battery_mux);
    s_battery_status = *status;
    taskEXIT_CRITICAL(&s_battery_mux);
}

static battery_status_t battery_status_snapshot(void)
{
    battery_status_t status;

    taskENTER_CRITICAL(&s_battery_mux);
    status = s_battery_status;
    taskEXIT_CRITICAL(&s_battery_mux);

    return status;
}

static void battery_task(void *arg)
{
    (void)arg;

    while (true) {
        if (!s_csi_capture_active) {
            battery_status_t status = {0};
            esp_err_t err = battery_monitor_read(&status);

            if (err == ESP_OK) {
                battery_status_store(&status);
            } else {
                ESP_LOGW(TAG, "Battery read fallo: %s", esp_err_to_name(err));
            }
        }

        vTaskDelay(pdMS_TO_TICKS(30000));
    }
}


static void publish_info(void)
{
    if (!s_mqtt_connected ||
        s_mqtt_client == NULL ||
        s_sta_netif == NULL) {
        return;
    }

    esp_netif_ip_info_t ip = {0};
    uint8_t mac[6] = {0};

    if (esp_netif_get_ip_info(s_sta_netif, &ip) != ESP_OK ||
        esp_wifi_get_mac(WIFI_IF_STA, mac) != ESP_OK) {
        return;
    }

    battery_status_t battery = battery_status_snapshot();
    char payload[768];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"firmware_version\":\"%s\","
            "\"ota_slot\":\"%s\","
            "\"idf_version\":\"%s\","
            "\"network\":{"
                "\"ip\":\"" IPSTR "\","
                "\"gateway\":\"" IPSTR "\","
                "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\""
            "},"
            "\"battery\":{"
                "\"valid\":%s,"
                "\"voltage_mv\":%d,"
                "\"percent_estimate\":%d"
            "}"
        "}",
        CONFIG_WS_DEVICE_ID,
        ws_ota_current_version(),
        ws_ota_current_slot(),
        esp_get_idf_version(),
        IP2STR(&ip.ip),
        IP2STR(&ip.gw),
        mac[0], mac[1], mac[2],
        mac[3], mac[4], mac[5],
        battery.valid ? "true" : "false",
        battery.valid ? battery.battery_mv : 0,
        battery.valid ? battery.percent_estimate : -1
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_info,
        payload,
        0,
        1,
        1
    );
}

static void publish_heartbeat(void)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
    battery_status_t battery = battery_status_snapshot();

    char payload[640];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"HEARTBEAT\","
            "\"firmware_version\":\"%s\","
            "\"link_generation\":%" PRIu32 ","
            "\"wifi_connected\":%s,"
            "\"csi_ready\":%s,"
            "\"capture_active\":%s,"
            "\"battery\":{"
                "\"valid\":%s,"
                "\"voltage_mv\":%d,"
                "\"percent_estimate\":%d"
            "}"
        "}",
        CONFIG_WS_DEVICE_ID,
        ws_ota_current_version(),
        s_link_generation,
        (bits & WIFI_CONNECTED_BIT) ? "true" : "false",
        (bits & CSI_READY_BIT) ? "true" : "false",
        s_csi_capture_active ? "true" : "false",
        battery.valid ? "true" : "false",
        battery.valid ? battery.battery_mv : 0,
        battery.valid ? battery.percent_estimate : -1
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        0,
        0
    );
}

static void heartbeat_task(void *arg)
{
    (void)arg;

    while (true) {
        vTaskDelay(
            pdMS_TO_TICKS(CONFIG_WS_HEARTBEAT_INTERVAL_MS)
        );
        publish_heartbeat();
    }
}

static void publish_ready(const experiment_config_t *cfg)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
    if (!(bits & CSI_READY_BIT)) {
        ESP_LOGW(TAG, "READY pospuesto: CSI_ENGINE no listo");
        return;
    }

    wifi_ap_record_t ap = {0};
    wifi_bandwidths_t bws = {0};

    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK ||
        esp_wifi_get_bandwidths(WIFI_IF_STA, &bws) != ESP_OK) {
        return;
    }

    wifi_bandwidth_t bw =
        ap.primary <= 14 ? bws.ghz_2g : bws.ghz_5g;

    int bw_mhz = bw == WIFI_BW20 ? 20 :
                 bw == WIFI_BW40 ? 40 : 0;

    char payload[640];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"READY\","
            "\"config_id\":%" PRIu64 ","
            "\"link_generation\":%" PRIu32 ","
            "\"applied\":{"
                "\"band\":\"%s\","
                "\"channel\":%u,"
                "\"bandwidth_mhz\":%d,"
                "\"phy\":\"802.11n\""
            "},"
            "\"network\":{\"rssi\":%d}"
        "}",
        CONFIG_WS_DEVICE_ID,
        cfg->config_id,
        s_link_generation,
        get_current_band(&ap),
        ap.primary,
        bw_mhz,
        ap.rssi
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        1,
        0
    );

    ESP_LOGI(
        TAG,
        "READY config=%" PRIu64 " band=%s ch=%u BW=%d gen=%" PRIu32,
        cfg->config_id,
        get_current_band(&ap),
        ap.primary,
        bw_mhz,
        s_link_generation
    );
}


static bool parse_config(const char *payload, experiment_config_t *out)
{
    cJSON *root = cJSON_Parse(payload);
    if (root == NULL) {
        return false;
    }

    bool valid = false;

    cJSON *schema = cJSON_GetObjectItem(root, "schema_version");
    cJSON *config_id = cJSON_GetObjectItem(root, "config_id");
    cJSON *sensing = cJSON_GetObjectItem(root, "sensing");
    cJSON *probe = cJSON_GetObjectItem(root, "probe");
    cJSON *capture = cJSON_GetObjectItem(root, "capture");

    if (!cJSON_IsNumber(schema) ||
        schema->valueint != 1 ||
        !cJSON_IsNumber(config_id) ||
        !cJSON_IsObject(sensing) ||
        !cJSON_IsObject(probe) ||
        !cJSON_IsObject(capture)) {
        goto cleanup;
    }

    cJSON *band = cJSON_GetObjectItem(sensing, "band");
    cJSON *phy = cJSON_GetObjectItem(sensing, "phy");
    cJSON *bandwidth = cJSON_GetObjectItem(sensing, "bandwidth_mhz");
    cJSON *channel = cJSON_GetObjectItem(sensing, "expected_channel");
    cJSON *rate = cJSON_GetObjectItem(probe, "rate_hz");
    cJSON *blocks = cJSON_GetObjectItem(capture, "blocks");
    cJSON *target = cJSON_GetObjectItem(capture, "target_packets");
    cJSON *nominal = cJSON_GetObjectItem(capture, "nominal_duration_ms");
    cJSON *max_capture = cJSON_GetObjectItem(capture, "max_capture_ms");
    cJSON *settle = cJSON_GetObjectItem(capture, "settle_time_ms");

    if (!cJSON_IsString(band) ||
        !cJSON_IsString(phy) ||
        !cJSON_IsNumber(bandwidth) ||
        !cJSON_IsNumber(channel) ||
        !cJSON_IsNumber(rate) ||
        !cJSON_IsNumber(blocks) ||
        !cJSON_IsNumber(target) ||
        !cJSON_IsNumber(nominal) ||
        !cJSON_IsNumber(max_capture) ||
        !cJSON_IsNumber(settle)) {
        goto cleanup;
    }

    if ((strcmp(band->valuestring, "2.4g") != 0 &&
         strcmp(band->valuestring, "5g") != 0) ||
        strcmp(phy->valuestring, "802.11n") != 0 ||
        bandwidth->valueint != 20 ||
        target->valueint <= 0 ||
        target->valueint > WS_MAX_TARGET_PACKETS) {
        goto cleanup;
    }

    memset(out, 0, sizeof(*out));

    out->config_id = (uint64_t)config_id->valuedouble;
    strlcpy(out->band, band->valuestring, sizeof(out->band));
    strlcpy(out->phy, phy->valuestring, sizeof(out->phy));

    out->bandwidth_mhz = bandwidth->valueint;
    out->expected_channel = channel->valueint;
    out->probe_rate_hz = rate->valueint;
    out->blocks = blocks->valueint;
    out->target_packets = target->valueint;
    out->nominal_duration_ms = nominal->valueint;
    out->max_capture_ms = max_capture->valueint;
    out->settle_time_ms = settle->valueint;

    valid = true;

cleanup:
    cJSON_Delete(root);
    return valid;
}

static void config_task(void *arg)
{
    (void)arg;

    experiment_config_t cfg;

    while (true) {
        if (xQueueReceive(
                s_config_queue,
                &cfg,
                portMAX_DELAY
            ) != pdTRUE) {
            continue;
        }

        ESP_LOGI(
            TAG,
            "CONFIG id=%" PRIu64
            " band=%s ch=%d rate=%d target=%d",
            cfg.config_id,
            cfg.band,
            cfg.expected_channel,
            cfg.probe_rate_hz,
            cfg.target_packets
        );

        if (!wifi_matches_config(&cfg)) {
            ESP_LOGW(
                TAG,
                "CONFIG no coincide con link actual; esperando AP correcto"
            );
            continue;
        }

        EventBits_t bits = xEventGroupWaitBits(
            s_wifi_event_group,
            CSI_READY_BIT,
            pdFALSE,
            pdTRUE,
            pdMS_TO_TICKS(5000)
        );

        if (!(bits & CSI_READY_BIT)) {
            ESP_LOGW(TAG, "CONFIG link correcto pero CSI no quedó listo");
            continue;
        }

        s_applied_config_id = cfg.config_id;
        s_config_target_packets = (uint32_t)cfg.target_packets;
        s_config_max_capture_ms = (uint32_t)cfg.max_capture_ms;
        s_config_settle_time_ms = (uint32_t)cfg.settle_time_ms;

        if (s_config_settle_time_ms > 0) {
            vTaskDelay(
                pdMS_TO_TICKS(s_config_settle_time_ms)
            );
        }

        publish_ready(&cfg);
    }
}


static void reset_capture(
    uint64_t config_id,
    const char *session_id,
    uint32_t block_id
)
{
    s_csi_capture_active = false;
    s_udp_armed = false;

    xQueueReset(s_csi_queue);

    memset(s_udp_seen, 0, sizeof(s_udp_seen));
    memset(s_csi_probe_seen, 0, sizeof(s_csi_probe_seen));

    s_capture_config_id = config_id;
    s_capture_block_id = block_id;
    s_capture_generation++;
    s_capture_target_packets = s_config_target_packets;
    strlcpy(
        s_capture_session_id,
        session_id,
        sizeof(s_capture_session_id)
    );

    s_udp_unique = 0;
    s_udp_duplicates = 0;
    s_udp_invalid = 0;

    s_csi_callbacks = 0;
    s_csi_source_matches = 0;
    s_csi_probe_matches = 0;
    s_csi_enqueued = 0;
    s_csi_queue_drops = 0;
    s_csi_oversize = 0;
    s_csi_invalid = 0;
    s_csi_first_invalid = 0;
    s_csi_estimate_invalid = 0;
    s_csi_transport_generated = 0;
    s_csi_udp_sent = 0;
    s_csi_udp_errors = 0;

    s_completion_reason = CAPTURE_NONE;
    s_final_udp_unique = 0;
    s_final_udp_duplicates = 0;
    s_final_udp_invalid = 0;

    s_csi_capture_active = true;
    s_udp_armed = true;
}

static void publish_armed(void)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    char payload[512];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"ARMED\","
            "\"config_id\":%" PRIu64 ","
            "\"session_id\":\"%s\","
            "\"block_id\":%" PRIu32 ","
            "\"target_packets\":%" PRIu32
        "}",
        CONFIG_WS_DEVICE_ID,
        s_capture_config_id,
        s_capture_session_id,
        s_capture_block_id,
        s_capture_target_packets
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        1,
        0
    );
}

static bool capture_finalize_if_current(
    capture_completion_reason_t reason,
    uint64_t config_id,
    uint32_t block_id,
    uint32_t generation
)
{
    bool notify = false;

    taskENTER_CRITICAL(&s_capture_mux);

    if (s_udp_armed &&
        s_capture_config_id == config_id &&
        s_capture_block_id == block_id &&
        s_capture_generation == generation &&
        s_completion_reason == CAPTURE_NONE) {

        s_udp_armed = false;
        s_completion_reason = reason;

        s_final_udp_unique = s_udp_unique;
        s_final_udp_duplicates = s_udp_duplicates;
        s_final_udp_invalid = s_udp_invalid;

        notify = true;
    }

    taskEXIT_CRITICAL(&s_capture_mux);

    if (notify && s_capture_finalize_task_handle != NULL) {
        xTaskNotifyGive(s_capture_finalize_task_handle);
    }

    return notify;
}

static void capture_timeout_task(void *arg)
{
    capture_timeout_ctx_t *ctx =
        (capture_timeout_ctx_t *)arg;

    vTaskDelay(pdMS_TO_TICKS(ctx->timeout_ms));

    capture_finalize_if_current(
        CAPTURE_TIMEOUT,
        ctx->config_id,
        ctx->block_id,
        ctx->generation
    );

    free(ctx);
    vTaskDelete(NULL);
}

static void handle_arm(const char *payload)
{
    if (ws_ota_is_in_progress() || s_restart_pending) {
        ESP_LOGW(TAG, "ARM ignorado: dispositivo ocupado");
        return;
    }

    EventBits_t bits = xEventGroupGetBits(s_wifi_event_group);
    if (!(bits & CSI_READY_BIT)) {
        ESP_LOGW(TAG, "ARM ignorado: CSI_ENGINE no listo");
        return;
    }

    cJSON *root = cJSON_Parse(payload);
    if (root == NULL) {
        return;
    }

    cJSON *config_id = cJSON_GetObjectItem(root, "config_id");
    cJSON *session_id = cJSON_GetObjectItem(root, "session_id");
    cJSON *block_id = cJSON_GetObjectItem(root, "block_id");

    if (!cJSON_IsNumber(config_id) ||
        !cJSON_IsString(session_id) ||
        !cJSON_IsNumber(block_id)) {
        cJSON_Delete(root);
        return;
    }

    uint64_t cfg = (uint64_t)config_id->valuedouble;
    uint32_t blk = (uint32_t)block_id->valuedouble;

    if (cfg != s_applied_config_id) {
        ESP_LOGW(
            TAG,
            "ARM config=%" PRIu64 " != applied=%" PRIu64,
            cfg,
            s_applied_config_id
        );
        cJSON_Delete(root);
        return;
    }

    reset_capture(
        cfg,
        session_id->valuestring,
        blk
    );

    capture_timeout_ctx_t *ctx =
        calloc(1, sizeof(*ctx));

    if (ctx == NULL) {
        s_udp_armed = false;
        s_csi_capture_active = false;
        cJSON_Delete(root);
        return;
    }

    ctx->config_id = cfg;
    ctx->block_id = blk;
    ctx->generation = s_capture_generation;
    ctx->timeout_ms = s_config_max_capture_ms;

    if (xTaskCreate(
            capture_timeout_task,
            "capture_timeout",
            3072,
            ctx,
            4,
            NULL
        ) != pdPASS) {
        free(ctx);
        s_udp_armed = false;
        s_csi_capture_active = false;
        cJSON_Delete(root);
        return;
    }

    publish_armed();

    ESP_LOGI(
        TAG,
        "ARMED config=%" PRIu64 " session=%s block=%" PRIu32
        " target=%" PRIu32,
        s_capture_config_id,
        s_capture_session_id,
        s_capture_block_id,
        s_capture_target_packets
    );

    cJSON_Delete(root);
}


static void process_probe_packet(const uint8_t *data, int len)
{
    if (!s_udp_armed ||
        len < WS_PROBE_PACKET_SIZE ||
        data[0] != WS_PROBE_MAGIC_0 ||
        data[1] != WS_PROBE_MAGIC_1 ||
        data[2] != WS_PROBE_MAGIC_2 ||
        data[3] != WS_PROBE_MAGIC_3 ||
        data[4] != WS_PROBE_VERSION) {
        if (s_udp_armed) {
            s_udp_invalid++;
        }
        return;
    }

    uint64_t config_id = read_be64(&data[8]);
    uint32_t block_id = read_be32(&data[16]);
    uint32_t sequence = read_be32(&data[20]);

    if (config_id != s_capture_config_id ||
        block_id != s_capture_block_id ||
        sequence >= s_capture_target_packets ||
        sequence >= WS_MAX_TARGET_PACKETS) {
        s_udp_invalid++;
        return;
    }

    if (bitmap_test(s_udp_seen, sequence)) {
        s_udp_duplicates++;
        return;
    }

    bitmap_set(s_udp_seen, sequence);
    s_udp_unique++;

    if (s_udp_unique >= s_capture_target_packets) {
        capture_finalize_if_current(
            CAPTURE_TARGET_REACHED,
            config_id,
            block_id,
            s_capture_generation
        );
    }
}

static void udp_receiver_task(void *arg)
{
    (void)arg;

    uint8_t buffer[256];

    while (true) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);

        if (sock < 0) {
            ESP_LOGE(TAG, "Probe socket() errno=%d", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        struct sockaddr_in addr = {0};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(WS_UDP_PORT);
        addr.sin_addr.s_addr = htonl(INADDR_ANY);

        if (bind(
                sock,
                (struct sockaddr *)&addr,
                sizeof(addr)
            ) < 0) {
            ESP_LOGE(TAG, "Probe bind() errno=%d", errno);
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        ESP_LOGI(TAG, "Probe UDP RX listo :%d", WS_UDP_PORT);

        while (true) {
            int len = recvfrom(
                sock,
                buffer,
                sizeof(buffer),
                0,
                NULL,
                NULL
            );

            if (len < 0) {
                break;
            }

            process_probe_packet(buffer, len);
        }

        close(sock);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}


static void publish_csi_complete_status(void)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    char payload[1200];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"CSI_COMPLETE\","
            "\"config_id\":%" PRIu64 ","
            "\"session_id\":\"%s\","
            "\"block_id\":%" PRIu32 ","
            "\"target_frames\":%" PRIu32 ","
            "\"frames\":%" PRIu32 ","
            "\"chunks\":0,"
            "\"udp_received\":%" PRIu32 ","
            "\"udp_target\":%" PRIu32 ","
            "\"udp_duplicates\":%" PRIu32 ","
            "\"udp_invalid\":%" PRIu32 ","
            "\"completion_reason\":\"%s\","
            "\"callbacks_total\":%" PRIu32 ","
            "\"source_matches\":%" PRIu32 ","
            "\"probe_matches\":%" PRIu32 ","
            "\"csi_enqueued\":%" PRIu32 ","
            "\"queue_drop\":%" PRIu32 ","
            "\"udp_csi_sent\":%" PRIu32 ","
            "\"udp_csi_errors\":%" PRIu32 ","
            "\"oversize\":%" PRIu32 ","
            "\"invalid\":%" PRIu32 ","
            "\"first_invalid\":%" PRIu32 ","
            "\"invalid_estimate\":%" PRIu32 ","
            "\"link_generation\":%" PRIu32
        "}",
        CONFIG_WS_DEVICE_ID,
        s_capture_config_id,
        s_capture_session_id,
        s_capture_block_id,
        s_capture_target_packets,
        s_csi_enqueued,
        s_final_udp_unique,
        s_capture_target_packets,
        s_final_udp_duplicates,
        s_final_udp_invalid,
        completion_to_string(s_completion_reason),
        s_csi_callbacks,
        s_csi_source_matches,
        s_csi_probe_matches,
        s_csi_enqueued,
        s_csi_queue_drops,
        s_csi_udp_sent,
        s_csi_udp_errors,
        s_csi_oversize,
        s_csi_invalid,
        s_csi_first_invalid,
        s_csi_estimate_invalid,
        s_link_generation
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        1,
        0
    );

    ESP_LOGI(
        TAG,
        "BLOCK COMPLETE config=%" PRIu64
        " block=%" PRIu32
        " probes=%" PRIu32 "/%" PRIu32
        " csi=%" PRIu32
        " sent=%" PRIu32
        " qdrop=%" PRIu32
        " txerr=%" PRIu32
        " invalid_est=%" PRIu32,
        s_capture_config_id,
        s_capture_block_id,
        s_final_udp_unique,
        s_capture_target_packets,
        s_csi_enqueued,
        s_csi_udp_sent,
        s_csi_queue_drops,
        s_csi_udp_errors,
        s_csi_estimate_invalid
    );
}

static void capture_finalize_task(void *arg)
{
    (void)arg;

    while (true) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);


        vTaskDelay(pdMS_TO_TICKS(WS_CAPTURE_FINAL_DELAY_MS));

        s_csi_capture_active = false;

        int waited_ms = 0;
        while (uxQueueMessagesWaiting(s_csi_queue) > 0 &&
               waited_ms < WS_QUEUE_DRAIN_TIMEOUT_MS) {
            vTaskDelay(pdMS_TO_TICKS(10));
            waited_ms += 10;
        }

        vTaskDelay(pdMS_TO_TICKS(20));

        publish_csi_complete_status();
    }
}


static void publish_csi_reinit_status(
    const char *state,
    uint64_t request_id,
    const char *detail
)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    char payload[512];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"%s\","
            "\"request_id\":%" PRIu64 ","
            "\"detail\":\"%s\""
        "}",
        CONFIG_WS_DEVICE_ID,
        state,
        request_id,
        detail
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        1,
        0
    );
}

static void handle_csi_reinit(const char *payload)
{
    uint64_t request_id = 0;

    cJSON *root = cJSON_Parse(payload);
    if (root != NULL) {
        cJSON *item = cJSON_GetObjectItem(root, "request_id");
        if (cJSON_IsNumber(item)) {
            request_id = (uint64_t)item->valuedouble;
        }
        cJSON_Delete(root);
    }

    if (ws_ota_is_in_progress() ||
        s_udp_armed ||
        s_csi_capture_active ||
        s_restart_pending) {
        publish_csi_reinit_status(
            "CSI_REINIT_REJECTED",
            request_id,
            "device_busy"
        );
        return;
    }

    xEventGroupClearBits(
        s_wifi_event_group,
        CSI_READY_BIT
    );

    esp_err_t err = csi_reconfigure_current_link();

    if (err != ESP_OK) {
        char detail[96];
        snprintf(
            detail,
            sizeof(detail),
            "reconfigure_failed:%s",
            esp_err_to_name(err)
        );

        publish_csi_reinit_status(
            "CSI_REINIT_REJECTED",
            request_id,
            detail
        );
        return;
    }

    publish_csi_reinit_status(
        "CSI_REINIT_DONE",
        request_id,
        "link_csi_reinitialized"
    );
}

static void publish_restart_status(
    const char *state,
    const char *detail
)
{
    if (!s_mqtt_connected || s_mqtt_client == NULL) {
        return;
    }

    char payload[384];

    snprintf(
        payload,
        sizeof(payload),
        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"%s\","
            "\"detail\":\"%s\""
        "}",
        CONFIG_WS_DEVICE_ID,
        state,
        detail
    );

    esp_mqtt_client_publish(
        s_mqtt_client,
        s_topic_status,
        payload,
        0,
        1,
        0
    );
}

static void restart_task(void *arg)
{
    (void)arg;
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

static void handle_restart(void)
{
    if (s_restart_pending) {
        return;
    }

    if (ws_ota_is_in_progress() ||
        s_udp_armed ||
        s_csi_capture_active) {
        publish_restart_status(
            "RESTART_REJECTED",
            "device_busy"
        );
        return;
    }

    s_restart_pending = true;

    publish_restart_status(
        "RESTARTING",
        "remote_recovery"
    );

    if (xTaskCreate(
            restart_task,
            "restart_task",
            2048,
            NULL,
            4,
            NULL
        ) != pdPASS) {
        s_restart_pending = false;
        publish_restart_status(
            "RESTART_REJECTED",
            "task_create_failed"
        );
    }
}


static void mqtt_event_handler(
    void *handler_args,
    esp_event_base_t base,
    int32_t event_id,
    void *event_data
)
{
    (void)handler_args;
    (void)base;

    esp_mqtt_event_handle_t event =
        (esp_mqtt_event_handle_t)event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            s_mqtt_connected = true;

            ESP_LOGI(TAG, "MQTT conectado");

            esp_mqtt_client_subscribe(
                s_mqtt_client,
                s_topic_config,
                1
            );
            esp_mqtt_client_subscribe(
                s_mqtt_client,
                s_topic_arm,
                1
            );
            esp_mqtt_client_subscribe(
                s_mqtt_client,
                s_topic_ota,
                1
            );
            esp_mqtt_client_subscribe(
                s_mqtt_client,
                s_topic_restart,
                1
            );
            esp_mqtt_client_subscribe(
                s_mqtt_client,
                s_topic_csi_reinit,
                1
            );

            publish_info();

            ws_ota_publish_boot_status(
                s_mqtt_client,
                s_topic_status
            );
            break;

        case MQTT_EVENT_DISCONNECTED:
            s_mqtt_connected = false;
            ESP_LOGW(TAG, "MQTT desconectado");
            break;

        case MQTT_EVENT_DATA: {
            if (event->current_data_offset != 0 ||
                event->data_len != event->total_data_len) {
                ESP_LOGW(TAG, "MQTT fragmentado ignorado");
                break;
            }

            char *payload = malloc(event->data_len + 1);
            if (payload == NULL) {
                break;
            }

            memcpy(payload, event->data, event->data_len);
            payload[event->data_len] = '\0';

            if (topic_equals(
                    event->topic,
                    event->topic_len,
                    s_topic_config
                )) {

                experiment_config_t cfg;
                if (parse_config(payload, &cfg)) {
                    xQueueOverwrite(
                        s_config_queue,
                        &cfg
                    );
                } else {
                    ESP_LOGE(TAG, "CONFIG inválido");
                }

            } else if (topic_equals(
                           event->topic,
                           event->topic_len,
                           s_topic_arm
                       )) {

                handle_arm(payload);

            } else if (topic_equals(
                           event->topic,
                           event->topic_len,
                           s_topic_ota
                       )) {

                bool busy =
                    s_udp_armed ||
                    s_csi_capture_active;

                ws_ota_handle_command(
                    payload,
                    s_sta_netif,
                    s_mqtt_client,
                    s_topic_status,
                    busy
                );

            } else if (topic_equals(
                           event->topic,
                           event->topic_len,
                           s_topic_restart
                       )) {

                handle_restart();

            } else if (topic_equals(
                           event->topic,
                           event->topic_len,
                           s_topic_csi_reinit
                       )) {

                handle_csi_reinit(payload);
            }

            free(payload);
            break;
        }

        case MQTT_EVENT_ERROR:
            ESP_LOGE(TAG, "MQTT_EVENT_ERROR");
            break;

        default:
            break;
    }
}

static void mqtt_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_WS_MQTT_URI,
        .credentials.username = CONFIG_WS_MQTT_USER,
        .credentials.client_id = CONFIG_WS_DEVICE_ID,
        .credentials.authentication.password =
            CONFIG_WS_MQTT_PASSWORD,
        .network.disable_auto_reconnect = false,
    };

    s_mqtt_client =
        esp_mqtt_client_init(&mqtt_cfg);

    if (s_mqtt_client == NULL) {
        abort();
    }

    ESP_ERROR_CHECK(
        esp_mqtt_client_register_event(
            s_mqtt_client,
            ESP_EVENT_ANY_ID,
            mqtt_event_handler,
            NULL
        )
    );

    ESP_ERROR_CHECK(
        esp_mqtt_client_start(s_mqtt_client)
    );
}


static void build_topics(void)
{
    snprintf(
        s_topic_config,
        sizeof(s_topic_config),
        "%s/devices/%s/cmd/config",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_arm,
        sizeof(s_topic_arm),
        "%s/devices/%s/cmd/arm",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_status,
        sizeof(s_topic_status),
        "%s/devices/%s/status",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_info,
        sizeof(s_topic_info),
        "%s/devices/%s/info",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_ota,
        sizeof(s_topic_ota),
        "%s/devices/%s/cmd/ota",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_restart,
        sizeof(s_topic_restart),
        "%s/devices/%s/cmd/restart",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );

    snprintf(
        s_topic_csi_reinit,
        sizeof(s_topic_csi_reinit),
        "%s/devices/%s/cmd/csi-reinit",
        CONFIG_WS_MQTT_BASE_TOPIC,
        CONFIG_WS_DEVICE_ID
    );
}

void app_main(void)
{
    ESP_LOGI(TAG, "========================================");
    ESP_LOGI(TAG, " WIFI SENSING RECEIVER V2");
    ESP_LOGI(TAG, " device_id=%s", CONFIG_WS_DEVICE_ID);
    ESP_LOGI(TAG, " IDF=%s", esp_get_idf_version());
    ESP_LOGI(TAG, " data_plane=UDP CSI2:%d", CONFIG_WS_CSI_UDP_PORT);
    ESP_LOGI(TAG, "========================================");

    esp_err_t ret = nvs_flash_init();

    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    } else {
        ESP_ERROR_CHECK(ret);
    }

    ws_ota_log_running();
    build_topics();

    esp_err_t battery_err = battery_monitor_init();
    if (battery_err == ESP_OK) {
        battery_status_t initial_battery = {0};
        if (battery_monitor_read(&initial_battery) == ESP_OK) {
            battery_status_store(&initial_battery);
            ESP_LOGI(
                TAG,
                "Battery: %d mV (~%d%%) valid=%s",
                initial_battery.battery_mv,
                initial_battery.percent_estimate,
                initial_battery.valid ? "true" : "false"
            );
        }
    } else {
        ESP_LOGW(
            TAG,
            "Battery monitor deshabilitado: %s",
            esp_err_to_name(battery_err)
        );
    }

    s_config_queue = xQueueCreate(
        1,
        sizeof(experiment_config_t)
    );

    s_csi_queue = xQueueCreate(
        WS_CSI_QUEUE_DEPTH,
        sizeof(csi_sample_t)
    );

    if (s_config_queue == NULL ||
        s_csi_queue == NULL) {
        abort();
    }

    wifi_init();

    if (xTaskCreate(
            link_manager_task,
            "link_manager",
            4096,
            NULL,
            6,
            NULL
        ) != pdPASS) {
        abort();
    }

    if (connect_wifi_auto() != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo conectar WiFi/CSI");
        return;
    }

    if (xTaskCreate(
            config_task,
            "config_task",
            6144,
            NULL,
            5,
            NULL
        ) != pdPASS) {
        abort();
    }

    if (xTaskCreate(
            csi_udp_sender_task,
            "csi_udp_tx",
            4096,
            NULL,
            6,
            NULL
        ) != pdPASS) {
        abort();
    }

    if (xTaskCreate(
            udp_receiver_task,
            "probe_udp_rx",
            4096,
            NULL,
            5,
            NULL
        ) != pdPASS) {
        abort();
    }

    if (xTaskCreate(
            capture_finalize_task,
            "capture_finalize",
            4096,
            NULL,
            5,
            &s_capture_finalize_task_handle
        ) != pdPASS) {
        abort();
    }

    mqtt_start();

    if (battery_err == ESP_OK) {
        if (xTaskCreate(
                battery_task,
                "battery",
                3072,
                NULL,
                1,
                NULL
            ) != pdPASS) {
            ESP_LOGW(TAG, "No se pudo crear battery task");
        }
    }

    if (xTaskCreate(
            heartbeat_task,
            "heartbeat",
            3072,
            NULL,
            2,
            NULL
        ) != pdPASS) {
        ESP_LOGW(TAG, "No se pudo crear heartbeat");
    }
}
