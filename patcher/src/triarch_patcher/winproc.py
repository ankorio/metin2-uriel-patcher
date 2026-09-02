"""Minimal Windows process helpers (ctypes only, no third-party deps).

The patcher needs to start the protected client, wait for Uriel to decrypt
.text in memory, and read back the PID and the module base it was relocated to.
"""
import ctypes
import ctypes.wintypes as w
import os
import subprocess
import time

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_MODULE_NAME32 = 255


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD),
                ("th32ProcessID", w.DWORD), ("GlblcntUsage", w.DWORD),
                ("ProccntUsage", w.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                ("modBaseSize", w.DWORD), ("hModule", w.HMODULE),
                ("szModule", ctypes.c_char * (MAX_MODULE_NAME32 + 1)),
                ("szExePath", ctypes.c_char * 260)]


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def module_base(pid, name):
    """Base address of `name` inside `pid`, or None."""
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == -1:
        return None
    try:
        me = MODULEENTRY32()
        me.dwSize = ctypes.sizeof(me)
        if not k.Module32First(snap, ctypes.byref(me)):
            return None
        while True:
            if me.szModule.decode("latin1").lower() == name.lower():
                return ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value
            if not k.Module32Next(snap, ctypes.byref(me)):
                return None
    finally:
        k.CloseHandle(snap)


def read_mem(pid, addr, n):
    k = _k32()
    h = k.OpenProcess(0x0400 | 0x0010, False, pid)     # QUERY_INFORMATION | VM_READ
    if not h:
        return None
    try:
        buf = (ctypes.c_char * n)()
        got = ctypes.c_size_t(0)
        if not k.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            return None
        return bytes(buf[:got.value])
    finally:
        k.CloseHandle(h)


def launch(folder, exe, args, log):
    """Start the client detached and return its Popen."""
    cmd = [os.path.join(folder, exe)] + list(args)
    log("launching %s %s" % (exe, " ".join(args)))
    return subprocess.Popen(cmd, cwd=folder,
                            creationflags=0x00000008 | 0x00000200)  # NO_WINDOW|NEW_GROUP


def _ticker(on_wait, every=10):
    """Call `on_wait(elapsed, remaining)` at most once per `every` seconds."""
    t0 = time.time()
    last = [0]

    def tick(deadline):
        if not on_wait:
            return
        elapsed = int(time.time() - t0)
        if elapsed // every > last[0]:
            last[0] = elapsed // every
            on_wait(elapsed, max(0, int(deadline - time.time())))
    return tick


def wait_for_iat(pid, base, iat_rva, iat_size, log, timeout=120, settle=3,
                 on_wait=None):
    """Block until every IAT slot is non-zero and the table stops changing.

    .text is decrypted almost immediately, but Uriel keeps resolving imports for
    a while afterwards. Harvesting on the first signal caught the table half
    filled and silently produced a client with NULL import slots.
    """
    deadline = time.time() + timeout
    prev, stable = None, 0
    tick = _ticker(on_wait)
    while time.time() < deadline:
        blob = read_mem(pid, base + iat_rva, iat_size)
        if blob and len(blob) == iat_size:
            slots = [int.from_bytes(blob[i:i + 4], "little")
                     for i in range(0, iat_size, 4)]
            zeros = sum(1 for i, v in enumerate(slots) if v == 0)
            if blob == prev:
                stable += 1
                if stable >= settle:
                    log("IAT settled: %d slots, %d still zero" % (len(slots), zeros))
                    return True
            else:
                stable = 0
            prev = blob
        tick(deadline)
        time.sleep(1.0)
    log("WARNING: IAT never settled within %ds" % timeout)
    return False


def wait_for_decrypt(pid, exe_name, disk_text, text_rva, log, timeout=90,
                     on_wait=None, proc=None):
    """Poll until the in-memory .text stops matching the encrypted bytes on disk.

    Uriel decrypts lazily via a VEH: a page is only unscrambled when execution
    actually faults into it. Left alone at the login screen the client touches
    very little code, so this can sit near the timeout - hence `on_wait`, which
    the caller uses to tell the operator to click the window and get code
    running. Returns the module base once decryption is visible, else None.
    """
    deadline = time.time() + timeout
    base = None
    tick = _ticker(on_wait)
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            log("client exited early (code %s) before decrypting" % proc.poll(), "fail")
            return None
        if base is None:
            base = module_base(pid, exe_name)
            if base:
                log("module base 0x%08X" % base)
        if base:
            live = read_mem(pid, base + text_rva, 4096)
            if live and live != disk_text[:4096] and any(live):
                log("in-memory .text differs from disk - decrypted")
                return base
        tick(deadline)
        time.sleep(1.0)
    return None
