#include "stadia_xinput.h"

enum {
    STADIA_INPUT_REPORT = 3,
    STADIA_RUMBLE_REPORT = 5,
    STADIA_GUIDE_MASK = 0x10,
    STADIA_X_MASK = 0x10,
    STADIA_HAT_UP = 0,
    STADIA_HAT_DOWN = 4,
    STADIA_HAT_NEUTRAL = 8,
};

// In high-speed USB the interval is exponent-encoded in microframes.
const StadiaReportRate stadia_report_rates[STADIA_RATE_COUNT] = {
    {4, 6, 4, 38},  // 250 Hz
    {2, 5, 2, 82},  // 500 Hz
    {1, 4, 1, 145}, // 1000 Hz
};

static void encode_axis(uint8_t *output, uint8_t raw, bool invert)
{
    // The original descriptor uses the range 1 to 255, centered on 128.
    int offset = (raw ? raw : 1) - 128;
    if (invert) offset = -offset;
    int scale = offset < 0 ? 32768 : 32767;
    uint16_t word = (uint16_t)(offset * scale / 127);
    output[0] = (uint8_t)word;
    output[1] = (uint8_t)(word >> 8);
}

static void encode_report(const uint8_t *hid, uint8_t *xusb)
{
    enum { UP = 1, DOWN = 2, LEFT = 4, RIGHT = 8 };
    static const uint8_t dpad[8] = {
        UP, UP | RIGHT, RIGHT, DOWN | RIGHT,
        DOWN, DOWN | LEFT, LEFT, UP | LEFT,
    };
    uint8_t hat = hid[1] & 0x0f;
    uint8_t system = hid[2];
    uint8_t buttons = hid[3];

    xusb[0] = 0;
    xusb[1] = STADIA_XUSB_INPUT_SIZE;
    xusb[2] = (hat < sizeof dpad ? dpad[hat] : 0)
              | ((system & 0x60) >> 1)   // Menu and View
              | ((buttons & 0x01) << 6)  // Left stick click
              | (system & 0x80);        // Right stick click
    xusb[3] = ((buttons & 0x04) >> 2)    // Left bumper
              | (buttons & 0x02)         // Right bumper
              | ((system & 0x10) >> 2)  // Guide
              | ((buttons & 0x40) >> 2) // A
              | (buttons & 0x20)        // B
              | ((buttons & 0x10) << 2) // X
              | ((buttons & 0x08) << 4); // Y
    xusb[4] = hid[8];
    xusb[5] = hid[9];
    encode_axis(xusb + 6, hid[4], false);
    encode_axis(xusb + 8, hid[5], true);
    encode_axis(xusb + 10, hid[6], false);
    encode_axis(xusb + 12, hid[7], true);
    for (size_t i = 14; i < STADIA_XUSB_INPUT_SIZE; ++i) xusb[i] = 0;
}

bool stadia_xusb_encode_input(const uint8_t *hid, size_t size,
                              uint8_t xusb[STADIA_XUSB_INPUT_SIZE])
{
    if (size < STADIA_HID_INPUT_MIN_SIZE || hid[0] != STADIA_INPUT_REPORT) return false;
    encode_report(hid, xusb);
    return true;
}

StadiaUsbAction stadia_xinput_step(StadiaXInputMode *mode, uint8_t *hid,
                                  size_t size, uint8_t xusb[STADIA_XUSB_INPUT_SIZE])
{
    if (size < STADIA_HID_INPUT_MIN_SIZE || hid[0] != STADIA_INPUT_REPORT) {
        return STADIA_SEND_HID;
    }

    bool stadia = (hid[2] & STADIA_GUIDE_MASK) != 0;
    bool x = (hid[3] & STADIA_X_MASK) != 0;
    uint8_t hat = hid[1] & 0x0f;
    bool rate_direction = hat == STADIA_HAT_UP || hat == STADIA_HAT_DOWN;
    bool rate_chord = mode->enabled && stadia && rate_direction;
    bool switch_mode = stadia && x && !mode->chord_latched;
    bool request_rate = rate_chord && !x && !mode->chord_latched && !mode->rate_chord_latched;
    mode->chord_latched = switch_mode || (mode->chord_latched && (stadia || x));
    mode->rate_chord_latched = rate_chord && (request_rate || mode->rate_chord_latched);
    mode->guide_latched = stadia && (mode->guide_latched || mode->rate_chord_latched);
    if (mode->chord_latched) {
        hid[2] &= (uint8_t)~STADIA_GUIDE_MASK;
        hid[3] &= (uint8_t)~STADIA_X_MASK;
    }
    if (mode->guide_latched) hid[2] &= (uint8_t)~STADIA_GUIDE_MASK;
    if (mode->rate_chord_latched) hid[1] = (hid[1] & 0xf0) | STADIA_HAT_NEUTRAL;
    if (switch_mode) {
        mode->enabled = !mode->enabled;
        return STADIA_REENUMERATE_USB;
    }
    if (!mode->enabled) return STADIA_SEND_HID;

    encode_report(hid, xusb);
    if (!request_rate) return STADIA_SEND_XUSB;

    uint8_t rate = mode->rate_index;
    if (hat == STADIA_HAT_UP && rate < STADIA_RATE_COUNT - 1) ++rate;
    if (hat == STADIA_HAT_DOWN && rate > 0) --rate;
    if (rate == mode->rate_index) return STADIA_SEND_XUSB;
    mode->rate_index = rate;
    return STADIA_CHANGE_RATE;
}

bool stadia_xinput_to_hid_rumble(const uint8_t *xusb, size_t size,
                                uint8_t hid[STADIA_HID_RUMBLE_SIZE])
{
    if (size != 8 || xusb[0] != 0 || xusb[1] != 8) return false;

    hid[0] = STADIA_RUMBLE_REPORT;
    // Repeating each byte multiplies the amplitude by 257.
    hid[1] = hid[2] = xusb[3];
    hid[3] = hid[4] = xusb[4];
    return true;
}
