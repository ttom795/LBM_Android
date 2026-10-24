/* Recompiled modules linked into this build. */
#include "rt.h"
#include "rt_table.h"
#include <dlfcn.h>

extern const FnEntry g_fn_table_exe[];
extern const unsigned g_fn_count_exe;

static const FnEntry *s_d3dx9_table = NULL;
static unsigned s_d3dx9_count = 0;
static const FnEntry *s_bink_table = NULL;
static unsigned s_bink_count = 0;

FnModule g_modules[] = {
    {"LEGOBatman.exe", NULL, 0},
    {"d3dx9_35.dll", NULL, 0},
    {"binkw32.dll", NULL, 0},
};
const unsigned g_nmodules = sizeof g_modules / sizeof g_modules[0];

typedef void (*RegisterDataFn)(const char *, const char *, uint32_t);
static RegisterDataFn s_d3db_reg_data = NULL;

void rt_modules_register_data(const char *modname, const char *name, uint32_t addr) {
    if (s_d3db_reg_data) s_d3db_reg_data(modname, name, addr);
}

void rt_modules_init(void) {
    g_modules[0].table = g_fn_table_exe;
    g_modules[0].count = g_fn_count_exe;

    void *h = dlopen("libd3db.so", RTLD_NOW | RTLD_GLOBAL);
    if (!h) {
        LOG("d3db: dlopen libd3db.so: %s", dlerror());
        return;
    }

    uint8_t **d3db_mem = (uint8_t **)dlsym(h, "MEM");
    if (d3db_mem) {
        *d3db_mem = MEM;
        LOG("d3db: synchronized MEM guest pointer (%p)", (void *)MEM);
    }

    void (*d3db_init_shims)(void) = (void (*)(void))dlsym(h, "rt_init_shims");
    if (d3db_init_shims) {
        d3db_init_shims();
        LOG("d3db: initialized d3db internal shims and thunks");
    }

    s_d3db_reg_data = (RegisterDataFn)dlsym(h, "rt_register_data");

    const FnEntry *t_d3dx9 = (const FnEntry *)dlsym(h, "g_fn_table_d3dx9");
    const unsigned *c_d3dx9 = (const unsigned *)dlsym(h, "g_fn_count_d3dx9");
    if (t_d3dx9 && c_d3dx9) {
        s_d3dx9_table = t_d3dx9;
        s_d3dx9_count = *c_d3dx9;
        g_modules[1].table = s_d3dx9_table;
        g_modules[1].count = s_d3dx9_count;
        LOG("d3db: loaded d3dx9 table (%u functions)", s_d3dx9_count);
    } else {
        LOG("d3db: failed to resolve d3dx9 symbols from libd3db.so");
    }

    const FnEntry *t_bink = (const FnEntry *)dlsym(h, "g_fn_table_bink");
    const unsigned *c_bink = (const unsigned *)dlsym(h, "g_fn_count_bink");
    if (t_bink && c_bink) {
        s_bink_table = t_bink;
        s_bink_count = *c_bink;
        g_modules[2].table = s_bink_table;
        g_modules[2].count = s_bink_count;
        LOG("d3db: loaded bink table (%u functions)", s_bink_count);
    } else {
        LOG("d3db: failed to resolve bink symbols from libd3db.so");
    }
}

/* Counts are link-time constants in other translation units. */
unsigned rt_module_count(unsigned m) {
    switch (m) {
    case 0: return g_fn_count_exe;
    case 1: return s_d3dx9_count;
    case 2: return s_bink_count;
    default: return 0;
    }
}

/* Recompiled DLLs loaded at startup, from the game directory. */
const char *const g_dll_modules[] = {"d3dx9_35.dll", "binkw32.dll", NULL};
const uint32_t g_dll_bases[] = {0x08000000u, 0x09000000u, 0};
