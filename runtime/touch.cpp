// On-screen touch controls: a virtual Xbox pad (left stick, A/B/X/Y,
// shoulders, Start/Back) that feeds the game's XInput input, drawn over
// each frame with D3D9 just before Present.
//
// Available on Android; on desktop enable with LB_TOUCH=1 (mouse clicks
// then act as touches for testing). Where available, the PC-only "Vertical
// Sync" item in Options > Video Settings becomes "Touch Controls" and shows
// or hides the overlay (vsync itself is then always on, see d3d9_bridge.cpp).
#include "d3d9_bridge.h"
#include <SDL2/SDL.h>
#include <cmath>
#include <string>

extern "C" void input_set_virtual(uint16_t buttons, int16_t lx, int16_t ly, int16_t rx, int16_t ry,
                                  uint8_t lt, uint8_t rt);

namespace {
// Layout in units of screen height, anchored to a corner (ax, ay in {0,1}).
struct Button { const char *name; uint16_t bit; float ax, ay, dx, dy, r; uint32_t color; };
const Button k_buttons[] = {
    {"A", 0x1000, 1, 1, -0.20f, -0.14f, 0.075f, 0xFF40C040},
    {"B", 0x2000, 1, 1, -0.08f, -0.27f, 0.075f, 0xFFE04040},
    {"X", 0x4000, 1, 1, -0.32f, -0.27f, 0.075f, 0xFF4080F0},
    {"Y", 0x8000, 1, 1, -0.20f, -0.40f, 0.075f, 0xFFF0D040},
    {"LB", 0x0100, 0, 0, 0.12f, 0.30f, 0.06f, 0xFFB0B0B0},
    {"RB", 0x0200, 1, 0, -0.12f, 0.30f, 0.06f, 0xFFB0B0B0},
    {"Back", 0x0020, 0, 0, 0.36f, 0.07f, 0.045f, 0xFFD0D0D0},
    {"Start", 0x0010, 1, 0, -0.36f, 0.07f, 0.045f, 0xFFFFFFFF},
};
const int NB = sizeof k_buttons / sizeof k_buttons[0];
const float STICK_X = 0.24f, STICK_Y = -0.26f, STICK_R = 0.17f; // from bottom-left

struct Finger { SDL_FingerID id; int button; bool stick; float x, y; bool used; };
Finger g_fingers[10];
float g_stick_x, g_stick_y;   // knob offset, -1..1
bool g_available, g_decided, g_seen, g_was_on;
bool g_touch_on = true;       // the player's choice, kept in <save>/touch.cfg
uint16_t g_buttons;
float g_aspect = 16.f / 9.f;  // width / height of the screen

extern "C" int g_is_gog;

// Addresses for Steam vs GOG: GOG .data addresses are shifted by -0x1000.
static inline uint32_t vsync_option_addr() { return g_is_gog ? 0x9CFF3Cu : 0x9D0F3Cu; }
static inline uint32_t aspect_addr() { return g_is_gog ? 0x94B070u : 0x94C070u; }
static inline uint32_t aspect_menu_addr() { return g_is_gog ? 0xAD6294u : 0xAD7294u; }
static inline uint32_t sound_target_addr() { return g_is_gog ? 0x99D380u : 0x99E380u; }
static inline uint32_t sound_source_addr() { return g_is_gog ? 0x99D390u : 0x99E390u; }
static inline uint32_t mode_list_ptr_addr() { return g_is_gog ? 0x9CFEECu : 0x9D0EECu; }
static inline uint32_t mode_index_addr() { return g_is_gog ? 0x9CFF38u : 0x9D0F38u; }
static inline uint32_t on_text_addr() { return g_is_gog ? 0x9D488Cu : 0x9D588Cu; }
static inline uint32_t off_text_addr() { return g_is_gog ? 0x9D4890u : 0x9D5890u; }
static inline uint32_t aspect_text_ptr_addr() { return g_is_gog ? 0x9D48B0u : 0x9D58B0u; }
const uint32_t ASPECT_FROM_RES = 3;

bool available() {
    if (!g_decided) {
        g_decided = true;
#ifdef __ANDROID__
        g_available = true;
#else
        const char *e = getenv("LB_TOUCH");
        g_available = e && atoi(e);
#endif
    }
    return g_available;
}

bool enabled() {
    if (!available()) return false;
    // The game loads and resets its own VSync value; ours always wins, and only
    // the menu changes it (f_6C42C0 below).
    M8(vsync_option_addr()) = g_touch_on;
    M32(aspect_menu_addr()) = ASPECT_FROM_RES;
    bool on = g_touch_on;
    if (g_was_on && !on) {
        // Switched off in the menu: release everything the overlay was holding.
        for (auto &g : g_fingers) g.used = false;
        g_stick_x = g_stick_y = 0;
        g_buttons = 0;
        input_set_virtual(0, 0, 0, 0, 0, 0, 0);
    }
    g_was_on = on;
    return on;
}

// Centre of a button in normalised screen coordinates (0..1 both axes).
void button_pos(const Button &b, float *x, float *y) {
    *x = b.ax + b.dx / g_aspect;
    *y = b.ay + b.dy;
}

void stick_pos(float *x, float *y) {
    *x = STICK_X / g_aspect;
    *y = 1 + STICK_Y;
}

int hit_button(float x, float y) {
    for (int i = 0; i < NB; i++) {
        float bx, by;
        button_pos(k_buttons[i], &bx, &by);
        float dx = (x - bx) * g_aspect, dy = y - by, r = k_buttons[i].r * 1.3f;
        if (dx * dx + dy * dy < r * r) return i;
    }
    return -1;
}

void update() {
    uint16_t b = 0;
    float sx = 0, sy = 0;
    for (auto &f : g_fingers) {
        if (!f.used) continue;
        if (f.stick) {
            float cx, cy;
            stick_pos(&cx, &cy);
            sx = (f.x - cx) * g_aspect / STICK_R;
            sy = (f.y - cy) / STICK_R;
            float l = std::sqrt(sx * sx + sy * sy);
            if (l > 1) { sx /= l; sy /= l; }
        } else {
            // a finger may slide between buttons
            int h = hit_button(f.x, f.y);
            if (h >= 0) f.button = h;
            if (f.button >= 0) b |= k_buttons[f.button].bit;
        }
    }
    g_buttons = b;
    g_stick_x = sx;
    g_stick_y = sy;
    input_set_virtual(b, (int16_t)(sx * 32767), (int16_t)(-sy * 32767), 0, 0, 0, 0);
}
} // namespace

extern "C" void touch_handle_event(const SDL_Event *e) {
    if (!enabled()) return;
    float x, y;
    SDL_FingerID id;
    bool down = false, up = false;
    switch (e->type) {
    case SDL_FINGERDOWN: down = true; /* fallthrough */
    case SDL_FINGERMOTION:
        x = e->tfinger.x; y = e->tfinger.y; id = e->tfinger.fingerId; break;
    case SDL_FINGERUP:
        up = true; x = e->tfinger.x; y = e->tfinger.y; id = e->tfinger.fingerId; break;
#ifndef __ANDROID__
    case SDL_MOUSEBUTTONDOWN: case SDL_MOUSEBUTTONUP: case SDL_MOUSEMOTION: {
        int w, h;
        SDL_GetWindowSize(SDL_GetWindowFromID(e->button.windowID), &w, &h);
        if (e->type == SDL_MOUSEMOTION && !(e->motion.state & SDL_BUTTON_LMASK)) return;
        x = (float)(e->type == SDL_MOUSEMOTION ? e->motion.x : e->button.x) / w;
        y = (float)(e->type == SDL_MOUSEMOTION ? e->motion.y : e->button.y) / h;
        id = 1;
        down = e->type == SDL_MOUSEBUTTONDOWN;
        up = e->type == SDL_MOUSEBUTTONUP;
        break;
    }
#endif
    default:
        return;
    }
    g_seen = true;
    Finger *f = nullptr;
    for (auto &g : g_fingers) if (g.used && g.id == id) f = &g;
    if (down && !f) {
        for (auto &g : g_fingers) if (!g.used) { f = &g; break; }
        if (!f) return;
        f->used = true;
        f->id = id;
        f->button = hit_button(x, y);
        // anything on the left half that is not a button drives the stick
        f->stick = f->button < 0 && x < 0.5f;
    }
    if (!f) return;
    f->x = x;
    f->y = y;
    if (up) f->used = false;
    update();
}

// ---------------------------------------------------------------- drawing
namespace {
IDirect3DTexture9 *g_tex;      // soft-edged disc
IDirect3DStateBlock9 *g_state;

struct Vtx { float x, y, z, rhw; uint32_t color; float u, v; };

void make_texture(IDirect3DDevice9 *d) {
    const int N = 64;
    if (FAILED(d->CreateTexture(N, N, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &g_tex, nullptr))) return;
    D3DLOCKED_RECT lr;
    if (FAILED(g_tex->LockRect(0, &lr, nullptr, 0))) return;
    for (int y = 0; y < N; y++) {
        uint32_t *row = (uint32_t *)((uint8_t *)lr.pBits + y * lr.Pitch);
        for (int x = 0; x < N; x++) {
            float dx = (x + 0.5f) / N * 2 - 1, dy = (y + 0.5f) / N * 2 - 1;
            float r = std::sqrt(dx * dx + dy * dy);
            // translucent fill with a brighter rim
            float a = r > 1 ? 0 : r > 0.86f ? 1 - (r - 0.86f) / 0.14f * 0.3f : 0.45f;
            if (r > 0.97f) a *= (1 - r) / 0.03f;
            row[x] = (uint32_t)(a * 255) << 24 | 0xFFFFFF;
        }
    }
    g_tex->UnlockRect(0);
}

void quad(Vtx *v, float cx, float cy, float r, float w, float h, uint32_t color) {
    float x0 = (cx - r / g_aspect) * w, x1 = (cx + r / g_aspect) * w;
    float y0 = (cy - r) * h, y1 = (cy + r) * h;
    Vtx q[4] = {{x0, y0, 0, 1, color, 0, 0}, {x1, y0, 0, 1, color, 1, 0},
                {x0, y1, 0, 1, color, 0, 1}, {x1, y1, 0, 1, color, 1, 1}};
    v[0] = q[0]; v[1] = q[1]; v[2] = q[2];
    v[3] = q[2]; v[4] = q[1]; v[5] = q[3];
}
} // namespace

void touch_draw(IDirect3DDevice9 *d) {
    if (!enabled()) return;
    IDirect3DSurface9 *bb = nullptr, *old = nullptr;
    if (FAILED(d->GetBackBuffer(0, 0, D3DBACKBUFFER_TYPE_MONO, &bb))) return;
    D3DSURFACE_DESC desc;
    bb->GetDesc(&desc);
    float w = (float)desc.Width, h = (float)desc.Height;
    g_aspect = w / h;
    if (!g_tex) make_texture(d);
    if (!g_state) d->CreateStateBlock(D3DSBT_ALL, &g_state);
    if (!g_tex || !g_state) { bb->Release(); return; }
    g_state->Capture();
    d->GetRenderTarget(0, &old);
    d->SetRenderTarget(0, bb);
    D3DVIEWPORT9 vp = {0, 0, desc.Width, desc.Height, 0, 1};
    d->SetViewport(&vp);
    d->BeginScene();
    d->SetVertexShader(nullptr);
    d->SetPixelShader(nullptr);
    d->SetFVF(D3DFVF_XYZRHW | D3DFVF_DIFFUSE | D3DFVF_TEX1);
    d->SetTexture(0, g_tex);
    for (int s = 1; s < 8; s++) d->SetTexture(s, nullptr);
    d->SetRenderState(D3DRS_ZENABLE, FALSE);
    d->SetRenderState(D3DRS_ZWRITEENABLE, FALSE);
    d->SetRenderState(D3DRS_STENCILENABLE, FALSE);
    d->SetRenderState(D3DRS_CULLMODE, D3DCULL_NONE);
    d->SetRenderState(D3DRS_LIGHTING, FALSE);
    d->SetRenderState(D3DRS_FOGENABLE, FALSE);
    d->SetRenderState(D3DRS_ALPHATESTENABLE, FALSE);
    d->SetRenderState(D3DRS_SCISSORTESTENABLE, FALSE);
    d->SetRenderState(D3DRS_COLORWRITEENABLE, 0xF);
    d->SetRenderState(D3DRS_SRGBWRITEENABLE, FALSE);
    d->SetRenderState(D3DRS_ALPHABLENDENABLE, TRUE);
    d->SetRenderState(D3DRS_SRCBLEND, D3DBLEND_SRCALPHA);
    d->SetRenderState(D3DRS_DESTBLEND, D3DBLEND_INVSRCALPHA);
    d->SetRenderState(D3DRS_BLENDOP, D3DBLENDOP_ADD);
    d->SetTextureStageState(0, D3DTSS_COLOROP, D3DTOP_MODULATE);
    d->SetTextureStageState(0, D3DTSS_COLORARG1, D3DTA_TEXTURE);
    d->SetTextureStageState(0, D3DTSS_COLORARG2, D3DTA_DIFFUSE);
    d->SetTextureStageState(0, D3DTSS_ALPHAOP, D3DTOP_MODULATE);
    d->SetTextureStageState(0, D3DTSS_ALPHAARG1, D3DTA_TEXTURE);
    d->SetTextureStageState(0, D3DTSS_ALPHAARG2, D3DTA_DIFFUSE);
    d->SetTextureStageState(0, D3DTSS_TEXCOORDINDEX, 0);
    d->SetTextureStageState(0, D3DTSS_TEXTURETRANSFORMFLAGS, D3DTTFF_DISABLE);
    d->SetTextureStageState(1, D3DTSS_COLOROP, D3DTOP_DISABLE);
    d->SetTextureStageState(1, D3DTSS_ALPHAOP, D3DTOP_DISABLE);
    d->SetSamplerState(0, D3DSAMP_MINFILTER, D3DTEXF_LINEAR);
    d->SetSamplerState(0, D3DSAMP_MAGFILTER, D3DTEXF_LINEAR);
    d->SetSamplerState(0, D3DSAMP_ADDRESSU, D3DTADDRESS_CLAMP);
    d->SetSamplerState(0, D3DSAMP_ADDRESSV, D3DTADDRESS_CLAMP);

    Vtx v[6 * (NB + 2)];
    int n = 0;
    float sx, sy;
    stick_pos(&sx, &sy);
    quad(v + 6 * n++, sx, sy, STICK_R, w, h, 0x90FFFFFF);
    quad(v + 6 * n++, sx + g_stick_x * STICK_R / g_aspect, sy + g_stick_y * STICK_R, STICK_R * 0.45f, w, h,
         0xD0FFFFFF);
    for (int i = 0; i < NB; i++) {
        float bx, by;
        button_pos(k_buttons[i], &bx, &by);
        uint32_t col = k_buttons[i].color;
        uint32_t a = (g_buttons & k_buttons[i].bit) ? 0xF0 : 0x90;
        quad(v + 6 * n++, bx, by, k_buttons[i].r, w, h, (col & 0xFFFFFF) | a << 24);
    }
    d->DrawPrimitiveUP(D3DPT_TRIANGLELIST, 2 * n, v, sizeof(Vtx));
    d->EndScene();
    d->SetRenderTarget(0, old);
    if (old) old->Release();
    bb->Release();
    g_state->Apply();
}

extern "C" int touch_available(void) { return available(); }

static std::string cfg_path() { return std::string(g_save_dir) + "/touch.cfg"; }

// Port settings kept beside the saves: "<touch controls> <nintendo layout> <skip cutscenes>".
static bool g_nintendo, g_skip_cutscenes;
extern "C" void input_set_nintendo(int on);
extern "C" void input_set_skip_cutscenes(int on);
static void save_cfg() {
    if (FILE *f = fopen(cfg_path().c_str(), "w")) {
        fprintf(f, "%d %d %d\n", g_touch_on, g_nintendo, g_skip_cutscenes);
        fclose(f);
    }
}
static uint32_t guest_str(const char *s) {
    uint32_t g = gmalloc((uint32_t)strlen(s) + 1);
    memcpy(MEM + g, s, strlen(s) + 1);
    return g;
}

// Video (and Port) Settings item input handler.
// Steam: f_6C42C0, GOG: f_6C4290.
static void video_settings_input_impl(CPU *c, void (*guest_fn)(CPU *)) {
    uint32_t in = ARG(0);
    int item = (int16_t)M16(in + 0xA);
    bool back = M32(in + 0x68) != 0;
    if (available() && !back && (item == 1 || item == 3)) {
        if (M32(in + 0x5C) || M32(in + 0x60)) {
            if (item == 1) {
                g_nintendo = !g_nintendo;
                input_set_nintendo(g_nintendo);
                LOG("touch: nintendo layout %s", g_nintendo ? "on" : "off");
            } else {
                g_skip_cutscenes = !g_skip_cutscenes;
                input_set_skip_cutscenes(g_skip_cutscenes);
                LOG("touch: skip cutscenes %s", g_skip_cutscenes ? "on" : "off");
            }
            save_cfg();
            M32(sound_target_addr()) = M32(sound_source_addr());  // as the game does on a change (menu sound)
        }
        RETC(0);
    }
    uint8_t before = M8(vsync_option_addr());
    guest_fn(c);
    uint8_t after = M8(vsync_option_addr());
    if (!available() || back || item != 2 || after == before) return;
    g_touch_on = after != 0;
    save_cfg();
    LOG("touch: controls %s", g_touch_on ? "on" : "off");
}

extern "C" __attribute__((weak)) void f_6C42C0_guest(CPU *c);
extern "C" void f_6C42C0(CPU *c) { if (f_6C42C0_guest) video_settings_input_impl(c, f_6C42C0_guest); }
extern "C" __attribute__((weak)) void f_6C4290_guest(CPU *c);
extern "C" void f_6C4290(CPU *c) { if (f_6C4290_guest) video_settings_input_impl(c, f_6C4290_guest); }

// Video Settings draw: values for the repurposed items.
// Steam: f_6C80D0, GOG: f_6C80A0.
static void video_settings_draw_impl(CPU *c, void (*guest_fn)(CPU *)) {
    uint32_t mode_list = mode_list_ptr_addr();
    if (!available() || !M32(mode_list)) { guest_fn(c); return; }  // no mode list yet
    uint32_t mode = M32(mode_list) + M32(mode_index_addr()) * 16;
    uint32_t w = M32(mode), h = M32(mode + 4), hz = M32(mode + 8);
    int ww = 1280, wh = 720;
    SDL_GetWindowSize((SDL_Window *)platform_window(), &ww, &wh);
    int tenths = ww > 0 ? (int)((w * 10 + ww / 2) / ww) : 10;
    if (tenths < 2) tenths = 2;
    if (tenths > 10) tenths = 10;
    M32(mode) = tenths / 10;
    M32(mode + 4) = tenths % 10;
    M32(mode + 8) = M32(g_nintendo ? on_text_addr() : off_text_addr());  // the game's "On"/"Off"
    uint32_t asp_menu = aspect_menu_addr();
    uint32_t asp_text = aspect_text_ptr_addr();
    uint32_t aspect = M32(asp_menu), aspect_text = M32(asp_text);
    M32(asp_menu) = 3;
    M32(asp_text) = M32(g_skip_cutscenes ? on_text_addr() : off_text_addr());
    guest_fn(c);
    M32(mode) = w;
    M32(mode + 4) = h;
    M32(mode + 8) = hz;
    M32(asp_menu) = aspect;
    M32(asp_text) = aspect_text;
}

extern "C" __attribute__((weak)) void f_6C80D0_guest(CPU *c);
extern "C" void f_6C80D0(CPU *c) { if (f_6C80D0_guest) video_settings_draw_impl(c, f_6C80D0_guest); }
extern "C" __attribute__((weak)) void f_6C80A0_guest(CPU *c);
extern "C" void f_6C80A0(CPU *c) { if (f_6C80A0_guest) video_settings_draw_impl(c, f_6C80A0_guest); }

// The display mode list builder drops modes under 640x480, which hides low resolution scales.
// It sorts the surviving modes with qsort right after filtering; refill array there.
// Steam: f_7401A0 (ret 0x52E3BD), GOG: f_740170 (ret 0x52E39D).
extern "C" void bridge_scaled_mode(uint32_t i, uint32_t *w, uint32_t *h);
static void qsort_display_modes_impl(CPU *c, void (*guest_fn)(CPU *), uint32_t expected_ret) {
    if (available() && M32(c->r[4]) == expected_ret && ARG(2) == 16) {
        uint32_t base = ARG(0), n = ARG(1), obj = c->r[7];  // edi = display object
        for (uint32_t i = 0; i < n; i++) {
            uint32_t w, h;
            bridge_scaled_mode(i, &w, &h);
            M32(base + i * 16) = w;
            M32(base + i * 16 + 4) = h;
            M32(base + i * 16 + 8) = 60;
            M32(base + i * 16 + 12) = 0x16;  // D3DFMT_X8R8G8B8, as enumerated
        }
        M32(obj + 0x618) = n;
    }
    if (guest_fn) guest_fn(c);
}

extern "C" __attribute__((weak)) void f_7401A0_guest(CPU *c);
extern "C" void f_7401A0(CPU *c) { if (f_7401A0_guest) qsort_display_modes_impl(c, f_7401A0_guest, 0x52E3BDu); }
extern "C" __attribute__((weak)) void f_740170_guest(CPU *c);
extern "C" void f_740170(CPU *c) { if (f_740170_guest) qsort_display_modes_impl(c, f_740170_guest, 0x52E39Du); }

// pcconfig "Widescreen" setter, used at load and by the menu's Accept.
// Steam: f_524860, GOG: f_524840.
extern "C" __attribute__((weak)) void f_524860_guest(CPU *c);
extern "C" void f_524860(CPU *c) {
    if (f_524860_guest) f_524860_guest(c);
    if (available()) M32(aspect_addr()) = ASPECT_FROM_RES;
}
extern "C" __attribute__((weak)) void f_524840_guest(CPU *c);
extern "C" void f_524840(CPU *c) {
    if (f_524840_guest) f_524840_guest(c);
    if (available()) M32(aspect_addr()) = ASPECT_FROM_RES;
}

extern "C" void touch_setup_guest(void) {
    if (!available()) return;
    if (FILE *f = fopen(cfg_path().c_str(), "r")) {
        int t = 1, n = 0, k = 0;
        if (fscanf(f, "%d %d %d", &t, &n, &k) >= 1) { g_touch_on = t != 0; g_nintendo = n != 0; g_skip_cutscenes = k != 0; }
        fclose(f);
    }
    input_set_nintendo(g_nintendo);
    input_set_skip_cutscenes(g_skip_cutscenes);
    M8(vsync_option_addr()) = g_touch_on;
    M32(aspect_addr()) = M32(aspect_menu_addr()) = ASPECT_FROM_RES;
    uint32_t touch = guest_str("Touch Controls"), quit = guest_str("Quit Game"),
             scale = guest_str("Resolution Scale"), layout = guest_str("Nintendo Button Layout"),
             skip = guest_str("Skip Cutscenes"), title = guest_str("Video and Port Settings");
    if (g_is_gog) {
        strcpy((char *)MEM + 0x872928, "%s: %d.%d");  // was "%s: %d x %d"
        strcpy((char *)MEM + 0x87291C, "%s: %s");     // was "%s: %dHz"
    } else {
        strcpy((char *)MEM + 0x872A10, "%s: %d.%d");  // was "%s: %d x %d"
        strcpy((char *)MEM + 0x872A04, "%s: %s");     // was "%s: %dHz"
    }
    // PC menu text tables (one per language): { id, char *text } pairs in .data.
    // Ids: 1 Quit to Windows, 0x12 Video Settings, 0x13 Screen Resolution,
    // 0x14 Refresh Rate, 0x15 Vertical Sync, 0x16 Aspect Ratio.
    int n = 0;
    for (uint32_t a = 0x934008; a < 0x934000 + 0x8A000 - 12; a += 4) {
        if (M32(a) != 0x15 || M32(a - 8) != 0x14 || M32(a + 8) != 0x16) continue;
        M32(a + 4) = touch;
        M32(a - 4) = layout;
        M32(a + 12) = skip;
        if (M32(a - 16) == 0x13) M32(a - 12) = scale;
        if (M32(a - 24) == 0x12) M32(a - 20) = title;
        n++;
        // "Quit to Windows" (id 1) sits 0x14 entries earlier in the same table.
        uint32_t q = a - 0x14 * 8;
        if (M32(q) == 1 && !strcmp((const char *)MEM + M32(q + 4), "Quit to Windows")) M32(q + 4) = quit;
    }
    LOG("touch: relabelled %d menu tables", n);
}
