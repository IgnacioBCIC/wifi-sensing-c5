#pragma once

#include <stdbool.h>

#include "esp_netif.h"
#include "mqtt_client.h"


const char *ws_ota_current_version(void);

const char *ws_ota_current_slot(void);

bool ws_ota_is_in_progress(void);

void ws_ota_log_running(void);

void ws_ota_publish_boot_status(
    esp_mqtt_client_handle_t mqtt_client,
    const char *status_topic
);

bool ws_ota_handle_command(
    const char *payload,
    esp_netif_t *sta_netif,
    esp_mqtt_client_handle_t mqtt_client,
    const char *status_topic,
    bool capture_busy
);
