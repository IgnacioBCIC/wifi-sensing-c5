#pragma once

#include <stdbool.h>
#include "esp_err.h"

typedef struct {
    bool valid;
    int adc_mv;
    int battery_mv;
    int percent_estimate;
} battery_status_t;

esp_err_t battery_monitor_init(void);
esp_err_t battery_monitor_read(battery_status_t *out_status);
