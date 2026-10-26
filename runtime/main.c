/* Host entry: maps guest memory, loads LEGOBatman.exe's sections and runs
 * the recompiled entry point. The exe itself is read from the user's game
 * directory at runtime; nothing from it is embedded in the build. */
#define _GNU_SOURCE
#include "rt.h"
#include "rt_table.h"
#include <stdarg.h>
#include <sys/mman.h>
#include <unistd.h>
#include <string.h>

uint8_t *MEM;
int g_log_level = 1;
char g_game_dir[1024] = ".";
char g_save_dir[1024] = "./save";
uint32_t g_image_size;
uint32_t g_entry;

void rt_fatal(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "FATAL: ");
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    fflush(stderr);
    abort();
}

static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t rd32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }

/* Loads a PE image at `want_base` (applying relocations if it differs from
 * the preferred base), resolves its imports and registers its exports.
 * Returns the entry point. */
static uint32_t load_pe(const char *path, uint32_t want_base, const char *modname) {
    FILE *f = fopen(path, "rb");
    if (!f) rt_fatal("cannot open %s", path);
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = malloc(n);
    if (fread(buf, 1, n, f) != (size_t)n) rt_fatal("short read %s", path);
    fclose(f);
    uint32_t pe = rd32(buf + 0x3C);
    if (rd32(buf + pe) != 0x4550) rt_fatal("not a PE file");
    uint16_t nsec = rd16(buf + pe + 6);
    uint16_t optsz = rd16(buf + pe + 20);
    const uint8_t *opt = buf + pe + 24;
    uint32_t pref = rd32(opt + 28);
    uint32_t base = want_base ? want_base : pref;
    uint32_t entry = rd32(opt + 16) ? base + rd32(opt + 16) : 0;
    uint32_t image_size = rd32(opt + 56);
    uint32_t hdrsz = rd32(opt + 60);
    memcpy(MEM + base, buf, hdrsz);
    const uint8_t *sec = opt + optsz;
    for (int i = 0; i < nsec; i++, sec += 40) {
        uint32_t vsz = rd32(sec + 8), va = rd32(sec + 12), rawsz = rd32(sec + 16), raw = rd32(sec + 20);
        uint32_t cp = rawsz < vsz ? rawsz : vsz;
        if (!vsz) cp = rawsz;
        memcpy(MEM + base + va, buf + raw, cp);
    }
    /* relocations */
    if (base != pref) {
        uint32_t delta = base - pref;
        uint32_t rva = rd32(opt + 136), size = rd32(opt + 140);
        for (uint32_t p = base + rva; p < base + rva + size;) {
            uint32_t page = M32(p), bsz = M32(p + 4);
            if (!bsz) break;
            for (uint32_t e = p + 8; e < p + bsz; e += 2) {
                uint16_t ent = M16(e);
                if ((ent >> 12) == 3) M32(base + page + (ent & 0xFFF)) += delta;
            }
            p += bsz;
        }
    }
    /* exports */
    uint32_t exp_rva = rd32(opt + 96);
    if (exp_rva) {
        uint32_t ed = base + exp_rva;
        uint32_t nnames = M32(ed + 24), funcs = base + M32(ed + 28), names = base + M32(ed + 32),
                 ords = base + M32(ed + 36), ordbase = M32(ed + 16), nfuncs = M32(ed + 20);
        for (uint32_t i = 0; i < nnames; i++) {
            const char *nm = (const char *)(MEM + base + M32(names + 4 * i));
            uint16_t o = M16(ords + 2 * i);
            rt_register_data(modname, nm, base + M32(funcs + 4 * o));
        }
        for (uint32_t i = 0; i < nfuncs; i++) {
            char nm[16];
            snprintf(nm, sizeof nm, "#%u", ordbase + i);
            if (M32(funcs + 4 * i)) rt_register_data(modname, nm, base + M32(funcs + 4 * i));
        }
    }
    /* imports */
    uint32_t imp_rva = rd32(opt + 104);
    for (uint32_t d = base + imp_rva; imp_rva && M32(d + 12); d += 20) {
        const char *dll = (const char *)(MEM + base + M32(d + 12));
        uint32_t ilt = M32(d) ? base + M32(d) : base + M32(d + 16);
        uint32_t iat = base + M32(d + 16);
        for (; M32(ilt); ilt += 4, iat += 4) {
            uint32_t e = M32(ilt);
            char name[128];
            if (e & 0x80000000u) snprintf(name, sizeof name, "#%u", e & 0xFFFF);
            else snprintf(name, sizeof name, "%s", (const char *)(MEM + base + e + 2));
            M32(iat) = rt_import(dll, name);
        }
    }
    free(buf);
    extern void rt_module_add(const char *, uint32_t);
    rt_module_add(modname, base);
    LOG("loaded %s at %08x: entry %08x image %08x", path, base, entry, image_size);
    if (!strcmp(modname, "LEGOBatman.exe")) { g_entry = entry; g_image_size = image_size; }
    return entry;
}

/* Decodes a movie with the recompiled binkw32 only and writes a few frames
 * as PPM files: isolates Bink/MMX lifting problems from the game. */
static int bink_test(CPU *c, const char *movie, const char *outdir) {
    uint32_t e = load_pe("binkw32.dll", 0x09000000u, "binkw32.dll");
    uint32_t a3[3] = {0x09000000u, 1, 0};
    rt_invoke(c, e, 3, a3);
    uint32_t name = gstrdup(movie);
    uint32_t a2[2] = {name, 0};
    uint32_t bink = rt_invoke(c, rt_import("binkw32.dll", "_BinkOpen@8"), 2, a2);
    if (!bink) { LOG("BinkOpen failed"); return 1; }
    uint32_t w = M32(bink), h = M32(bink + 4), frames = M32(bink + 8);
    LOG("bink: %ux%u, %u frames", w, h, frames);
    uint32_t buf = gmalloc(w * h * 4);
    for (uint32_t f = 1; f <= frames && f <= 90; f++) {
        uint32_t a1[1] = {bink};
        rt_invoke(c, rt_import("binkw32.dll", "_BinkDoFrame@4"), 1, a1);
        uint32_t a7[7] = {bink, buf, w * 4, h, 0, 0, 3 /* BINKSURFACE32 */};
        rt_invoke(c, rt_import("binkw32.dll", "_BinkCopyToBuffer@28"), 7, a7);
        if (f == 1 || f == 30 || f == 90) {
            char path[1024];
            snprintf(path, sizeof path, "%s/frame%03u.ppm", outdir, f);
            FILE *o = fopen(path, "wb");
            fprintf(o, "P6 %u %u 255\n", w, h);
            for (uint32_t i = 0; i < w * h; i++) {
                uint8_t *px = MEM + buf + 4 * i;
                uint8_t rgb[3] = {px[2], px[1], px[0]};
                fwrite(rgb, 1, 3, o);
            }
            fclose(o);
            LOG("wrote %s", path);
        }
        rt_invoke(c, rt_import("binkw32.dll", "_BinkNextFrame@4"), 1, a1);
    }
    return 0;
}

#if defined(__GLIBC__)
#include <execinfo.h>
#include <signal.h>
#include <ucontext.h>
/* Crash report: host backtrace plus the faulting address. */
static void on_crash(int sig, siginfo_t *si, void *uc) {
    (void)uc;
    fprintf(stderr, "CRASH: signal %d at address %p\n", sig, si->si_addr);
    void *bt[64];
    int n = backtrace(bt, 64);
    backtrace_symbols_fd(bt, n, 2);
    _exit(128 + sig);
}
static void install_crash_handler(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = on_crash;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);
}
#else
static void install_crash_handler(void) {}
#endif

#ifdef __ANDROID__
#include <SDL2/SDL_main.h>
#include <SDL2/SDL_system.h>
#include <sys/stat.h>
#endif

int main(int argc, char **argv) {
    install_crash_handler();
    const char *env = getenv("LB_LOG");
    if (env) g_log_level = atoi(env);
#ifdef __ANDROID__
    /* The launcher passes the folder the player chose (argv[1]) and its
     * port/save (argv[2]); the log goes to <folder>/port/log.txt. Without
     * them (0.1.6 layout) everything is in the app's external files folder:
     * <ext>/game the install, <ext>/save the saves, <ext>/log.txt the log. */
    const char *ext = SDL_AndroidGetExternalStoragePath();
    char logp[1100] = "";
    if (argc > 2) {
        snprintf(logp, sizeof logp, "%s/port", argv[1]);
        mkdir(logp, 0777);
        snprintf(logp, sizeof logp, "%s/port/log.txt", argv[1]);
        mkdir(argv[2], 0777);
    } else if (ext) {
        snprintf(g_game_dir, sizeof g_game_dir, "%s/game", ext);
        snprintf(g_save_dir, sizeof g_save_dir, "%s/save", ext);
        mkdir(g_game_dir, 0777);
        mkdir(g_save_dir, 0777);
        snprintf(logp, sizeof logp, "%s/log.txt", ext);
    }
    if (logp[0] && freopen(logp, "w", stderr)) setvbuf(stderr, NULL, _IOLBF, 0);
    if (logp[0] && !env) g_log_level = 1;
#endif
    if (argc > 1) snprintf(g_game_dir, sizeof g_game_dir, "%s", argv[1]);
    if (argc > 2) snprintf(g_save_dir, sizeof g_save_dir, "%s", argv[2]);
    MEM = mmap(NULL, 1ull << 32, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (MEM == MAP_FAILED) rt_fatal("cannot reserve guest address space");
    /* keep the null page inaccessible to catch null derefs */
    mprotect(MEM, 0x10000, PROT_NONE);
    rt_init_shims();
    if (chdir(g_game_dir) != 0) rt_fatal("chdir %s", g_game_dir);
    extern void platform_init(void);
    platform_init();
    CPU *c = rt_new_cpu();
    rt_thread_init(c);
    if (argc > 4 && !strcmp(argv[3], "--bink-test")) return bink_test(c, argv[4], argc > 5 ? argv[5] : ".");
    /* DLLs first so the exe's imports bind to their exports */
    extern const char *const g_dll_modules[];
    extern const uint32_t g_dll_bases[];
    uint32_t dll_entries[8];
    int ndll = 0;
    for (; g_dll_modules[ndll]; ndll++)
        dll_entries[ndll] = load_pe(g_dll_modules[ndll], g_dll_bases[ndll], g_dll_modules[ndll]);
    load_pe("LEGOBatman.exe", 0, "LEGOBatman.exe");
    extern void touch_setup_guest(void);
    touch_setup_guest();
    for (int i = 0; i < ndll; i++) {
        uint32_t args[3] = {g_dll_bases[i], 1, 0};
        if (dll_entries[i] && !rt_invoke(c, dll_entries[i], 3, args))
            rt_fatal("DllMain of %s failed", g_dll_modules[i]);
    }
    /* push a return address that ends the process when main returns */
    c->r[4] -= 4;
    M32(c->r[4]) = 0;
    rt_call(c, g_entry);
    LOG("entry returned eax=%u", c->r[0]);
    return (int)c->r[0];
}
