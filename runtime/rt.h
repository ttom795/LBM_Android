/* Host runtime shared declarations. */
#pragma once
#include "x86.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <pthread.h>

/* ---- guest memory layout ---- */
#define GUEST_IMAGE_BASE   0x00400000u
#define GUEST_STACK_BASE   0x10000000u  /* stacks grow down from here + size */
#define GUEST_STACK_SIZE   0x00800000u  /* 8 MiB per thread slot */
#define GUEST_STACK_SLOTS  16
#define GUEST_TEB_BASE     0x18000000u  /* one page per thread */
#define GUEST_MISC_BASE    0x18100000u  /* PEB, env strings, misc runtime data */
#define GUEST_HEAP_BASE    0x20000000u
#define GUEST_HEAP_END     0x60000000u
#define GUEST_VA_BASE      0x60000000u  /* VirtualAlloc */
#define GUEST_VA_END       0xE0000000u
#define GUEST_THUNK_BASE   0xFF000000u  /* fake code addresses for host shims */

/* ---- logging ---- */
extern int g_log_level;
#define LOG(...) do { if (g_log_level > 0) { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } } while (0)
#define TRACE(...) do { if (g_log_level > 1) { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } } while (0)
void rt_fatal(const char *fmt, ...) __attribute__((noreturn, format(printf, 1, 2)));

/* ---- shims ---- */
typedef void (*ShimFn)(CPU *c);
typedef struct { const char *dll; const char *name; ShimFn fn; } ShimDef;

#define ARG(n) M32(c->r[4] + 4 + 4 * (n))
#define ARGF(n) MF32(c->r[4] + 4 + 4 * (n))
/* stdcall return: pop return address + nargs dwords */
#define RET(v, nargs) do { c->r[0] = (uint32_t)(v); c->r[4] += 4 + 4 * (nargs); return; } while (0)
/* cdecl return: caller pops args */
#define RETC(v) do { c->r[0] = (uint32_t)(v); c->r[4] += 4; return; } while (0)
#define SHIM(name) static void shim_##name(CPU *c)

/* Allocate a thunk address that dispatches to a host function. */
uint32_t rt_thunk(ShimFn fn, const char *name);
/* Look up an import by dll/name; returns a thunk (an aborting stub if unknown). */
uint32_t rt_import(const char *dll, const char *name);
uint32_t rt_import_opt(const char *dll, const char *name);
void rt_register_data(const char *dll, const char *name, uint32_t addr);
void rt_register_shims(const ShimDef *defs);

/* Call a guest function from the host with stdcall/cdecl args already chosen. */
uint32_t rt_invoke(CPU *c, uint32_t fn, int nargs, const uint32_t *args);
int rt_has_code(uint32_t addr);

/* ---- guest heap ---- */
uint32_t gmalloc(uint32_t size);
void gfree(uint32_t p);
uint32_t grealloc(uint32_t p, uint32_t size);
uint32_t gsize(uint32_t p);
uint32_t gvalloc(uint32_t addr, uint32_t size);
void gvfree(uint32_t addr);
static inline char *GSTR(uint32_t a) { return a ? (char *)(MEM + a) : NULL; }
static inline void *GPTR(uint32_t a) { return a ? (void *)(MEM + a) : NULL; }
uint32_t gstrdup(const char *s);

/* ---- handles ---- */
enum { H_FREE, H_FILE, H_EVENT, H_MUTEX, H_SEM, H_THREAD, H_FIND, H_HEAP, H_MODULE, H_OTHER };
typedef struct HObj HObj;
uint32_t h_new(int type, void *obj);
void *h_get(uint32_t h, int type);
void h_close(uint32_t h);

/* ---- threads ---- */
CPU *rt_new_cpu(void);
CPU *rt_current_cpu(void);
void rt_thread_init(CPU *c);
extern uint32_t g_last_error_tls; /* unused; last error stored in TEB */
void rt_set_last_error(CPU *c, uint32_t e);
uint32_t rt_get_last_error(CPU *c);

/* ---- files ---- */
extern char g_game_dir[1024];
extern char g_save_dir[1024];
/* Map a Windows path to a host path, resolving case-insensitively. */
int rt_host_path(const char *win, char *out, size_t outsz);

void rt_init_shims(void);
