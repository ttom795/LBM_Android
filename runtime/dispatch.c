/* Indirect call/jump dispatch, host<->guest call bridging, and traps. */
#include "rt.h"
#include "rt_table.h"
#include <string.h>
#include <time.h>

unsigned rt_module_count(unsigned m);

#define MAX_THUNKS 8192
static struct { ShimFn fn; const char *name; } g_thunks[MAX_THUNKS];
static uint32_t g_nthunks;
static pthread_mutex_t g_thunk_lock = PTHREAD_MUTEX_INITIALIZER;

uint32_t rt_thunk(ShimFn fn, const char *name) {
    pthread_mutex_lock(&g_thunk_lock);
    if (g_nthunks >= MAX_THUNKS) rt_fatal("out of thunks");
    uint32_t i = g_nthunks++;
    g_thunks[i].fn = fn;
    g_thunks[i].name = name;
    pthread_mutex_unlock(&g_thunk_lock);
    return GUEST_THUNK_BASE + i * 4;
}

const char *rt_thunk_name(uint32_t a) {
    if (a >= GUEST_THUNK_BASE && (a - GUEST_THUNK_BASE) / 4 < g_nthunks) return g_thunks[(a - GUEST_THUNK_BASE) / 4].name;
    return NULL;
}

/* Small direct-mapped cache in front of the sorted function table. */
#define CACHE_BITS 14
static struct { uint32_t addr; GuestFn fn; } g_cache[1 << CACHE_BITS];

static GuestFn lookup(uint32_t a) {
    unsigned h = (a ^ (a >> 13)) & ((1 << CACHE_BITS) - 1);
    if (g_cache[h].addr == a && g_cache[h].fn) return g_cache[h].fn;
    for (unsigned m = 0; m < g_nmodules; m++) {
        const FnEntry *t = g_modules[m].table;
        unsigned n = rt_module_count(m);
        if (!t || !n || a < t[0].addr || a > t[n - 1].addr) continue;
        unsigned lo = 0, hi = n;
        while (lo < hi) {
            unsigned mid = (lo + hi) / 2;
            if (t[mid].addr < a) lo = mid + 1;
            else hi = mid;
        }
        if (lo < n && t[lo].addr == a) {
            g_cache[h].addr = a;
            g_cache[h].fn = t[lo].fn;
            return t[lo].fn;
        }
    }
    return NULL;
}

int rt_has_code(uint32_t a) {
    if (a >= GUEST_THUNK_BASE) {
        uint32_t i = (a - GUEST_THUNK_BASE) / 4;
        return i < g_nthunks && g_thunks[i].fn != NULL;
    }
    return lookup(a) != NULL;
}

void rt_call(CPU *c, uint32_t t) {
    if (t >= GUEST_THUNK_BASE) {
        uint32_t i = (t - GUEST_THUNK_BASE) / 4;
        if (i < g_nthunks) {
            TRACE("-> %s", g_thunks[i].name);
            if (!g_thunks[i].fn)
                rt_fatal("unimplemented import %s (called from %08x)", g_thunks[i].name, M32(c->r[4]));
            g_thunks[i].fn(c);
            return;
        } else {
            rt_fatal("unknown import thunk index %u (addr %08x, called from %08x, registered %u)",
                     i, t, M32(c->r[4]), g_nthunks);
        }
    }
    GuestFn fn = lookup(t);
    if (!fn) {
        uint32_t ret = M32(c->r[4]), esp = c->r[4];
        uint32_t a0 = M32(esp + 4);
        fprintf(stderr, "=== BAD CALL / CRASH DIAGNOSTICS ===\n");
        fprintf(stderr, "Target: %08x (no recompiled code)\n", t);
        fprintf(stderr, "Return address: %08x\n", ret);
        fprintf(stderr, "Registers:\n");
        fprintf(stderr, "  eax=%08x ecx=%08x edx=%08x ebx=%08x\n", c->r[0], c->r[1], c->r[2], c->r[3]);
        fprintf(stderr, "  esp=%08x ebp=%08x esi=%08x edi=%08x\n", c->r[4], c->r[5], c->r[6], c->r[7]);
        fprintf(stderr, "Stack dump from esp=%08x:\n", esp);
        for (int i = 0; i < 8; i++) {
            fprintf(stderr, "  [esp+%02x] = %08x\n", i * 4, M32(esp + i * 4));
        }
        if (a0 >= 0x10000) {
            uint32_t vt = M32(a0);
            fprintf(stderr, "Object at a0 (%08x): vtbl=%08x +4=%08x +8=%08x\n", a0, vt, M32(a0 + 4), M32(a0 + 8));
            if (vt >= 0x10000) {
                fprintf(stderr, "  vtbl methods: [0]=%08x [1]=%08x [2]=%08x [3]=%08x [4]=%08x\n",
                        M32(vt), M32(vt + 4), M32(vt + 8), M32(vt + 12), M32(vt + 16));
            }
        }
        fprintf(stderr, "====================================\n");
        fflush(stderr);
        rt_fatal("no recompiled code at %08x (return address %08x)\n\n"
                 "Registers:\n"
                 "eax=%08x ecx=%08x edx=%08x ebx=%08x\n"
                 "esp=%08x ebp=%08x esi=%08x edi=%08x\n\n"
                 "Stack:\n"
                 "[esp+0]=%08x [esp+4]=%08x\n"
                 "[esp+8]=%08x [esp+c]=%08x",
                 t, ret,
                 c->r[0], c->r[1], c->r[2], c->r[3],
                 c->r[4], c->r[5], c->r[6], c->r[7],
                 M32(esp), M32(esp + 4), M32(esp + 8), M32(esp + 12));
    }
    fn(c);
}

uint32_t rt_invoke(CPU *c, uint32_t fn, int nargs, const uint32_t *args) {
    uint32_t saved_esp = c->r[4];
    for (int i = nargs - 1; i >= 0; i--) {
        c->r[4] -= 4;
        M32(c->r[4]) = args[i];
    }
    c->r[4] -= 4;
    M32(c->r[4]) = 0xFEEDF00Du; /* fake return address */
    rt_call(c, fn);
    /* works for both stdcall (callee popped) and cdecl (we restore) */
    c->r[4] = saved_esp;
    return c->r[0];
}

void rt_unimpl(CPU *c, uint32_t addr, const char *what) {
    rt_fatal("unimplemented instruction '%s' at %08x", what, addr);
}

void rt_int3(CPU *c, uint32_t addr) {
    rt_fatal("int3 at %08x", addr);
}

void rt_cpuid(CPU *c, uint32_t *eax, uint32_t *ebx, uint32_t *ecx, uint32_t *edx) {
    uint32_t leaf = *eax;
    if (leaf == 0) {
        *eax = 1;
        memcpy(ebx, "Genu", 4);
        memcpy(edx, "ineI", 4);
        memcpy(ecx, "ntel", 4);
    } else if (leaf == 1) {
        *eax = 0x00000F29; /* a Pentium 4 */
        *ebx = 0;
        *ecx = 0;
        *edx = (1u << 0) | (1u << 4) | (1u << 15) | (1u << 23) | (1u << 25) | (1u << 26); /* fpu tsc cmov mmx sse sse2 */
    } else {
        *eax = *ebx = *ecx = *edx = 0;
    }
}

uint64_t rt_rdtsc(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}
