#include "stadia_xinput.h"
#include "series_report_map.h"
#include <stdatomic.h>

enum {
    USB_DEVICE_DESCRIPTOR = 0x0b,
    USB_CONFIGURATION_DESCRIPTOR = 0x0c,
    USB_HID_REPORT_DESCRIPTOR = 0x0f,
    USB_BOS_DESCRIPTOR = 0x11,
    USB_VENDOR_DESCRIPTOR = 0x13,
    USB_INVALID_REQUEST = 1,
    USB_BUSY = 2,
    USB_INVALID_HANDLE = 3,
    USB_GAMEPAD_ENDPOINT = 3,
    USB_CONFIG_SIZE = 49,
    USB_IN_INTERVAL = 41,
    USB_OUT_INTERVAL = 48,
    USB_SPEED_STATUS = 2,
    USB_HIGH_SPEED = 2,
    USB_DEVICE_ROLE = 2,
    RATE_FEEDBACK_MS = 90,
    SERIES_INPUT_ID = 1,
    SERIES_INPUT_SIZE = 16,
    SERIES_OUTPUT_SIZE = 8,
};

typedef struct {
    const uint8_t *data;
    uint32_t size;
} ByteSpan;

typedef const uint8_t *(*ReportGetter)(const void *, uint32_t *);

typedef struct {
    const ReportGetter *methods;
    uint8_t bytes[1 + SERIES_INPUT_SIZE];
} BlePacket;

typedef struct {
    _Alignas(32) uint8_t usb[32];
    BlePacket ble;
    uint8_t usb_config[USB_CONFIG_SIZE];
    volatile uint8_t rate;
    bool rate_chord;
    bool suppress_guide;
} ReportBuffers;

extern _Atomic(ReportBuffers *) report_buffers;

static uint8_t *const input_task = (uint8_t *)0x200108a8;
static uint8_t *const usb_role = (uint8_t *)0x2000a2a8;
static const char rate_key[] = "XusbRate";
// Single 90 ms pulse on both motors when the rate changes. These amplitudes are
// deliberately higher than the stadia_report_rates[] values the hotkey variant
// uses (38 / 82 / 145); the weaker steps were hard to feel through the shell.
static const uint8_t rate_feedback[STADIA_RATE_COUNT] = {96, 176, 255};  // ~38% / 69% / 100%
static void *(*const allocate)(size_t) = (void *(*)(size_t))0x00007f59;
static void (*const release)(void *) = (void (*)(void *))0x000080d9;
static void (*const copy_bytes)(void *, const void *, size_t) =
    (void (*)(void *, const void *, size_t))0x0000b573;
static uint32_t (*const init_usb)(uint32_t, void *, void **) =
    (uint32_t (*)(uint32_t, void *, void **))0x600550e5;
static void (*const stop_usb)(void *) = (void (*)(void *))0x600d1067;
static void (*const start_usb)(void *) = (void (*)(void *))0x600d105f;
static uint32_t (*const usb_status)(void *, uint32_t, uint8_t *) =
    (uint32_t (*)(void *, uint32_t, uint8_t *))0x600d1179;
static uint32_t (*const set_poll_period)(void *, uint32_t) =
    (uint32_t (*)(void *, uint32_t))0x60076439;
static void (*const delay_ticks)(uint32_t) = (void (*)(uint32_t))0x0000737d;
static void **(*const get_stores)(void) = (void **(*)(void))0x60065d81;
static uint32_t (*const read_setting)(void *, const char *, void *, uint32_t, uint32_t) =
    (uint32_t (*)(void *, const char *, void *, uint32_t, uint32_t))0x600cb599;
static uint32_t (*const write_setting)(void *, const char *, const void *, uint32_t) =
    (uint32_t (*)(void *, const char *, const void *, uint32_t))0x60101199;
static uint32_t (*const usb_send)(void *, uint32_t, const uint8_t *, uint32_t) =
    (uint32_t (*)(void *, uint32_t, const uint8_t *, uint32_t))0x600d0e17;
static uint32_t (*const usb_device_event)(void *, uint32_t, void *) =
    (uint32_t (*)(void *, uint32_t, void *))0x60060d55;
static void (*const usb_rumble)(const uint8_t *, uint32_t) =
    (void (*)(const uint8_t *, uint32_t))0x60058b65;
static uint32_t (*const ble_send)(void *, const void *) =
    (uint32_t (*)(void *, const void *))0x60081551;
static void (*const rumble_payload)(const uint8_t *, uint32_t) =
    (void (*)(const uint8_t *, uint32_t))0x60058aa9;
static uint32_t (*const set_gatt_value)(void *, const ByteSpan *) =
    (uint32_t (*)(void *, const ByteSpan *))0x600df4c5;

static const uint8_t usb_device[] = {
    18, 1, 0x00, 0x02, 0xff, 0xff, 0xff, 64,
    0x5e, 0x04, 0x8e, 0x02, 0x14, 0x01, 1, 2, 3, 1,
};

static const uint8_t usb_configuration[] = {
    9, 2, 49, 0, 1, 1, 0, 0x80, 250,
    9, 4, 0, 0, 2, 0xff, 0x5d, 1, 0,
    17, 0x21, 0, 1, 1, 0x25, 0x83, STADIA_XUSB_INPUT_SIZE,
    0, 0, 0, 0, 0x13, 0x03, 8, 0, 0,
    7, 5, 0x83, 3, 32, 0, 6,
    7, 5, 0x03, 3, 32, 0, 6,
};

_Static_assert(sizeof usb_configuration == 49, "XUSB descriptor length");
_Static_assert(sizeof series_report_map == 283, "Series report map length");

static ReportBuffers *input_buffers(void)
{
    ReportBuffers *buffers = atomic_load(&report_buffers);
    if (buffers) return buffers;

    void *raw = allocate(sizeof *buffers + 31);
    if (!raw) return NULL;
    buffers = (ReportBuffers *)(((uintptr_t)raw + 31) & ~(uintptr_t)31);
    copy_bytes(buffers->usb_config, usb_configuration, sizeof usb_configuration);
    buffers->rate = 0;
    buffers->rate_chord = false;
    buffers->suppress_guide = false;

    // USB attachment and the BLE input task may allocate concurrently.
    ReportBuffers *existing = NULL;
    if (atomic_compare_exchange_strong(&report_buffers, &existing, buffers)) return buffers;
    release(raw);
    return existing;
}

uint32_t bruce_direct_usb_init(uint32_t controller, void *config, void **handle)
{
    ReportBuffers *buffers = input_buffers();
    if (!buffers) return USB_BUSY;

    uint8_t saved_rate;
    void *store = get_stores()[0];
    if (store && read_setting(store, rate_key, &saved_rate, sizeof saved_rate, 0) == 0 &&
        saved_rate < STADIA_RATE_COUNT) {
        buffers->rate = saved_rate;
    }
    return init_usb(controller, config, handle);
}

static uint32_t apply_usb_period(void *task, uint8_t rate)
{
    uint32_t period = stadia_report_rates[rate].period_ms;
    uint32_t status = set_poll_period(task, period);
    if (status == 0) *(uint32_t *)((uint8_t *)task + 0x6c) = period - 1;
    return status;
}

uint32_t bruce_direct_poll_period(uint8_t *task, uint32_t period)
{
    const uint8_t *target = *(const uint8_t **)(task + 0x240);
    ReportBuffers *buffers = atomic_load(&report_buffers);
    if (buffers && target == (const uint8_t *)0x20003064 && *(const uint32_t *)(target + 8) == 0) {
        return apply_usb_period(task, buffers->rate);
    }
    return set_poll_period(task, period);
}

static void *active_usb_handle(void)
{
    if (*(volatile uint32_t *)(usb_role + 0x90) != USB_DEVICE_ROLE) return NULL;
    return *(void *volatile *)(usb_role + 0x98);
}

static bool change_usb_rate(ReportBuffers *buffers, uint8_t rate)
{
    void *controller = active_usb_handle();
    void *store = get_stores()[0];
    if (!controller || !store || apply_usb_period(input_task, rate) != 0) return false;
    if (write_setting(store, rate_key, &rate, sizeof rate) != 0) {
        apply_usb_period(input_task, buffers->rate);
        return false;
    }

    bool connected = active_usb_handle() == controller;
    if (connected) stop_usb(controller);
    buffers->rate = rate;
    if (!connected) return true;
    // Windows reads the new interrupt interval after reconnecting.
    uint32_t rumble = rate_feedback[rate] * 0x01010101u;
    rumble_payload((const uint8_t *)&rumble, sizeof rumble);
    delay_ticks(RATE_FEEDBACK_MS);
    rumble = 0;
    rumble_payload((const uint8_t *)&rumble, sizeof rumble);
    if (active_usb_handle() == controller) start_usb(controller);
    return true;
}

uint32_t bruce_direct_usb_event(void *handle, uint32_t event, void *param)
{
    if (event == USB_DEVICE_DESCRIPTOR) {
        if (!param) return USB_INVALID_REQUEST;
        *(ByteSpan *)param = (ByteSpan){usb_device, sizeof usb_device};
        return 0;
    }
    if (event == USB_CONFIGURATION_DESCRIPTOR) {
        ReportBuffers *buffers = atomic_load(&report_buffers);
        if (!param || !buffers) return USB_INVALID_REQUEST;
        uint8_t speed;
        uint32_t status = usb_status(handle, USB_SPEED_STATUS, &speed);
        if (status != 0) return status;
        const StadiaReportRate *rate = &stadia_report_rates[buffers->rate];
        buffers->usb_config[USB_IN_INTERVAL] = speed == USB_HIGH_SPEED ? rate->high_speed_interval
                                                                     : rate->full_speed_interval;
        buffers->usb_config[USB_OUT_INTERVAL] = speed == USB_HIGH_SPEED ? 6 : 4;
        *(ByteSpan *)param = (ByteSpan){buffers->usb_config, sizeof buffers->usb_config};
        return 0;
    }
    if (event == USB_HID_REPORT_DESCRIPTOR || event == USB_BOS_DESCRIPTOR || event == USB_VENDOR_DESCRIPTOR) {
        return USB_INVALID_REQUEST;
    }
    return usb_device_event(handle, event, param);
}

uint32_t bruce_direct_usb_send(void *handle, uint32_t endpoint, const uint8_t *data, uint32_t size)
{
    if (endpoint != USB_GAMEPAD_ENDPOINT) return usb_send(handle, endpoint, data, size);
    if (!handle) return USB_INVALID_HANDLE;
    if (!data || size < STADIA_HID_INPUT_MIN_SIZE || data[0] != 3) return USB_INVALID_REQUEST;
    ReportBuffers *buffers = atomic_load(&report_buffers);
    if (!buffers) return USB_BUSY;

    uint8_t hat = data[1] & 0x0f;
    bool guide = (data[2] & 0x10) != 0;
    bool chord = guide && (hat == 0 || hat == 4);
    bool pressed = chord && !buffers->rate_chord;
    buffers->rate_chord = chord;
    buffers->suppress_guide = guide && (chord || buffers->suppress_guide);
    if (pressed) {
        uint8_t rate = buffers->rate;
        if (hat == 0 && rate < STADIA_RATE_COUNT - 1 && change_usb_rate(buffers, rate + 1)) return USB_BUSY;
        if (hat == 4 && rate > 0 && change_usb_rate(buffers, rate - 1)) return USB_BUSY;
    }
    if (((const volatile uint8_t *)handle)[0x11]) return USB_BUSY;
    stadia_xusb_encode_input(data, size, buffers->usb);
    if (buffers->suppress_guide) buffers->usb[3] &= (uint8_t)~4;
    if (chord) buffers->usb[2] &= (uint8_t)~3;
    return usb_send(handle, endpoint, buffers->usb, STADIA_XUSB_INPUT_SIZE);
}

void bruce_direct_usb_receive(const uint8_t *data, uint32_t size)
{
    uint8_t report[STADIA_HID_RUMBLE_SIZE];
    if (data && stadia_xinput_to_hid_rumble(data, size, report)) usb_rumble(report, sizeof report);
}

static void write_word(uint8_t *dest, uint16_t value)
{
    dest[0] = (uint8_t)value;
    dest[1] = (uint8_t)(value >> 8);
}

static const uint8_t *packet_bytes(const void *object, uint32_t *len)
{
    *len = sizeof ((const BlePacket *)object)->bytes;
    return ((const BlePacket *)object)->bytes;
}

static const ReportGetter packet_methods[] = {packet_bytes};

uint32_t bruce_direct_ble_send(void *context, const void *report)
{
    uint32_t size;
    const ReportGetter *methods = *(const ReportGetter *const *)report;
    const uint8_t *hid = methods[0](report, &size);
    if (!hid || size < STADIA_HID_INPUT_MIN_SIZE || hid[0] != 3) return 0x0d;

    ReportBuffers *buffers = input_buffers();
    if (!buffers) return 0x08;
    BlePacket *packet = &buffers->ble;
    packet->methods = packet_methods;
    packet->bytes[0] = SERIES_INPUT_ID;
    uint8_t *data = packet->bytes + 1;
    for (size_t i = 0; i < 4; ++i) {
        // Bluetooth Series uses unsigned axes with Y increasing downwards.
        int axis = (hid[4 + i] ? hid[4 + i] : 1) - 128;
        write_word(data + 2 * i, (uint16_t)(32768 + axis * 258));
    }
    write_word(data + 8, (uint16_t)(4 * hid[8] + (hid[8] >> 6)));
    write_word(data + 10, (uint16_t)(4 * hid[9] + (hid[9] >> 6)));

    uint8_t hat = hid[1] & 0x0f;
    data[12] = hat < 8 ? hat + 1 : 0;
    const uint8_t sys = hid[2];
    const uint8_t keys = hid[3];
    uint16_t buttons = ((keys & 0x40) >> 6) | ((keys & 0x20) >> 4)  // A, B
                     | ((keys & 0x10) >> 1) | ((keys & 0x08) << 1)  // X, Y
                     | ((keys & 0x04) << 4) | ((keys & 0x02) << 6)  // LB, RB
                     | ((sys & 0x40) << 4) | ((sys & 0x20) << 6)    // View, Menu
                     | ((sys & 0x10) << 8) | ((keys & 1) << 13)     // Guide, LS
                     | ((sys & 0x80) << 7);                        // RS
    write_word(data + 13, buttons);
    data[15] = sys & 1; // Capture becomes Share in the Series report.
    return ble_send(context, packet);
}

uint32_t bruce_direct_ble_receive(void *context, const ByteSpan *report)
{
    (void)context;
    if (!report || report->size != SERIES_OUTPUT_SIZE || !report->data) return 0;

    const uint8_t *data = report->data;
    uint8_t output[4];
    for (size_t i = 0; i < 2; ++i) {
        uint8_t magnitude = data[3 + i];
        if (!(data[0] & (2 >> i))) magnitude = 0;
        if (magnitude > 100) magnitude = 100;
        write_word(output + 2 * i, (uint16_t)(magnitude * 65535u / 100));
    }
    rumble_payload(output, sizeof output);
    return 0;
}

void bruce_direct_ble_report_map(void)
{
    const ByteSpan map = {series_report_map, sizeof series_report_map};
    set_gatt_value((void *)0x200038f8, &map);
}
