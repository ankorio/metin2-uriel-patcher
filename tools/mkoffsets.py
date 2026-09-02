"""Re-derive every address uriel_stub.cpp hardcodes, from a fresh decrypted exe.

The stub pins ~15 virtual addresses. All of them move when the game updates, so
after every patch these must be recovered rather than copied. Each anchor below
is keyed off something stable across builds - a literal string, an import name,
or a structural relationship - never a raw offset.

    python mkoffsets.py <decrypted.exe> [-o offsets.h] [--check]
                        [--natives natives.json uriel_natives.ini]

`--check` compares against the values known good for sha256 f8e0db12... and is
how you verify the resolvers still work before trusting them on a new build.
"""
import json
import re
import struct
import sys

# This file is used two ways: as a CLI (`python mkoffsets.py ...`) and as a
# vendored module inside the frozen patcher. The old `sys.path` hack only worked
# for the first - it splits on "/" and needs a real directory on disk, neither of
# which holds under PyInstaller. Try the package-relative import first and fall
# back to the script case, so ONE file serves both and the two copies can stay
# byte-identical.
try:
    from .namemods import Image                                     # packaged
except ImportError:                                                 # plain script
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from namemods import Image  # noqa: E402
from capstone import Cs, CS_ARCH_X86, CS_MODE_32  # noqa: E402

# Used only by ret_imm(): the rest of this file is pattern matching on bytes,
# which is faster and has no dependency, but reading a `ret` immediate reliably
# needs real instruction boundaries.
MD = Cs(CS_ARCH_X86, CS_MODE_32)

# known-good for triarch_clean.exe sha256 f8e0db12...  (self-test baseline)
BASELINE = {
    "kTraceSink": 0x00583380, "kTraceReal": 0x006D9110,
    "kVerifyBufsEqual": 0x008835B0, "kAuthRecvPhase": 0x00582320,
    "kAuthProcess": 0x00581A70, "kGetPcName": 0x00736DA0,
    "kGetHwProfileId": 0x007368B0, "kSendAppend": 0x006E2960,
    "kRecvBuf": 0x006E2710, "kUrielObjOffset": 0x0006D594,
    "kIatConnect": 0x048A0984, "kIatClosesocket": 0x048A09A0,
    "kIatSend": 0x048A096C, "kIatRecv": 0x048A094C,
    "kIatWSAGetLastError": 0x048A0990, "kIatWinHttpConnect": 0x048A08C8,
    "kIatWinHttpOpenRequest": 0x048A08B4, "kIatInternetOpenUrlA": 0x048A08E8,
    "kIatURLDownloadToFileA": 0x048A0A18,
    "kPyRunLine": 0x00849790, "kPyRunFile": 0x008496A0,
    "kPyRunStringFlags": 0x046090C0, "kPyLauncherInst": 0x052A58C4,
    "kPackMgrInst": 0x0529DEB0, "kPackGet": 0x00727C60,
    # Native autohunt API. All of it is DERIVED by Resolver.autohunt_api() from
    # the "/auto_hunt end" literal plus the AttackPickedActor method name, so a
    # new build needs no research - re-run and the whole set moves. These are
    # the values for THIS exe and exist so --check catches resolver rot.
    #
    # Cross-checked live against the deployed build with Frida on 2026-08-03
    # (different addresses, same derivation): BOT-SYSTEMS.md 7.3c.
    "kOnPressActor": 0x00653B60, "kPlayerInst": 0x052A5844,
    "kPlayerSubObj": 0x00000004, "kGetMainInstOff": 0x000000AC,
    "kAutoHuntLoop": 0x0064F830, "kFindAndSetNewTarget": 0x0064F170,
    "kUpdateAutoAttack": 0x0064A7A0, "kPlayerUpdate": 0x0064A510,
    "kMiniMapInst": 0x0529DEE0, "kAutoHuntRangeOff": 0x00000168,
    "kAutoAtkVidOff": 0x00000050, "kPlayerStateOff": 0x00000058,
    # Skill drive. The two struct offsets differ from the deployed build
    # (0x50174 / 0x501E2 there) - which is the whole argument for deriving them
    # instead of carrying constants across builds.
    "kUseAutoSkills": 0x0064F4C0, "kHuntUseSkillOff": 0x000501D2,
    "kHuntSkillVecOff": 0x00050164, "kMountedOff": 0x00000014,
    "kHuntStoneOff": 0x000501B2,          # deployed build: 0x501C2
    # kAnchorOff deliberately absent: it does not resolve on THIS exe (the
    # anchor is passed in a third instruction form here) and the 0x5018C we use
    # was read off the deployed build, not this one. Asserting it here would be
    # a value we never verified against this image.
}


class Resolver(object):
    def __init__(self, path):
        self.img = Image(path)
        self.d = self.img.data
        self._cache = {}
        sec = [s for s in self.img.secs if s[0] == ".text"][0]
        _, self.tva, _, self.tro, self.trs = sec

    # ---- primitives -------------------------------------------------------
    def find_str(self, s):
        """VA of a NUL-terminated literal, or None."""
        pat = s.encode() + b"\0"
        i = self.d.find(pat)
        while i != -1:
            for nm, sva, vs, ro, rs in self.img.secs:
                if ro <= i < ro + rs:
                    return sva + (i - ro)
            i = self.d.find(pat, i + 1)
        return None

    def _find_all(self, pat):
        """Every .text offset matching a literal byte pattern.

        bytes.find runs in C. The obvious `for i in range(self.trs)` loop is a
        Python-level pass over 68.5 MB and costs ~3.6s PER CALL; resolve_all
        makes 24 such scans, which is where 87 of its 90 seconds went."""
        out = []
        end = self.tro + self.trs
        i = self.d.find(pat, self.tro, end)
        while i != -1:
            out.append(i)
            i = self.d.find(pat, i + 1, end)
        return out

    def pushes_of(self, va):
        """code VAs of `push imm32` where imm32 == va"""
        key = ("push", va)
        if key not in self._cache:
            pat = b"\x68" + struct.pack("<I", va)
            self._cache[key] = [self.tva + (o - self.tro) for o in self._find_all(pat)]
        return self._cache[key]

    def callers(self, target):
        """code VAs of `call rel32` landing on target.

        The displacement is position-dependent, so there is no fixed pattern to
        search for - but scanning only the E8 bytes still cuts the Python-level
        work by ~256x versus walking every byte."""
        key = ("call", target)
        if key in self._cache:
            return self._cache[key]
        out = []
        end = self.tro + self.trs
        for i in self._find_all(b"\xE8"):
            if i + 5 > end:
                continue
            va = self.tva + (i - self.tro)
            if va + 5 + struct.unpack_from("<i", self.d, i + 1)[0] == target:
                out.append(va)
        self._cache[key] = out
        return out

    def next_call(self, va, span=80):
        o = self.img.off(va)
        if o is None:
            return None
        for j in range(span):
            if self.d[o + j] == 0xE8:
                return va + j + 5 + struct.unpack_from("<i", self.d, o + j + 1)[0]
        return None

    def func_start(self, va, back=0x2000):
        """MSVC pads between functions with int3, so the first byte after a run
        of 0xCC is the entry.  Prologue matching alone is wrong: aligned-stack
        functions open `53 8B DC` (push ebx; mov ebx,esp), not `55 8B EC`."""
        o = self.img.off(va)
        if o is None or o < back:
            return None
        for i in range(1, back):
            if self.d[o - i] == 0xCC and self.d[o - i - 1] == 0xCC:
                cand = va - i + 1
                co = self.img.off(cand)
                if self.d[co] in (0x55, 0x53, 0x56, 0x57, 0x8B, 0x83, 0x51, 0x6A, 0xB8):
                    return cand
        return None

    def iat(self):
        """{import name: IAT slot VA}"""
        d, base = self.d, self.img.base
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        imp = struct.unpack_from("<I", d, pe + 24 + 96 + 8)[0]
        o = self.img.off(base + imp)
        out, i = {}, 0
        while True:
            ilt, _ts, _fc, _nr, ia = struct.unpack_from("<IIIII", d, o + i * 20)
            if not (ilt or _nr or ia):
                break
            lo, k = self.img.off(base + ilt), 0
            while lo:
                e = struct.unpack_from("<I", d, lo + 4 * k)[0]
                if e == 0:
                    break
                if not (e & 0x80000000):
                    p = self.img.off(base + e)
                    out.setdefault(d[p + 2:d.index(b"\0", p + 2)].decode("latin1"),
                                   base + ia + 4 * k)
                k += 1
            i += 1
        return out

    def iat_callers(self, slot):
        """code VAs of `call dword [slot]` / `jmp dword [slot]`

        Two literal patterns, so two C-level scans. Results are merged and
        sorted: callers of this are position-sensitive (fn_calling_import takes
        the first hit), and the old single pass yielded them in address order."""
        key = ("iat", slot)
        if key not in self._cache:
            out = []
            for op in (b"\xFF\x15", b"\xFF\x25"):
                pat = op + struct.pack("<I", slot)
                out += [self.tva + (o - self.tro) for o in self._find_all(pat)]
            self._cache[key] = sorted(out)
        return self._cache[key]

    # ---- anchors ----------------------------------------------------------
    def trace_real(self):
        """__TraceError: the only function that pushes the "SYSERR: " literal."""
        va = self.find_str("SYSERR: ")
        if va is None:
            return None
        for site in self.pushes_of(va):
            f = self.func_start(site)
            if f:
                return f
        return None

    def trace_sink(self):
        """The dead trace stub: called with (file,line,fmt,...) but is `ret 0`."""
        anchor = self.find_str("client-source\\source\\UserInterface\\PythonApplication.cpp")
        if anchor is None:
            return None
        seen = {}
        for site in self.pushes_of(anchor):
            t = self.next_call(site, 24)
            if t:
                seen[t] = seen.get(t, 0) + 1
        for t, _n in sorted(seen.items(), key=lambda kv: -kv[1]):
            o = self.img.off(t)
            if o and self.d[o] == 0xC2 and self.d[o + 1] == 0 and self.d[o + 2] == 0:
                return t
        return None

    def verify_bufs_equal(self):
        """CryptoPP::VerifyBufsEqual - identified by its constant-time compare
        loop `lea ecx,[ecx+4] / xor eax,[ecx-4] / or edi,eax`, which no other
        routine in the image has."""
        sig = bytes([0x8D, 0x49, 0x04, 0x33, 0x41, 0xFC, 0x0B])
        i = self.d.find(sig, self.tro, self.tro + self.trs)
        if i != -1:
            f = self.func_start(self.tva + (i - self.tro))
            if f:
                return f
        return self._vbe_fallback()

    def _vbe_fallback(self):
        va = self.find_str("HashTransformation: can't truncate a %d byte digest to %d bytes")
        if va is None:
            va = self.find_str("HashTransformation: can't truncate a ")
        if va is None:
            return None
        site = self.pushes_of(va)
        f = self.func_start(site[0]) if site else None
        if not f:
            return None
        o, last = self.img.off(f), None
        for j in range(0x260):
            if self.d[o + j] == 0xE8:
                t = f + j + 5 + struct.unpack_from("<i", self.d, o + j + 1)[0]
                if self.img.off(t):
                    last = t
            if self.d[o + j] == 0xC2 and j > 0x40:
                break
        return last

    def auth_recv_phase(self):
        """Five functions push '__AuthState_RecvPhase ERROR!' (variants for the
        other connection types). The one we want is the only one that also
        collects the hardware fingerprint - it is the login gate's caller."""
        va = self.find_str("__AuthState_RecvPhase ERROR!")
        if not va:
            return None
        hw = self.fn_calling_import(self.iat(), "GetCurrentHwProfileA")
        cands = []
        for site in self.pushes_of(va):
            f = self.func_start(site)
            if f and f not in cands:
                cands.append(f)
        for f in cands:
            if hw and hw in self.calls_in(f):
                return f
        return cands[0] if cands else None

    def calls_in(self, f, span=0x800):
        o = self.img.off(f)
        out = set()
        if o is None:
            return out
        for j in range(span):
            if self.d[o + j] == 0xE8:
                out.add(f + j + 5 + struct.unpack_from("<i", self.d, o + j + 1)[0])
        return out

    def auth_process(self):
        """The sole caller of __AuthState_RecvPhase."""
        rp = self.auth_recv_phase()
        if not rp:
            return None
        c = self.callers(rp)
        return self.func_start(c[0]) if len(c) == 1 else None

    def fn_calling_import(self, iatmap, name):
        slot = iatmap.get(name)
        if not slot:
            return None
        c = self.iat_callers(slot)
        return self.func_start(c[0]) if c else None

    def fire_in_the_hole(self):
        """(callbackA VA, uriel object offset) from the single stub-DLL import."""
        iatmap = self.iat()
        slot = next((v for k, v in iatmap.items() if k == "FireInTheHole"), None)
        if not slot:
            return None, None
        sites = self.iat_callers(slot)
        if not sites:
            return None, None
        o = self.img.off(sites[0])
        cb_a, obj = None, None
        for j in range(4, 60):                       # mov [eax+4], imm32
            if self.d[o + j] == 0xC7 and self.d[o + j + 1] == 0x40 and self.d[o + j + 2] == 0x04:
                cb_a = struct.unpack_from("<I", self.d, o + j + 3)[0]
                break
        for j in range(1, 40):                       # lea esi,[edi+imm32] just before
            if self.d[o - j] == 0x8D and self.d[o - j + 1] == 0xB7:
                obj = struct.unpack_from("<I", self.d, o - j + 2)[0]
                break
        return cb_a, obj

    def send_append(self):
        """Callback A tail-calls / jumps to the outgoing-buffer append."""
        cb_a, _ = self.fire_in_the_hole()
        if not cb_a or self.img.off(cb_a) is None:
            return None
        o = self.img.off(cb_a)
        for j in range(0x40):
            if self.d[o + j] in (0xE9, 0xE8):
                return cb_a + j + 5 + struct.unpack_from("<i", self.d, o + j + 1)[0]
        return None

    def recv_buf(self):
        """CNetworkStream::Recv(int,void*). Anchored on its prologue plus the
        two member reads that follow (+0x24 / +0x28 = buffer end / used),
        mirroring how SendAppend is identified by its +0x49 connected flag.
        If the layout ever shifts this fails loudly rather than silently."""
        sig = bytes([0x55, 0x8B, 0xEC, 0x56, 0x8B, 0xF1, 0x57, 0x8B, 0x7D, 0x08,
                     0x8B, 0x46, 0x24, 0x8B, 0x4E, 0x28])
        i = self.d.find(sig, self.tro, self.tro + self.trs)
        if i == -1:
            return None
        if self.d.find(sig, i + 1, self.tro + self.trs) != -1:
            return None                      # ambiguous - refuse to guess
        return self.tva + (i - self.tro)

    def find_phase_tables(self):
        """movzx eax,byte[idx] ; jmp dword[eax*4+jmp] - the game-phase dispatcher."""
        d = self.d
        for i in range(self.trs - 14):
            if d[self.tro + i] == 0x0F and d[self.tro + i + 1] == 0xB6 and \
               d[self.tro + i + 2] == 0x80 and d[self.tro + i + 7] == 0xFF and \
               d[self.tro + i + 8] == 0x24 and d[self.tro + i + 9] == 0x85:
                idx = struct.unpack_from("<I", d, self.tro + i + 3)[0]
                jmp = struct.unpack_from("<I", d, self.tro + i + 10)[0]
                if self.img.off(idx) and self.img.off(jmp):
                    return idx, jmp
        return None, None

    def slot2_arg_bytes(self):
        """`sub esp,N` + pushes before `call edi` in the login gate -> ret N."""
        rp = self.auth_recv_phase()
        if not rp:
            return None
        o = self.img.off(rp)
        for j in range(0x600):
            # mov edi,[eax+8] ... call edi
            if self.d[o + j] == 0x8B and self.d[o + j + 1] == 0x78 and self.d[o + j + 2] == 0x08:
                # Count the pointer push plus the by-value struct only. Pushes
                # after `sub esp` are arguments to the struct's constructor,
                # which cleans them itself (ret 4) before `call edi`.
                total, seen_sub = 0, False
                for k in range(j, j + 0x40):
                    if self.d[o + k] == 0x50 and not seen_sub:      # push eax
                        total += 4
                    if self.d[o + k] == 0x83 and self.d[o + k + 1] == 0xEC:   # sub esp,imm8
                        total += self.d[o + k + 2]
                        seen_sub = True
                    if self.d[o + k] == 0xFF and self.d[o + k + 1] == 0xD7:   # call edi
                        return total
        return None

    # ---- CPythonLauncher: the mod host's entry into the interpreter --------
    #
    # The client embeds __FILE__ in its trace calls, so the source path is a
    # free index of every function defined in PythonLauncher.cpp. From there
    # RunLine is the one primitive the mod loader needs: a __thiscall taking a
    # plain `const char*`, which sidesteps the CPython C API entirely. That
    # matters because CPython 3.14 interns identifier literals as immortal
    # _Py_ID() objects rather than `const char*`, so its own entry points have
    # no `push imm32` string sites to anchor on.

    def _body(self, va, n=0x140):
        o = self.img.off(va)
        return self.d[o:o + n] if o is not None else b""

    def _in_image(self, va):
        """VA lands inside some section's *virtual* range.

        Not the same as img.off(): both singletons live past the raw end of
        .data (zero-initialised tail), so they have no file offset at all."""
        return any(sva <= va < sva + vs for _nm, sva, vs, _ro, _rs in self.img.secs)

    def _launcher_fns(self):
        if "launcher_fns" in self._cache:
            return self._cache["launcher_fns"]
        va = self.find_str("client-source\\source\\script\\PythonLauncher.cpp")
        if va is None:
            return []
        out = []
        for site in self.pushes_of(va):
            f = self.func_start(site)
            if f and f not in out:
                out.append(f)
        self._cache["launcher_fns"] = out
        return out

    def _fns_pushing(self, text):
        va = self.find_str(text)
        if va is None:
            return set()
        return {self.func_start(s) for s in self.pushes_of(va)} - {None}

    def run_line(self):
        _k = 'run_line'
        if _k in self._cache:
            return self._cache[_k]
        """CPythonLauncher::RunLine(const char*) - __thiscall, ret 4.

        Two functions in PythonLauncher.cpp report "RunMain Error %s"; only the
        one that evaluates a string pushes Py_file_input (0x101) on its way into
        PyRun_StringFlags, so that constant disambiguates them."""
        err = self._fns_pushing("RunMain Error %s")
        out = None
        for f in self._launcher_fns():
            if f in err and b"\x68\x01\x01\x00\x00" in self._body(f):
                out = f
                break
        self._cache[_k] = out
        return out

    def run_file(self):
        _k = 'run_file'
        if _k in self._cache:
            return self._cache[_k]
        """CPythonLauncher::RunFile(const char*) - reads through the pack VFS."""
        nf = self._fns_pushing("file not found! %s")
        out = None
        for f in self._launcher_fns():
            if f in nf:
                out = f
                break
        self._cache[_k] = out
        return out

    def py_run_string_flags(self):
        """RunLine's first call. Not used by the stub - recorded so a future
        change can tell a CPython move from a game-code move."""
        rl = self.run_line()
        return self.next_call(rl, 0x40) if rl else None

    def launcher_inst(self):
        """`mov ecx,[imm32]` in RunFile's only caller (app.RunPythonFile).

        NOTE the caller derefs twice: this = *(void**)(*(void**)imm32). The
        stub must reproduce that exactly, not assume one level."""
        rf = self.run_file()
        if not rf:
            return None
        c = self.callers(rf)
        if not c:
            return None
        f = self.func_start(c[0])
        o = self.img.off(f) if f else None
        if o is None:
            return None
        for j in range(0x80):
            if self.d[o + j] == 0x8B and self.d[o + j + 1] == 0x0D:
                va = struct.unpack_from("<I", self.d, o + j + 2)[0]
                if self._in_image(va):
                    return va
        return None

    def attack_bridge(self):
        """Everything the stub's native attack bridge needs, in one pass.

        Anchor: the `AttackPickedActor` PyMethodDef entry. That name is a real
        NUL-terminated C string (method tables need one), so it is stable across
        builds in a way a raw offset never is, and the entry's second dword is
        the binding's address.

        The compiler inlines CPythonPlayer::AttackPickedActor into that binding,
        so its body hands us the rest:

            mov  eax, [imm32]        ; kPlayerInst  (deref TWICE, as PlayerThis does)
            mov  edi, [eax]
            mov  eax, [edi+4]        ; kPlayerSubObj = 4
            call dword ptr [eax+0xAC]; kGetMainInstOff -> GetMainInstance
            ...
            push 0 / push vid / push pInstance / mov ecx,edi
            call rel32               ; kOnPressActor

        Returns {} rather than raising if anything looks wrong: these offsets
        are optional in the stub and a miss must only disable the feature.
        """
        out = {}
        sva = self.find_str("AttackPickedActor")
        if not sva:
            return out
        # PyMethodDef = {const char* name; PyCFunction fn; int flags; ...}
        want = struct.pack("<I", sva)
        fn = None
        i = self.d.find(want)
        while i != -1 and fn is None:
            cand = struct.unpack_from("<I", self.d, i + 4)[0]
            if self._in_image(cand):
                fn = cand
            i = self.d.find(want, i + 1)
        if not fn:
            return out

        o = self.img.off(fn)
        if o is None:
            return out
        body = self.d[o:o + 0x80]

        # mov eax, dword ptr [imm32]   ->  A1 xx xx xx xx
        for j in range(len(body) - 5):
            if body[j] == 0xA1:
                va = struct.unpack_from("<I", body, j + 1)[0]
                if self._in_image(va):
                    out["kPlayerInst"] = va
                    break
        # call dword ptr [eax+imm8|imm32]  ->  FF 50 xx  /  FF 90 xx xx xx xx
        for j in range(len(body) - 6):
            if body[j] == 0xFF and body[j + 1] == 0x50:
                out["kGetMainInstOff"] = body[j + 2]
                break
            if body[j] == 0xFF and body[j + 1] == 0x90:
                out["kGetMainInstOff"] = struct.unpack_from("<I", body, j + 2)[0]
                break
        # the last direct `call rel32` in the body is __OnPressActor; the one
        # after it is the epilogue's stack-check helper, so take the call that
        # is immediately preceded by `mov ecx, edi` (8B CF).
        for j in range(len(body) - 7):
            if body[j] == 0x8B and body[j + 1] == 0xCF and body[j + 2] == 0xE8:
                rel = struct.unpack_from("<i", body, j + 3)[0]
                out["kOnPressActor"] = fn + j + 7 + rel
                break
        if "kPlayerInst" in out:
            out["kPlayerSubObj"] = 4
        return out

    def _fn_calls(self, va, limit=0x1200):
        """Direct `call rel32` targets inside one function, in order.

        Bounded at the first `ret` followed by padding or the next prologue.
        An unbounded window silently merges the NEXT function's calls, which
        already caused one wrong identification - see BOT-SYSTEMS.md 7.3b.

        Walks INSTRUCTIONS, not bytes. The first version treated every 0xE8
        byte as a call opcode, which held until the 2026-08-30 build moved
        the Py_BuildValue format literal "i" to 0x0513E874: `push 0x0513E874`
        encodes as `68 74 E8 13 05`, so the walker took the E8 inside the
        immediate as a call, stepped 5 bytes and landed PAST the real
        `call Py_BuildValue` right behind it. app.GetChannel and
        playerm2g2.GetTargetVID both then reported zero calls and
        kPyBuildValue silently vanished. An immediate or displacement can
        hold any byte, so only a decoder knows where an opcode starts.
        Capstone stops on a byte it cannot decode (inline jump tables); the
        walk then steps over that byte and resumes, which is what the byte
        walker did implicitly.
        """
        o = self.img.off(va)
        if o is None:
            return []
        out, i = [], 0
        while i < limit:
            n = 0
            for ins in MD.disasm(self.d[o + i:o + limit], va + i):
                n += ins.size
                op = ins.bytes[0]
                if op == 0xE8 and ins.size == 5:
                    t = ins.address + 5 + struct.unpack_from("<i", ins.bytes, 1)[0]
                    if self._in_image(t):
                        out.append(t)
                elif op in (0xC3, 0xC2) and ins.mnemonic == "ret":
                    if self.d[o + i + n] in (0xCC, 0x90, 0x55):
                        return out
            i += n + 1          # n < window: an undecodable byte, step over it
        return out

    def skip_collision(self):
        """The engine's own actor-vs-actor collision switch.

        Anchored on the two sentinels the developers chose rather than on any
        address: skip-collision is NOT a bool, it is a magic dword, and the
        exact same constants appear in the aarch64 build. That makes them the
        most stable anchor in this file - a compiler cannot change them and a
        rebuild cannot move them.

            mov dword ptr [ecx + off32], 0x000A35F5   -> EnableSkipCollision
            mov dword ptr [ecx + off32], 0x000F35F5   -> DisableSkipCollision
            cmp dword ptr [ecx + off32], 0x000A35F5   -> CanSkipCollision

        One match yields the function VA *and* the struct offset at once. The
        CInstanceBase -> CActorInstance step comes from the client's own call
        site (`mov ecx,[reg+0x574]; call EnableSkipCollision`), so it is read
        out of the caller rather than assumed.

        This is deliberately actor-only. Terrain blocking lives in
        CPythonBackground::IsBlock and is NOT touched here - walking through
        scenery is server-visible, walking through a mob is not.
        """
        out = {}
        for key, tag in (("kEnableSkipCollision", 0x000A35F5),
                         ("kDisableSkipCollision", 0x000F35F5)):
            pat = b"\xC7\x81"                      # mov dword [ecx+imm32], imm32
            for o in self._find_all(pat):
                if struct.unpack_from("<I", self.d, o + 6)[0] != tag:
                    continue
                off = struct.unpack_from("<I", self.d, o + 2)[0]
                if not (0x100 <= off <= 0x8000):   # a plausible struct field
                    continue
                out[key] = self.tva + (o - self.tro)
                out["kSkipCollisionOff"] = off
                break

        # CInstanceBase -> CActorInstance, from the caller of Enable.
        en = out.get("kEnableSkipCollision")
        if en:
            for site in self.callers(en):
                o = self.img.off(site)
                # look back for `mov ecx, [reg+imm32]`  (8B 8x imm32)
                for back in range(3, 12):
                    p = o - back
                    if self.d[p] == 0x8B and (self.d[p + 1] & 0xF8) == 0x88:
                        cand = struct.unpack_from("<I", self.d, p + 2)[0]
                        if 0x100 <= cand <= 0x2000:
                            out["kInstActorOff"] = cand
                            break
                if "kInstActorOff" in out:
                    break
        return out

    def actor_pass(self):
        """The race whitelist that decides which actors are allowed to be
        walked through - the engine's own body-block exemption.

        `CActorInstance::IsBlockObject` (the movement one, not the raycaster) is
        a cascade of exemptions, each falling through to `xor eax,eax; ret 4`.
        Two of them are hardcoded race ranges:

            mov ecx, esi        \\
            call GetRace         |  if (race >= LO && race <= HI)
            cmp eax, LO          |      return NOT_BLOCKING;
            jb  short +0x12      |
            mov ecx, esi         |
            call GetRace         |
            cmp eax, HI          |
            jbe <exit>          /

        Widening the first range to [0, 0xFFFFFFFF] exempts every actor, which
        is verified live to remove BOTH the displacement (the adjuster stops
        being called at all) and the collision-loop BlockMovement, while leaving
        terrain untouched - terrain is a different predicate with its own
        callers.

        LO/HI are server data (mount vnums: 34001-34200 and 60420-60440 on the
        build this was found on) so they are NOT part of the signature. The
        shape is: the `jb short +0x12` displacement is exactly the length of the
        second half, and both calls must land on the same one-instruction field
        getter `mov eax,[ecx+off]; ret`. That is what is matched.

        Returns the ADDRESSES OF THE TWO IMMEDIATE FIELDS, not of the
        instructions - the stub patches data, never control flow. Restoring is
        then just writing the original dwords back.
        """
        out = {}
        found = []
        for o in self._find_all(b"\x72\x12"):          # jb short +0x12
            s = o - 12                                  # start of the shape
            if s < self.tro or s + 32 > self.tro + self.trs:
                continue

            def is_mov_ecx(p):
                # 8B /r with mod=11 and reg=ecx  ->  mov ecx, <reg>
                return (self.d[p] == 0x8B and
                        (self.d[p + 1] & 0xC0) == 0xC0 and
                        ((self.d[p + 1] >> 3) & 7) == 1)

            if not (is_mov_ecx(s) and self.d[s + 2] == 0xE8 and
                    self.d[s + 7] == 0x3D and
                    is_mov_ecx(s + 14) and self.d[s + 16] == 0xE8 and
                    self.d[s + 21] == 0x3D and
                    self.d[s + 26] == 0x0F and self.d[s + 27] == 0x86):
                continue

            # Both calls must reach the same function...
            va = self.tva + (s - self.tro)
            t1 = va + 7 + struct.unpack_from("<i", self.d, s + 3)[0]
            t2 = va + 21 + struct.unpack_from("<i", self.d, s + 17)[0]
            if t1 != t2:
                continue

            # ...and that function must be a plain field getter, which is what
            # makes this a RACE range rather than a coincidental pair of compares.
            g = self.img.off(t1)
            if g is None or self.d[g] != 0x8B or self.d[g + 1] != 0x81 or \
               self.d[g + 6] != 0xC3:
                continue
            race_off = struct.unpack_from("<I", self.d, g + 2)[0]
            if not (0x100 <= race_off <= 0x8000):
                continue

            lo = struct.unpack_from("<I", self.d, s + 8)[0]
            hi = struct.unpack_from("<I", self.d, s + 22)[0]
            if lo > hi:
                continue

            found.append({"loVA": self.tva + (s + 8 - self.tro),
                          "hiVA": self.tva + (s + 22 - self.tro),
                          "lo": lo, "hi": hi,
                          "raceOff": race_off, "getter": t1})

        if not found:
            return out

        # Both ranges belong to the SAME function and sit back to back, so take
        # the pair that shares a race getter and are adjacent. Two ranges is what
        # makes an exact carve-out possible: widen the first to everything BELOW
        # a band and the second to everything ABOVE it, and the band itself stays
        # solid. That is how metin stones (race 8005, a band of their own well
        # clear of mobs at 401-599) keep their collision while everything else
        # loses it - no per-frame work and no approximation.
        found.sort(key=lambda f: f["loVA"])
        out["kActorPassLoVA"] = found[0]["loVA"]
        out["kActorPassHiVA"] = found[0]["hiVA"]
        out["kActorPassLo"] = found[0]["lo"]
        out["kActorPassHi"] = found[0]["hi"]
        out["kActorRaceOff"] = found[0]["raceOff"]
        out["kActorRaceGetter"] = found[0]["getter"]

        for f in found[1:]:
            if f["getter"] != found[0]["getter"]:
                continue
            # Adjacent means "the next range in the same cascade". 0x40 is
            # generous for the 32-byte shape plus the mov/call between them, and
            # tight enough not to reach an unrelated pair elsewhere.
            if not (0 < f["loVA"] - found[0]["loVA"] <= 0x40):
                continue
            out["kActorPass2LoVA"] = f["loVA"]
            out["kActorPass2HiVA"] = f["hiVA"]
            out["kActorPass2Lo"] = f["lo"]
            out["kActorPass2Hi"] = f["hi"]
            break
        return out

    def auto_move(self):
        """CPythonPlayer's "a walk-to-waypoint is in progress" flag.

        Set in AutoMoveToPosition once the route is accepted, and cleared on
        arrival, on cancel and when the path cannot be built:

            mov byte [esi+off], 1     ; committed to walking
            call <build the route>
            test al, al
            je   <failed>             ; -> clears it again
            mov  al, 1                ; returned true: we are walking

        That five-instruction shape is what is matched, because the offset moves
        between builds while the shape does not. Unique in .text on every build
        checked.

        This is the honest signal for "travelling": every route goes through
        AutoMoveToPosition, whether it came from a mod calling api.move_to or
        from the player clicking the atlas, so one flag covers both without
        mods having to announce themselves.
        """
        for o in self._find_all(b"\xC6"):
            if (self.d[o + 1] & 0xF8) != 0x80:      # mov byte [reg+imm32], imm8
                continue
            if self.d[o + 6] != 0x01:               # ...the imm8 is 1
                continue
            if self.d[o + 7] != 0xE8:               # call rel32
                continue
            if self.d[o + 12] != 0x84 or self.d[o + 13] != 0xC0:   # test al,al
                continue
            if self.d[o + 14] != 0x74:              # je short (the failure path)
                continue
            if self.d[o + 16] != 0xB0 or self.d[o + 17] != 0x01:   # mov al,1
                continue
            off = struct.unpack_from("<I", self.d, o + 2)[0]
            if not (0x1000 <= off <= 0x80000):      # inside the big player block
                continue
            out = {"kAutoMoveActiveOff": off}

            # The destination and the "enabled" byte are written just above, in
            # the same function. Read them off the instructions rather than by a
            # fixed delta from the flag - the delta happens to be the same on
            # every build checked, but nothing guarantees that, and a wrong
            # diagnostic field is worse than no diagnostic field.
            #
            #   mov   byte [reg+E], 1          <- enabled
            #   movss dword [reg+D],   xmm     <- destination X
            #   movss dword [reg+D+4], xmm     <- destination Y
            #   mov   byte [reg+A], 1          <- the flag we anchored on
            back = max(self.tro, o - 0x80)
            dest = None
            p = back
            while p < o - 8:
                # F3 0F 11 /r with mod=10 (disp32) -> movss [reg+imm32], xmm
                if (self.d[p] == 0xF3 and self.d[p + 1] == 0x0F and
                        self.d[p + 2] == 0x11 and (self.d[p + 3] & 0xC0) == 0x80):
                    d1 = struct.unpack_from("<I", self.d, p + 4)[0]
                    q = p + 8
                    if (self.d[q] == 0xF3 and self.d[q + 1] == 0x0F and
                            self.d[q + 2] == 0x11 and (self.d[q + 3] & 0xC0) == 0x80):
                        d2 = struct.unpack_from("<I", self.d, q + 4)[0]
                        if d2 == d1 + 4 and d1 < off:
                            dest = d1          # x and y are contiguous
                p += 1
            if dest is not None:
                out["kAutoMoveDestOff"] = dest

            p = back
            while p < o:
                if (self.d[p] == 0xC6 and (self.d[p + 1] & 0xF8) == 0x80 and
                        self.d[p + 6] == 0x01):
                    e = struct.unpack_from("<I", self.d, p + 2)[0]
                    if e < off and (dest is None or e < dest):
                        out["kAutoMoveEnabledOff"] = e
                p += 1

            # The AUTO-MOVE STATE constant, which is what actually distinguishes
            # "following a route" from "cancelled".
            #
            #     cmp dword ptr [esi+0x58], 0x8A      81 7E 58 8A 00 00 00
            #
            # Measured live: player_state reads 0x8A while a waypoint route
            # walks and 0x85 the moment it is cancelled, while auto_move_active
            # stays 1 for ever. So this comparison - the engine's own test for
            # "am I auto-moving" - is the signal, and the constant is read out
            # of the instruction rather than hardcoded in a mod.
            q = max(self.tro, o - 0x140)
            while q < o - 7:
                if self.d[q] == 0x81 and (self.d[q + 1] & 0xF8) == 0x78:
                    st_off = self.d[q + 2]                     # disp8
                    val = struct.unpack_from("<I", self.d, q + 3)[0]
                    if 0x20 <= st_off <= 0xFF and 0x80 <= val <= 0xFF:
                        out["kPlayerStateOff2"] = st_off
                        out["kPlayerWalkState"] = val
                        break
                q += 1
            return out
        return {}

    def block_movement(self):
        """CActorInstance::BlockMovement - the thing that STOPS movement.

            mov eax,[ecx+off32] ; test eax,eax ; cmovne ecx,eax ; jmp <tail>

        the x86 twin of the aarch64
            ldr x8,[x0,#off] ; cmp x8,#0 ; csel x0,x0,x8,eq ; b ...

        It forwards to the real implementation after walking one link of the
        attached-instance chain, which is why the body is a jmp rather than a
        call. The struct offset moves between builds so it is a wildcard; the
        instruction shape is the signature, and it matches EXACTLY ONCE in the
        whole .text.

        This is the TERRAIN stop. Monsters do not use it to block - they
        displace instead, and only reach it after the collision retry loop has
        already given up. Neutering it is what makes walls stop mattering, and
        that is a much bigger deal than walking through a mob: the map attribute
        is what the server also knows about.
        """
        pat = (b"\x8B\x81", b"\x85\xC0\x0F\x45\xC8\xE9")
        hits = []
        for o in self._find_all(pat[0]):
            if self.d[o + 6:o + 12] != pat[1]:
                continue
            off = struct.unpack_from("<I", self.d, o + 2)[0]
            if not (0x100 <= off <= 0x8000):
                continue
            hits.append(self.tva + (o - self.tro))
        # Ambiguity here would mean patching an arbitrary function, so a second
        # match is a hard failure rather than a "take the first".
        if len(hits) != 1:
            return {}
        return {"kBlockMovement": hits[0]}

    def autohunt_api(self):
        """Every address and struct offset the autohunt bridge needs.

        All of it chains off ONE stable anchor - the `/auto_hunt end` literal -
        plus the `AttackPickedActor` PyMethodDef name, so a new build needs no
        research: re-run this and the whole set moves with it. Verified live on
        the deployed build (BOT-SYSTEMS.md 7.3c).

            "/auto_hunt end"  -> AutoHuntLoop
            AutoHuntLoop      -> FindAndSetNewTarget  (its only callee that
                                                       calls __OnPressActor)
                              -> CPythonPlayer::Update (its only caller)
            Update            -> __Update_AutoAttack  (the callee opening with
                                                       two `cmp [r+x],0`)
            FindAndSetNewTarget body -> minimap singleton, autoHuntRange off,
                                        anchor off
            __Update_AutoAttack body -> auto-attack vid off, GetMainInstance
                                        vtable slot
            __OnPressActor body      -> player state off (the `cmp ..,0x89`)

        Returns {} on any miss: these are optional in the stub and a failure
        must only disable the feature, never break the run.
        """
        out = {}
        base = self.attack_bridge()          # kOnPressActor, kPlayerInst, ...
        out.update(base)
        onpress = base.get("kOnPressActor")
        if not onpress:
            return out

        # ---- AutoHuntLoop, from the chat literal it alone emits -------------
        loop = None
        for va in sorted(self._fns_pushing("/auto_hunt end")):
            loop = va
            break
        if not loop:
            return out
        out["kAutoHuntLoop"] = loop

        # ---- FindAndSetNewTarget: the callee that calls __OnPressActor ------
        for t in dict.fromkeys(self._fn_calls(loop)):
            if onpress in self._fn_calls(t):
                out["kFindAndSetNewTarget"] = t
                break
        fasnt = out.get("kFindAndSetNewTarget")

        # ---- minimap singleton / range / anchor, from that body -------------
        if fasnt:
            o = self.img.off(fasnt)
            body = self.d[o:o + 0x120]
            for j in range(len(body) - 6):
                # mov eax,[imm32]  (A1) - the minimap singleton holder
                if body[j] == 0xA1 and "kMiniMapInst" not in out:
                    va = struct.unpack_from("<I", body, j + 1)[0]
                    if self._in_image(va):
                        out["kMiniMapInst"] = va
                # movss xmm0,[eax+imm32]  F3 0F 10 80 xx xx xx xx
                if body[j:j + 4] == b"\xf3\x0f\x10\x80" and "kAutoHuntRangeOff" not in out:
                    out["kAutoHuntRangeOff"] = struct.unpack_from("<I", body, j + 4)[0]
                # The anchor is passed to FindVictim as `this + off`. MSVC emits
                # that as `add ecx,imm32` on one build and `lea reg,[reg+imm32]`
                # on another, so accept both rather than assume one form.
                if "kAnchorOff" not in out:
                    off = None
                    if body[j:j + 2] == b"\x81\xc1":            # add ecx,imm32
                        off = struct.unpack_from("<I", body, j + 2)[0]
                    elif body[j] == 0x8D and (body[j + 1] & 0xC7) == 0x80:
                        off = struct.unpack_from("<I", body, j + 2)[0]  # lea r,[r+imm32]
                    if off and 0x1000 < off < 0x100000:
                        out["kAnchorOff"] = off

        # ---- Update -> __Update_AutoAttack ---------------------------------
        up = self.callers(loop)
        if up:
            upd = self.func_start(up[0])
            if upd:
                out["kPlayerUpdate"] = upd
                for t in dict.fromkeys(self._fn_calls(upd)):
                    o = self.img.off(t)
                    if o is None:
                        continue
                    head = self.d[o:o + 0x30]
                    # opens with: cmp dword [reg+x],0 / je / cmp dword [reg+y],0
                    k = head.find(b"\x83\x7e")           # cmp dword [esi+x],0
                    if k != -1 and head.find(b"\x83\x7e", k + 1) != -1:
                        out["kUpdateAutoAttack"] = t
                        out["kAutoAtkVidOff"] = head[k + 2] + 4   # 2nd of the pair
                        break

        # ---- player state offset, from __OnPressActor's 0x89 check ---------
        o = self.img.off(onpress)
        head = self.d[o:o + 0x40]
        k = head.find(b"\x89\x00\x00\x00")               # imm32 0x89
        if k > 2:
            out["kPlayerStateOff"] = head[k - 1]

        # ---- bStone, from AutoHuntLoop's own call site ----------------------
        # AutoHuntLoop passes it as `movzx eax, byte [edi+i32]` immediately
        # before calling FindAndSetNewTarget, so anchor on the call rather than
        # on the byte - there are other byte loads from that struct.
        if fasnt:
            body = self._fn_body(loop)
            for j in range(len(body) - 16):
                if body[j:j + 3] != b"\x0f\xb6\x87":
                    continue
                seg = body[j + 7:j + 7 + 12]
                k = seg.find(b"\xe8")
                if k == -1:
                    continue
                site = loop + j + 7 + k
                if site + 5 + struct.unpack_from("<i", seg, k + 1)[0] == fasnt:
                    out["kHuntStoneOff"] = struct.unpack_from("<I", body, j + 3)[0]
                    break

        # The CPythonItem singleton, for ground-item enumeration. Anchored on
        # playerm2g2.PickCloseItem, whose body opens
        #     mov eax, [singleton] ; mov edi, [eax] ; lea ecx, [edi+4]
        # i.e. load the holder, deref to the object, take the map at +4.
        b = self.binding("playerm2g2", "PickCloseItem")
        if b:
            for t in self._fn_calls(b):
                body = self._fn_body(t)
                m = re.search(rb"\xa1(....)\x8b\x38", body, re.S)   # mov eax,[imm32]; mov edi,[eax]
                if m:
                    va = struct.unpack("<I", m.group(1))[0]
                    if self._in_image(va):
                        out["kItemInst"] = va
                        break

        out.update(self._skill_api(out.get("kGetMainInstOff")))
        return out

    def _fn_body(self, va, limit=0x1200):
        """Bytes of one function, cut at the int3 padding that follows it."""
        o = self.img.off(va)
        if o is None:
            return b""
        body = self.d[o:o + limit]
        k = body.find(b"\xcc\xcc\xcc")
        return body[:k] if k != -1 else body

    def ret_imm(self, va, limit=0x4000):
        """(bytes the callee cleans, note) for the function at `va`.

        EVERY `ret` in one function carries the same immediate - the calling
        convention is a property of the function, not of the exit path. So a
        region whose rets DISAGREE is not one function: it is two, packed
        without int3 padding between them. `OpenCharacterMenu` is exactly that
        (`ret 4` twice, then `ret 0xc` five times from the next function), and
        reading the last one would have declared it a three-argument function.

        Returns the immediate of the leading run, and a note when the region
        turned out to hold more than one function.
        """
        o = self.img.off(va)
        if o is None:
            return None, "unmapped"
        end = o
        while end < o + limit and not (self.d[end] == 0xCC and self.d[end + 1] == 0xCC):
            end += 1
        imms = []
        for ins in MD.disasm(self.d[o:end], va):
            if ins.mnemonic == "ret":
                imms.append(int(ins.op_str, 16) if ins.op_str else 0)
        if not imms:
            return None, "no ret found"
        first = imms[0]
        if all(i == first for i in imms):
            return first, ""
        return first, ("region holds >1 function (saw %s); took the leading run"
                       % sorted(set(imms)))

    # ---- the native-call gateway ------------------------------------------
    STACK_WIDTH = {"i32": 4, "u32": 4, "vid": 4, "vnum": 4, "f32": 4,
                   "ptr": 4, "cstr": 4, "bool32": 4,
                   "i64": 8, "u64": 8, "f64": 8}

    def py_glue(self):
        """The CPython glue the stub needs to register a real Python module.

        All of it comes off the module-registration call sites, which every one
        of the ~50 game modules goes through:

            push <methods table>      68 xx xx xx xx
            push <module name>        68 yy yy yy yy
            call InitModule           E8 rel

        InitModule's own first two calls are PyImport_AddModule (which is what
        makes the module importable - it lands in sys.modules) and
        PyModule_AddFunctions.

        The marshalling helpers come off ONE simple binding whose shape is
        unambiguous: a single-bool setter, i.e.
            PyTuple_GetBoolean(args, 0, &b)  -> on failure Py_BuildException
                                             -> on success Py_BuildNone

        IDENTITY-ANCHORED, deliberately. A first attempt matched the byte
        pattern anywhere in .text and took the most-voted call target; it
        returned 0x04625750 for InitModule (wrong - it is 0x0084E1A0) and
        labelled PyTuple_GetBoolean as Py_BuildNone. The ret-immediate
        cross-check cannot catch that class of error: those functions have the
        right SHAPE, they are simply not the right functions.

        So every anchor here starts from a name. The PyMethodDef tables carry
        their method names as plain strings, so `miniMap.SetAutoHuntRangeStatus`
        is an exact, checkable starting point rather than a fingerprint.
        """
        out = {}
        tables = self._py_tables()
        tabvas = set(tables)

        # InitModule: the call right after `push <a real methods table>;
        # push <name>`. Constraining the pushed table to one we actually
        # harvested is what makes this exact.
        votes = {}
        for sva, ro, rs in self._text_spans():
            blob = self.d[ro:ro + rs]
            for m in re.finditer(rb"\x68(....)\x68(....)\xe8(....)", blob, re.S):
                if struct.unpack("<I", m.group(1))[0] not in tabvas:
                    continue
                tgt = sva + m.start() + 15 + struct.unpack("<i", m.group(3))[0]
                if self._in_image(tgt):
                    votes[tgt] = votes.get(tgt, 0) + 1
        if not votes:
            return out
        init = max(votes, key=lambda k: votes[k])
        if votes[init] < 5:              # ~50 game modules go through it
            return out
        out["kPyInitModule"] = init
        inner = self._fn_calls(init)
        if len(inner) >= 2:
            out["kPyImportAddModule"] = inner[0]
            out["kPyModuleAddFunctions"] = inner[1]

        # Marshalling, off one binding whose body was read by hand:
        #   miniMapSetAutoHuntRangeStatus(self, args)
        #     PyTuple_GetBoolean(args, 0, &b);  if (!ok) return Py_BuildException
        #     ... ; return Py_BuildNone()
        setter = self.binding("miniMap", "SetAutoHuntRangeStatus", tables)
        if setter:
            calls = self._fn_calls(setter)
            if len(calls) >= 3:
                out["kPyTupleGetBoolean"] = calls[0]
                out["kPyBuildException"] = calls[1]
                out["kPyBuildNone"] = calls[-1]

        # The unsigned-int getter, from a binding that takes exactly one VID.
        # Cross-checked against two more single-uint bindings (HasInstance,
        # ClickSkillSlot) - all three open with the same call, which is what
        # makes this an identification rather than a guess.
        for mod, meth in (("pack_chr", "SelectInstance"),
                          ("pack_chr", "HasInstance"),
                          ("playerm2g2", "ClickSkillSlot")):
            b = self.binding(mod, meth, tables)
            if not b:
                continue
            calls = self._fn_calls(b)
            if not calls:
                continue
            if "kPyTupleGetUInt" not in out:
                out["kPyTupleGetUInt"] = calls[0]
            elif out["kPyTupleGetUInt"] != calls[0]:
                out.pop("kPyTupleGetUInt")       # disagreement -> fail closed
                break

        # The string getter. Anchored on two bindings that take a string at
        # DIFFERENT argument positions - SendChatPacket(text) reads it first,
        # AppendChat(type, text) second - so agreement between them is evidence
        # of identity rather than of position.
        a = self.binding("m2netm2g", "SendChatPacket", tables)
        b = self.binding("chat", "AppendChat", tables)
        ca = self._fn_calls(a) if a else []
        cb = self._fn_calls(b) if b else []
        if ca and len(cb) >= 3 and ca[0] == cb[2]:
            out["kPyTupleGetString"] = ca[0]

        # The value builder, so a native can RETURN something rather than being
        # limited to None. Anchored on two no-argument int-returning bindings -
        # the builder is their ONLY call.
        #
        # It is Py_BuildValue(const char* fmt, ...), NOT a plain int builder:
        # app.GetChannel() pushes the literal "i" and then the value, and cleans
        # 8 bytes. Calling it with the value in the format position raised
        # "SystemError: bad format char passed to Py_BuildValue" - cleanly, but
        # wrong. The caller must supply the format.
        # The float getter. AutoMoveToPosition(x, y) calls it TWICE - one per
        # float - which is what distinguishes it from the int getter rather
        # than merely "it is the first call".
        f = self.binding("playerm2g2", "AutoMoveToPosition", tables)
        fc = self._fn_calls(f) if f else []
        if len(fc) >= 3 and fc[0] == fc[2]:
            out["kPyTupleGetFloat"] = fc[0]

        # 2026-08-30 build: both bindings are byte-identical in shape, but
        # the "i" literal moved to 0x0513E874, whose encoding carries an 0xE8
        # byte inside the `push imm32`. The old byte-level call walker took
        # it for the call opcode and skipped the real one, so both bindings
        # reported NO calls. Fixed in _fn_calls (it now decodes instructions);
        # the anchor itself - one call each, and they must agree - is intact.
        cands = []
        for mod, meth in (("app", "GetChannel"),
                          ("playerm2g2", "GetTargetVID")):
            f = self.binding(mod, meth, tables)
            c = self._fn_calls(f) if f else []
            if len(c) == 1:
                cands.append(c[0])
        if len(cands) == 2 and cands[0] == cands[1]:
            out["kPyBuildValue"] = cands[0]
        return out

    def _text_spans(self):
        return [(sva, ro, rs) for nm, sva, vs, ro, rs in self.img.secs
                if nm.startswith(".text")]

    def _py_tables(self):
        """{table VA: [(method name, function VA)]} - the PyMethodDef arrays."""
        if "pytables" in self._cache:
            return self._cache["pytables"]
        tlo = thi = None
        for nm, sva, vs, ro, rs in self.img.secs:
            if nm.startswith(".text"):
                tlo, thi = sva, sva + vs
        ok = {0x0, 0x1, 0x2, 0x3, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80, 0x81,
              0x83, 0x84, 0x88, 0x90, 0xC0, 0x100, 0x180, 0x200}
        entries = {}
        for nm, sva, vs, ro, rs in self.img.secs:
            if nm not in (".rdata", ".data"):
                continue
            blob = self.d[ro:ro + rs]
            for off in range(0, len(blob) - 16, 4):
                nmp, fn, fl, doc = struct.unpack_from("<IIII", blob, off)
                if not (nmp and fn) or fl not in ok or not (tlo <= fn < thi):
                    continue
                s = self._cstr(nmp)
                if s:
                    entries[sva + off] = (s, fn)
        tables, cur = {}, []
        for va in sorted(entries):
            if cur and va == cur[-1] + 16:
                cur.append(va)
            else:
                if len(cur) >= 2:
                    tables[cur[0]] = [entries[v] for v in cur]
                cur = [va]
        if len(cur) >= 2:
            tables[cur[0]] = [entries[v] for v in cur]
        self._cache["pytables"] = tables
        return tables

    def _cstr(self, va, maxn=64):
        o = self.img.off(va)
        if o is None:
            return None
        e = self.d.find(b"\0", o, o + maxn)
        if e < 0:
            return None
        s = self.d[o:e]
        if not s or not re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_.]*", s):
            return None
        return s.decode("latin1")

    def binding(self, module, method, tables=None):
        """VA of a Python binding, addressed the way a human would: by name.

        Resolving `module` needs the Py_InitModule call site, because the game
        uses the old form where the module name is an immediate pushed beside
        the table rather than a field in a PyModuleDef.
        """
        tables = tables if tables is not None else self._py_tables()
        # Search per table rather than scanning for the generic `68 .. 68 ..`
        # shape: re.finditer yields NON-OVERLAPPING matches, so a registration
        # site that begins inside a previous match is silently skipped. That is
        # how playerm2g2 went missing even though its site exists at 0x665CB1.
        for tva, entries in tables.items():
            pat = b"\x68" + struct.pack("<I", tva) + b"\x68"
            for sva, ro, rs in self._text_spans():
                i = self.d.find(pat, ro, ro + rs)
                if i < 0:
                    continue
                nameva = struct.unpack_from("<I", self.d, i + 6)[0]
                if self._cstr(nameva, 48) != module:
                    continue
                for name, fn in entries:
                    if name == method:
                        return fn
                return None
        return None

    def offlineshop_cap(self):
        """MARKET-SCAN offline-shop price capture: the recv handler + the
        CPythonOfflineshop singleton global. Optional - an absent key disables
        the feature (the stub treats absent as off, never a zero-as-address).

        Handler (CPythonNetworkStream::RecvOfflineshopPacket): the unique string
        "UNKNOWN OFFLINESHOP SUBHEADER" is pushed exactly once, inside its
        default: case. From that lone `push imm32`, scan BACKWARD to the nearest
        stack-realign prologue 53 8B DC 83 EC ?? 83 E4 F8 83 C4 04 55 8B 6B 04
        (the `sub esp,imm8` byte wildcarded) -> the function start. The prologue
        alone is NOT unique, so it is always paired with the unique string-push.

        Singleton: offlineshop.GetShopUnlockSlotCount (a name unique to this
        module) opens with `A1 <imm32> ; 8B 00` (mov eax,[global]; mov eax,[eax]);
        the imm32 is the singleton global. Instance is *(*(global)) at runtime.
        """
        out = {}
        # --- handler, via the unique string ---
        sva = None
        i = self.d.find(b"UNKNOWN OFFLINESHOP SUBHEADER")
        if i != -1:
            for nm, s2, vs, ro, rs in self.img.secs:
                if ro <= i < ro + rs:
                    sva = s2 + (i - ro)
                    break
        if sva is not None:
            pushes = self.pushes_of(sva)
            if len(pushes) == 1:
                po = self.img.off(pushes[0])
                if po is not None:
                    sig = re.compile(
                        rb"\x53\x8B\xDC\x83\xEC.\x83\xE4\xF8\x83\xC4\x04\x55\x8B\x6B\x04",
                        re.S)
                    # the handler is a large dispatcher; the string-push sits deep
                    # in its body (~0x1300 past the prologue). Scan a wide window
                    # back and take the NEAREST prologue (its own function start).
                    lo = max(self.tro, po - 0x3000)
                    last = None
                    for m in sig.finditer(self.d[lo:po]):
                        last = m.start()
                    if last is not None:
                        fnoff = lo + last
                        out["kOfflineshopRecv"] = self.tva + (fnoff - self.tro)
        # --- singleton global, via GetShopUnlockSlotCount's first instruction ---
        b = self.binding("offlineshop", "GetShopUnlockSlotCount")
        if b:
            o = self.img.off(b)
            if o is not None:
                va = None
                if self.d[o] == 0xA1 and self.d[o + 5] == 0x8B and self.d[o + 6] == 0x00:
                    va = struct.unpack_from("<I", self.d, o + 1)[0]          # mov eax,[imm32]
                elif self.d[o] == 0x8B and (self.d[o + 1] & 0xC7) == 0x05:
                    va = struct.unpack_from("<I", self.d, o + 2)[0]          # mov reg,[imm32]
                if va is not None and self._in_image(va):
                    out["kOfflineshopInst"] = va
        return out

    def minimap_waypoint(self):
        """The two CPythonMiniMap waypoint natives, for the type-13 target mark.

        Python's `miniMap.AddWayPoint` binding hardcodes mark type 6, which
        renders the animated swirl on the ATLAS only. The native underneath it
        takes the type as its first argument; type 13 is the blinking target
        mark that draws on BOTH the minimap and the atlas and auto-tracks a VID
        every frame (CPythonMiniMap::Update). Its calling convention is
        nonstandard - x arrives in XMM3 and the name is a by-value std::string -
        so the stub calls it through a hand-rolled thunk rather than the generic
        gateway; this only supplies the two addresses.

        Both are anchored inside their own Python binding bodies, located by
        name through the miniMap PyMethodDef table:

          AddWayPoint    - the binding pushes the literal `6` (`6A 06`, the
                           hardcoded type) immediately before calling the native.
                           `6A 06 E8 <rel32>` is unique in that body.
          RemoveWayPoint - the binding derefs the singleton and pushes the id,
                           then `mov ecx,[ecx]` (`8B 09`) right before the call:
                           `8B 09 E8 <rel32>` is unique in that body.

        Each body is bounded above by the next binding in the miniMap table, so
        a small binding's window cannot bleed into its neighbour (RemoveWayPoint
        is ~0x40 bytes and the shape recurs in the following binding otherwise).

        Returns {} if either anchor is missing or ambiguous - the feature is
        then simply off, never a zero the stub would treat as an address."""
        # Locate the miniMap PyMethodDef table and collect every binding VA, so
        # each body can be bounded by the next function that starts after it.
        entries = None
        for tva, ents in self._py_tables().items():
            pat = b"\x68" + struct.pack("<I", tva) + b"\x68"
            for sva, ro, rs in self._text_spans():
                i = self.d.find(pat, ro, ro + rs)
                if i < 0:
                    continue
                if self._cstr(struct.unpack_from("<I", self.d, i + 6)[0], 48) == "miniMap":
                    entries = ents
                break
            if entries is not None:
                break
        if not entries:
            return {}
        fns = sorted(set(fn for _, fn in entries))

        out = {}
        specs = (("AddWayPoint",    b"\x6A\x06\xE8", "kMiniMapAddWayPoint"),
                 ("RemoveWayPoint",  b"\x8B\x09\xE8", "kMiniMapRemoveWayPoint"))
        for method, pat, key in specs:
            bva = self.binding("miniMap", method)
            if not bva:
                return {}
            o = self.img.off(bva)
            if o is None:
                return {}
            nxt = min((f for f in fns if f > bva), default=bva + 0x400)
            body = self.d[o:o + min(0x400, nxt - bva)]
            hits = [m.start() for m in re.finditer(re.escape(pat), body)]
            if len(hits) != 1:
                return {}                       # 0 = shape moved, >1 = ambiguous
            i = hits[0] + len(pat)              # first byte of the rel32
            rel = struct.unpack_from("<i", body, i)[0]
            tgt = self.tva + (o + i - self.tro) + 4 + rel
            if not self._in_image(tgt):
                return {}
            out[key] = tgt
        return out

    def natives(self, registry_path):
        """Resolve every entry in natives.json and CROSS-CHECK it.

        The registry declares an argument list; the binary states, in its `ret`
        immediate, how many bytes the callee actually cleans. For thiscall and
        stdcall those must agree exactly. A mismatch is not a warning - the
        entry is DROPPED, because a wrong argument spec is a stack imbalance at
        runtime rather than an exception, and the trampoline cannot detect it.

        cdecl carries no immediate, so it cannot be cross-checked at all; those
        entries are emitted marked `declared-only` and must have had their call
        site read by hand.
        """
        reg = json.load(open(registry_path))
        known = self.autohunt_api()
        extra = self._native_extras(known)
        addrs = dict(known)
        addrs.update(extra)

        NAMEMAP = {
            "FindAndSetNewTarget": "kFindAndSetNewTarget",
            "OnPressActor": "kOnPressActor",
            "UseAutoSkills": "kUseAutoSkills",
            "UpdateAutoAttack": "kUpdateAutoAttack",
            "OpenCharacterMenu": "kOpenCharacterMenu",
            "ReserveProcessClickActor": "kReserveProcessClickActor",
            "CreateAutoBotSettings": "kCreateAutoBotSettings",
            "SendChatPacket": "kSendChatPacket",
        }

        rows, problems = [], []
        for n in reg.get("natives", []):
            key = NAMEMAP.get(n["name"])
            va = addrs.get(key) if key else None
            if not va:
                problems.append("%s: unresolved (%s)" % (n["name"], key))
                continue
            declared = 0
            bad = None
            for a in n["args"]:
                w = self.STACK_WIDTH.get(a["type"])
                if w is None:
                    bad = "unknown type %r" % a["type"]
                    break
                declared += w
            if bad:
                problems.append("%s: %s" % (n["name"], bad))
                continue
            imm, note = self.ret_imm(va)
            conv = n["abi"].rsplit("-", 1)[-1]
            if conv in ("thiscall", "stdcall"):
                if imm is None or imm != declared:
                    problems.append(
                        "%s: DECLARED %d bytes but the binary cleans %s%s"
                        % (n["name"], declared, imm,
                           " [%s]" % note if note else ""))
                    continue
            rows.append({"name": n["name"], "va": va, "conv": conv,
                         "stack": declared, "this": n.get("this") or "",
                         "ret": n["return"]["type"],
                         "checked": conv in ("thiscall", "stdcall")})
        return rows, problems, addrs

    def _native_extras(self, known):
        """The few natives not already produced by autohunt_api()."""
        out = {}
        loop = known.get("kAutoHuntLoop")
        onpress = known.get("kOnPressActor")
        uas = known.get("kUseAutoSkills")

        # AutoHuntLoop calls, in order: __OnPressActor, OpenCharacterMenu,
        # __ReserveProcess_ClickActor. Anchor on the first, take the next two
        # distinct callees after it.
        #
        # The call window is the WHOLE function, cut at its int3 padding, not
        # _fn_calls' default 0x1200 cap. From the 2026-08-18 build on,
        # AutoHuntLoop is 0x13DD bytes (a block of extra checks was inserted
        # ahead of the tail) and the ReserveProcess_ClickActor call sits at
        # +0x1212 - just past the cap, so OpenCharacterMenu was the last call
        # seen and kReserveProcessClickActor came back empty. The 0x1200
        # floor keeps the old behaviour if the padding cut ever comes early.
        if loop and onpress:
            seq = self._fn_calls(loop, limit=max(0x1200, len(self._fn_body(loop, 0x4000))))
            if onpress in seq:
                i = seq.index(onpress)
                rest = [t for t in seq[i + 1:] if t != onpress]
                if len(rest) >= 1:
                    out["kOpenCharacterMenu"] = rest[0]
                if len(rest) >= 2:
                    out["kReserveProcessClickActor"] = rest[1]

        # SendChatPacket: UseAutoSkills pushes "/user_horse_ride" and calls it.
        if uas:
            s = self.find_str("/user_horse_ride")
            if s is not None:
                body = self._fn_body(uas)
                pat = struct.pack("<I", s)
                j = body.find(b"\x68" + pat)
                if j != -1:
                    k = body.find(b"\xe8", j)
                    if k != -1 and k - j < 24:
                        site = uas + k
                        out["kSendChatPacket"] = (
                            site + 5 + struct.unpack_from("<i", body, k + 1)[0])

        # CreateAutoBotSettings, via its binding BY NAME. Matching
        # `mov ecx,[playerInst] / mov ecx,[ecx] / call` instead found the first
        # of many functions with that shape and returned 0x00657840 - wrong,
        # and undetectable by the ret-immediate check because a no-argument
        # thiscall matches thousands of functions.
        b = self.binding("playerm2g2", "CreateAutoBotSettings")
        if b:
            calls = self._fn_calls(b)
            if calls:
                out["kCreateAutoBotSettings"] = calls[0]

        # Singleton holders used as `this` by the registry.
        if uas:
            body = self._fn_body(uas)
            j = body.find(b"\x8b\x0d")          # mov ecx, [imm32]
            if j != -1:
                va = struct.unpack_from("<I", body, j + 2)[0]
                if self._in_image(va):
                    out["kNetStreamInst"] = va
        fas = known.get("kFindAndSetNewTarget")
        if fas:
            body = self._fn_body(fas)
            for m in re.finditer(rb"\xa1(....)", body, re.S):
                va = struct.unpack("<I", m.group(1))[0]
                if self._in_image(va) and va != known.get("kMiniMapInst"):
                    out["kCharMgrInst"] = va
                    break
        return out

    @staticmethod
    def _skill_gate_vec(body):
        """(gate_off, vec_off) if this body has the UseAutoSkills skill shape.

        gate: the function's FIRST  cmp byte [this+i32],0  (0x80 /7, mod=10, the
              base register wildcarded - it is edi on the older builds and ebx
              on 1.0.11, so the old edi-only 0x80,0xBF shape no longer matches).
        vec:  an adjacent pair  mov r32,[this+A] / mov r32,[this+A+4]  sharing
              the same base register (the skill-slot vector begin/end).

        Disambiguated by the gate byte sitting just above the vector in the
        struct - a stable intra-struct relationship (gate == vec + 0x6E on every
        build measured), which rejects the other "/user_horse_ride" frame whose
        shapes match but whose "gate" is an unrelated small offset far from its
        vector. Returns None if either shape is absent or they are not close.
        """
        gate = None
        for k in range(len(body) - 6):
            if body[k] == 0x80 and (body[k + 1] & 0xF8) == 0xB8 \
                    and (body[k + 1] & 7) != 4 and body[k + 6] == 0:
                gate = struct.unpack_from("<I", body, k + 2)[0]
                break
        if gate is None:
            return None
        for j in range(len(body) - 12):
            if body[j] != 0x8B or (body[j + 1] & 0xC0) != 0x80:
                continue
            base = body[j + 1] & 7
            if base in (4, 5):                           # esp = SIB, ebp special
                continue
            a = struct.unpack_from("<I", body, j + 2)[0]
            if body[j + 6] != 0x8B or (body[j + 7] & 0xC0) != 0x80:
                continue
            if (body[j + 7] & 7) != base:
                continue
            if struct.unpack_from("<I", body, j + 8)[0] == a + 4 \
                    and 0 < gate - a <= 0x100:
                return (gate, a)
        return None

    def _skill_api(self, get_main_off):
        """UseAutoSkills and the three struct offsets the skill drive reads.

        Anchored on UseAutoSkills' OWN body shape among the functions that push
        "/user_horse_ride" (it emits a mount command), NOT on being a callee of
        AutoHuntLoop. The loop anchor broke on the 1.0.11 build: an intermediate
        frame sits between AutoHuntLoop and UseAutoSkills, so the direct-callee
        intersection finds nothing. UseAutoSkills is the one such function whose
        body has the gate + skill-vector shapes (see _skill_gate_vec); if two
        match, that is ambiguous and we emit nothing rather than a guess.

            kMountedOff  the byte test right after the GetMainInstance indirect
                         call - IsMountingHorse(), inlined. Anchoring it on
                         `call [eax+kGetMainInstOff]` cross-checks the vtable
                         slot: a wrong slot finds nothing, not a wrong number.
        """
        out = {}
        cands = {}
        for fn in self._fns_pushing("/user_horse_ride"):
            gv = self._skill_gate_vec(self._fn_body(fn, limit=0x2000))
            if gv is not None:
                cands[fn] = gv
        if len(cands) != 1:                 # 0 = not found, >1 = ambiguous
            return out
        uas, (gate, vec) = next(iter(cands.items()))
        out["kUseAutoSkills"] = uas
        out["kHuntUseSkillOff"] = gate
        out["kHuntSkillVecOff"] = vec

        body = self._fn_body(uas, limit=0x2000)

        # hunt_use_mount: the REMOUNT gate. UseAutoSkills dismounts to cast an
        # attack skill, then only gets back on if this byte is set - it is the
        # byte just above hunt_use_skill (gate+1) that the function reads as a
        # second flag. Emitted only when the body actually contains
        # `cmp byte [this+gate+1],0`, so it is a confirmed reference, not a blind
        # "+1". Without it set, the client dismounts and never remounts.
        mount = gate + 1
        for k in range(len(body) - 6):
            if body[k] == 0x80 and (body[k + 1] & 0xF8) == 0xB8 \
                    and (body[k + 1] & 7) != 4 and body[k + 6] == 0 \
                    and struct.unpack_from("<I", body, k + 2)[0] == mount:
                out["kHuntUseMountOff"] = mount
                break

        if get_main_off:
            call = b"\xff\x90" + struct.pack("<I", get_main_off)
            j = body.find(call)
            if j != -1:
                seg = body[j:j + 0x18]
                m = seg.find(b"\x80\x78")                # cmp byte [eax+d8],0
                if m != -1 and len(seg) > m + 3 and seg[m + 3] == 0:
                    out["kMountedOff"] = seg[m + 2]
        return out

    def pack_mgr(self):
        """(CEterPackManager singleton VA, its Get method VA).

        RunFile pulls the script through the VFS:
            A1 <inst>   mov eax,[singleton]
            ...
            8B 08       mov ecx,[eax]        ; this
            E8 <rel>    call Get
        Not used by the stub yet - this is the chokepoint a loose-file overlay
        would hook, and harvesting it now costs nothing."""
        rf = self.run_file()
        if not rf:
            return None, None
        o = self.img.off(rf)
        for j in range(0x100):
            if self.d[o + j] == 0x8B and self.d[o + j + 1] == 0x08 and self.d[o + j + 2] == 0xE8:
                get = rf + j + 7 + struct.unpack_from("<i", self.d, o + j + 3)[0]
                for k in range(j, max(0, j - 0x30), -1):
                    if self.d[o + k] == 0xA1:
                        va = struct.unpack_from("<I", self.d, o + k + 1)[0]
                        if self._in_image(va):
                            return va, get
                return None, get
        return None, None


def resolve_all(path, r=None):
    """{name: VA} for every address the stub reads from uriel_offsets.ini.

    The patcher imports this rather than shelling out, so this function and
    main() must stay in agreement - they did NOT for a while, and the vendored
    copy silently drifted a whole feature behind. Keep exactly one place that
    knows the key list: this one.

    Values are None when unresolved; the caller decides what is fatal. Anything
    autohunt_api() cannot find is OMITTED rather than emitted as zero, because
    the stub treats an absent key as "feature off" but a zero as an address."""
    r = r or Resolver(path)
    iatmap = r.iat()
    _cb, obj = r.fire_in_the_hole()
    pack_inst, pack_get = r.pack_mgr()
    vals = {
        "kTraceSink": r.trace_sink(),
        "kTraceReal": r.trace_real(),
        "kVerifyBufsEqual": r.verify_bufs_equal(),
        "kAuthRecvPhase": r.auth_recv_phase(),
        "kAuthProcess": r.auth_process(),
        "kGetPcName": r.fn_calling_import(iatmap, "GetComputerNameW"),
        "kGetHwProfileId": r.fn_calling_import(iatmap, "GetCurrentHwProfileA"),
        "kSendAppend": r.send_append(),
        "kRecvBuf": r.recv_buf(),
        "kUrielObjOffset": obj,
        "kIatConnect": iatmap.get("connect"),
        "kIatClosesocket": iatmap.get("closesocket"),
        "kIatSend": iatmap.get("send"),
        "kIatRecv": iatmap.get("recv"),
        "kIatWSAGetLastError": iatmap.get("WSAGetLastError"),
        "kIatWinHttpConnect": iatmap.get("WinHttpConnect"),
        "kIatWinHttpOpenRequest": iatmap.get("WinHttpOpenRequest"),
        "kIatInternetOpenUrlA": iatmap.get("InternetOpenUrlA"),
        "kIatURLDownloadToFileA": iatmap.get("URLDownloadToFileA"),
        "kPyRunLine": r.run_line(),
        "kPyRunFile": r.run_file(),
        "kPyRunStringFlags": r.py_run_string_flags(),
        "kPyLauncherInst": r.launcher_inst(),
        "kPackMgrInst": pack_inst,
        "kPackGet": pack_get,
        # Not an address: the byte count slot2 must clean. The stub rewrites its
        # own `ret imm16` from this, so a wrong value corrupts the stack on a
        # build where the client pushes a different number of arguments.
        "SLOT2_ARG_BYTES": r.slot2_arg_bytes(),
    }
    vals.update(r.autohunt_api())
    # Optional like autohunt_api: absent keys mean the feature is off,
    # never a zero the stub would treat as an address.
    vals.update(r.skip_collision())
    vals.update(r.actor_pass())
    vals.update(r.block_movement())
    vals.update(r.auto_move())
    vals.update(r.offlineshop_cap())
    vals.update(r.minimap_waypoint())
    return vals


# Keys the native gateway cannot work without. Absent glue means no
# triarch_native module at all, so it is emitted all-or-nothing.
NATIVE_GLUE_KEYS = ("kPyInitModule", "kPyImportAddModule", "kPyModuleAddFunctions",
                    "kPyTupleGetUInt", "kPyTupleGetString", "kPyTupleGetFloat",
                    "kPyBuildValue", "kPyBuildNone", "kPyBuildException")

NATIVE_SINGLETONS = ("kPlayerInst", "kCharMgrInst", "kNetStreamInst",
                     "kMiniMapInst", "kItemInst")

# The field types the stub can marshal - exactly the names FieldType() in
# uriel_stub.cpp accepts. A field whose effective ini type (adapter or type) is
# not in this list is REJECTED here rather than emitted, because the stub used
# to default an unknown name to u32 and hand mods a float's bit pattern as an
# int (auto_move_dest was declared "vec2f" and nothing noticed).
STUB_FIELD_TYPES = ("bool8", "i32", "u32", "vec2", "vector_u8")


def write_natives_ini(path, reg_path, out_path, vals=None, r=None, log=print):
    """Emit uriel_natives.ini - the whole triarch_native gateway.

    Returns (rows, problems, missing_glue). An empty missing_glue means the file
    was written. This is what makes ground_items(), actors(), the typed fields
    and every api.player.* native call exist; without it the stub still loads
    and the client still runs, but every native feature is silently absent -
    so the caller should report a miss loudly rather than treat it as optional.
    """
    r = r or Resolver(path)
    vals = resolve_all(path, r) if vals is None else vals
    glue = r.py_glue()
    rows, problems, addrs = r.natives(reg_path)
    missing = [k for k in NATIVE_GLUE_KEYS if k not in glue]
    for p in problems:
        log("  REJECTED %s" % p)
    if missing:
        return rows, problems, missing

    reg = json.load(open(reg_path))
    with open(out_path, "w") as f:
        f.write("; generated by mkoffsets.py --natives - do not edit\n")
        f.write("; Adding a native: edit natives.json and re-run. The\n")
        f.write("; stub reads this table, so no rebuild is required.\n")
        f.write("\n[pyglue]\n")
        for k in NATIVE_GLUE_KEYS:
            f.write("%s=%08X\n" % (k, glue[k]))
        f.write("\n[singletons]\n")
        for k in NATIVE_SINGLETONS:
            if addrs.get(k):
                f.write("%s=%08X\n" % (k, addrs[k]))
        f.write("\n; name = owner | offset | type | access\n[fields]\n")
        for fld in (reg.get("fields", []) + reg.get("instance_fields", [])):
            o = vals.get(fld["offset"])
            if not o:
                log("  field %s: %s unresolved - skipped" % (fld["name"], fld["offset"]))
                continue
            ftype = fld.get("adapter") or fld["type"]
            if ftype not in STUB_FIELD_TYPES:
                # same fail-closed treatment as a native with a bad stack width
                p = "field %s: unknown type '%s' (stub knows %s)" % (
                    fld["name"], ftype, "/".join(STUB_FIELD_TYPES))
                problems.append(p)
                log("  REJECTED %s" % p)
                continue
            f.write("%s=%s|%08X|%s|%s\n"
                    % (fld["name"], fld["owner"], o + fld.get("offset_delta", 0),
                       ftype, fld.get("access", "r")))
        f.write("\n; name = address | convention | stack bytes | this"
                " | return | checked\n[natives]\n")
        for x in rows:
            f.write("%s=%08X|%s|%d|%s|%s|%s\n"
                    % (x["name"], x["va"], x["conv"], x["stack"],
                       x["this"] or "-", x["ret"], "1" if x["checked"] else "0"))
    return rows, problems, missing


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exe", help="decrypted exe")
    ap.add_argument("-o", dest="out", default=None, metavar="offsets.h",
                    help="write the resolved addresses as a C header")
    ap.add_argument("--check", action="store_true",
                    help="compare against the known-good BASELINE values")
    ap.add_argument("--natives", nargs=2, metavar=("natives.json", "out.ini"),
                    help="also emit the uriel_natives.ini gateway table")
    a = ap.parse_args()
    path, out = a.exe, a.out
    r = Resolver(path)

    # One key list, in resolve_all(). main() and the patcher now see exactly the
    # same offsets - previously each built its own dict and they drifted.
    vals = resolve_all(path, r)
    slot2 = vals.pop("SLOT2_ARG_BYTES", None)

    bad = [k for k, v in vals.items() if not v]
    width = max(len(k) for k in vals)
    for k in sorted(vals):
        v = vals[k]
        note = ""
        if a.check and k in BASELINE:
            note = "  OK" if v == BASELINE[k] else "  MISMATCH (baseline 0x%08X)" % BASELINE[k]
        print("%-*s = %s%s" % (width, k, ("0x%08X" % v) if v else "*** NOT FOUND ***", note))
    print("%-*s = %s" % (width, "kSlot2ArgBytes", ("0x%02X" % slot2) if slot2 else "*** NOT FOUND ***"))

    if bad:
        print("\n%d anchor(s) unresolved - fix the resolver before building." % len(bad))
        sys.exit(1)
    print("\nall anchors resolved")

    if out:
        with open(out, "w") as f:
            f.write("// generated by mkoffsets.py - do not edit\n#pragma once\n\n")
            for k in sorted(vals):
                f.write("static const DWORD %s = 0x%08X;\n" % (k, vals[k]))
            # a #define, not a const: MSVC inline asm `ret imm` needs a literal
            f.write("#define SLOT2_ARG_BYTES 0x%02X\n" % (slot2 or 0x1C))
        print("wrote %s" % out)

    # ---- the native gateway table -----------------------------------------
    # Everything the stub needs to expose `triarch_native` WITHOUT being
    # rebuilt: adding a native later is an entry here, not a C++ edit.
    if a.natives:
        reg, nout = a.natives
        print("\n--- native gateway ---")
        vals["SLOT2_ARG_BYTES"] = slot2
        rows, problems, missing = write_natives_ini(path, reg, nout, vals=vals, r=r)
        glue = r.py_glue()
        for k in NATIVE_GLUE_KEYS:
            print("  %-24s %s" % (k, ("0x%08X" % glue[k]) if k in glue
                                  else "*** NOT FOUND ***"))
        if missing:
            print("  gateway NOT emitted: glue incomplete")
            sys.exit(1)
        print("  wrote %s  (%d native(s), %d rejected)"
              % (nout, len(rows), len(problems)))


if __name__ == "__main__":
    main()
