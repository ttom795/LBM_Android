"""Static recompiler: lifts x86-32 functions discovered by discover.py into C.

Usage: lift.py LEGOBatman.exe functions.json outdir

Each guest function at address A becomes `void f_A(CPU *c)`. Registers and
flags are C locals; they are spilled to CPU before any call or return.
Unsupported instructions become rt_unimpl() traps carrying their address,
so a missing feature fails loudly at runtime instead of silently.
"""
import json
import os
import sys

import capstone
import pefile
from capstone import x86

import peimg

EXE, FUNCS, OUT = sys.argv[1:4]
MOD = sys.argv[4] if len(sys.argv) > 4 else "exe"
NEW_BASE = int(sys.argv[5], 16) if len(sys.argv) > 5 else None
FILES = 64 if MOD == "exe" else 24

pe, BASE, img = peimg.load(EXE, NEW_BASE)
img = bytes(img)
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True

data = json.load(open(FUNCS))
iat = {int(k, 16): v for k, v in data["iat"].items()}
jump_tables = {int(k, 16): [int(t, 16) for t in v] for k, v in data["jump_tables"].items()}

JUNK = {
    "in", "out", "insb", "insd", "insw", "outsb", "outsd", "outsw", "hlt", "retf", "iretd",
    "les", "lds", "arpl", "bound", "into", "sti", "cli", "daa", "das", "aaa", "aas", "aam",
    "aad", "salc", "sldt", "lcall", "ljmp", "xabort", "int1", "fstpnce", "lock sbb",
    "fbld", "loope", "loopne", "enter", "jo", "jno",
}


def decode_block(a, b):
    return list(md.disasm(img[a - BASE:b - BASE], a))


def import_via(t):
    """Import name if `t` is the IAT slot itself or a `jmp [iat]` stub."""
    if t in iat:
        return iat[t]
    if not (BASE <= t < BASE + len(img) - 6):
        return ""
    if img[t - BASE] == 0xFF and img[t - BASE + 1] == 0x25:
        return iat.get(int.from_bytes(img[t - BASE + 2:t - BASE + 6], "little"), "")
    return ""


REG32 = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
REG16 = {"ax": "eax", "cx": "ecx", "dx": "edx", "bx": "ebx", "sp": "esp", "bp": "ebp",
         "si": "esi", "di": "edi"}
REG8L = {"al": "eax", "cl": "ecx", "dl": "edx", "bl": "ebx"}
REG8H = {"ah": "eax", "ch": "ecx", "dh": "edx", "bh": "ebx"}
UT = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}
ST_ = {1: "int8_t", 2: "int16_t", 4: "int32_t", 8: "int64_t"}
BITS = {1: 8, 2: 16, 4: 32}
SPILL = "SPILL();"
RELOAD = "RELOAD();"


CMP_PRED = {"eq": "a_ == b_", "lt": "a_ < b_", "le": "a_ <= b_", "unord": "a_ != a_ || b_ != b_",
            "neq": "!(a_ == b_)", "nlt": "!(a_ < b_)", "nle": "!(a_ <= b_)", "ord": "a_ == a_ && b_ == b_"}


class Unsupported(Exception):
    pass


# Guest functions implemented on the host instead (runtime defines f_X).
HOST_OVERRIDES = {
    "exe": {0x51E280,   # IsXInputDevice via WMI (Steam) -> runtime/input.c
            0x6C42C0,   # Video Settings item input (Steam) -> runtime/touch.cpp
            0x6C80D0,   # Video Settings draw (Steam) -> runtime/touch.cpp
            0x7401A0,   # qsort: refills display mode list (Steam) -> runtime/touch.cpp
            0x524860,   # pcconfig "Widescreen" setter (Steam) -> runtime/touch.cpp
            0x51E240,   # IsXInputDevice via WMI (GOG) -> runtime/input.c
            0x6C4290,   # Video Settings item input (GOG) -> runtime/touch.cpp
            0x6C80A0,   # Video Settings draw (GOG) -> runtime/touch.cpp
            0x740170,   # qsort: refills display mode list (GOG) -> runtime/touch.cpp
            0x524840},  # pcconfig "Widescreen" setter (GOG) -> runtime/touch.cpp
}


class Lifter:
    def __init__(self, fstart, blocks, all_funcs):
        self.f = fstart
        self.blocks = blocks  # list of (start, end)
        self.block_starts = {a for a, _ in blocks}
        self.all_funcs = all_funcs
        self.out = []
        self.calls = set()
        self.need_dispatch = False

    # ---------------- operands ----------------
    def reg_get(self, r):
        if r in REG32:
            return r
        if r in REG16:
            return "((uint16_t)%s)" % REG16[r]
        if r in REG8L:
            return "((uint8_t)%s)" % REG8L[r]
        if r in REG8H:
            return "((uint8_t)(%s >> 8))" % REG8H[r]
        if r in ("cs", "ds", "es", "ss", "fs", "gs"):
            return "((uint16_t)0x%X)" % {"cs": 0x23, "ds": 0x2B, "es": 0x2B, "ss": 0x2B, "fs": 0x53, "gs": 0x2B}[r]
        raise Unsupported("reg " + r)

    def reg_set(self, r, e):
        if r in REG32:
            return "%s = (uint32_t)(%s);" % (r, e)
        if r in REG16:
            g = REG16[r]
            return "%s = (%s & 0xFFFF0000u) | (uint16_t)(%s);" % (g, g, e)
        if r in REG8L:
            g = REG8L[r]
            return "%s = (%s & 0xFFFFFF00u) | (uint8_t)(%s);" % (g, g, e)
        if r in REG8H:
            g = REG8H[r]
            return "%s = (%s & 0xFFFF00FFu) | ((uint32_t)(uint8_t)(%s) << 8);" % (g, g, e)
        raise Unsupported("reg " + r)

    def addr(self, i, op):
        m = op.mem
        parts = []
        for r in (m.base, m.index):
            if r and i.reg_name(r) in ("bx", "si", "di", "bp"):
                raise Unsupported("addr16")
        if m.base:
            parts.append(i.reg_name(m.base))
        if m.index:
            idx = i.reg_name(m.index)
            parts.append("%s*%d" % (idx, m.scale) if m.scale != 1 else idx)
        disp = m.disp & 0xFFFFFFFF
        if disp or not parts:
            parts.append("0x%Xu" % disp)
        if m.segment and i.reg_name(m.segment) == "fs":
            parts.append("c->fs_base")
        elif m.segment and i.reg_name(m.segment) not in ("ds", "ss", "es", "cs"):
            raise Unsupported("segment")
        return "(uint32_t)(%s)" % " + ".join(parts)

    def mem(self, size, a):
        return {1: "M8", 2: "M16", 4: "M32", 8: "M64"}[size] + "(" + a + ")"

    def get(self, i, op, size=None):
        size = size or op.size
        if op.type == x86.X86_OP_REG:
            return self.reg_get(i.reg_name(op.reg))
        if op.type == x86.X86_OP_IMM:
            v = op.imm & ((1 << (8 * size)) - 1)
            return "(%s)0x%Xu" % (UT[size], v)
        if op.type == x86.X86_OP_MEM:
            return self.mem(size, self.addr(i, op))
        raise Unsupported("operand")

    def set(self, i, op, e):
        if op.type == x86.X86_OP_REG:
            return self.reg_set(i.reg_name(op.reg), e)
        if op.type == x86.X86_OP_MEM:
            return "%s = (%s)(%s);" % (self.mem(op.size, self.addr(i, op)), UT[op.size], e)
        raise Unsupported("set operand")

    # ---------------- conditions ----------------
    COND = {
        "o": "f.of", "no": "!f.of", "b": "f.cf", "ae": "!f.cf", "e": "f.zf", "ne": "!f.zf",
        "be": "(f.cf || f.zf)", "a": "(!f.cf && !f.zf)", "s": "f.sf", "ns": "!f.sf",
        "p": "f.pf", "np": "!f.pf", "l": "(f.sf != f.of)", "ge": "(f.sf == f.of)",
        "le": "(f.zf || f.sf != f.of)", "g": "(!f.zf && f.sf == f.of)",
    }

    # ---------------- control flow ----------------
    def goto(self, t):
        if t in self.block_starts:
            return "goto L_%X;" % t
        if t in self.all_funcs:
            self.calls.add(t)
            return "{ %s f_%X(c); return; }" % (SPILL, t)
        return "{ %s rt_call(c, 0x%Xu); return; }" % (SPILL, t)

    def call_direct(self, t, ret):
        pre = "esp -= 4; M32(esp) = 0x%Xu; %s " % (ret, SPILL)
        if t in self.all_funcs:
            self.calls.add(t)
            return pre + "f_%X(c); %s" % (t, RELOAD)
        return pre + "rt_call(c, 0x%Xu); %s" % (t, RELOAD)

    def indirect_jump(self, i, target_expr):
        self.need_dispatch = True
        return "{ jt_ = %s; goto L_dispatch; }" % target_expr

    # ---------------- instruction lifting ----------------
    def lift(self, i):
        mn = i.mnemonic
        ops = i.operands
        nxt = i.address + i.size
        o = []
        e = o.append

        if mn in JUNK:
            raise Unsupported("junk " + mn)
        if mn.startswith("lock "):
            mn = mn[5:]

        # x87 is decoded from raw opcode bytes (capstone's register forms are ambiguous)
        b = i.bytes
        k = 0
        while k < len(b) and b[k] in (0x9B, 0x66, 0xF2, 0xF3, 0x2E, 0x3E, 0x26, 0x36, 0x64, 0x65):
            k += 1
        if k < len(b) and 0xD8 <= b[k] <= 0xDF:
            return self.lift_x87(i, b[k], b[k + 1])
        if mn in ("wait", "fwait"):
            return []

        if mn == "nop":
            return []
        if mn == "mov":
            e(self.set(i, ops[0], self.get(i, ops[1])))
        elif mn in ("movzx", "movsx"):
            src = self.get(i, ops[1])
            if mn == "movsx":
                src = "(%s)(%s)%s" % (UT[ops[0].size], ST_[ops[1].size], src)
            e(self.set(i, ops[0], src))
        elif mn == "lea":
            e(self.set(i, ops[0], self.addr(i, ops[1])))
        elif mn == "push":
            v = self.get(i, ops[0], 4 if ops[0].type == x86.X86_OP_IMM else ops[0].size)
            if ops[0].type == x86.X86_OP_REG and i.reg_name(ops[0].reg) in ("cs", "ds", "es", "fs", "gs", "ss"):
                e("esp -= 4; M16(esp) = %s;" % v)  # a segment push moves esp by 4
            elif ops[0].size == 2 and ops[0].type != x86.X86_OP_IMM:
                e("{ uint16_t v_ = %s; esp -= 2; M16(esp) = v_; }" % v)
            else:
                if ops[0].type == x86.X86_OP_IMM:
                    v = "0x%Xu" % (ops[0].imm & 0xFFFFFFFF)
                e("{ uint32_t v_ = %s; esp -= 4; M32(esp) = v_; }" % v)
        elif mn == "pop":
            if ops[0].type == x86.X86_OP_REG and i.reg_name(ops[0].reg) == "esp":
                e("esp = M32(esp);")
            else:
                e("{ uint32_t v_ = M32(esp); esp += 4; %s }" % self.set(i, ops[0], "v_"))
        elif mn in ("add", "sub", "adc", "sbb", "cmp"):
            n = BITS[ops[0].size]
            fn = {"cmp": "sub"}.get(mn, mn)
            r = "%s%d(%s, %s, &f)" % (fn, n, self.get(i, ops[0]), self.get(i, ops[1], ops[0].size))
            if mn == "cmp":
                e("(void)%s;" % r)
            else:
                e(self.set(i, ops[0], r))
        elif mn in ("and", "or", "xor", "test"):
            n = BITS[ops[0].size]
            opc = {"and": "&", "test": "&", "or": "|", "xor": "^"}[mn]
            if mn == "xor" and ops[1].type == ops[0].type == x86.X86_OP_REG and ops[0].reg == ops[1].reg:
                e(self.set(i, ops[0], "logic%d(0, &f)" % n))
            else:
                r = "logic%d((%s)(%s %s %s), &f)" % (n, UT[ops[0].size], self.get(i, ops[0]), opc,
                                                     self.get(i, ops[1], ops[0].size))
                e("(void)%s;" % r if mn == "test" else self.set(i, ops[0], r))
        elif mn in ("inc", "dec", "neg"):
            n = BITS[ops[0].size]
            e(self.set(i, ops[0], "%s%d(%s, &f)" % (mn, n, self.get(i, ops[0]))))
        elif mn == "not":
            e(self.set(i, ops[0], "~%s" % self.get(i, ops[0])))
        elif mn in ("shl", "sal", "shr", "sar", "rol", "ror", "rcl", "rcr"):
            n = BITS[ops[0].size]
            cnt = self.get(i, ops[1], 1) if len(ops) > 1 else "1"
            fn = "shl" if mn == "sal" else mn
            e(self.set(i, ops[0], "%s%d(%s, %s, &f)" % (fn, n, self.get(i, ops[0]), cnt)))
        elif mn in ("shld", "shrd"):
            if ops[0].size != 4:
                raise Unsupported(mn + "16")
            e(self.set(i, ops[0], "%s32(%s, %s, %s, &f)" % (mn, self.get(i, ops[0]), self.get(i, ops[1]),
                                                             self.get(i, ops[2], 1))))
        elif mn == "imul":
            if len(ops) == 1:
                s = ops[0].size
                if s == 4:
                    e("{ int64_t r_ = (int64_t)(int32_t)eax * (int32_t)%s; eax = (uint32_t)r_; "
                      "edx = (uint32_t)(r_ >> 32); f.cf = f.of = r_ != (int32_t)r_; }" % self.get(i, ops[0]))
                elif s == 2:
                    e("{ int32_t r_ = (int16_t)eax * (int16_t)%s; %s %s f.cf = f.of = r_ != (int16_t)r_; }" % (
                        self.get(i, ops[0]), self.reg_set("ax", "r_"), self.reg_set("dx", "r_ >> 16")))
                else:
                    e("{ int32_t r_ = (int8_t)eax * (int8_t)%s; %s f.cf = f.of = r_ != (int8_t)r_; }" % (
                        self.get(i, ops[0]), self.reg_set("ax", "r_")))
            else:
                a, bb = (ops[0], ops[1]) if len(ops) == 2 else (ops[1], ops[2])
                s = ops[0].size
                e("{ int64_t r_ = (int64_t)(%s)%s * (%s)%s; %s f.cf = f.of = r_ != (%s)r_; }" % (
                    ST_[s], self.get(i, a), ST_[s], self.get(i, bb, s), self.set(i, ops[0], "r_"), ST_[s]))
        elif mn == "mul":
            s = ops[0].size
            if s == 4:
                e("{ uint64_t r_ = (uint64_t)eax * %s; eax = (uint32_t)r_; edx = (uint32_t)(r_ >> 32); "
                  "f.cf = f.of = edx != 0; }" % self.get(i, ops[0]))
            elif s == 2:
                e("{ uint32_t r_ = (uint16_t)eax * (uint32_t)%s; %s %s f.cf = f.of = (r_ >> 16) != 0; }" % (
                    self.get(i, ops[0]), self.reg_set("ax", "r_"), self.reg_set("dx", "r_ >> 16")))
            else:
                e("{ uint32_t r_ = (uint8_t)eax * (uint32_t)%s; %s f.cf = f.of = (r_ >> 8) != 0; }" % (
                    self.get(i, ops[0]), self.reg_set("ax", "r_")))
        elif mn in ("div", "idiv"):
            s = ops[0].size
            v = self.get(i, ops[0])
            if s == 4:
                if mn == "div":
                    e("{ uint64_t n_ = ((uint64_t)edx << 32) | eax; uint32_t d_ = %s; "
                      "eax = (uint32_t)(n_ / d_); edx = (uint32_t)(n_ %% d_); }" % v)
                else:
                    e("{ int64_t n_ = (int64_t)(((uint64_t)edx << 32) | eax); int32_t d_ = (int32_t)%s; "
                      "eax = (uint32_t)(int32_t)(n_ / d_); edx = (uint32_t)(int32_t)(n_ %% d_); }" % v)
            elif s == 2:
                t, st = ("uint32_t", "uint16_t") if mn == "div" else ("int32_t", "int16_t")
                e("{ %s n_ = (%s)((((uint32_t)(uint16_t)edx) << 16) | (uint16_t)eax); %s d_ = (%s)%s; %s %s }" % (
                    t, t, t, st, v, self.reg_set("ax", "n_ / d_"), self.reg_set("dx", "n_ % d_")))
            else:
                t, st = ("uint32_t", "uint8_t") if mn == "div" else ("int32_t", "int8_t")
                e("{ %s n_ = (%s)(%s)(uint16_t)eax; %s d_ = (%s)%s; %s %s }" % (
                    t, t, "uint16_t" if mn == "div" else "int16_t", t, st, v,
                    self.reg_set("al", "n_ / d_"), self.reg_set("ah", "n_ % d_")))
        elif mn == "cdq":
            e("edx = (uint32_t)((int32_t)eax >> 31);")
        elif mn == "cwde":
            e("eax = (uint32_t)(int32_t)(int16_t)eax;")
        elif mn == "cbw":
            e(self.reg_set("ax", "(int16_t)(int8_t)eax"))
        elif mn == "cwd":
            e(self.reg_set("dx", "(uint16_t)((int16_t)eax >> 15)"))
        elif mn == "xchg":
            a0, a1 = self.get(i, ops[0]), self.get(i, ops[1])
            e("{ %s t_ = %s; %s %s }" % (UT[ops[0].size], a0, self.set(i, ops[0], a1), self.set(i, ops[1], "t_")))
        elif mn.startswith("set"):
            e(self.set(i, ops[0], self.COND[mn[3:]]))
        elif mn.startswith("cmov"):
            e("if (%s) { %s }" % (self.COND[mn[4:]], self.set(i, ops[0], self.get(i, ops[1]))))
        elif mn in ("bt", "bts", "btr", "btc"):
            if ops[0].type != x86.X86_OP_REG:
                a = self.addr(i, ops[0])
                if ops[1].type == x86.X86_OP_REG:  # bit string addressing
                    r = self.get(i, ops[1], 4)
                    e("f.cf = (M32(%s + (uint32_t)(((int32_t)%s >> 5) * 4)) >> (%s & 31)) & 1;" % (a, r, r))
                else:
                    e("f.cf = (M32(%s) >> (%d & 31)) & 1;" % (a, ops[1].imm))
                if mn != "bt":
                    raise Unsupported(mn + " mem")
                return o
            v = self.get(i, ops[0])
            bit = "(%s & 31)" % self.get(i, ops[1], 4 if ops[1].type == x86.X86_OP_REG else 1)
            e("f.cf = (%s >> %s) & 1;" % (v, bit))
            if mn == "bts":
                e(self.set(i, ops[0], "%s | (1u << %s)" % (v, bit)))
            elif mn == "btr":
                e(self.set(i, ops[0], "%s & ~(1u << %s)" % (v, bit)))
            elif mn == "btc":
                e(self.set(i, ops[0], "%s ^ (1u << %s)" % (v, bit)))
        elif mn == "pushal":
            e("{ uint32_t s_ = esp; esp -= 32; M32(esp + 28) = eax; M32(esp + 24) = ecx; M32(esp + 20) = edx; "
              "M32(esp + 16) = ebx; M32(esp + 12) = s_; M32(esp + 8) = ebp; M32(esp + 4) = esi; M32(esp) = edi; }")
        elif mn == "popal":
            e("edi = M32(esp); esi = M32(esp + 4); ebp = M32(esp + 8); ebx = M32(esp + 16); edx = M32(esp + 20); "
              "ecx = M32(esp + 24); eax = M32(esp + 28); esp += 32;")
        elif mn == "xlatb":
            e(self.reg_set("al", "M8(ebx + (uint8_t)eax)"))
        elif mn == "leave":
            e("esp = ebp; ebp = M32(esp); esp += 4;")
        elif mn == "clc":
            e("f.cf = 0;")
        elif mn == "stc":
            e("f.cf = 1;")
        elif mn == "cmc":
            e("f.cf = !f.cf;")
        elif mn == "cld":
            e("c->df = 0;")
        elif mn == "std":
            e("c->df = 1;")
        elif mn == "sahf":
            e("{ uint32_t h_ = (eax >> 8) & 0xFF; f.cf = h_ & 1; f.pf = (h_ >> 2) & 1; f.zf = (h_ >> 6) & 1; "
              "f.sf = (h_ >> 7) & 1; }")
        elif mn == "lahf":
            e(self.reg_set("ah", "(f.cf | 2 | (f.pf << 2) | (f.zf << 6) | (f.sf << 7))"))
        elif mn == "pushfd":
            e("esp -= 4; M32(esp) = f.cf | 2 | (f.pf << 2) | (f.zf << 6) | (f.sf << 7) | (c->df << 10) | "
              "(f.of << 11) | 0x200 | c->eflags_hi;")
        elif mn == "popfd":
            e("{ uint32_t v_ = M32(esp); esp += 4; f.cf = v_ & 1; f.pf = (v_ >> 2) & 1; f.zf = (v_ >> 6) & 1; "
              "f.sf = (v_ >> 7) & 1; c->df = (v_ >> 10) & 1; f.of = (v_ >> 11) & 1; "
              "c->eflags_hi = v_ & 0x00200000u; }")
        elif mn == "cpuid":
            e("rt_cpuid(c, &eax, &ebx, &ecx, &edx);")
        elif mn == "rdtsc":
            e("{ uint64_t t_ = rt_rdtsc(); eax = (uint32_t)t_; edx = (uint32_t)(t_ >> 32); }")
        elif mn == "int3":
            e("%s rt_int3(c, 0x%Xu);" % (SPILL, i.address))
        elif mn == "int":
            e("%s rt_unimpl(c, 0x%Xu, \"int\");" % (SPILL, i.address))
        elif mn.split()[-1] in ("movsb", "movsw", "movsd", "stosb", "stosw", "stosd", "lodsb", "lodsd",
                                "cmpsb", "cmpsd", "cmpsw", "scasb", "scasd", "scasw", "lodsw") and \
                not (mn.endswith("movsd") and ops and ops[0].type == x86.X86_OP_REG):
            o.extend(self.lift_string(i))
        elif mn in ("loop", "jecxz"):
            raise Unsupported(mn)  # handled as terminators
        else:
            o.extend(self.lift_sse(i))
        return o

    def lift_string(self, i):
        mn = i.mnemonic.split()
        rep = mn[0] if len(mn) > 1 else None
        op = mn[-1]
        size = {"b": 1, "w": 2, "d": 4}[op[-1]]
        kind = op[:-1]
        T = UT[size]
        M = {1: "M8", 2: "M16", 4: "M32"}[size]
        step = "(c->df ? -%d : %d)" % (size, size)
        if kind == "movs":
            body = "%s(edi) = %s(esi); esi += %s; edi += %s;" % (M, M, step, step)
        elif kind == "stos":
            body = "%s(edi) = (%s)eax; edi += %s;" % (M, T, step)
        elif kind == "lods":
            body = "%s esi += %s;" % (self.reg_set({1: "al", 2: "ax", 4: "eax"}[size], M + "(esi)"), step)
        elif kind == "cmps":
            body = "(void)sub%d(%s(esi), %s(edi), &f); esi += %s; edi += %s;" % (8 * size, M, M, step, step)
        elif kind == "scas":
            body = "(void)sub%d((%s)eax, %s(edi), &f); edi += %s;" % (8 * size, T, M, step)
        else:
            raise Unsupported(op)
        if not rep:
            return [body]
        if kind in ("movs", "stos") and rep == "rep":
            if kind == "movs" and size == 4:
                return ["if (!c->df) { rt_movsd(ecx, edi, esi); edi += ecx * 4; esi += ecx * 4; ecx = 0; } "
                        "else while (ecx) { %s ecx--; }" % body]
            return ["while (ecx) { %s ecx--; }" % body]
        if rep in ("repe", "repz", "rep"):
            return ["while (ecx) { %s ecx--; if (!f.zf) break; }" % body]
        return ["while (ecx) { %s ecx--; if (f.zf) break; }" % body]

    # ---------------- x87 ----------------
    def lift_x87(self, i, op, modrm):
        mod = modrm >> 6
        reg = (modrm >> 3) & 7
        rm = modrm & 7
        o = []
        e = o.append
        ARITH = {0: "ST(0) + %s", 1: "ST(0) * %s", 4: "ST(0) - %s", 5: "%s - ST(0)", 6: "ST(0) / %s",
                 7: "%s / ST(0)"}
        if mod != 3:
            memop = next(x for x in i.operands if x.type == x86.X86_OP_MEM)
            a = self.addr(i, memop)
            if op in (0xD8, 0xDC, 0xDA, 0xDE):
                src = {0xD8: "(double)MF32(%s)", 0xDC: "MF64(%s)", 0xDA: "(double)(int32_t)M32(%s)",
                       0xDE: "(double)(int16_t)M16(%s)"}[op] % a
                if reg in ARITH:
                    e("ST(0) = %s;" % (ARITH[reg] % src))
                elif reg == 2:
                    e("fcompare(c, ST(0), %s);" % src)
                else:
                    e("fcompare(c, ST(0), %s); fpop(c);" % src)
            elif op == 0xD9:
                if reg == 0:
                    e("fpush(c, (double)MF32(%s));" % a)
                elif reg == 2:
                    e("MF32(%s) = (float)ST(0);" % a)
                elif reg == 3:
                    e("MF32(%s) = (float)ST(0); fpop(c);" % a)
                elif reg == 4:
                    e("fpu_env(c, %s, 1);" % a)
                elif reg == 5:
                    e("c->fpu_cw = M16(%s);" % a)
                elif reg == 6:
                    e("fpu_env(c, %s, 0);" % a)
                elif reg == 7:
                    e("M16(%s) = c->fpu_cw;" % a)
                else:
                    raise Unsupported("d9 /%d" % reg)
            elif op == 0xDB:
                if reg == 0:
                    e("fpush(c, (double)(int32_t)M32(%s));" % a)
                elif reg == 1:
                    e("M32(%s) = (uint32_t)(int32_t)trunc(ST(0)); fpop(c);" % a)
                elif reg == 2:
                    e("M32(%s) = (uint32_t)fist32(c, ST(0));" % a)
                elif reg == 3:
                    e("M32(%s) = (uint32_t)fist32(c, ST(0)); fpop(c);" % a)
                elif reg == 5:
                    e("fpush(c, f80_load(%s));" % a)
                elif reg == 7:
                    e("f80_store(%s, ST(0)); fpop(c);" % a)
                else:
                    raise Unsupported("db /%d" % reg)
            elif op == 0xDD:
                if reg == 0:
                    e("fpush(c, MF64(%s));" % a)
                elif reg == 1:
                    e("M64(%s) = (uint64_t)(int64_t)trunc(ST(0)); fpop(c);" % a)
                elif reg == 2:
                    e("MF64(%s) = ST(0);" % a)
                elif reg == 3:
                    e("MF64(%s) = ST(0); fpop(c);" % a)
                elif reg == 4:
                    e("fpu_env(c, %s, 3);" % a)
                elif reg == 6:
                    e("fpu_env(c, %s, 2);" % a)
                elif reg == 7:
                    e("M16(%s) = fstatus(c);" % a)
                else:
                    raise Unsupported("dd /%d" % reg)
            elif op == 0xDF:
                if reg == 0:
                    e("fpush(c, (double)(int16_t)M16(%s));" % a)
                elif reg == 1:
                    e("M16(%s) = (uint16_t)(int16_t)trunc(ST(0)); fpop(c);" % a)
                elif reg == 2:
                    e("M16(%s) = (uint16_t)fist16(c, ST(0));" % a)
                elif reg == 3:
                    e("M16(%s) = (uint16_t)fist16(c, ST(0)); fpop(c);" % a)
                elif reg == 5:
                    e("fpush(c, (double)(int64_t)M64(%s));" % a)
                elif reg == 7:
                    e("M64(%s) = (uint64_t)fist64(c, ST(0)); fpop(c);" % a)
                else:
                    raise Unsupported("df /%d" % reg)
            return o
        # register forms
        s = "ST(%d)" % rm
        if op == 0xD8:
            if reg in ARITH:
                e("ST(0) = %s;" % (ARITH[reg] % s))
            elif reg == 2:
                e("fcompare(c, ST(0), %s);" % s)
            else:
                e("fcompare(c, ST(0), %s); fpop(c);" % s)
        elif op == 0xD9:
            if reg == 0:
                e("{ double v_ = %s; fpush(c, v_); }" % s)
            elif reg == 1:
                e("{ double v_ = ST(0); ST(0) = %s; %s = v_; }" % (s, s))
            elif modrm == 0xD0:
                pass
            else:
                ONE = {
                    0xE0: "ST(0) = -ST(0);", 0xE1: "ST(0) = fabs(ST(0));",
                    0xE4: "fcompare(c, ST(0), 0.0);", 0xE5: "fxam(c);",
                    0xE8: "fpush(c, 1.0);", 0xE9: "fpush(c, 3.321928094887362347870);",
                    0xEA: "fpush(c, 1.442695040888963407360);", 0xEB: "fpush(c, M_PI);",
                    0xEC: "fpush(c, 0.301029995663981195214);", 0xED: "fpush(c, M_LN2);",
                    0xEE: "fpush(c, 0.0);",
                    0xF0: "ST(0) = exp2(ST(0)) - 1.0;",
                    0xF1: "ST(1) = ST(1) * log2(ST(0)); fpop(c);",
                    0xF2: "ST(0) = tan(ST(0)); fpush(c, 1.0); c->c2 = 0;",
                    0xF3: "ST(1) = atan2(ST(1), ST(0)); fpop(c);",
                    0xF5: "fprem(c, 1);", 0xF6: "c->top = (c->top - 1) & 7;",
                    0xF7: "c->top = (c->top + 1) & 7;", 0xF8: "fprem(c, 0);",
                    0xF9: "ST(1) = ST(1) * log2(ST(0) + 1.0); fpop(c);",
                    0xFA: "ST(0) = sqrt(ST(0));",
                    0xFB: "{ double v_ = ST(0); ST(0) = sin(v_); fpush(c, cos(v_)); c->c2 = 0; }",
                    0xFC: "ST(0) = fround(c, ST(0));", 0xFD: "ST(0) = ldexp(ST(0), (int)trunc(ST(1)));",
                    0xFE: "ST(0) = sin(ST(0)); c->c2 = 0;", 0xFF: "ST(0) = cos(ST(0)); c->c2 = 0;",
                }
                if modrm not in ONE:
                    raise Unsupported("d9 %02x" % modrm)
                e(ONE[modrm])
        elif op == 0xDA:
            if modrm == 0xE9:
                e("fcompare(c, ST(0), ST(1)); fpop(c); fpop(c);")
            elif reg in (0, 1, 2, 3):
                cond = ["f.cf", "f.zf", "(f.cf || f.zf)", "f.pf"][reg]
                e("if (%s) ST(0) = %s;" % (cond, s))
            else:
                raise Unsupported("da %02x" % modrm)
        elif op == 0xDB:
            if modrm == 0xE2:
                e("c->c0 = c->c1 = c->c2 = c->c3 = 0;")
            elif modrm == 0xE3:
                e("c->top = 0; c->fpu_cw = 0x37F; c->c0 = c->c1 = c->c2 = c->c3 = 0;")
            elif reg in (0, 1, 2, 3):
                cond = ["!f.cf", "!f.zf", "(!f.cf && !f.zf)", "!f.pf"][reg]
                e("if (%s) ST(0) = %s;" % (cond, s))
            elif reg in (5, 6):
                e("{ double a_ = ST(0), b_ = %s; if (a_ != a_ || b_ != b_) { f.zf = f.pf = f.cf = 1; } "
                  "else { f.zf = a_ == b_; f.pf = 0; f.cf = a_ < b_; } f.of = f.sf = 0; }" % s)
            else:
                raise Unsupported("db %02x" % modrm)
        elif op == 0xDC:
            DC = {0: "%s = %s + ST(0);", 1: "%s = %s * ST(0);", 4: "%s = ST(0) - %s;", 5: "%s = %s - ST(0);",
                  6: "%s = ST(0) / %s;", 7: "%s = %s / ST(0);"}
            if reg in DC:
                e(DC[reg] % (s, s))
            elif reg == 2:
                e("fcompare(c, ST(0), %s);" % s)
            else:
                e("fcompare(c, ST(0), %s); fpop(c);" % s)
        elif op == 0xDD:
            if reg == 0:
                pass  # ffree: tag only
            elif reg == 2:
                e("%s = ST(0);" % s)
            elif reg == 3:
                e("%s = ST(0); fpop(c);" % s)
            elif reg == 4:
                e("fcompare(c, ST(0), %s);" % s)
            elif reg == 5:
                e("fcompare(c, ST(0), %s); fpop(c);" % s)
            else:
                raise Unsupported("dd %02x" % modrm)
        elif op == 0xDE:
            DE = {0: "%s = %s + ST(0);", 1: "%s = %s * ST(0);", 4: "%s = ST(0) - %s;", 5: "%s = %s - ST(0);",
                  6: "%s = ST(0) / %s;", 7: "%s = %s / ST(0);"}
            if modrm == 0xD9:
                e("fcompare(c, ST(0), ST(1)); fpop(c); fpop(c);")
            elif reg in DE:
                e((DE[reg] % (s, s)) + " fpop(c);")
            else:
                raise Unsupported("de %02x" % modrm)
        elif op == 0xDF:
            if modrm == 0xE0:
                e(self.reg_set("ax", "fstatus(c)"))
            elif reg in (5, 6):
                e("{ double a_ = ST(0), b_ = %s; if (a_ != a_ || b_ != b_) { f.zf = f.pf = f.cf = 1; } "
                  "else { f.zf = a_ == b_; f.pf = 0; f.cf = a_ < b_; } f.of = f.sf = 0; fpop(c); }" % s)
            elif reg == 0:
                e("fpop(c);")
            else:
                raise Unsupported("df %02x" % modrm)
        return o

    # ---------------- SSE (subset; mostly CRT internals) ----------------
    def xmm(self, i, op):
        return "c->xmm[%d]" % (op.reg - x86.X86_REG_XMM0)

    def lift_mmx(self, i):
        mn = i.mnemonic
        ops = i.operands
        ismm = lambda op: op.type == x86.X86_OP_REG and x86.X86_REG_MM0 <= op.reg <= x86.X86_REG_MM7
        mmr = lambda op: "c->mm[%d]" % (op.reg - x86.X86_REG_MM0)

        def src64(op):
            if ismm(op):
                return mmr(op)
            if op.type == x86.X86_OP_MEM:
                return "M64(%s)" % self.addr(i, op)
            if op.type == x86.X86_OP_IMM:
                return "(uint64_t)%du" % (op.imm & 0xFF)
            raise Unsupported("mmx src")
        if mn in ("emms", "femms"):
            return ["c->top = 0;"]
        if mn == "movq":
            if ismm(ops[0]):
                return ["%s = %s;" % (mmr(ops[0]), src64(ops[1]))]
            return ["M64(%s) = %s;" % (self.addr(i, ops[0]), mmr(ops[1]))]
        if mn == "movd":
            if ismm(ops[0]):
                return ["%s = (uint64_t)%s;" % (mmr(ops[0]), self.get(i, ops[1], 4))]
            return [self.set(i, ops[0], "(uint32_t)%s" % mmr(ops[1]))]
        if mn in ("pxor", "por", "pand", "pandn"):
            d = mmr(ops[0])
            if mn == "pandn":
                return ["%s = ~%s & %s;" % (d, d, src64(ops[1]))]
            opc = {"pxor": "^", "por": "|", "pand": "&"}[mn]
            return ["%s %s= %s;" % (d, opc, src64(ops[1]))]
        if mn == "movntq":
            return ["M64(%s) = %s;" % (self.addr(i, ops[0]), mmr(ops[1]))]
        if mn in ("psllw", "pslld", "psllq", "psrlw", "psrld", "psrlq", "psraw", "psrad", "paddw", "psubw",
                  "paddsw", "psubsw", "pmulhw", "pmullw", "psubusw", "paddusw", "packuswb", "packsswb",
                  "punpcklbw", "punpckhbw", "punpcklwd", "punpckhwd", "punpckldq", "punpckhdq", "paddb",
                  "psubb", "paddd", "psubd", "paddusb", "psubusb", "pcmpeqb", "pcmpeqw", "pcmpgtw", "pavgb",
                  "pmaddwd"):
            d = mmr(ops[0])
            return ["%s = mmx_%s(%s, %s);" % (d, mn, d, src64(ops[1]))]
        raise Unsupported("mmx " + mn)

    def lift_sse(self, i):
        mn = i.mnemonic
        ops = i.operands
        o = []
        e = o.append
        if mn in ("emms", "femms") or any(op.type == x86.X86_OP_REG and x86.X86_REG_MM0 <= op.reg <= x86.X86_REG_MM7
                               for op in ops):
            return self.lift_mmx(i)
        if mn in ("prefetchnta", "prefetcht0", "prefetcht1", "prefetcht2", "sfence", "lfence", "mfence"):
            return []
        if mn == "movntps":
            return ["memcpy(MEM + %s, &%s, 16);" % (self.addr(i, ops[0]), self.xmm(i, ops[1]))]
        if mn == "bsr" or mn == "bsf":
            v = self.get(i, ops[1])
            fn = "31 - __builtin_clz" if mn == "bsr" else "__builtin_ctz"
            return ["{ uint32_t v_ = %s; f.zf = v_ == 0; if (v_) { %s } }" % (v, self.set(i, ops[0], "%s(v_)" % fn))]
        isx = lambda op: op.type == x86.X86_OP_REG and x86.X86_REG_XMM0 <= op.reg <= x86.X86_REG_XMM7

        def src128(op):
            if isx(op):
                return self.xmm(i, op)
            return "(*(Xmm *)(MEM + %s))" % self.addr(i, op)

        if mn in ("movaps", "movapd", "movdqa", "movups", "movupd", "movdqu"):
            if isx(ops[0]):
                e("%s = %s;" % (self.xmm(i, ops[0]), src128(ops[1])))
            else:
                e("memcpy(MEM + %s, &%s, 16);" % (self.addr(i, ops[0]), self.xmm(i, ops[1])))
        elif mn in ("movsd", "movq", "movlpd", "movlps") and len(ops) == 2:
            zero = mn in ("movsd", "movq")
            if isx(ops[0]) and isx(ops[1]):
                if mn == "movq":
                    e("%s.q[0] = %s.q[0]; %s.q[1] = 0;" % (self.xmm(i, ops[0]), self.xmm(i, ops[1]),
                                                         self.xmm(i, ops[0])))
                else:
                    e("%s.q[0] = %s.q[0];" % (self.xmm(i, ops[0]), self.xmm(i, ops[1])))
            elif isx(ops[0]):
                e("%s.q[0] = M64(%s);%s" % (self.xmm(i, ops[0]), self.addr(i, ops[1]),
                                            (" %s.q[1] = 0;" % self.xmm(i, ops[0])) if zero else ""))
            else:
                e("M64(%s) = %s.q[0];" % (self.addr(i, ops[0]), self.xmm(i, ops[1])))
        elif mn == "movss":
            if isx(ops[0]) and isx(ops[1]):
                e("%s.u[0] = %s.u[0];" % (self.xmm(i, ops[0]), self.xmm(i, ops[1])))
            elif isx(ops[0]):
                x = self.xmm(i, ops[0])
                e("%s.u[0] = M32(%s); %s.u[1] = %s.u[2] = %s.u[3] = 0;" % (x, self.addr(i, ops[1]), x, x, x))
            else:
                e("M32(%s) = %s.u[0];" % (self.addr(i, ops[0]), self.xmm(i, ops[1])))
        elif mn == "movd":
            if isx(ops[0]):
                x = self.xmm(i, ops[0])
                e("%s.u[0] = %s; %s.u[1] = %s.u[2] = %s.u[3] = 0;" % (x, self.get(i, ops[1]), x, x, x))
            else:
                e(self.set(i, ops[0], "%s.u[0]" % self.xmm(i, ops[1])))
        elif mn in ("addsd", "subsd", "mulsd", "divsd", "addss", "subss", "mulss", "divss"):
            opc = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[mn[:3]]
            fld, sz = ("d", 8) if mn.endswith("sd") else ("f", 4)
            src = ("%s.%s[0]" % (self.xmm(i, ops[1]), fld)) if isx(ops[1]) else \
                ("MF%d(%s)" % (sz * 8, self.addr(i, ops[1])))
            x = self.xmm(i, ops[0])
            e("%s.%s[0] = %s.%s[0] %s %s;" % (x, fld, x, fld, opc, src))
        elif mn in ("sqrtsd", "sqrtss"):
            fld, sz = ("d", 8) if mn.endswith("sd") else ("f", 4)
            src = ("%s.%s[0]" % (self.xmm(i, ops[1]), fld)) if isx(ops[1]) else \
                ("MF%d(%s)" % (sz * 8, self.addr(i, ops[1])))
            e("%s.%s[0] = %s(%s);" % (self.xmm(i, ops[0]), fld, "sqrt" if fld == "d" else "sqrtf", src))
        elif mn in ("addpd", "subpd", "mulpd", "divpd", "addps", "subps", "mulps", "divps"):
            opc = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[mn[:3]]
            fld, n = ("d", 2) if mn.endswith("pd") else ("f", 4)
            x = self.xmm(i, ops[0])
            e("{ Xmm s_ = %s; for (int k_ = 0; k_ < %d; k_++) %s.%s[k_] %s= s_.%s[k_]; }" % (
                src128(ops[1]), n, x, fld, opc, fld))
        elif mn in ("andpd", "andps", "pand", "orpd", "orps", "por", "xorpd", "xorps", "pxor", "andnpd",
                    "andnps", "pandn"):
            x = self.xmm(i, ops[0])
            if mn.startswith("andn") or mn == "pandn":
                e("{ Xmm s_ = %s; %s.q[0] = ~%s.q[0] & s_.q[0]; %s.q[1] = ~%s.q[1] & s_.q[1]; }" % (
                    src128(ops[1]), x, x, x, x))
            else:
                opc = "&" if "and" in mn else "^" if "xor" in mn else "|"
                e("{ Xmm s_ = %s; %s.q[0] %s= s_.q[0]; %s.q[1] %s= s_.q[1]; }" % (src128(ops[1]), x, opc, x, opc))
        elif mn in ("cvttsd2si", "cvtsd2si"):
            src = ("%s.d[0]" % self.xmm(i, ops[1])) if isx(ops[1]) else "MF64(%s)" % self.addr(i, ops[1])
            fn = "trunc" if mn == "cvttsd2si" else "nearbyint"
            e(self.set(i, ops[0], "(uint32_t)(int32_t)%s(%s)" % (fn, src)))
        elif mn == "cvtsi2sd":
            e("%s.d[0] = (double)(int32_t)%s;" % (self.xmm(i, ops[0]), self.get(i, ops[1])))
        elif mn in ("comisd", "ucomisd"):
            src = ("%s.d[0]" % self.xmm(i, ops[1])) if isx(ops[1]) else "MF64(%s)" % self.addr(i, ops[1])
            e("{ double a_ = %s.d[0], b_ = %s; if (a_ != a_ || b_ != b_) { f.zf = f.pf = f.cf = 1; } "
              "else { f.zf = a_ == b_; f.pf = 0; f.cf = a_ < b_; } f.of = f.sf = 0; }" % (self.xmm(i, ops[0]), src))
        elif mn in ("stmxcsr", "ldmxcsr"):
            a = self.addr(i, ops[0])
            e("M32(%s) = c->mxcsr;" % a if mn == "stmxcsr" else "c->mxcsr = M32(%s);" % a)
        elif mn in ("unpcklpd", "unpckhpd", "movlhps", "movhlps"):
            x = self.xmm(i, ops[0])
            s = src128(ops[1])
            body = {"unpcklpd": "%s.q[1] = s_.q[0];" % x,
                    "unpckhpd": "%s.q[0] = %s.q[1]; %s.q[1] = s_.q[1];" % (x, x, x),
                    "movlhps": "%s.q[1] = s_.q[0];" % x,
                    "movhlps": "%s.q[0] = s_.q[1];" % x}[mn]
            e("{ Xmm s_ = %s; %s }" % (s, body))
        elif mn == "shufps":
            x = self.xmm(i, ops[0])
            imm = ops[2].imm & 0xFF
            e("{ Xmm d_ = %s, s_ = %s; %s.u[0] = d_.u[%d]; %s.u[1] = d_.u[%d]; %s.u[2] = s_.u[%d]; "
              "%s.u[3] = s_.u[%d]; }" % (x, src128(ops[1]), x, imm & 3, x, (imm >> 2) & 3, x, (imm >> 4) & 3,
                                         x, (imm >> 6) & 3))
        elif mn == "pshufd":
            x = self.xmm(i, ops[0])
            imm = ops[2].imm & 0xFF
            e("{ Xmm s_ = %s; %s" % (src128(ops[1]), "".join(
                "%s.u[%d] = s_.u[%d]; " % (x, k, (imm >> (2 * k)) & 3) for k in range(4))) + "}")
        elif mn in ("psrlq", "psllq") and ops[1].type == x86.X86_OP_IMM:
            x = self.xmm(i, ops[0])
            n = ops[1].imm
            sh = ">>" if mn == "psrlq" else "<<"
            if n > 63:
                e("%s.q[0] = %s.q[1] = 0;" % (x, x))
            else:
                e("%s.q[0] %s= %d; %s.q[1] %s= %d;" % (x, sh, n, x, sh, n))
        elif mn == "pextrw":
            e(self.set(i, ops[0], "%s.w[%d]" % (self.xmm(i, ops[1]), ops[2].imm & 7)))
        elif mn == "pinsrw":
            e("%s.w[%d] = (uint16_t)%s;" % (self.xmm(i, ops[0]), ops[2].imm & 7, self.get(i, ops[1], 2)))
        elif mn == "cvtdq2pd":
            x = self.xmm(i, ops[0])
            e("{ Xmm s_ = %s; %s.d[0] = s_.i[0]; %s.d[1] = s_.i[1]; }" % (src128(ops[1]), x, x))
        elif mn in ("psubd", "paddq", "psubq", "pcmpeqd", "pcmpgtd"):
            x = self.xmm(i, ops[0])
            body = {"psubd": "for (int k_ = 0; k_ < 4; k_++) %s.u[k_] -= s_.u[k_];",
                    "paddq": "for (int k_ = 0; k_ < 2; k_++) %s.q[k_] += s_.q[k_];",
                    "psubq": "for (int k_ = 0; k_ < 2; k_++) %s.q[k_] -= s_.q[k_];",
                    "pcmpeqd": "for (int k_ = 0; k_ < 4; k_++) %s.u[k_] = %s.u[k_] == s_.u[k_] ? ~0u : 0;",
                    "pcmpgtd": "for (int k_ = 0; k_ < 4; k_++) %s.u[k_] = %s.i[k_] > s_.i[k_] ? ~0u : 0;"}[mn]
            e("{ Xmm s_ = %s; %s }" % (src128(ops[1]), body.replace("%s", x)))
        elif mn == "pmovmskb":
            x = self.xmm(i, ops[1])
            e(self.set(i, ops[0], "({ uint32_t m_ = 0; for (int k_ = 0; k_ < 16; k_++) m_ |= "
                                  "(uint32_t)(%s.b[k_] >> 7) << k_; m_; })" % x))
        elif mn in ("movhps", "movhpd"):
            if isx(ops[0]):
                e("%s.q[1] = M64(%s);" % (self.xmm(i, ops[0]), self.addr(i, ops[1])))
            else:
                e("M64(%s) = %s.q[1];" % (self.addr(i, ops[0]), self.xmm(i, ops[1])))
        elif mn in ("unpcklps", "unpckhps"):
            x = self.xmm(i, ops[0])
            h = 2 if mn == "unpckhps" else 0
            e("{ Xmm d_ = %s, s_ = %s; %s.u[0] = d_.u[%d]; %s.u[1] = s_.u[%d]; %s.u[2] = d_.u[%d]; "
              "%s.u[3] = s_.u[%d]; }" % (x, src128(ops[1]), x, h, x, h, x, h + 1, x, h + 1))
        elif mn == "shufpd":
            x = self.xmm(i, ops[0])
            imm = ops[2].imm & 3
            e("{ Xmm d_ = %s, s_ = %s; %s.q[0] = d_.q[%d]; %s.q[1] = s_.q[%d]; }" % (
                x, src128(ops[1]), x, imm & 1, x, imm >> 1))
        elif mn in ("rsqrtss", "rcpss", "rsqrtps", "rcpps", "sqrtps"):
            x = self.xmm(i, ops[0])
            fn = {"rsqrt": "1.0f / sqrtf(%s)", "rcp": "1.0f / (%s)", "sqrt": "sqrtf(%s)"}[mn[:-2]]
            if mn.endswith("ss"):
                src = ("%s.f[0]" % self.xmm(i, ops[1])) if isx(ops[1]) else "MF32(%s)" % self.addr(i, ops[1])
                e("%s.f[0] = %s;" % (x, fn % src))
            else:
                e("{ Xmm s_ = %s; for (int k_ = 0; k_ < 4; k_++) %s.f[k_] = %s; }" % (
                    src128(ops[1]), x, fn % "s_.f[k_]"))
        elif mn in ("comiss", "ucomiss"):
            src = ("%s.f[0]" % self.xmm(i, ops[1])) if isx(ops[1]) else "MF32(%s)" % self.addr(i, ops[1])
            e("{ float a_ = %s.f[0], b_ = %s; if (a_ != a_ || b_ != b_) { f.zf = f.pf = f.cf = 1; } "
              "else { f.zf = a_ == b_; f.pf = 0; f.cf = a_ < b_; } f.of = f.sf = 0; }" % (self.xmm(i, ops[0]), src))
        elif mn.startswith("cmp") and mn[-2:] in ("ps", "pd", "ss", "sd") and mn[3:-2] in CMP_PRED:
            x = self.xmm(i, ops[0])
            kind = mn[-2:]
            fld, n = ("d", 2) if kind[1] == "d" else ("f", 4)
            if kind[0] == "s":
                n = 1
            if kind[0] == "s" and not isx(ops[1]):
                src = "(*(Xmm *)(MEM + %s))" % self.addr(i, ops[1])
                src = "({ Xmm t_ = {0}; t_.%s[0] = MF%d(%s); t_; })" % (fld, 64 if fld == "d" else 32,
                                                                       self.addr(i, ops[1]))
            else:
                src = src128(ops[1])
            ufld = "q" if fld == "d" else "u"
            ones = "~0ull" if fld == "d" else "~0u"
            pred = CMP_PRED[mn[3:-2]]
            e("{ Xmm s_ = %s; for (int k_ = 0; k_ < %d; k_++) { %s a_ = %s.%s[k_], b_ = s_.%s[k_]; "
              "%s.%s[k_] = (%s) ? %s : 0; } }" % (src, n, "double" if fld == "d" else "float", x, fld, fld,
                                                 x, ufld, pred, ones))
        elif mn in ("psllq", "psrlq", "pslld", "psrld", "psrad", "psllw", "psrlw", "psraw"):
            x = self.xmm(i, ops[0])
            cnt = "%d" % ops[1].imm if ops[1].type == x86.X86_OP_IMM else "(%s.q[0] > 64 ? 64 : (int)%s.q[0])" % (
                src128(ops[1]), src128(ops[1]))
            lane, n, bits = {"q": ("q", 2, 64), "d": ("u", 4, 32), "w": ("w", 8, 16)}[mn[-1]]
            if mn[3] == "a":
                st = "int32_t" if lane == "u" else "int16_t"
                e("{ int n_ = %s; if (n_ > %d) n_ = %d; for (int k_ = 0; k_ < %d; k_++) %s.%s[k_] = "
                  "(%s)((%s)%s.%s[k_] >> n_); }" % (cnt, bits - 1, bits - 1, n, x, lane,
                                                    "uint32_t" if lane == "u" else "uint16_t", st, x, lane))
            else:
                sh = "<<" if mn[2] == "l" else ">>"
                e("{ int n_ = %s; for (int k_ = 0; k_ < %d; k_++) %s.%s[k_] = n_ >= %d ? 0 : %s.%s[k_] %s n_; }" % (
                    cnt, n, x, lane, bits, x, lane, sh))
        elif mn in ("paddd", "paddw", "psubw", "pmaxsw", "pminsw", "pcmpeqw", "pmullw"):
            x = self.xmm(i, ops[0])
            body = {"paddd": "for (int k_ = 0; k_ < 4; k_++) X.u[k_] += s_.u[k_];",
                    "paddw": "for (int k_ = 0; k_ < 8; k_++) X.w[k_] += s_.w[k_];",
                    "psubw": "for (int k_ = 0; k_ < 8; k_++) X.w[k_] -= s_.w[k_];",
                    "pmullw": "for (int k_ = 0; k_ < 8; k_++) X.w[k_] = (uint16_t)((int16_t)X.w[k_] * (int16_t)s_.w[k_]);",
                    "pmaxsw": "for (int k_ = 0; k_ < 8; k_++) if ((int16_t)s_.w[k_] > (int16_t)X.w[k_]) X.w[k_] = s_.w[k_];",
                    "pminsw": "for (int k_ = 0; k_ < 8; k_++) if ((int16_t)s_.w[k_] < (int16_t)X.w[k_]) X.w[k_] = s_.w[k_];",
                    "pcmpeqw": "for (int k_ = 0; k_ < 8; k_++) X.w[k_] = X.w[k_] == s_.w[k_] ? 0xFFFF : 0;"}[mn]
            e("{ Xmm s_ = %s; %s }" % (src128(ops[1]), body.replace("X.", x + ".")))
        elif mn in ("punpcklwd", "punpckhwd", "punpckldq", "punpckhdq", "punpcklbw", "punpckhbw", "punpcklqdq",
                    "punpckhqdq"):
            x = self.xmm(i, ops[0])
            lane, n = {"bw": ("b", 16), "wd": ("w", 8), "dq": ("u", 4), "qdq": ("q", 2)}[
                "qdq" if mn.endswith("qdq") else mn[-2:]]
            h = n // 2 if mn[5] == "h" else 0
            e("{ Xmm d_ = %s, s_ = %s; for (int k_ = 0; k_ < %d; k_++) { %s.%s[2 * k_] = d_.%s[%d + k_]; "
              "%s.%s[2 * k_ + 1] = s_.%s[%d + k_]; } }" % (x, src128(ops[1]), n // 2, x, lane, lane, h, x, lane,
                                                          lane, h))
        elif mn == "packssdw":
            x = self.xmm(i, ops[0])
            e("{ Xmm d_ = %s, s_ = %s; for (int k_ = 0; k_ < 4; k_++) { int32_t a_ = d_.i[k_], b_ = s_.i[k_]; "
              "%s.w[k_] = (uint16_t)(a_ > 32767 ? 32767 : a_ < -32768 ? -32768 : a_); "
              "%s.w[k_ + 4] = (uint16_t)(b_ > 32767 ? 32767 : b_ < -32768 ? -32768 : b_); } }" % (
                  x, src128(ops[1]), x, x))
        elif mn == "cvtdq2ps":
            x = self.xmm(i, ops[0])
            e("{ Xmm s_ = %s; for (int k_ = 0; k_ < 4; k_++) %s.f[k_] = (float)s_.i[k_]; }" % (src128(ops[1]), x))
        else:
            e("%s rt_unimpl(c, 0x%Xu, \"%s\");" % (SPILL, i.address, mn))
        return o

    # ---------------- function ----------------
    def emit(self):
        o = self.out
        # A function replaced by host code keeps its guest body as f_X_guest.
        o.append("void f_%X%s(CPU *c) {" % (self.f, "_guest" if self.f in HOST_OVERRIDES.get(MOD, ()) else ""))
        o.append("    uint32_t eax = c->r[0], ecx = c->r[1], edx = c->r[2], ebx = c->r[3], "
                 "esp = c->r[4], ebp = c->r[5], esi = c->r[6], edi = c->r[7];")
        o.append("    Flags f = {0};")
        o.append("    uint32_t jt_ = 0;")
        o.append("    (void)f; (void)jt_;")
        order = sorted(self.blocks)
        if order[0][0] != self.f:
            o.append("    goto L_%X;" % self.f)
        for bi, (a, b) in enumerate(order):
            o.append("L_%X:" % a)
            insns = decode_block(a, b)
            if not insns or insns[-1].address + insns[-1].size != b:
                raise Unsupported("decode %X" % a)
            for i in insns:
                last = i is insns[-1]
                nxt = i.address + i.size
                lines = self.lift_terminator(i, nxt) if self.is_term(i) else None
                if lines is None:
                    lines = self.lift(i)
                for line in lines:
                    o.append("    " + line)
                if last and not self.is_term(i):
                    # fall through into the next block
                    if bi + 1 < len(order) and order[bi + 1][0] == nxt:
                        pass
                    else:
                        o.append("    " + self.goto(nxt))
        if self.need_dispatch:
            cases = " ".join("case 0x%Xu: goto L_%X;" % (b, b) for b in sorted(self.block_starts))
            o.append("L_dispatch:")
            o.append("    switch (jt_) { %s default: %s rt_call(c, jt_); return; }" % (cases, SPILL))
        o.append("}")
        return o

    def is_term(self, i):
        g = set(i.groups)
        return i.mnemonic in ("call", "loop", "jecxz") or x86.X86_GRP_JUMP in g or x86.X86_GRP_RET in g

    def lift_terminator(self, i, nxt):
        mn = i.mnemonic
        op = i.operands[0] if i.operands else None
        if mn == "call":
            direct = op.type == x86.X86_OP_IMM
            tgt = (op.imm if direct else op.mem.disp) & 0xFFFFFFFF
            if (direct or (op.type == x86.X86_OP_MEM and not op.mem.base and not op.mem.index)) and \
                    import_via(tgt).endswith("!_setjmp3"):
                # setjmp must run in this C frame so a later longjmp can return here
                return ["{ esp -= 4; M32(esp) = 0x%Xu; %s if (setjmp(*rt_setjmp_slot(c, M32(esp + 4)))) { %s } "
                        "else { rt_setjmp3(c); %s } }" % (nxt, SPILL, RELOAD, RELOAD)]
            if direct:
                return [self.call_direct(op.imm & 0xFFFFFFFF, nxt)]
            t = self.get(i, op, 4)
            return ["{ uint32_t t_ = %s; esp -= 4; M32(esp) = 0x%Xu; %s rt_call(c, t_); %s }" % (
                t, nxt, SPILL, RELOAD)]
        if x86.X86_GRP_RET in i.groups:
            n = op.imm if op else 0
            return ["esp += %d; %s return;" % (4 + n, SPILL)]
        if mn == "jmp":
            if op.type == x86.X86_OP_IMM:
                return [self.goto(op.imm & 0xFFFFFFFF)]
            if i.address in jump_tables:
                return [self.indirect_jump(i, self.get(i, op, 4))]
            if op.type == x86.X86_OP_MEM and not op.mem.base and not op.mem.index and \
                    (op.mem.disp & 0xFFFFFFFF) in iat:
                return ["{ %s rt_call(c, M32(0x%Xu)); return; }" % (SPILL, op.mem.disp & 0xFFFFFFFF)]
            return [self.indirect_jump(i, self.get(i, op, 4))]
        if mn == "loop":
            return ["if (--ecx) %s" % self.goto(op.imm), self.goto(nxt)]
        if mn == "jecxz":
            return ["if (!ecx) %s" % self.goto(op.imm), self.goto(nxt)]
        if mn.startswith("j"):
            cond = self.COND[mn[1:]]
            return ["if (%s) %s" % (cond, self.goto(op.imm & 0xFFFFFFFF)), self.goto(nxt)]
        raise Unsupported(mn)


def write_if_changed(path, text):
    try:
        if open(path).read() == text:
            return
    except OSError:
        pass
    open(path, "w").write(text)


def main():
    os.makedirs(OUT, exist_ok=True)
    funcs = {int(k, 16): [tuple(b) for b in v] for k, v in data["functions"].items()}
    # Pass 1: lift; drop functions that contain junk (data decoded as code).
    good = {}
    failed = {}
    all_funcs = set(funcs)
    for rnd in range(3):
        good.clear()
        for fa, blocks in sorted(funcs.items()):
            if fa in failed:
                continue
            try:
                L = Lifter(fa, blocks, all_funcs)
                good[fa] = (L.emit(), L.calls)
            except Unsupported as ex:
                failed[fa] = str(ex)
        new_all = set(good)
        if new_all == all_funcs:
            break
        all_funcs = new_all
    reasons = {}
    for v in failed.values():
        k = v.split()[0] + " " + v.split()[1] if len(v.split()) > 1 else v
        reasons[k] = reasons.get(k, 0) + 1
    print("lifted", len(good), "rejected", len(failed))
    print(sorted(reasons.items(), key=lambda x: -x[1])[:30])

    names = sorted(good)
    write_if_changed(os.path.join(OUT, "funcs_%s.h" % MOD),
                     '#pragma once\n#include "x86.h"\n' + "".join("void f_%X(CPU *c);\n" % fa for fa in names))
    # Bucket by address so that local changes only touch a few files.
    lo, hi = names[0], names[-1] + 1
    buckets = [[] for _ in range(FILES)]
    for fa in names:
        buckets[(fa - lo) * FILES // (hi - lo)].append(fa)
    for k in range(FILES):
        chunk = buckets[k]
        text = '#include "funcs_%s.h"\n#include "lift_prelude.h"\n\n' % MOD
        text += "".join("\n".join(good[fa][0]) + "\n\n" for fa in chunk)
        write_if_changed(os.path.join(OUT, "%s_%02d.c" % (MOD, k)), text)
    text = '#include "funcs_%s.h"\n#include "rt_table.h"\nconst FnEntry g_fn_table_%s[] = {\n' % (MOD, MOD)
    text += "".join("    {0x%Xu, f_%X},\n" % (fa, fa) for fa in names)
    text += "};\nconst unsigned g_fn_count_%s = %d;\n" % (MOD, len(names))
    write_if_changed(os.path.join(OUT, "%s_table.c" % MOD), text)
    json.dump({hex(k): v for k, v in failed.items()}, open(os.path.join(OUT, "%s_rejected.json" % MOD), "w"), indent=0)


if __name__ == "__main__":
    main()
