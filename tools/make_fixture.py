#!/usr/bin/env python3
"""make_fixture.py - build a synthetic, Uriel-shaped 32-bit PE for offline tests.

The real client cannot be redistributed, so this writes a small executable
that has the *on-disk layout* documented in docs/01 and docs/02 - exactly the
things `tools/unuriel.py derive` / `rebuild` and `tools/ksattack.py` look at:

  .text    N pages of pseudo-x86 (realistic byte distribution: ~12% 0x00,
           CC padding between functions, 55 8B EC prologues, C3/C2 returns,
           E8 rel32 calls to in-image functions, 68 imm32 pushes, FF 15 calls
           through the IAT) XORed with one random 4096-byte key tiled per page;
           IMAGE_SCN_MEM_EXECUTE cleared. It contains one CRT-shaped entry:
           CC, then E8 <__security_init_cookie> E9 <__scrt_common_main_seh>,
           where the callee holds the single `mov [__security_cookie], ecx`
           and is called exactly once, and the jmp target is never called.
  .rdata   the ORIGINAL import address table (data directory 12): one
           NUL-terminated thunk array per DLL, in the linker's sorted-by-DLL
           order, each named slot pointing at a hint/name entry whose 16-bit
           "hint" is the name LENGTH and whose bytes are name[i] ^ key[i];
           ordinal slots are 0x80000000 | ordinal. Plus a load config
           directory (data directory 10) whose SecurityCookie (+0x3C) points
           into .data.
  .data    the security cookie global.
  .reloc   a real base-relocation table for every absolute imm32 in .text.
  xyz      an injected section with a random three-letter name: a NOP sled
           (AddressOfEntryPoint points at it) and the replacement import
           directory (data directory 1): a single descriptor importing
           FireInTheHole from client_x86.dll, with PLAIN names, whose
           FirstThunk points back at the original client_x86 slot in .rdata.

Alongside <out.exe> it writes <out>.truth.json: the key, the OEP RVA, the
sha256 of the plaintext .text, and the slot -> (dll, name) map, so a test can
check every stage of the unpacker against ground truth.

    python3 tools/make_fixture.py fixture.exe [--pages 600] [--seed 1]
                                              [--plain] [--wrong-name-key]

  --plain           write .text in the clear (everything else identical) -
                    the ground-truth twin `tools/ksattack.py` scores against.
  --wrong-name-key  obfuscate the import names with a key that is NOT the
                    .text key, so `derive` must reject the decoded names.

This is the file format only. Nothing here reproduces the protector's runtime
(the VEH that decrypts pages lazily, the sled patching, the IAT filling), and
the "code" only has to look like x86 statistically; it does not run.
"""
import argparse
import base64
import hashlib
import json
import os
import random
import struct
import sys

PAGE = 0x1000
FILE_ALIGN = 0x200
SEC_ALIGN = 0x1000
IMAGE_BASE = 0x400000
SIZE_OF_HEADERS = 0x400
E_LFANEW = 0x80
OPT_SIZE = 0xE0

HERE = os.path.dirname(os.path.abspath(__file__))
IMPORTS_DB = os.path.join(HERE, "imports_db.json")

CNT_CODE = 0x00000020
CNT_INIT = 0x00000040
DISCARDABLE = 0x02000000
MEM_EXECUTE = 0x20000000
MEM_READ = 0x40000000
MEM_WRITE = 0x80000000

PROTECTOR_DLL = "client_x86.dll"
PROTECTOR_FUNC = "FireInTheHole"
# A name the lookup table has never seen: derive must attribute it by
# inheritance from its group (it sits in the KERNEL32 run).
UNKNOWN_NAME = "UrielFixtureUnknownApiW"
# The name whose obfuscated form contains a NUL: key[NUL_INDEX] is forced to
# equal NUL_NAME[NUL_INDEX], so stopping at the first zero byte would truncate.
NUL_NAME = "CloseHandle"
NUL_INDEX = 2

COOKIE_INIT = 0xBB40E64E


def align(v, a):
    return (v + a - 1) // a * a


def le32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


# ---------------------------------------------------------------- imports
def pick_imports(rng, db):
    """Return the ordered list of (dll, [entry...]) groups.

    entry = {"name": str} for a named import, {"ordinal": n} otherwise.
    Groups are in the linker's order: sorted by DLL name (bytes order, so
    uppercase names come before lowercase ones), names sorted within a group.
    """
    names_db = db["names"]
    by_dll = {}
    for nm, dll in names_db.items():
        by_dll.setdefault(dll, []).append(nm)
    for dll in by_dll:
        by_dll[dll].sort()

    def sample(dll, n, must=()):
        pool = [x for x in by_dll[dll] if x not in must]
        chosen = set(must) | set(rng.sample(pool, n - len(must)))
        return sorted(chosen)

    groups = [
        ("ADVAPI32.dll", [{"name": n} for n in sample("ADVAPI32.dll", 5)]),
        ("KERNEL32.dll", [{"name": n} for n in
                          sorted(sample("KERNEL32.dll", 14, must=(NUL_NAME,)) + [UNKNOWN_NAME])]),
        # A group holding only ordinal #6: fits both OLEAUT32 (SysFreeString)
        # and WS2_32 (getsockname); the sorted-descriptor tie-break decides.
        ("OLEAUT32.dll", [{"ordinal": 6}]),
        ("USER32.dll", [{"name": n} for n in sample("USER32.dll", 9)]),
        ("WS2_32.dll", [{"ordinal": o} for o in (2, 6, 9, 23, 115, 116, 151)]),
        (PROTECTOR_DLL, [{"name": PROTECTOR_FUNC}]),
    ]
    order = [g[0].encode() for g in groups]
    assert order == sorted(order), "groups must be in linker (sorted) order"
    return groups


def build_rdata(groups, key, name_key, rdata_rva, cookie_va):
    """Lay out .rdata: IAT first (as in the real builds), then the hint/name
    entries, then the load config directory, then a couple of strings.
    Returns (bytes, info)."""
    nslots = sum(len(g[1]) for g in groups) + len(groups)
    iat_size = 4 * nslots
    blob = bytearray(b"\0" * iat_size)
    slots = []                        # (rva, dll, entry)
    slot_rva = rdata_rva
    hn_offsets = {}
    for dll, entries in groups:
        for e in entries:
            if "ordinal" in e:
                struct.pack_into("<I", blob, slot_rva - rdata_rva, 0x80000000 | e["ordinal"])
            else:
                if len(blob) & 1:
                    blob.append(0)
                nm = e["name"].encode("latin1")
                cipher = bytes(b ^ name_key[i] for i, b in enumerate(nm))
                hn_offsets[e["name"]] = len(blob)
                blob += struct.pack("<H", len(nm)) + cipher + b"\0"   # hint = LENGTH
                struct.pack_into("<I", blob, slot_rva - rdata_rva, rdata_rva + hn_offsets[e["name"]])
            slots.append((slot_rva, dll, e))
            slot_rva += 4
        slot_rva += 4                                                 # NUL terminator
    while len(blob) & 3:
        blob.append(0)
    lc_off = len(blob)
    lc = bytearray(0x5C)
    struct.pack_into("<I", lc, 0, 0x5C)                               # Size
    struct.pack_into("<I", lc, 0x3C, cookie_va)                       # SecurityCookie
    blob += lc
    while len(blob) & 0xF:
        blob.append(0)
    blob += b"Uriel unpacker test fixture - not a real client\0"
    blob += b"\0" * (16 - len(blob) % 16)
    blob += b"D3DX9_43.dll\0OpenAL32.dll\0"
    return bytes(blob), {
        "iat_size": iat_size,
        "slots": slots,
        "hint_name": hn_offsets,
        "loadconfig_off": lc_off,
    }


# ---------------------------------------------------------------- pseudo-x86
class Emitter:
    """Generates a stream of MSVC-looking functions with a realistic byte
    distribution. Records where E8 rel32 calls sit (patched to in-image
    targets afterwards) and where absolute imm32 addresses sit (for .reloc)."""

    def __init__(self, rng, tbase, tsize, image_vas, data_vas, iat_vas):
        self.rng = rng
        self.tbase = tbase          # VA of .text
        self.tsize = tsize
        self.image_vas = image_vas  # (lo, hi) range for push imm32
        self.data_vas = data_vas    # list of global VAs for A1 / 8B 0D / 89 0D
        self.iat_vas = iat_vas      # list of IAT slot VAs for FF 15
        self.buf = bytearray()
        self.calls = []             # offsets of E8 needing a target
        self.relocs = []            # offsets of absolute imm32
        self.funcs = []             # start offsets of ordinary functions
        disp = [0x08, 0x0C, 0x10, 0x14, 0x18, 0xF8, 0xF4, 0xF0, 0xEC, 0xFC, 0xE8, 0xE0]
        regs_ebp = [0x45, 0x4D, 0x55, 0x5D, 0x75, 0x7D]
        rr = [0xC0, 0xC1, 0xC8, 0xD0, 0xD8, 0xF0, 0xF8, 0xC6, 0xCE, 0xD6, 0xFE]
        R = self.rng
        self.ops = [
            (9, lambda: bytes([R.choice([0x8B, 0x8B, 0x8B, 0x8A]), R.choice(regs_ebp), R.choice(disp)])),  # mov r,[ebp+d]
            (9, lambda: bytes([R.choice([0x89, 0x89, 0x88]), R.choice(regs_ebp), R.choice(disp)])),  # mov [ebp+d],r
            (4, lambda: bytes([0x8B, R.choice(rr)])),                                  # mov r,r
            (4, lambda: bytes([0x8D, R.choice(regs_ebp), R.choice(disp)])),           # lea r,[ebp+d]
            (11, lambda: bytes([0x50 + R.randrange(8)])),                               # push r
            (6, lambda: bytes([0x58 + R.randrange(8)])),                               # pop r
            (4, lambda: bytes([0x33, R.choice(rr)])),                                  # xor r,r
            (6, lambda: bytes([0x85, R.choice(rr)])),                                  # test r,r
            (5, lambda: bytes([0x3B, R.choice(rr)])),                                  # cmp r,r
            (6, lambda: bytes([0x74, R.randrange(2, 0x60)])),                          # jz short
            (6, lambda: bytes([0x75, R.randrange(2, 0x60)])),                          # jnz short
            (3, lambda: bytes([0xEB, R.randrange(2, 0x40)])),                          # jmp short
            (3, lambda: bytes([0x7C + R.randrange(4), R.randrange(2, 0x40)])),        # jl/jge/jle/jg
            (6, lambda: bytes([0x83, 0xC4, R.choice([4, 8, 12, 16, 20])])),            # add esp,imm8
            (3, lambda: bytes([0x83, 0xEC, R.choice([4, 8, 12, 16, 0x20])])),          # sub esp,imm8
            (3, lambda: bytes([0x83, 0xC0 + R.randrange(8), R.randrange(1, 0x40)])),   # add r,imm8
            (4, lambda: bytes([0x83, 0x7D, R.choice(disp), R.choice([0, 1, 2, 4, 0xFF])])),  # cmp [ebp+d],imm8
            (6, lambda: bytes([0x6A, R.choice([0, 1, 1, 2, 4, 8, 0x10, 0x20, 0xFF])])),  # push imm8
            (4, lambda: bytes([0xB8 + R.randrange(8)]) + le32(R.choice([0, 1, 2, 4, 8, 16, 0x100, R.randrange(0x10000), R.randrange(0x1000000)]))),  # mov r,imm32
            (3, lambda: bytes([0xC7, 0x45, R.choice(disp)]) + le32(R.choice([0, 1, R.randrange(0x100), R.randrange(0x10000)]))),  # mov [ebp+d],imm32
            (2, lambda: bytes([0x3D]) + le32(R.choice([0, 1, 0x10, 0xFF, R.randrange(0x1000), R.randrange(0x100000)]))),  # cmp eax,imm32
            (2, lambda: bytes([0x8B, R.choice([0x80, 0x86, 0x8E, 0x87, 0x81])]) + le32(R.choice([0x80, 0x84, 0x88, 0x90, 0xA0, 0xC4, 0x100, 0x104, 0x120]))),  # mov r,[r+disp32]
            (2, lambda: bytes([0xFF, R.choice([0x90, 0x91, 0x92, 0x96])]) + le32(R.choice([0, 4, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24]))),  # call [r+disp32] (vtable)
            (3, lambda: bytes([0xC6, 0x45, R.choice(disp), R.randrange(2)])),         # mov byte [ebp+d],imm8
            (3, lambda: bytes([0x0F, 0x84 + R.randrange(2)]) + le32(R.randrange(0x40, 0x800))),  # jz/jnz near
            (2, lambda: bytes([0x0F, 0xB6, R.choice([0xC0, 0xC8, 0x45, 0x4D])]) + (bytes([R.choice(disp)]) if R.random() < 0.5 else b"")),  # movzx
            (2, lambda: bytes([0x8B, R.choice([0x40, 0x46, 0x41, 0x47, 0x4E]), R.choice([0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20])])),  # mov r,[r+d]
            (2, lambda: bytes([0x89, R.choice([0x46, 0x47, 0x4E, 0x41]), R.choice([0x04, 0x08, 0x0C, 0x10, 0x14])])),  # mov [r+d],r
            (2, lambda: bytes([0xF7, 0xD8 + R.randrange(2)])),                          # neg/not
            (2, lambda: bytes([0xC1, R.choice([0xE0, 0xE8, 0xF8]), R.choice([1, 2, 3, 4, 5, 8])])),  # shl/shr/sar
            (2, lambda: bytes([0xD9, R.choice([0x45, 0x5D, 0xEE, 0xC0]), R.choice(disp)])),  # fld/fstp
            (9, self.op_call),                                                         # call func
            (5, self.op_push_imm32),                                                   # push offset
            (4, self.op_call_iat),                                                     # call [iat]
            (4, self.op_mov_global),                                                   # mov r,[global]
        ]
        self.total_w = sum(w for w, _ in self.ops)

    # --- ops needing bookkeeping
    def op_call(self):
        self.calls.append(len(self.buf))
        return b"\xE8\0\0\0\0"

    def abs32(self, va):
        self.relocs.append(len(self.buf))
        return le32(va)

    def op_push_imm32(self):
        lo, hi = self.image_vas
        return b"\x68" + self.abs32(self.rng.randrange(lo, hi, 4))

    def op_call_iat(self):
        return b"\xFF\x15" + self.abs32(self.rng.choice(self.iat_vas))

    def op_mov_global(self):
        op = self.rng.choice([b"\xA1", b"\x8B\x0D", b"\x8B\x15", b"\x89\x0D", b"\x89\x15", b"\xA3"])
        return op + self.abs32(self.rng.choice(self.data_vas))

    # --- hooks used by the special functions; they append to self.buf directly
    def emit(self, b):
        self.buf += b

    def rand_op(self):
        r = self.rng.random() * self.total_w
        for w, fn in self.ops:
            r -= w
            if r <= 0:
                return fn()
        return self.ops[-1][1]()

    def prologue(self):
        R = self.rng
        p = b"\x55\x8B\xEC"
        c = R.random()
        if c < 0.25:
            p += b"\x83\xEC" + bytes([R.choice([8, 0x10, 0x18, 0x20, 0x40])])
        elif c < 0.35:
            p += b"\x81\xEC" + le32(R.choice([0x104, 0x208, 0x400, 0x1000]))
        if R.random() < 0.4:
            p += bytes([R.choice([0x56, 0x57, 0x53])])
        return p

    def epilogue(self):
        R = self.rng
        e = b""
        if R.random() < 0.4:
            e += bytes([R.choice([0x5E, 0x5F, 0x5B])])
        if R.random() < 0.3:
            e += b"\x8B\xE5"
        e += b"\x5D"
        c = R.random()
        if c < 0.6:
            e += b"\xC3"
        else:
            e += b"\xC2" + struct.pack("<H", R.choice([4, 8, 12, 16]))
        return e

    def padding(self, minimum=0):
        n = max(minimum, self.rng.choice([0, 0, 1, 2, 3, 4, 5, 6, 7]))
        return b"\xCC" * n

    def body(self, n):
        for _ in range(n):
            self.emit(self.rand_op())

    def function(self, body_ops=None, callable_=True):
        """One ordinary function; returns its start offset."""
        start = len(self.buf)
        if callable_:
            self.funcs.append(start)
        self.emit(self.prologue())
        self.body(body_ops if body_ops is not None else self.rng.randint(3, 70))
        self.emit(self.epilogue())
        self.emit(self.padding())
        return start


def gen_text(rng, npages, tbase, image_vas, data_vas, iat_vas, cookie_va, fire_slot_va):
    """Generate the plaintext .text. Returns (bytes, info)."""
    tsize = npages * PAGE
    em = Emitter(rng, tbase, tsize, image_vas, data_vas, iat_vas)
    info = {}
    fire_at = int(tsize * 0.2)
    crt_at = int(tsize * 0.45)
    fire_done = crt_done = False
    while len(em.buf) < tsize - 512:
        if not fire_done and len(em.buf) >= fire_at:
            # the one legitimate call into the protector: cdecl, one argument
            start = len(em.buf)
            em.funcs.append(start)
            em.emit(em.prologue())
            em.body(rng.randint(2, 6))
            em.emit(b"\x8D\x45\xF8\x50")                     # lea eax,[ebp-8]; push eax
            info["fire_call"] = len(em.buf)
            em.emit(b"\xFF\x15" + em.abs32(fire_slot_va))    # call [FireInTheHole]
            em.emit(b"\x83\xC4\x04")                         # add esp,4
            em.body(rng.randint(2, 20))
            em.emit(em.epilogue())
            em.emit(em.padding())
            fire_done = True
            continue
        if not crt_done and len(em.buf) >= crt_at:
            # CC padding, then the CRT entry: call init_cookie; jmp main_seh
            em.emit(em.padding(minimum=1))
            entry = len(em.buf)
            em.emit(b"\xE8\0\0\0\0\xE9\0\0\0\0")
            em.emit(em.padding(minimum=1))
            # __security_init_cookie: the only writer of the cookie global
            init = len(em.buf)
            em.emit(em.prologue())
            em.emit(b"\x8B\x0D" + em.abs32(cookie_va))      # mov ecx,[__security_cookie]
            em.emit(b"\x81\xF9" + le32(COOKIE_INIT))         # cmp ecx, default
            em.emit(b"\x75\x08")
            em.body(3)
            em.emit(b"\x89\x0D" + em.abs32(cookie_va))      # mov [__security_cookie],ecx
            em.body(rng.randint(2, 8))
            em.emit(em.epilogue())
            em.emit(em.padding())
            # __scrt_common_main_seh: reached only by the jmp
            main = len(em.buf)
            em.emit(em.prologue())
            em.body(rng.randint(30, 80))
            em.emit(em.epilogue())
            em.emit(em.padding())
            cookie_off = em.buf.find(b"\x89\x0D" + le32(cookie_va))
            assert init <= cookie_off < init + 0x200
            struct.pack_into("<i", em.buf, entry + 1, init - (entry + 5))
            struct.pack_into("<i", em.buf, entry + 6, main - (entry + 10))
            info.update({"entry": entry, "init_cookie": init, "main_seh": main})
            crt_done = True
            continue
        em.function()
    assert fire_done and crt_done, "text too small to place the special functions"
    em.emit(b"\xCC" * (tsize - len(em.buf)))
    buf = em.buf
    assert len(buf) == tsize

    # patch every ordinary call to a real function start
    for p in em.calls:
        tgt = rng.choice(em.funcs)
        struct.pack_into("<i", buf, p + 1, tgt - (p + 5))

    info["relocs"] = sorted(em.relocs)
    info["nfuncs"] = len(em.funcs)
    return bytes(buf), info


def check_oep_shape(text, tbase, info):
    """Mirror find_oep's crude scan: the entry must never be a target, the
    init_cookie callee must be called exactly once (spurious E8 bytes inside
    immediates count too), and main_seh must never be called."""
    n = len(text)
    entry = tbase + info["entry"]
    init = tbase + info["init_cookie"]
    main = tbase + info["main_seh"]
    hits_entry = 0
    calls_init = 0
    calls_main = 0
    j = 0
    while True:
        j1 = text.find(b"\xE8", j)
        j2 = text.find(b"\xE9", j)
        if j1 < 0 and j2 < 0:
            break
        j = min(x for x in (j1, j2) if x >= 0)
        if j >= n - 5:
            break
        tgt = tbase + j + 5 + struct.unpack_from("<i", text, j + 1)[0]
        if tgt == entry:
            hits_entry += 1
        if text[j] == 0xE8:
            if tgt == init:
                calls_init += 1
            if tgt == main:
                calls_main += 1
        j += 1
    return hits_entry == 0 and calls_init == 1 and calls_main == 0


def histogram(text):
    counts = [0] * 256
    for b in text:
        counts[b] += 1
    n = float(len(text))
    order = sorted(range(256), key=lambda b: -counts[b])
    return {"zero_share": counts[0] / n,
            "top": [(b, counts[b] / n) for b in order[:6]]}


def key_margin(text, key):
    """Smallest (mode votes - runner-up votes) over all 4096 offsets when the
    ciphertext is voted the way static_keystream does. Positive means the key
    recovers exactly."""
    npages = len(text) // PAGE
    worst = None
    for j in range(PAGE):
        c = {}
        for p in range(npages):
            b = text[p * PAGE + j] ^ key[j]
            c[b] = c.get(b, 0) + 1
        top = sorted(c.values(), reverse=True)
        mode_is_key = c.get(key[j], 0) == top[0]
        m = (c.get(key[j], 0) - (top[1] if len(top) > 1 else 0)) if mode_is_key else -1
        worst = m if worst is None else min(worst, m)
    return worst


# ---------------------------------------------------------------- relocations
def build_reloc(text_rva, offsets):
    blocks = {}
    for o in offsets:
        rva = text_rva + o
        blocks.setdefault(rva & ~0xFFF, []).append(0x3000 | (rva & 0xFFF))
    out = bytearray()
    for page in sorted(blocks):
        ents = sorted(blocks[page])
        if len(ents) & 1:
            ents.append(0)                                     # IMAGE_REL_BASED_ABSOLUTE pad
        out += struct.pack("<II", page, 8 + 2 * len(ents))
        out += b"".join(struct.pack("<H", e) for e in ents)
    return bytes(out)


# ---------------------------------------------------------------- PE assembly
def section_header(name, vsize, rva, rsize, raw, chars):
    return (name.encode("latin1")[:8].ljust(8, b"\0")
            + struct.pack("<IIII", vsize, rva, rsize, raw)
            + struct.pack("<IIHHI", 0, 0, 0, 0, chars))


def build(out_path, npages, seed, plain, wrong_name_key, db_path=IMPORTS_DB):
    db = json.load(open(db_path))
    rng = random.Random(seed)

    # -- key(s)
    key = bytearray(rng.getrandbits(8) for _ in range(PAGE))
    key[NUL_INDEX] = ord(NUL_NAME[NUL_INDEX])        # forces a NUL inside one obfuscated name
    key = bytes(key)
    name_key = key
    if wrong_name_key:
        wk = bytearray(rng.getrandbits(8) for _ in range(64))
        wk[0] = key[0] ^ 0x80                         # guarantees a non-identifier byte
        name_key = bytes(wk)

    groups = pick_imports(rng, db)
    sec_name = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(3))
    timestamp = 0x68B00000 + rng.randrange(0x100000)

    # -- layout (RVAs)
    text_rva = 0x1000
    text_size = npages * PAGE
    rdata_rva = text_rva + text_size
    # .rdata size is only known after building it, but it needs cookie_va;
    # .data goes after .rdata, so build .rdata with a provisional data rva
    # and fix up once the size is known (the load config only stores a VA).
    provisional = build_rdata(groups, key, name_key, rdata_rva, 0)[0]
    rdata_vsize = len(provisional)
    data_rva = align(rdata_rva + rdata_vsize, SEC_ALIGN)
    cookie_va = IMAGE_BASE + data_rva + 4
    rdata, rinfo = build_rdata(groups, key, name_key, rdata_rva, cookie_va)
    assert len(rdata) == rdata_vsize
    data_vsize = 0x1000
    data = bytearray(0x200)
    struct.pack_into("<I", data, 4, COOKIE_INIT)
    struct.pack_into("<I", data, 8, 0x2B992DDF)               # __security_cookie_complement
    reloc_rva = align(data_rva + data_vsize, SEC_ALIGN)

    slots = rinfo["slots"]
    fire_slot_rva = next(r for r, d, e in slots if d == PROTECTOR_DLL)
    iat_vas = [IMAGE_BASE + r for r, d, e in slots if "name" in e and d != PROTECTOR_DLL]
    data_vas = [IMAGE_BASE + data_rva + 4 * k for k in range(3, 0x40)]
    image_vas = (IMAGE_BASE + text_rva, IMAGE_BASE + rdata_rva + rdata_vsize)

    # -- .text (retry if a spurious E8/E9 inside an immediate breaks the OEP shape)
    tbase = IMAGE_BASE + text_rva
    for attempt in range(32):
        trng = random.Random(seed * 1000003 + attempt)
        text, tinfo = gen_text(trng, npages, tbase, image_vas, data_vas, iat_vas,
                               cookie_va, IMAGE_BASE + fire_slot_rva)
        if check_oep_shape(text, tbase, tinfo):
            break
    else:
        sys.exit("could not generate a .text with an unambiguous CRT entry")
    reloc = build_reloc(text_rva, tinfo["relocs"])
    reloc_vsize = len(reloc)
    inj_rva = align(reloc_rva + reloc_vsize, SEC_ALIGN)

    # -- injected section: NOP sled + fake import directory (plain names)
    inj = bytearray(b"\x90" * 0x400)
    desc_off = len(inj)
    inj += b"\0" * 40                                          # 1 descriptor + terminator
    int_off = len(inj)
    inj += b"\0" * 8
    hn_off = len(inj)
    inj += struct.pack("<H", 0) + PROTECTOR_FUNC.encode() + b"\0"
    if len(inj) & 1:
        inj.append(0)
    dll_off = len(inj)
    inj += PROTECTOR_DLL.encode() + b"\0"
    struct.pack_into("<I", inj, int_off, inj_rva + hn_off)
    struct.pack_into("<IIIII", inj, desc_off, inj_rva + int_off, 0, 0,
                     inj_rva + dll_off, fire_slot_rva)
    inj_vsize = 0x1000
    inj_chars = CNT_CODE | CNT_INIT | MEM_EXECUTE | MEM_READ | MEM_WRITE

    # -- ciphertext
    if plain:
        text_out = text
    else:
        text_out = bytes(b ^ key[i % PAGE] for i, b in enumerate(text))

    # -- file layout
    secs = []          # (name, vsize, rva, bytes, chars)
    secs.append((".text", text_size, text_rva, text_out, CNT_CODE | MEM_READ))   # no EXECUTE
    secs.append((".rdata", rdata_vsize, rdata_rva, rdata, CNT_INIT | MEM_READ))
    secs.append((".data", data_vsize, data_rva, bytes(data), CNT_INIT | MEM_READ | MEM_WRITE))
    secs.append((".reloc", reloc_vsize, reloc_rva, reloc, CNT_INIT | DISCARDABLE | MEM_READ))
    secs.append((sec_name, inj_vsize, inj_rva, bytes(inj), inj_chars))

    raw = SIZE_OF_HEADERS
    table = b""
    body = bytearray()
    for name, vsize, rva, content, chars in secs:
        rsize = align(len(content), FILE_ALIGN)
        table += section_header(name, vsize, rva, rsize, raw, chars)
        body += content + b"\0" * (rsize - len(content))
        raw += rsize
    size_of_image = align(inj_rva + inj_vsize, SEC_ALIGN)

    # -- headers
    hdr = bytearray(SIZE_OF_HEADERS)
    hdr[0:2] = b"MZ"
    struct.pack_into("<H", hdr, 2, 0x90)
    struct.pack_into("<H", hdr, 4, 3)
    struct.pack_into("<H", hdr, 8, 4)
    struct.pack_into("<H", hdr, 0x18, 0x40)
    struct.pack_into("<I", hdr, 0x3C, E_LFANEW)
    stub = (b"\x0E\x1F\xBA\x0E\x00\xB4\x09\xCD\x21\xB8\x01\x4C\xCD\x21"
            b"This program cannot be run in DOS mode.\r\r\n$")
    hdr[0x40:0x40 + len(stub)] = stub
    pe = E_LFANEW
    hdr[pe:pe + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", hdr, pe + 4,
                     0x14C, len(secs), timestamp, 0, 0, OPT_SIZE, 0x0102)
    o = pe + 24
    entry_rva = inj_rva                                        # the NOP sled
    struct.pack_into("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", hdr, o,
                     0x10B, 14, 29,
                     text_size,                                 # SizeOfCode
                     rdata_vsize + data_vsize + reloc_vsize + inj_vsize,
                     0,
                     entry_rva, text_rva, rdata_rva, IMAGE_BASE,
                     SEC_ALIGN, FILE_ALIGN,
                     6, 0, 0, 0, 6, 0, 0,
                     size_of_image, SIZE_OF_HEADERS, 0,
                     2,                                          # GUI subsystem
                     0x8140,                                     # DYNAMIC_BASE|NX|TS_AWARE
                     0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dd = o + 96

    def set_dd(i, rva, size):
        struct.pack_into("<II", hdr, dd + 8 * i, rva, size)

    set_dd(1, inj_rva + desc_off, 40)                          # import directory (fake)
    set_dd(5, reloc_rva, reloc_vsize)                          # base relocations
    set_dd(10, rdata_rva + rinfo["loadconfig_off"], 0x5C)      # load config
    set_dd(12, rdata_rva, rinfo["iat_size"])                   # ORIGINAL IAT
    sec_off = o + OPT_SIZE
    hdr[sec_off:sec_off + len(table)] = table
    assert sec_off + len(table) <= SIZE_OF_HEADERS

    image = bytes(hdr) + bytes(body)
    with open(out_path, "wb") as f:
        f.write(image)

    # -- truth
    slot_map = {}
    truth_groups = []
    ord_db = db.get("ordinals", {})
    for dll, entries in groups:
        g = {"dll": dll, "slots": []}
        for r, d, e in slots:
            if d != dll:
                continue
            if "ordinal" in e:
                rec = {"rva": "0x%x" % r, "name": "#%d" % e["ordinal"], "ordinal": e["ordinal"],
                       "ordinal_name": ord_db.get(dll, {}).get(str(e["ordinal"]))}
            else:
                rec = {"rva": "0x%x" % r, "name": e["name"]}
            g["slots"].append(rec)
            slot_map["0x%x" % r] = [dll, rec["name"]]
        truth_groups.append(g)
    hist = histogram(text)
    truth = {
        "seed": seed, "pages": npages, "plain": plain, "wrong_name_key": wrong_name_key,
        "file_sha256": hashlib.sha256(image).hexdigest(),
        "image_base": "0x%x" % IMAGE_BASE,
        "key_b64": base64.b64encode(key).decode(),
        "name_key_b64": base64.b64encode(name_key[:64]).decode(),
        "text_rva": "0x%x" % text_rva, "text_size": "0x%x" % text_size,
        "text_sha256": hashlib.sha256(text).hexdigest(),
        "oep_rva": "0x%x" % (text_rva + tinfo["entry"]),
        "init_cookie_rva": "0x%x" % (text_rva + tinfo["init_cookie"]),
        "main_seh_rva": "0x%x" % (text_rva + tinfo["main_seh"]),
        "cookie_va": "0x%x" % cookie_va,
        "fire_call_rva": "0x%x" % (text_rva + tinfo["fire_call"]),
        "fire_slot_rva": "0x%x" % fire_slot_rva,
        "injected_section": sec_name,
        "entry_rva": "0x%x" % entry_rva,
        "iat_rva": "0x%x" % rdata_rva, "iat_size": "0x%x" % rinfo["iat_size"],
        "loadconfig_rva": "0x%x" % (rdata_rva + rinfo["loadconfig_off"]),
        "groups": truth_groups,
        "slots": slot_map,
        "nul_name": NUL_NAME, "nul_index": NUL_INDEX,
        "nul_entry_rva": "0x%x" % (rdata_rva + rinfo["hint_name"][NUL_NAME]),
        "inherited_names": [UNKNOWN_NAME],
        "text_stats": {"zero_share": round(hist["zero_share"], 4),
                       "top_bytes": [["0x%02x" % b, round(s, 4)] for b, s in hist["top"]],
                       "functions": tinfo["nfuncs"], "relocs": len(tinfo["relocs"]),
                       "key_vote_margin": key_margin(text, key)},
    }
    with open(out_path + ".truth.json", "w") as f:
        json.dump(truth, f, indent=1)
    return truth


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--pages", type=int, default=600, help=".text size in 4 KB pages (default 600)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--plain", action="store_true", help="leave .text unencrypted (ground-truth twin)")
    ap.add_argument("--wrong-name-key", action="store_true",
                    help="obfuscate import names with a key that is not the .text key")
    ap.add_argument("--db", default=IMPORTS_DB, help="imports_db.json to draw names from")
    a = ap.parse_args()
    t = build(a.out, a.pages, a.seed, a.plain, a.wrong_name_key, a.db)
    st = t["text_stats"]
    print("wrote %s (%s pages, seed %d%s%s)" % (a.out, a.pages, a.seed,
                                                ", plain" if a.plain else "",
                                                ", wrong name key" if a.wrong_name_key else ""))
    print("  injected section %-4s entry 0x%s   real OEP rva %s" % (t["injected_section"], t["entry_rva"][2:], t["oep_rva"]))
    print("  IAT %s (%s), %d groups, %d slots" % (t["iat_rva"], t["iat_size"], len(t["groups"]), len(t["slots"])))
    print("  .text: %d functions, %d relocs, 0x00 share %.1f%%, runner-up %s at %.1f%%, key vote margin %d"
          % (st["functions"], st["relocs"], 100 * st["zero_share"],
             st["top_bytes"][1][0], 100 * st["top_bytes"][1][1], st["key_vote_margin"]))
    print("  truth -> %s.truth.json" % a.out)


if __name__ == "__main__":
    main()
