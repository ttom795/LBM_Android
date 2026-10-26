/* Input: XInput 1.3 shims backed by SDL game controllers, plus a virtual
 * pad fed by the keyboard (desktop) and the touch overlay (Android). */
#include "rt.h"
#include <SDL2/SDL.h>
#include <math.h>

static SDL_GameController *g_pads[4];
static struct {
    uint16_t buttons;
    uint8_t lt, rt;
    int16_t lx, ly, rx, ry;
} g_virt;
static uint32_t g_packet;
static int g_nintendo;  /* swap A/B and X/Y on physical controllers */

void input_set_nintendo(int on) { g_nintendo = on; g_packet++; }

/* Skip Cutscenes: story cutscenes are levels of their own. While one is
 * playing or starting, pad 0 taps Start, which skips it, and A, which also
 * gets past the "press any key" screen of the level loading next. */
static int g_skip_cutscenes;
void input_set_skip_cutscenes(int on) { g_skip_cutscenes = on; }
/* The level being played: world (0x960894) +0x12C; level +0x64 flags, where
 * 0x20 intro, 0x40 cutscene/midtro and 0x80 outro mark cutscene levels. */
static int in_cutscene_level(void) {
    uint32_t w = M32(0x960894), lv = w >= 0x10000 ? M32(w + 0x12C) : 0;
    if (lv < 0x10000) lv = M32(0xACA89C);  /* none yet: the one being started */
    return lv >= 0x10000 && (M8(lv + 0x64) & 0xE0);
}
static uint16_t skip_tap(void) {
    if (!g_skip_cutscenes || !in_cutscene_level()) return 0;
    uint32_t t = SDL_GetTicks() % 300;  /* Start at 0-60 ms, A at 150-210 ms */
    return t < 60 ? 0x10 : (t >= 150 && t < 210) ? 0x1000 : 0;
}

/* Gyro aiming: while B (Special, which aims Batman's batarangs) is held, the
 * phone's turn rate moves the left stick, so turning or tilting the phone
 * steers the targeting reticle. */
static int g_gyro;
static SDL_Sensor *g_gyro_sensor;
void input_set_gyro(int on) { g_gyro = on; }

static void gyro_stick(int16_t *lx, int16_t *ly) {
    if (!g_gyro) {
        if (g_gyro_sensor) { SDL_SensorClose(g_gyro_sensor); g_gyro_sensor = NULL; }
        return;
    }
    if (!g_gyro_sensor) {
        static int tried;
        if (tried) return;
        tried = 1;
        if (SDL_InitSubSystem(SDL_INIT_SENSOR) != 0) return;
        for (int i = 0; i < SDL_NumSensors(); i++)
            if (SDL_SensorGetDeviceType(i) == SDL_SENSOR_GYRO) { g_gyro_sensor = SDL_SensorOpen(i); break; }
        LOG("gyro: %s", g_gyro_sensor ? "on" : "no gyroscope");
        if (!g_gyro_sensor) return;
        tried = 0;
    }
    float d[3];
    if (SDL_SensorGetData(g_gyro_sensor, d, 3) != 0) return;
    /* rad/s about the phone's portrait axes (x right, y up, z out of the
     * screen). Held in landscape with its right side up, the player's up
     * axis is +x and their right is -y; turning right is negative about up,
     * tilting the top edge towards them (looking up) positive about right. */
    float yaw_right = -d[0], pitch_up = -d[1];
    if (SDL_GetDisplayOrientation(0) == SDL_ORIENTATION_LANDSCAPE_FLIPPED) { yaw_right = -yaw_right; pitch_up = -pitch_up; }
    const float dead = 0.04f, full = 2.0f;  /* rad/s */
    float v[2] = {yaw_right, pitch_up};
    int16_t *out[2] = {lx, ly};
    for (int k = 0; k < 2; k++) {
        float a = fabsf(v[k]);
        if (a < dead) continue;
        /* start past the game's stick dead zone (~8000) */
        float m = 8000.0f + (a - dead) / (full - dead) * (32767.0f - 8000.0f);
        int t = *out[k] + (int)(v[k] < 0 ? -m : m);
        *out[k] = (int16_t)(t > 32767 ? 32767 : t < -32767 ? -32767 : t);
    }
}

static const struct { SDL_Keycode key; uint16_t bit; } k_keymap[] = {
    {SDLK_UP, 0x1}, {SDLK_DOWN, 0x2}, {SDLK_LEFT, 0x4}, {SDLK_RIGHT, 0x8},
    {SDLK_RETURN, 0x10}, {SDLK_ESCAPE, 0x20}, {SDLK_SPACE, 0x1000}, {SDLK_LCTRL, 0x2000},
    {SDLK_LSHIFT, 0x4000}, {SDLK_TAB, 0x8000}, {SDLK_q, 0x100}, {SDLK_e, 0x200},
};

void input_set_virtual(uint16_t buttons, int16_t lx, int16_t ly, int16_t rx, int16_t ry, uint8_t lt, uint8_t rt) {
    g_virt.buttons = buttons;
    g_virt.lx = lx; g_virt.ly = ly; g_virt.rx = rx; g_virt.ry = ry;
    g_virt.lt = lt; g_virt.rt = rt;
    g_packet++;
}

void input_handle_event(const SDL_Event *e) {
    switch (e->type) {
    case SDL_CONTROLLERDEVICEADDED:
        for (int i = 0; i < 4; i++)
            if (!g_pads[i]) { g_pads[i] = SDL_GameControllerOpen(e->cdevice.which); break; }
        break;
    case SDL_CONTROLLERDEVICEREMOVED:
        for (int i = 0; i < 4; i++)
            if (g_pads[i] && SDL_JoystickInstanceID(SDL_GameControllerGetJoystick(g_pads[i])) == e->cdevice.which) {
                SDL_GameControllerClose(g_pads[i]);
                g_pads[i] = NULL;
            }
        break;
    case SDL_KEYDOWN:
    case SDL_KEYUP:
        for (unsigned i = 0; i < sizeof k_keymap / sizeof k_keymap[0]; i++)
            if (k_keymap[i].key == e->key.keysym.sym) {
                if (e->type == SDL_KEYDOWN) g_virt.buttons |= k_keymap[i].bit;
                else g_virt.buttons &= (uint16_t)~k_keymap[i].bit;
                g_packet++;
            }
        {
            /* WASD = left stick; tracked from events so scripted keys work too */
            static int held[4];
            static const SDL_Keycode wasd[4] = {SDLK_w, SDLK_a, SDLK_s, SDLK_d};
            for (int i = 0; i < 4; i++)
                if (e->key.keysym.sym == wasd[i]) held[i] = e->type == SDL_KEYDOWN;
            g_virt.lx = (int16_t)((held[3] ? 32767 : 0) - (held[1] ? 32767 : 0));
            g_virt.ly = (int16_t)((held[0] ? 32767 : 0) - (held[2] ? 32767 : 0));
            g_packet++;
        }
        break;
    }
}

static void fill_state(int idx, uint32_t g) {
    uint16_t b = 0;
    uint8_t lt = 0, rt = 0;
    int16_t lx = 0, ly = 0, rx = 0, ry = 0;
    SDL_GameController *p = g_pads[idx];
    if (p) {
        static const struct { SDL_GameControllerButton b; uint16_t bit; } map[] = {
            {SDL_CONTROLLER_BUTTON_DPAD_UP, 0x1}, {SDL_CONTROLLER_BUTTON_DPAD_DOWN, 0x2},
            {SDL_CONTROLLER_BUTTON_DPAD_LEFT, 0x4}, {SDL_CONTROLLER_BUTTON_DPAD_RIGHT, 0x8},
            {SDL_CONTROLLER_BUTTON_START, 0x10}, {SDL_CONTROLLER_BUTTON_BACK, 0x20},
            {SDL_CONTROLLER_BUTTON_LEFTSTICK, 0x40}, {SDL_CONTROLLER_BUTTON_RIGHTSTICK, 0x80},
            {SDL_CONTROLLER_BUTTON_LEFTSHOULDER, 0x100}, {SDL_CONTROLLER_BUTTON_RIGHTSHOULDER, 0x200},
            {SDL_CONTROLLER_BUTTON_A, 0x1000}, {SDL_CONTROLLER_BUTTON_B, 0x2000},
            {SDL_CONTROLLER_BUTTON_X, 0x4000}, {SDL_CONTROLLER_BUTTON_Y, 0x8000},
        };
        for (unsigned i = 0; i < sizeof map / sizeof map[0]; i++)
            if (SDL_GameControllerGetButton(p, map[i].b)) b |= map[i].bit;
        if (g_nintendo)
            b = (uint16_t)((b & 0x0FFF) | ((b & 0x1000) << 1) | ((b & 0x2000) >> 1) |
                           ((b & 0x4000) << 1) | ((b & 0x8000) >> 1));
        lt = (uint8_t)(SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_TRIGGERLEFT) >> 7);
        rt = (uint8_t)(SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_TRIGGERRIGHT) >> 7);
        lx = SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_LEFTX);
        ly = (int16_t)~SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_LEFTY);
        rx = SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_RIGHTX);
        ry = (int16_t)~SDL_GameControllerGetAxis(p, SDL_CONTROLLER_AXIS_RIGHTY);
    }
    if (idx == 0) {
        b |= g_virt.buttons | skip_tap();
        if (g_virt.lt) lt = g_virt.lt;
        if (g_virt.rt) rt = g_virt.rt;
        if (g_virt.lx || g_virt.ly) { lx = g_virt.lx; ly = g_virt.ly; }
        if (g_virt.rx || g_virt.ry) { rx = g_virt.rx; ry = g_virt.ry; }
        if (b & 0x4000) gyro_stick(&lx, &ly);
        else if (!g_gyro && g_gyro_sensor) gyro_stick(&lx, &ly);  /* closes it */
    }
    { /* a new packet number whenever the reported state changes */
        static uint16_t last_b[4];
        if (b != last_b[idx & 3]) { last_b[idx & 3] = b; g_packet++; }
    }
    M32(g) = g_packet;
    M16(g + 4) = b;
    M8(g + 6) = lt;
    M8(g + 7) = rt;
    M16(g + 8) = (uint16_t)lx;
    M16(g + 10) = (uint16_t)ly;
    M16(g + 12) = (uint16_t)rx;
    M16(g + 14) = (uint16_t)ry;
}

static int connected(uint32_t i) { return i == 0 || (i < 4 && g_pads[i]); }

SHIM(XInputGetState) {
    uint32_t i = ARG(0);
    if (!connected(i)) RET(1167, 2);
    fill_state((int)i, ARG(1));
    RET(0, 2);
}
SHIM(XInputSetState) {
    uint32_t i = ARG(0);
    if (!connected(i)) RET(1167, 2);
    if (i < 4 && g_pads[i])
        SDL_GameControllerRumble(g_pads[i], M16(ARG(1)), M16(ARG(1) + 2), 200);
    RET(0, 2);
}
SHIM(XInputGetCapabilities) {
    uint32_t i = ARG(0);
    if (!connected(i)) RET(1167, 3);
    uint32_t g = ARG(2);
    memset(MEM + g, 0, 20);
    M8(g) = 1;       /* XINPUT_DEVTYPE_GAMEPAD */
    M8(g + 1) = 1;   /* XINPUT_DEVSUBTYPE_GAMEPAD */
    M16(g + 4) = 0xF3FF;
    M8(g + 6) = 0xFF; M8(g + 7) = 0xFF;
    M16(g + 8) = 0xFFC0; M16(g + 10) = 0xFFC0; M16(g + 12) = 0xFFC0; M16(g + 14) = 0xFFC0;
    M16(g + 16) = 0xFF; M16(g + 18) = 0xFF;
    RET(0, 3);
}

/* Pads the game is told about: pad 0 (touch overlay, keyboard, first SDL
 * controller) always, plus any further SDL controllers. */
int input_pad_count(void) {
    int n = 1;
    while (n < 4 && g_pads[n]) n++;
    return n;
}

/* Replaces the game's IsXInputDevice (0x51E280, cdecl, GUID *guidProduct),
 * which asks WMI for "IG_" device ids. Our pads use the 360 pad's product id. */
void f_51E280(CPU *c) {
    RETC(M32(ARG(0)) == 0x028E045Eu);
}

static const ShimDef defs[] = {
    {"XINPUT1_3.dll", "#2", shim_XInputGetState},
    {"XINPUT1_3.dll", "#3", shim_XInputSetState},
    {"XINPUT1_3.dll", "#4", shim_XInputGetCapabilities},
    {"XINPUT1_3.dll", "XInputGetState", shim_XInputGetState},
    {"XINPUT1_3.dll", "XInputSetState", shim_XInputSetState},
    {"XINPUT1_3.dll", "XInputGetCapabilities", shim_XInputGetCapabilities},
    {NULL, NULL, NULL}
};

void input_init(void) { rt_register_shims(defs); }
