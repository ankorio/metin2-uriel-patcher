"""unuriel.py - strip Uriel Anti-Cheat from a Triarch client, producing a normal,
debuggable PE.

The protector does four things to triarch.exe:
  1. XOR-encrypts .text with a fixed 4096-byte keystream, tiled once per page
  2. clears MEM_EXECUTE on .text (it re-adds it per page at runtime via a VEH)
  3. replaces the import directory with one fake import of client_x86.dll.
     The ORIGINAL import address table is left in place: the per-DLL thunk
     arrays, their NUL terminators and the hint/name entries all survive in
     .rdata. Only the DLL descriptors are gone, and the function names are
     XOR-obfuscated in place with the first bytes of the same keystream
     (the "hint" word of each entry holds the name length instead of a hint).
  4. redirects AddressOfEntryPoint into an injected NOP-sled section, which it
     patches at runtime to jump into itself

This tool undoes all four, entirely offline:

    python unuriel.py derive  <triarch.exe> -o profile.json
    python unuriel.py rebuild <triarch.exe> profile.json -o triarch_clean.exe

`derive` recovers everything from the file alone. The keystream falls out of a
many-time-pad attack on .text (one 4096-byte key reused across ~17 000 pages
of x86 code, whose modal byte per offset is 0x00). The import names decode
with that key. The only thing not on disk is which DLL each import group came
from, and that is looked up in imports_db.json (name -> DLL), with the group's
majority DLL covering names the table has never seen.

`harvest` is the original, live-process method - launch the game, wait for
Uriel to decrypt, read the keystream and resolved IAT out of memory - and is
kept as a fallback and as a way to regenerate imports_db.json:

    python unuriel.py harvest <triarch.exe> <pid> <live_base_hex> -o profile.json

The keystream differs per build, so re-derive after every launcher patch.
"""
import argparse
import base64
import hashlib
import json
import os
import struct
import sys
from collections import Counter

PAGE = 0x1000
IMPORTS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imports_db.json")
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_READ = 0x40000000
IMAGE_SCN_MEM_WRITE = 0x80000000
IMAGE_SCN_CNT_INITIALIZED_DATA = 0x00000040
EXEC_PROT = (0x10, 0x20, 0x40, 0x80)
PROTECTOR_DLL = "client_x86"


# ---------------------------------------------------------------- PE helpers
class PE:
    def __init__(self, data):
        self.d = bytearray(data)
        d = self.d
        self.pe = struct.unpack_from("<I", d, 0x3C)[0]
        self.opt = self.pe + 24
        self.magic = struct.unpack_from("<H", d, self.opt)[0]
        self.base = struct.unpack_from("<I", d, self.opt + 28)[0]
        self.nsec = struct.unpack_from("<H", d, self.pe + 6)[0]
        self.optsz = struct.unpack_from("<H", d, self.pe + 20)[0]
        self.sec_align = struct.unpack_from("<I", d, self.opt + 32)[0]
        self.file_align = struct.unpack_from("<I", d, self.opt + 36)[0]
        self.size_of_image = struct.unpack_from("<I", d, self.opt + 56)[0]
        self.size_of_headers = struct.unpack_from("<I", d, self.opt + 60)[0]
        self.sec_off = self.pe + 24 + self.optsz
        self.secs = []
        for i in range(self.nsec):
            o = self.sec_off + 40 * i
            nm = bytes(d[o:o + 8]).rstrip(b"\0").decode("latin1")
            vs, va, rs, rp = struct.unpack_from("<IIII", d, o + 8)
            ch = struct.unpack_from("<I", d, o + 36)[0]
            self.secs.append({"i": i, "off": o, "name": nm, "vsize": vs,
                              "rva": va, "rsize": rs, "raw": rp, "chars": ch})

    def sec(self, name):
        for s in self.secs:
            if s["name"] == name:
                return s
        raise KeyError(name)

    def rva2off(self, rva):
        for s in self.secs:
            if s["rva"] <= rva < s["rva"] + max(s["vsize"], s["rsize"]):
                return s["raw"] + (rva - s["rva"])
        return None

    def ddir(self, idx):
        o = self.opt + (112 if self.magic == 0x20B else 96) + 8 * idx
        return struct.unpack_from("<II", self.d, o)

    def set_ddir(self, idx, rva, size):
        o = self.opt + (112 if self.magic == 0x20B else 96) + 8 * idx
        struct.pack_into("<II", self.d, o, rva, size)

    def parse_relocs(self):
        rva, size = self.ddir(5)
        off = self.rva2off(rva)
        out = {}
        end = off + size
        while off < end:
            page_rva, blk = struct.unpack_from("<II", self.d, off)
            if blk < 8:
                break
            lst = []
            for i in range((blk - 8) // 2):
                e = struct.unpack_from("<H", self.d, off + 8 + 2 * i)[0]
                if (e >> 12) == 3:
                    lst.append(e & 0xFFF)
            if lst:
                out[page_rva] = lst
            off += blk
        return out


def align(v, a):
    return (v + a - 1) // a * a


# ---------------------------------------------------------------- OEP finder
def find_oep(pe, text_bytes):
    """Locate the CRT entry: E8 <__security_init_cookie> E9 <__scrt_common_main_seh>,
    CC-padded, never called, whose callee is called exactly once and contains the
    single write to the SecurityCookie global."""
    t = pe.sec(".text")
    tbase = pe.base + t["rva"]
    n = len(text_bytes)

    lc_rva = pe.ddir(10)[0]
    cookie = struct.unpack_from("<I", pe.d, pe.rva2off(lc_rva) + 0x3C)[0]
    wpat = b"\x89\x0d" + struct.pack("<I", cookie)
    wi = text_bytes.find(wpat)
    cookie_write = tbase + wi if wi >= 0 else None

    targets = set()
    callcount = Counter()
    for j in range(n - 5):
        b = text_bytes[j]
        if b == 0xE8 or b == 0xE9:
            tgt = tbase + j + 5 + struct.unpack_from("<i", text_bytes, j + 1)[0]
            if tbase <= tgt < tbase + t["vsize"]:
                targets.add(tgt)
                if b == 0xE8:
                    callcount[tgt] += 1

    cands = []
    for i in range(1, n - 10):
        if text_bytes[i] != 0xE8 or text_bytes[i + 5] != 0xE9:
            continue
        if text_bytes[i - 1] != 0xCC:
            continue
        va = tbase + i
        if va in targets:
            continue
        ct = va + 5 + struct.unpack_from("<i", text_bytes, i + 1)[0]
        jt = va + 10 + struct.unpack_from("<i", text_bytes, i + 6)[0]
        if not (tbase <= ct < tbase + t["vsize"] and tbase <= jt < tbase + t["vsize"]):
            continue
        if callcount.get(ct, 0) != 1:
            continue
        score = 0
        if cookie_write is not None and ct <= cookie_write < ct + 0x200:
            score += 10          # callee contains the cookie write -> init_cookie
        if callcount.get(jt, 0) == 0:
            score += 1           # main_seh is only reached by this jmp
        cands.append((score, va, ct, jt))

    if not cands:
        return None, []
    cands.sort(reverse=True)
    return cands[0][1], cands


# ---------------------------------------------------------------- harvest
def harvest(args):
    import ctypes
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                    ("AllocationProtect", w.DWORD), ("__a1", w.DWORD),
                    ("RegionSize", ctypes.c_ulonglong), ("State", w.DWORD),
                    ("Protect", w.DWORD), ("Type", w.DWORD), ("__a2", w.DWORD)]

    raw = open(args.exe, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    pe = PE(raw)
    t = pe.sec(".text")
    relocs = pe.parse_relocs()
    live = int(args.base, 16)
    delta = live - pe.base
    print("target      : %s" % args.exe)
    print("sha256      : %s" % sha)
    print("preferred   : 0x%x   live 0x%x   delta 0x%x" % (pe.base, live, delta))

    h = k32.OpenProcess(0x0400 | 0x0010, False, args.pid)
    if not h:
        sys.exit("OpenProcess failed %d" % ctypes.get_last_error())

    def rd(a, n):
        buf = (ctypes.c_char * n)()
        got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(a), buf, n, ctypes.byref(got)):
            return None
        return bytes(buf[:got.value])

    # ---- 1. keystream ----------------------------------------------------
    votes = [Counter() for _ in range(PAGE)]
    npages = 0
    start = live + t["rva"]
    end = start + t["vsize"]
    addr = start
    mbi = MBI()
    while addr < end:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.RegionSize == 0:
            break
        if mbi.State == 0x1000 and (mbi.Protect & 0xFF) in EXEC_PROT and not (mbi.Protect & 0x100):
            lo = max(mbi.BaseAddress, start)
            hi = min(mbi.BaseAddress + mbi.RegionSize, end)
            for a in range(lo & ~(PAGE - 1), hi, PAGE):
                p = rd(a, PAGE)
                if not p or len(p) != PAGE:
                    continue
                p = bytearray(p)
                rva = a - live
                for o in relocs.get(rva, []):
                    if o + 4 <= PAGE:
                        v = struct.unpack_from("<I", p, o)[0]
                        struct.pack_into("<I", p, o, (v - delta) & 0xFFFFFFFF)
                foff = t["raw"] + (rva - t["rva"])
                c = raw[foff:foff + PAGE]
                if len(c) != PAGE:
                    continue
                for i in range(PAGE):
                    votes[i][p[i] ^ c[i]] += 1
                npages += 1
        addr = mbi.BaseAddress + mbi.RegionSize

    if npages < 16:
        sys.exit("only %d decrypted pages visible - let the client run longer" % npages)
    key = bytearray(PAGE)
    unan = 0
    for i in range(PAGE):
        v, c = votes[i].most_common(1)[0]
        key[i] = v
        if c == npages:
            unan += 1
    print("\nkeystream   : %d pages sampled, %d/%d offsets unanimous (%.2f%%)"
          % (npages, unan, PAGE, 100.0 * unan / PAGE))

    # ---- 2. IAT ----------------------------------------------------------
    bases = []
    addr = 0
    seen = set()
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.RegionSize == 0:
            break
        if mbi.Type == 0x1000000 and mbi.AllocationBase and mbi.AllocationBase not in seen:
            seen.add(mbi.AllocationBase)
            bases.append(mbi.AllocationBase)
        addr = mbi.BaseAddress + mbi.RegionSize

    exports = {}
    modnames = {}
    for b in bases:
        hdr = rd(b, 0x1000)
        if not hdr or hdr[:2] != b"MZ":
            continue
        p2 = struct.unpack_from("<I", hdr, 0x3C)[0]
        if hdr[p2:p2 + 4] != b"PE\0\0":
            continue
        o2 = p2 + 24
        mg = struct.unpack_from("<H", hdr, o2)[0]
        dd = o2 + (112 if mg == 0x20B else 96)
        erva, esize = struct.unpack_from("<II", hdr, dd)
        if not erva:
            continue
        e = rd(b + erva, max(esize, 0x28))
        if not e or len(e) < 0x28:
            continue
        nrva, ordbase, naddr, nnames, frva, nmrva, orva = struct.unpack_from("<IIIIIII", e, 12)
        s = rd(b + nrva, 64)
        mn = s.split(b"\0")[0].decode("latin1", "replace") if s else "?"
        modnames[b] = mn
        fu = rd(b + frva, 4 * naddr) or b""
        nb = rd(b + nmrva, 4 * nnames) or b""
        ob = rd(b + orva, 2 * nnames) or b""
        named = {}
        for i in range(nnames):
            if 4 * i + 4 > len(nb) or 2 * i + 2 > len(ob):
                break
            nr = struct.unpack_from("<I", nb, 4 * i)[0]
            oi = struct.unpack_from("<H", ob, 2 * i)[0]
            sn = rd(b + nr, 128)
            if sn:
                named[oi] = sn.split(b"\0")[0].decode("latin1", "replace")
        for oi in range(naddr):
            if 4 * oi + 4 > len(fu):
                break
            fr = struct.unpack_from("<I", fu, 4 * oi)[0]
            if not fr or (erva <= fr < erva + esize):
                continue      # unused slot or forwarder
            exports.setdefault(b + fr, (mn, named.get(oi, "#%d" % (oi + ordbase))))

    iat_rva, iat_size = pe.ddir(12)
    iatraw = rd(live + iat_rva, iat_size)
    if not iatraw:
        sys.exit("could not read IAT")

    entries = []
    unresolved = 0
    for i in range(0, len(iatraw) - 3, 4):
        v = struct.unpack_from("<I", iatraw, i)[0]
        rec = {"slot_rva": iat_rva + i, "value": v, "dll": None, "name": None}
        if v:
            mod, nm = exports.get(v, (None, None))
            if mod is None:
                unresolved += 1
            rec["dll"], rec["name"] = mod, nm
        entries.append(rec)
    nz = sum(1 for e in entries if e["value"])
    print("IAT         : %d slots, %d non-zero, %d resolved, %d unresolved"
          % (len(entries), nz, nz - unresolved, unresolved))
    k32.CloseHandle(h)

    # ---- 3. OEP (needs the decrypted text) -------------------------------
    text = bytearray(raw[t["raw"]:t["raw"] + t["rsize"]])
    for i in range(len(text)):
        text[i] ^= key[i % PAGE]
    oep, cands = find_oep(pe, bytes(text))
    print("OEP         : %s  (%d candidates)"
          % ("0x%08x (rva 0x%x)" % (oep, oep - pe.base) if oep else "NOT FOUND", len(cands)))

    prof = {
        "source_sha256": sha,
        "source_size": len(raw),
        "image_base": "0x%x" % pe.base,
        "key_b64": base64.b64encode(bytes(key)).decode(),
        "key_unanimous": unan,
        "key_pages": npages,
        "iat_rva": "0x%x" % iat_rva,
        "iat_size": "0x%x" % iat_size,
        "iat": entries,
        "oep_rva": ("0x%x" % (oep - pe.base)) if oep else None,
    }
    json.dump(prof, open(args.out, "w"), indent=1)
    print("\nprofile written to %s" % args.out)


# ---------------------------------------------------------------- derive (offline)
# Name prefixes that identify a DLL when imports_db.json has never seen the
# name and the group has no known neighbour to inherit from. Last resort only.
PREFIX_DLL = [
    ("Reg", "ADVAPI32.dll"), ("Crypt", "ADVAPI32.dll"), ("Event", "ADVAPI32.dll"),
    ("Imm", "IMM32.dll"), ("WinHttp", "WINHTTP.dll"), ("Internet", "WININET.dll"),
    ("Sym", "dbghelp.dll"), ("Co", "ole32.dll"), ("SH", "SHELL32.dll"),
    ("Shell", "SHELL32.dll"), ("PathCch", "KERNELBASE.dll"), ("BCrypt", "bcrypt.dll"),
    ("SetupDi", "SETUPAPI.dll"), ("CM_", "SETUPAPI.dll"), ("Uuid", "RPCRT4.dll"),
    ("Rpc", "RPCRT4.dll"), ("VerQueryValue", "VERSION.dll"), ("GetFileVersion", "VERSION.dll"),
    ("time", "WINMM.dll"), ("WSA", "WS2_32.dll"), ("URL", "urlmon.dll"), ("Sys", "OLEAUT32.dll"),
    ("GetIf", "IPHLPAPI.DLL"), ("ConvertInterface", "IPHLPAPI.DLL"), ("FreeMib", "IPHLPAPI.DLL"),
    ("EnumProcessModules", "PSAPI.DLL"), ("GetModuleBaseName", "PSAPI.DLL"),
    ("GetProcessMemoryInfo", "PSAPI.DLL"), ("FireInTheHole", PROTECTOR_DLL + ".dll"),
]
# Candidate DLLs for the live GetProcAddress fallback (Windows only).
PROBE_DLLS = ["KERNEL32.dll", "USER32.dll", "GDI32.dll", "ADVAPI32.dll", "SHELL32.dll",
              "ole32.dll", "OLEAUT32.dll", "WS2_32.dll", "WINHTTP.dll", "WININET.dll",
              "IMM32.dll", "IPHLPAPI.DLL", "PSAPI.DLL", "RPCRT4.dll", "SETUPAPI.dll",
              "VERSION.dll", "WINMM.dll", "bcrypt.dll", "dbghelp.dll", "urlmon.dll",
              "KERNELBASE.dll", "ntdll.dll", "CRYPT32.dll", "COMCTL32.dll", "COMDLG32.dll",
              "SHLWAPI.dll", "DBGHELP.dll", "WTSAPI32.dll", "USERENV.dll", "NETAPI32.dll"]
IDENT = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_@?$")


def static_keystream(raw, pe, sample=2500):
    """Recover the 4096-byte keystream from ciphertext alone.

    Every page of .text is XORed with the same key, so for offset j the bytes
    C_p[j] = P_p[j] ^ K[j] over thousands of pages p. x86 code is dominated by
    0x00, so the modal ciphertext byte at each offset is K[j] itself.
    Returns (key, weak) where weak counts offsets whose winning byte had less
    than one vote in twenty - a diagnostic, not a failure.
    """
    t = pe.sec(".text")
    ro, size = t["raw"], min(t["rsize"], t["vsize"])
    total = size // PAGE
    step = max(1, total // sample)
    pages = range(0, total, step)
    key = bytearray(PAGE)
    weak = 0
    for j in range(PAGE):
        c = Counter(raw[ro + p * PAGE + j] for p in pages)
        v, n = c.most_common(1)[0]
        key[j] = v
        if n * 20 < len(pages):        # mode holds < 5% of the votes
            weak += 1
    return bytes(key), weak


def decode_iat(raw, pe, key):
    """Walk the original IAT that Uriel left on disk.

    Returns a list of groups; each group is one DLL's thunk array (they are
    NUL-terminated, exactly as the linker wrote them) as a list of dicts:
    {slot_rva, value, name} with name either a decoded string or "#ordinal".
    """
    iat_rva, iat_size = pe.ddir(12)
    groups, cur = [], []
    for i in range(0, iat_size, 4):
        rva = iat_rva + i
        v = struct.unpack_from("<I", raw, pe.rva2off(rva))[0]
        if v == 0:
            if cur:
                groups.append(cur)
            cur = []
            continue
        if v & 0x80000000:
            cur.append({"slot_rva": rva, "value": v, "name": "#%d" % (v & 0xFFFF)})
            continue
        off = pe.rva2off(v)
        if off is None:
            cur.append({"slot_rva": rva, "value": v, "name": None})
            continue
        n = struct.unpack_from("<H", raw, off)[0]          # length, not a hint
        cipher = raw[off + 2:off + 2 + n]
        name = bytes(b ^ key[k] for k, b in enumerate(cipher)).decode("latin1")
        cur.append({"slot_rva": rva, "value": v, "name": name})
    if cur:
        groups.append(cur)
    return groups


def _probe_windows(names):
    """GetProcAddress every candidate DLL for every name. Windows only."""
    try:
        import ctypes
    except ImportError:
        return {}
    if not hasattr(ctypes, "WinDLL"):
        return {}
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = ctypes.c_void_p
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    found = {}
    for dll in PROBE_DLLS:
        h = k32.LoadLibraryExW(dll, None, 0x00000001)       # DONT_RESOLVE_DLL_REFERENCES
        if not h:
            continue
        for nm in names:
            if nm not in found and k32.GetProcAddress(h, nm.encode("latin1")):
                found[nm] = dll
    return found


def attribute_dlls(groups, db, log=print):
    """Decide which DLL each import group belongs to.

    A group is one DLL by construction (the linker emits one thunk array per
    import library), so a single recognised name settles the whole group and
    the unknown names in it simply inherit. Ordinal-only groups are matched
    against the ordinal tables; if several DLLs fit, the one that keeps the
    descriptor order sorted by DLL name (which is how the linker wrote them)
    wins. Returns the list of unknown names that were assigned by inheritance.
    """
    names_db = db.get("names", {})
    ord_db = db.get("ordinals", {})
    lower = {k.lower(): k for k in set(names_db.values()) | set(ord_db)}
    unknown = []
    pending = []
    for gi, g in enumerate(groups):
        votes = Counter()
        for e in g:
            nm = e["name"]
            if nm and not nm.startswith("#") and nm in names_db:
                votes[names_db[nm]] += 1
        if votes:
            dll = votes.most_common(1)[0][0]
        else:
            ords = [int(e["name"][1:]) for e in g if e["name"] and e["name"].startswith("#")]
            plain = [e["name"] for e in g if e["name"] and not e["name"].startswith("#")]
            cands = []
            if ords and not plain:
                cands = [d for d, tbl in ord_db.items() if all(str(o) in tbl for o in ords)]
            elif plain:
                for nm in plain:
                    for pfx, d in PREFIX_DLL:
                        if nm.startswith(pfx):
                            cands.append(d)
                            break
                if not cands:
                    probed = _probe_windows(plain)
                    cands = list(Counter(probed.values()).keys())
            dll = None
            if len(cands) == 1:
                dll = cands[0]
            elif len(cands) > 1:
                pending.append((gi, cands))
            else:
                log("    !! group %d (%d imports) matched no DLL: %s"
                    % (gi, len(g), ", ".join(str(e["name"]) for e in g[:6])))
        for e in g:
            e["dll"] = dll
            if dll and e["name"] and not e["name"].startswith("#") and e["name"] not in names_db:
                unknown.append(e["name"])
            if dll and e["name"] and e["name"].startswith("#"):
                e["ordinal_name"] = ord_db.get(dll, {}).get(e["name"][1:])
    # tie-break ambiguous groups by the linker's sorted-descriptor order
    for gi, cands in pending:
        prev = next((groups[i][0]["dll"] for i in range(gi - 1, -1, -1) if groups[i][0]["dll"]), "")
        nxt = next((groups[i][0]["dll"] for i in range(gi + 1, len(groups)) if groups[i][0]["dll"]), "\x7f")
        fit = [c for c in cands if prev.encode() < c.encode() < nxt.encode()]
        dll = fit[0] if len(fit) == 1 else None
        log("    group %d: ordinal-only, candidates %s -> %s (between %s and %s)"
            % (gi, cands, dll or "UNRESOLVED", prev or "-", nxt if nxt != "\x7f" else "-"))
        for e in groups[gi]:
            e["dll"] = dll
            if dll and e["name"].startswith("#"):
                e["ordinal_name"] = ord_db.get(dll, {}).get(e["name"][1:])
    return unknown


def derive(args):
    raw = open(args.exe, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    pe = PE(raw)
    t = pe.sec(".text")
    print("target      : %s" % args.exe)
    print("sha256      : %s" % sha)

    # ---- 1. keystream, statically ----------------------------------------
    key, weak = static_keystream(raw, pe)
    print("keystream   : recovered from %d pages of ciphertext, %d weak offset(s)"
          % (min(t["rsize"], t["vsize"]) // PAGE, weak))
    if weak > 64:
        sys.exit("keystream recovery is unreliable (%d weak offsets) - is this "
                 "really a Uriel-packed client?" % weak)

    # ---- 2. IAT, from the on-disk table ----------------------------------
    db_path = args.db or IMPORTS_DB
    db = json.load(open(db_path)) if os.path.isfile(db_path) else {}
    if not db:
        print("!! %s missing - DLL attribution will rely on prefixes/probing" % db_path)
    groups = decode_iat(raw, pe, key)
    entries = [e for g in groups for e in g]
    bad = [e["name"] for e in entries if e["name"] and not e["name"].startswith("#")
           and not all(ch in IDENT for ch in e["name"])]
    if bad:
        sys.exit("decoded import names are garbage (%s ...) - the keystream is wrong"
                 % ", ".join(repr(b) for b in bad[:3]))
    unknown = attribute_dlls(groups, db, log=print)
    unresolved = [e for e in entries if not e["dll"]]
    nord = sum(1 for e in entries if e["name"] and e["name"].startswith("#"))
    print("IAT         : %d groups, %d imports (%d by ordinal), %d name(s) not in the "
          "table (inherited their group's DLL), %d unresolved"
          % (len(groups), len(entries), nord, len(unknown), len(unresolved)))
    for g in groups:
        print("    %-16s %3d  %s%s" % (g[0]["dll"] or "?", len(g), g[0]["name"],
                                       "..." if len(g) > 1 else ""))
    if unknown:
        print("    inherited: %s" % ", ".join(unknown[:12]) + (" ..." if len(unknown) > 12 else ""))
    if unresolved:
        sys.exit("%d import(s) could not be attributed to a DLL - run `harvest` "
                 "against a live client and regenerate imports_db.json" % len(unresolved))

    # ---- 3. OEP, from the decrypted text ---------------------------------
    text = bytearray(raw[t["raw"]:t["raw"] + t["rsize"]])
    for i in range(len(text)):
        text[i] ^= key[i % PAGE]
    oep, cands = find_oep(pe, bytes(text))
    print("OEP         : %s  (%d candidates)"
          % ("0x%08x (rva 0x%x)" % (oep, oep - pe.base) if oep else "NOT FOUND", len(cands)))
    if not oep:
        sys.exit("entry point not found in the decrypted text")

    # The profile is the same shape harvest writes, plus the NUL separators the
    # rebuild step uses as run boundaries, so `rebuild` needs no changes.
    iat_rva, iat_size = pe.ddir(12)
    flat = []
    by_slot = {e["slot_rva"]: e for e in entries}
    for i in range(0, iat_size, 4):
        rva = iat_rva + i
        e = by_slot.get(rva)
        if e is None:
            flat.append({"slot_rva": rva, "value": 0, "dll": None, "name": None})
        else:
            rec = {"slot_rva": rva, "value": e["value"], "dll": e["dll"], "name": e["name"]}
            if e["name"] and e["name"].startswith("#"):
                # Import by ordinal. Emit it by NAME when the table knows the
                # name: the loader binds either way, but the offset resolver
                # finds IAT slots by import name, and a by-name entry is what
                # a debugger shows you. "#n" is kept when the name is unknown.
                rec["ordinal"] = int(e["name"][1:])
                if e.get("ordinal_name"):
                    rec["name"] = e["ordinal_name"]
            flat.append(rec)
    prof = {
        "source_sha256": sha,
        "source_size": len(raw),
        "image_base": "0x%x" % pe.base,
        "method": "static",
        "key_b64": base64.b64encode(key).decode(),
        "key_weak_offsets": weak,
        "iat_rva": "0x%x" % iat_rva,
        "iat_size": "0x%x" % iat_size,
        "iat_inherited_names": unknown,
        "iat": flat,
        "oep_rva": "0x%x" % (oep - pe.base),
    }
    json.dump(prof, open(args.out, "w"), indent=1)
    print("\nprofile written to %s" % args.out)


# ---------------------------------------------------------------- rebuild
def rebuild(args):
    raw = open(args.exe, "rb").read()
    prof = json.load(open(args.profile))
    sha = hashlib.sha256(raw).hexdigest()
    if sha != prof["source_sha256"]:
        print("!! profile was harvested from a DIFFERENT build")
        print("   profile: %s" % prof["source_sha256"])
        print("   this   : %s" % sha)
        if not args.force:
            sys.exit("refusing (use --force to override)")
    pe = PE(raw)
    key = base64.b64decode(prof["key_b64"])
    t = pe.sec(".text")

    # 1. decrypt .text
    for i in range(t["rsize"]):
        pe.d[t["raw"] + i] ^= key[i % PAGE]
    print("[1] decrypted .text: 0x%x bytes at file 0x%x" % (t["rsize"], t["raw"]))

    # 2. restore execute permission
    newch = t["chars"] | IMAGE_SCN_MEM_EXECUTE | IMAGE_SCN_MEM_READ
    struct.pack_into("<I", pe.d, t["off"] + 36, newch)
    print("[2] .text characteristics 0x%08x -> 0x%08x (added MEM_EXECUTE)"
          % (t["chars"], newch))

    # 3. pin the image base: no ASLR, so our stub can use absolute addresses and
    #    every address in the file matches every address in the debugger.
    dc_off = pe.opt + 70
    dc = struct.unpack_from("<H", pe.d, dc_off)[0]
    struct.pack_into("<H", pe.d, dc_off, dc & ~0x0040)
    print("[3] DllCharacteristics 0x%04x -> 0x%04x (ASLR disabled; fixed base 0x%x)"
          % (dc, dc & ~0x0040, pe.base))

    # 4. group IAT into contiguous same-DLL runs, excluding the protector
    runs, cur = [], None
    for e in prof["iat"]:
        is_prot = e["dll"] and e["dll"].lower().startswith(PROTECTOR_DLL)
        # "#n" is an import by ordinal; it gets an ordinal thunk in the INT below.
        ok = (e["value"] and e["dll"] and e["name"] and (not is_prot or args.stub_dll))
        if ok and is_prot:
            e = dict(e, dll=args.stub_dll)
        if ok:
            if cur and cur["dll"] == e["dll"] and e["slot_rva"] == cur["slots"][-1] + 4:
                cur["slots"].append(e["slot_rva"])
                cur["names"].append(e["name"])
            else:
                if cur:
                    runs.append(cur)
                cur = {"dll": e["dll"], "slots": [e["slot_rva"]], "names": [e["name"]]}
        else:
            if cur:
                runs.append(cur)
            cur = None
    if cur:
        runs.append(cur)
    nimp = sum(len(r["names"]) for r in runs)
    print("[4] %d import descriptors covering %d functions across %d DLLs"
          % (len(runs), nimp, len({r["dll"] for r in runs})))

    # 5. lay out the new section: imports + a replacement stub for the protector
    last = max(pe.secs, key=lambda s: s["rva"])
    new_rva = align(last["rva"] + max(last["vsize"], last["rsize"]), pe.sec_align)
    new_raw = align(len(pe.d), pe.file_align)

    desc_sz = (len(runs) + 1) * 20
    blob = bytearray()

    def put(b):
        off = len(blob)
        blob.extend(b)
        return off

    blob.extend(b"\0" * desc_sz)
    int_offs = []
    for r in runs:
        int_offs.append(len(blob))
        blob.extend(b"\0" * (4 * (len(r["names"]) + 1)))
    name_off = {}
    for r in runs:
        for nm in r["names"]:
            if nm.startswith("#") or nm in name_off:
                continue
            if len(blob) & 1:
                blob.append(0)
            name_off[nm] = put(struct.pack("<H", 0) + nm.encode("latin1") + b"\0")
    dll_off = {}
    for r in runs:
        if r["dll"] not in dll_off:
            dll_off[r["dll"]] = put(r["dll"].encode("latin1") + b"\0")

    while len(blob) & 0xF:
        blob.append(0)
    scratch_off = put(b"\0" * 0x40)          # object the game scribbles callbacks into
    scratch_va = pe.base + new_rva + scratch_off
    stub_off = put(b"\x8b\x44\x24\x04"                       # mov eax,[esp+4]  (arg1)
                   + b"\xc7\x00" + struct.pack("<I", scratch_va)  # mov [eax],scratch
                   + b"\xb0\x01"                              # mov al,1  (success)
                   + b"\xc3")                                  # ret  (cdecl)
    stub_va = pe.base + new_rva + stub_off

    for i, r in enumerate(runs):
        struct.pack_into("<IIIII", blob, i * 20,
                         new_rva + int_offs[i], 0, 0,
                         new_rva + dll_off[r["dll"]], r["slots"][0])
        for j, nm in enumerate(r["names"]):
            thunk = (0x80000000 | int(nm[1:])) if nm.startswith("#") else new_rva + name_off[nm]
            struct.pack_into("<I", blob, int_offs[i] + 4 * j, thunk)

    # 6. redirect every protector call to the stub
    prot_slots = {} if args.stub_dll else {
        pe.base + e["slot_rva"]: e["name"] for e in prof["iat"]
        if e["value"] and e["dll"] and e["dll"].lower().startswith(PROTECTOR_DLL)}
    patched = 0
    for slot_va, fname in prot_slots.items():
        pat = b"\xff\x15" + struct.pack("<I", slot_va)
        pos = t["raw"]
        while True:
            i = pe.d.find(pat, pos, t["raw"] + t["rsize"])
            if i < 0:
                break
            va = pe.base + t["rva"] + (i - t["raw"])
            after = bytes(pe.d[i + 6:i + 8])
            if after[:1] != b"\x83" or after[1:2] != b"\xc4":
                print("    !! call to %s at 0x%08x is not cdecl (next %s) - LEFT ALONE"
                      % (fname, va, after.hex()))
                pos = i + 6
                continue
            rel = stub_va - (va + 5)
            pe.d[i:i + 6] = b"\xe8" + struct.pack("<i", rel) + b"\x90"
            patched += 1
            print("    %s at 0x%08x -> stub 0x%08x (out-param object at 0x%08x)"
                  % (fname, va, stub_va, scratch_va))
            pos = i + 6
    if args.stub_dll:
        print("[5] protector import RETAINED but renamed to '%s' - supply that DLL"
              % args.stub_dll)
    else:
        print("[5] redirected %d protector call(s) to an in-image stub" % patched)

    # 7. commit the section
    vsize = len(blob)
    free_hdr = pe.size_of_headers - (pe.sec_off + 40 * pe.nsec)
    if free_hdr < 40:
        sys.exit("no room in headers for another section (%d bytes free)" % free_hdr)
    sh = pe.sec_off + 40 * pe.nsec
    chars = (IMAGE_SCN_CNT_INITIALIZED_DATA | IMAGE_SCN_MEM_READ
             | IMAGE_SCN_MEM_WRITE | IMAGE_SCN_MEM_EXECUTE)
    pe.d[sh:sh + 40] = (b".unuriel"[:8].ljust(8, b"\0")
                        + struct.pack("<IIII", vsize, new_rva,
                                      align(vsize, pe.file_align), new_raw)
                        + struct.pack("<IIHHI", 0, 0, 0, 0, chars))
    struct.pack_into("<H", pe.d, pe.pe + 6, pe.nsec + 1)
    struct.pack_into("<I", pe.d, pe.opt + 56, align(new_rva + vsize, pe.sec_align))
    pe.d.extend(b"\0" * (new_raw - len(pe.d)))
    pe.d.extend(blob)
    pe.d.extend(b"\0" * (align(len(blob), pe.file_align) - len(blob)))
    print("[6] section .unuriel rva 0x%x (0x%x bytes) RWX at file 0x%x"
          % (new_rva, vsize, new_raw))

    # 8. data directories + entry point
    pe.set_ddir(1, new_rva, desc_sz)
    pe.set_ddir(12, int(prof["iat_rva"], 16), int(prof["iat_size"], 16))
    print("[7] IMPORT -> 0x%x (0x%x)   IAT -> %s   [%s dropped]"
          % (new_rva, desc_sz, prof["iat_rva"], PROTECTOR_DLL))
    if prof.get("oep_rva"):
        oep = int(prof["oep_rva"], 16)
        old = struct.unpack_from("<I", pe.d, pe.opt + 16)[0]
        struct.pack_into("<I", pe.d, pe.opt + 16, oep)
        print("[8] AddressOfEntryPoint 0x%x -> 0x%x" % (old, oep))

    open(args.out, "wb").write(bytes(pe.d))
    print("\nwrote %s (%d bytes)" % (args.out, len(pe.d)))
    print("sha256: %s" % hashlib.sha256(bytes(pe.d)).hexdigest())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("derive", help="recover keystream, imports and OEP from the file alone")
    d.add_argument("exe")
    d.add_argument("-o", "--out", required=True)
    d.add_argument("--db", default=None, help="imports_db.json (default: next to this script)")
    d.set_defaults(fn=derive)
    h = sub.add_parser("harvest", help="the live-process method (fallback)")
    h.add_argument("exe")
    h.add_argument("pid", type=int)
    h.add_argument("base")
    h.add_argument("-o", "--out", required=True)
    h.set_defaults(fn=harvest)
    r = sub.add_parser("rebuild")
    r.add_argument("exe")
    r.add_argument("profile")
    r.add_argument("-o", "--out", required=True)
    r.add_argument("--force", action="store_true")
    r.add_argument("--stub-dll", default=None,
                   help="keep the protector import but rename it to this DLL, "
                        "which you supply as a do-nothing stub")
    r.set_defaults(fn=rebuild)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
