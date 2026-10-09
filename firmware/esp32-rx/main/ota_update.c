#include "ota_update.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_app_desc.h"
#include "esp_err.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"

#include "cJSON.h"


static const char *TAG = "WS_OTA";

static volatile bool s_ota_in_progress = false;


typedef struct
{
    esp_netif_t *sta_netif;

    esp_mqtt_client_handle_t mqtt_client;

    char status_topic[128];

    char target_version[32];

    char filename[128];

} ota_task_context_t;


const char *ws_ota_current_version(void)
{
    const esp_app_desc_t *desc =
        esp_app_get_description();

    if (desc == NULL)
    {
        return "unknown";
    }

    return desc->version;
}


const char *ws_ota_current_slot(void)
{
    const esp_partition_t *partition =
        esp_ota_get_running_partition();

    if (partition == NULL)
    {
        return "unknown";
    }

    return partition->label;
}


bool ws_ota_is_in_progress(void)
{
    return s_ota_in_progress;
}


void ws_ota_log_running(void)
{
    ESP_LOGI(
        TAG,
        "========================================"
    );

    ESP_LOGI(
        TAG,
        "OTA runtime"
    );

    ESP_LOGI(
        TAG,
        "version = %s",
        ws_ota_current_version()
    );

    ESP_LOGI(
        TAG,
        "slot    = %s",
        ws_ota_current_slot()
    );

    ESP_LOGI(
        TAG,
        "========================================"
    );
}


static void publish_status(
    esp_mqtt_client_handle_t mqtt_client,
    const char *status_topic,
    const char *state,
    const char *target_version,
    const char *detail
)
{
    if (
        mqtt_client == NULL ||
        status_topic == NULL
    )
    {
        return;
    }


    char payload[512];


    snprintf(
        payload,
        sizeof(payload),

        "{"
            "\"device_id\":\"%s\","
            "\"state\":\"%s\","
            "\"firmware_version\":\"%s\","
            "\"ota_slot\":\"%s\","
            "\"target_version\":\"%s\","
            "\"detail\":\"%s\""
        "}",

        CONFIG_WS_DEVICE_ID,
        state != NULL ? state : "OTA_UNKNOWN",
        ws_ota_current_version(),
        ws_ota_current_slot(),
        target_version != NULL ? target_version : "",
        detail != NULL ? detail : ""
    );


    esp_mqtt_client_publish(
        mqtt_client,
        status_topic,
        payload,
        0,
        1,
        0
    );
}


void ws_ota_publish_boot_status(
    esp_mqtt_client_handle_t mqtt_client,
    const char *status_topic
)
{
    publish_status(
        mqtt_client,
        status_topic,
        "ONLINE",
        ws_ota_current_version(),
        "boot_ok"
    );
}


static bool safe_token(
    const char *value,
    size_t max_len
)
{
    if (
        value == NULL ||
        value[0] == '\0'
    )
    {
        return false;
    }


    size_t len =
        strlen(value);


    if (
        len == 0 ||
        len >= max_len
    )
    {
        return false;
    }


    for (
        size_t i = 0;
        i < len;
        i++
    )
    {
        unsigned char ch =
            (unsigned char)value[i];


        if (
            isalnum(ch) ||
            ch == '.' ||
            ch == '_' ||
            ch == '-'
        )
        {
            continue;
        }


        return false;
    }


    return true;
}


static void ota_task(
    void *arg
)
{
    ota_task_context_t *ctx =
        (ota_task_context_t *)arg;


    esp_https_ota_handle_t ota_handle =
        NULL;


    bool ota_started = false;


    char failure_detail[96] =
        "unknown";


    esp_netif_ip_info_t ip_info =
        {0};


    esp_err_t err =
        esp_netif_get_ip_info(
            ctx->sta_netif,
            &ip_info
        );


    if (
        err != ESP_OK ||
        ip_info.gw.addr == 0
    )
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "gateway_unavailable"
        );

        goto fail;
    }


    char url[256];


    int written =
        snprintf(
            url,
            sizeof(url),
            "http://" IPSTR ":%d/%s",
            IP2STR(&ip_info.gw),
            CONFIG_WS_OTA_PORT,
            ctx->filename
        );


    if (
        written < 0 ||
        written >= (int)sizeof(url)
    )
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "url_too_long"
        );

        goto fail;
    }


    ESP_LOGI(
        TAG,
        "========================================"
    );

    ESP_LOGI(
        TAG,
        "OTA solicitada"
    );

    ESP_LOGI(
        TAG,
        "actual  = %s",
        ws_ota_current_version()
    );

    ESP_LOGI(
        TAG,
        "destino = %s",
        ctx->target_version
    );

    ESP_LOGI(
        TAG,
        "URL     = %s",
        url
    );

    ESP_LOGI(
        TAG,
        "========================================"
    );


    publish_status(
        ctx->mqtt_client,
        ctx->status_topic,
        "OTA_DOWNLOADING",
        ctx->target_version,
        "connecting"
    );


    esp_http_client_config_t http_config =
    {
        .url = url,
        .timeout_ms = 20000,
        .keep_alive_enable = true,
    };


    esp_https_ota_config_t ota_config =
    {
        .http_config =
            &http_config,
    };


    err =
        esp_https_ota_begin(
            &ota_config,
            &ota_handle
        );


    if (err != ESP_OK)
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "ota_begin:%s",
            esp_err_to_name(err)
        );

        goto fail;
    }


    ota_started = true;


    esp_app_desc_t new_app_info =
        {0};


    err =
        esp_https_ota_get_img_desc(
            ota_handle,
            &new_app_info
        );


    if (err != ESP_OK)
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "image_desc:%s",
            esp_err_to_name(err)
        );

        goto fail;
    }


    ESP_LOGI(
        TAG,
        "Firmware remoto version=%s",
        new_app_info.version
    );


    if (
        strcmp(
            new_app_info.version,
            ctx->target_version
        ) != 0
    )
    {
        ESP_LOGE(
            TAG,
            "Version OTA no coincide: solicitada=%s bin=%s",
            ctx->target_version,
            new_app_info.version
        );


        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "version_mismatch"
        );

        goto fail;
    }


    do
    {
        err =
            esp_https_ota_perform(
                ota_handle
            );

    } while (
        err ==
        ESP_ERR_HTTPS_OTA_IN_PROGRESS
    );


    if (err != ESP_OK)
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "ota_perform:%s",
            esp_err_to_name(err)
        );

        goto fail;
    }


    if (
        !esp_https_ota_is_complete_data_received(
            ota_handle
        )
    )
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "incomplete_image"
        );

        goto fail;
    }


    err =
        esp_https_ota_finish(
            ota_handle
        );


    ota_started = false;
    ota_handle = NULL;


    if (err != ESP_OK)
    {
        snprintf(
            failure_detail,
            sizeof(failure_detail),
            "ota_finish:%s",
            esp_err_to_name(err)
        );

        goto fail;
    }


    ESP_LOGI(
        TAG,
        "OTA finalizada correctamente"
    );


    publish_status(
        ctx->mqtt_client,
        ctx->status_topic,
        "OTA_SUCCESS",
        ctx->target_version,
        "rebooting"
    );


    vTaskDelay(
        pdMS_TO_TICKS(1500)
    );


    esp_restart();


fail:

    if (
        ota_started &&
        ota_handle != NULL
    )
    {
        esp_https_ota_abort(
            ota_handle
        );
    }


    ESP_LOGE(
        TAG,
        "OTA fallida: %s",
        failure_detail
    );


    publish_status(
        ctx->mqtt_client,
        ctx->status_topic,
        "OTA_FAILED",
        ctx->target_version,
        failure_detail
    );


    s_ota_in_progress =
        false;


    free(
        ctx
    );


    vTaskDelete(
        NULL
    );
}


bool ws_ota_handle_command(
    const char *payload,
    esp_netif_t *sta_netif,
    esp_mqtt_client_handle_t mqtt_client,
    const char *status_topic,
    bool capture_busy
)
{
    if (
        payload == NULL ||
        sta_netif == NULL ||
        mqtt_client == NULL ||
        status_topic == NULL
    )
    {
        return false;
    }


    cJSON *root =
        cJSON_Parse(
            payload
        );


    if (root == NULL)
    {
        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            "",
            "invalid_json"
        );

        return false;
    }


    cJSON *version =
        cJSON_GetObjectItemCaseSensitive(
            root,
            "version"
        );


    cJSON *filename =
        cJSON_GetObjectItemCaseSensitive(
            root,
            "filename"
        );


    if (
        !cJSON_IsString(version) ||
        !cJSON_IsString(filename) ||
        version->valuestring == NULL ||
        filename->valuestring == NULL
    )
    {
        cJSON_Delete(
            root
        );


        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            "",
            "missing_fields"
        );

        return false;
    }


    if (
        !safe_token(
            version->valuestring,
            32
        ) ||
        !safe_token(
            filename->valuestring,
            128
        )
    )
    {
        cJSON_Delete(
            root
        );


        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            "",
            "invalid_value"
        );

        return false;
    }


    if (capture_busy)
    {
        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            version->valuestring,
            "capture_busy"
        );


        cJSON_Delete(
            root
        );

        return false;
    }


    if (s_ota_in_progress)
    {
        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            version->valuestring,
            "ota_in_progress"
        );


        cJSON_Delete(
            root
        );

        return false;
    }


    if (
        strcmp(
            version->valuestring,
            ws_ota_current_version()
        ) == 0
    )
    {
        publish_status(
            mqtt_client,
            status_topic,
            "OTA_REJECTED",
            version->valuestring,
            "already_current"
        );


        cJSON_Delete(
            root
        );

        return false;
    }


    ota_task_context_t *ctx =
        calloc(
            1,
            sizeof(
                ota_task_context_t
            )
        );


    if (ctx == NULL)
    {
        cJSON_Delete(
            root
        );

        return false;
    }


    ctx->sta_netif =
        sta_netif;


    ctx->mqtt_client =
        mqtt_client;


    strlcpy(
        ctx->status_topic,
        status_topic,
        sizeof(
            ctx->status_topic
        )
    );


    strlcpy(
        ctx->target_version,
        version->valuestring,
        sizeof(
            ctx->target_version
        )
    );


    strlcpy(
        ctx->filename,
        filename->valuestring,
        sizeof(
            ctx->filename
        )
    );


    cJSON_Delete(
        root
    );


    s_ota_in_progress =
        true;


    publish_status(
        mqtt_client,
        status_topic,
        "OTA_STARTING",
        ctx->target_version,
        "accepted"
    );


    BaseType_t result =
        xTaskCreate(
            ota_task,
            "ota_task",
            8192,
            ctx,
            4,
            NULL
        );


    if (result != pdPASS)
    {
        s_ota_in_progress =
            false;


        publish_status(
            mqtt_client,
            status_topic,
            "OTA_FAILED",
            ctx->target_version,
            "task_create_failed"
        );


        free(
            ctx
        );


        return false;
    }


    return true;
}
