#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static wchar_t tap_path[MAX_PATH];
static wchar_t aime_path[MAX_PATH];
static wchar_t felica_path[MAX_PATH];
static UINT scan_key;
static DWORD hold_ms;

static uint8_t tap_id[10];
static size_t tap_len;
static DWORD tap_start;

static uint8_t card_id[10];
static size_t card_len;

static int hexval(char c)
{
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

static size_t read_id(const wchar_t *path, uint8_t *out)
{
    char buf[64];
    char hex[20];
    size_t len, n = 0;
    FILE *f;

    f = _wfopen(path, L"rb");
    if (!f)
        return 0;

    len = fread(buf, 1, sizeof(buf), f);
    fclose(f);

    for (size_t i = 0; i < len; i++) {
        if (hexval(buf[i]) < 0)
            continue;
        if (n == sizeof(hex))
            return 0;
        hex[n++] = buf[i];
    }

    if (n != 16 && n != 20)
        return 0;

    for (size_t i = 0; i < n / 2; i++)
        out[i] = (uint8_t)(hexval(hex[i * 2]) << 4 | hexval(hex[i * 2 + 1]));

    return n / 2;
}

__declspec(dllexport) uint16_t aime_io_get_api_version(void)
{
    return 0x0100;
}

__declspec(dllexport) HRESULT aime_io_init(void)
{
    const wchar_t *ini = L".\\segatools.ini";

    GetPrivateProfileStringW(L"aimeio", L"tapPath", L"DEVICE\\tap.txt", tap_path, MAX_PATH, ini);
    GetPrivateProfileStringW(L"aime", L"aimePath", L"DEVICE\\aime.txt", aime_path, MAX_PATH, ini);
    GetPrivateProfileStringW(L"aime", L"felicaPath", L"DEVICE\\felica.txt", felica_path, MAX_PATH, ini);
    hold_ms = GetPrivateProfileIntW(L"aimeio", L"tapHoldMs", 3000, ini);
    scan_key = GetPrivateProfileIntW(L"aime", L"scan", VK_RETURN, ini);

    DeleteFileW(tap_path);
    OutputDebugStringA("aimeio_tap: ready\n");

    return S_OK;
}

__declspec(dllexport) HRESULT aime_io_nfc_poll(uint8_t unit_no)
{
    uint8_t id[10];
    size_t len;

    if (unit_no != 0)
        return S_OK;

    card_len = 0;

    if (GetFileAttributesW(tap_path) != INVALID_FILE_ATTRIBUTES) {
        len = read_id(tap_path, id);
        DeleteFileW(tap_path);

        if (len) {
            memcpy(tap_id, id, len);
            tap_len = len;
            tap_start = GetTickCount();
            OutputDebugStringA("aimeio_tap: card tapped\n");
        }
    }

    if (tap_len) {
        if (GetTickCount() - tap_start < hold_ms) {
            memcpy(card_id, tap_id, tap_len);
            card_len = tap_len;
            return S_OK;
        }
        tap_len = 0;
    }

    if (!(GetAsyncKeyState(scan_key) & 0x8000))
        return S_OK;

    len = read_id(aime_path, id);
    if (len != 10) {
        len = read_id(felica_path, id);
        if (len != 8)
            return S_OK;
    }

    memcpy(card_id, id, len);
    card_len = len;

    return S_OK;
}

__declspec(dllexport) HRESULT aime_io_nfc_get_aime_id(uint8_t unit_no, uint8_t *luid, size_t luid_size)
{
    if (unit_no != 0 || card_len != 10 || luid_size < 10)
        return S_FALSE;

    memcpy(luid, card_id, 10);

    return S_OK;
}

__declspec(dllexport) HRESULT aime_io_nfc_get_felica_id(uint8_t unit_no, uint64_t *IDm)
{
    uint64_t v = 0;

    if (unit_no != 0 || card_len != 8)
        return S_FALSE;

    for (int i = 0; i < 8; i++)
        v = v << 8 | card_id[i];

    *IDm = v;

    return S_OK;
}

__declspec(dllexport) void aime_io_led_set_color(uint8_t unit_no, uint8_t r, uint8_t g, uint8_t b)
{
}
