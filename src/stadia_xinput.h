#ifndef STADIA_XINPUT_H
#define STADIA_XINPUT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum {
    STADIA_HID_INPUT_MIN_SIZE = 10,
    STADIA_HID_RUMBLE_SIZE = 5,
    STADIA_XUSB_INPUT_SIZE = 20,
    STADIA_RATE_COUNT = 3,
};

typedef struct {
    uint8_t period_ms;
    uint8_t high_speed_interval;
    uint8_t full_speed_interval;
    uint8_t feedback_intensity;
} StadiaReportRate;

extern const StadiaReportRate stadia_report_rates[STADIA_RATE_COUNT];

bool stadia_xusb_encode_input(const uint8_t *hid, size_t size,
                              uint8_t xusb[STADIA_XUSB_INPUT_SIZE]);

typedef struct {
    volatile bool enabled;
    bool chord_latched;
    bool rate_chord_latched;
    bool guide_latched;
    volatile uint8_t rate_index;
} StadiaXInputMode;

typedef enum {
    STADIA_SEND_HID,
    STADIA_SEND_XUSB,
    STADIA_REENUMERATE_USB,
    STADIA_CHANGE_RATE,
} StadiaUsbAction;

// Keep the Stadia HID and XUSB buffers separate. The HID buffer is writable so
// hotkey buttons can be removed from reports until the chord is released.
StadiaUsbAction stadia_xinput_step(StadiaXInputMode *mode, uint8_t *hid,
                                  size_t size, uint8_t xusb[STADIA_XUSB_INPUT_SIZE]);

bool stadia_xinput_to_hid_rumble(const uint8_t *xusb, size_t size,
                                uint8_t hid[STADIA_HID_RUMBLE_SIZE]);

#endif
