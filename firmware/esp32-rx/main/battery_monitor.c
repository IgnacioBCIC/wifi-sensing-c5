#include "battery_monitor.h"

#include <stddef.h>
#include <stdint.h>

#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"

#define BATTERY_ADC_UNIT            ADC_UNIT_1
#define BATTERY_ADC_CHANNEL         ADC_CHANNEL_5
#define BATTERY_ADC_ATTEN           ADC_ATTEN_DB_6
#define BATTERY_ADC_SAMPLES         32
#define BATTERY_DIVIDER_MULTIPLIER  3
#define BATTERY_VALID_MIN_MV        2500
#define BATTERY_VALID_MAX_MV        4500

static const char *TAG = "WS_BAT";

static adc_oneshot_unit_handle_t s_adc_handle = NULL;
static adc_cali_handle_t s_cali_handle = NULL;
static bool s_initialized = false;

typedef struct {
    int mv;
    int percent;
} battery_curve_point_t;


static const battery_curve_point_t s_curve[] = {
    {4200, 100},
    {4150,  95},
    {4100,  90},
    {4050,  85},
    {4000,  80},
    {3950,  75},
    {3900,  70},
    {3850,  60},
    {3800,  50},
    {3750,  40},
    {3700,  30},
    {3650,  20},
    {3550,  10},
    {3450,   5},
    {3300,   0},
};

static int estimate_percent(int battery_mv)
{
    const size_t count = sizeof(s_curve) / sizeof(s_curve[0]);

    if (battery_mv >= s_curve[0].mv) {
        return 100;
    }

    if (battery_mv <= s_curve[count - 1].mv) {
        return 0;
    }

    for (size_t i = 0; i + 1 < count; ++i) {
        const battery_curve_point_t hi = s_curve[i];
        const battery_curve_point_t lo = s_curve[i + 1];

        if (battery_mv <= hi.mv && battery_mv >= lo.mv) {
            const int span_mv = hi.mv - lo.mv;
            const int span_pct = hi.percent - lo.percent;
            const int above_lo_mv = battery_mv - lo.mv;

            return lo.percent +
                   (above_lo_mv * span_pct + span_mv / 2) / span_mv;
        }
    }

    return 0;
}

esp_err_t battery_monitor_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = BATTERY_ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };

    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_adc_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "adc_oneshot_new_unit: %s", esp_err_to_name(err));
        return err;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    err = adc_oneshot_config_channel(
        s_adc_handle,
        BATTERY_ADC_CHANNEL,
        &chan_cfg
    );
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "adc_oneshot_config_channel: %s", esp_err_to_name(err));
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
        return err;
    }

    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = BATTERY_ADC_UNIT,
        .chan = BATTERY_ADC_CHANNEL,
        .atten = BATTERY_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };

    err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali_handle);
    if (err != ESP_OK) {
        ESP_LOGE(
            TAG,
            "ADC curve-fitting calibration no disponible: %s",
            esp_err_to_name(err)
        );
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
        return err;
    }

    s_initialized = true;

    ESP_LOGI(
        TAG,
        "Battery ADC listo: GPIO6/ADC1_CH5, atten=6dB, divider=x3, samples=%d",
        BATTERY_ADC_SAMPLES
    );

    return ESP_OK;
}

esp_err_t battery_monitor_read(battery_status_t *out_status)
{
    if (out_status == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    *out_status = (battery_status_t){0};
    out_status->percent_estimate = -1;

    if (!s_initialized || s_adc_handle == NULL || s_cali_handle == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    int64_t sum_mv = 0;
    int successful = 0;
    esp_err_t last_err = ESP_OK;

    for (int i = 0; i < BATTERY_ADC_SAMPLES; ++i) {
        int sample_mv = 0;
        esp_err_t err = adc_oneshot_get_calibrated_result(
            s_adc_handle,
            s_cali_handle,
            BATTERY_ADC_CHANNEL,
            &sample_mv
        );

        if (err == ESP_OK) {
            sum_mv += sample_mv;
            successful++;
        } else {
            last_err = err;
        }
    }

    if (successful == 0) {
        return last_err == ESP_OK ? ESP_FAIL : last_err;
    }

    out_status->adc_mv = (int)((sum_mv + successful / 2) / successful);
    out_status->battery_mv =
        out_status->adc_mv * BATTERY_DIVIDER_MULTIPLIER;

    out_status->valid =
        out_status->battery_mv >= BATTERY_VALID_MIN_MV &&
        out_status->battery_mv <= BATTERY_VALID_MAX_MV;

    if (out_status->valid) {
        out_status->percent_estimate =
            estimate_percent(out_status->battery_mv);
    }

    return ESP_OK;
}
