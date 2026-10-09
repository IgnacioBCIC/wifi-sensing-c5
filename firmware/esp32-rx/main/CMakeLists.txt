idf_component_register(
    SRCS
        "main.c"
        "ota_update.c"
        "battery_monitor.c"

    INCLUDE_DIRS
        "."

    REQUIRES
        esp_wifi
        esp_adc
        esp_timer
        esp_event
        esp_netif
        nvs_flash
        mqtt
        cjson
        mbedtls
        lwip
        esp_https_ota
        esp_http_client
        esp_app_format
        app_update
)
