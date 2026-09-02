"""The whole patch, start to finish.

Steps, in order:
  1 preflight   confirm we are in a game folder and can write to it
  2 derive      recover the keystream, the import table and the OEP from the
                file alone (no game launch) -> _patcher\\profile.json
  3 rebuild     write triarch_clean.exe: decrypted, imports restored, ASLR off
  4 offsets     re-derive every address the stub hooks -> uriel_offsets.ini
  5 natives     re-derive the triarch_native gateway  -> uriel_natives.ini
  6 deploy      drop the prebuilt uriel_stub.dll next to the exe
  7 mods        install the mod host and shipped mods into <game>\\mods\\

Nothing needs the game to run. Each step reports OK or FAIL to the log and the
run stops at the first failure.

`run(folder, log, live=True)` swaps step 2 for the original live method:
launch the client, wait for Uriel to decrypt .text in memory, read the
keystream and the resolved IAT out of the process, then cross-check that
harvest against the static derivation. It exists as a fallback for a future
build that changes the on-disk layout, and as the way to regenerate
vendor/imports_db.json (the name -> DLL table `derive` relies on).

WHY 7 AND 9 EXIST
-----------------
They were missing, and their absence is silent rather than loud. Without
uriel_natives.ini the stub still loads and the client still runs perfectly,
but triarch_native is never registered - so ground-item and actor enumeration,
the typed player fields and the whole autohunt drive are simply absent, and
every mod that depends on them degrades to doing nothing. Without step 9 there
is no mods folder at all. Neither failure produces a crash or an error; they
just quietly do less, which is the worst way for a patcher to be wrong.
"""
import collections
import hashlib
import io
import os
import re
import struct
import sys
import time

from . import winproc
from .vendor import mkoffsets, unuriel

GAME_EXE = "triarch.exe"
CLEAN_EXE = "triarch_clean.exe"
STUB_DLL = "uriel_stub.dll"
INI = "uriel_offsets.ini"          # required at runtime - stays in the game folder
NATIVES_INI = "uriel_natives.ini"  # the triarch_native gateway table
NATIVES_REG = "natives.json"       # the registry it is generated FROM
MODS_DIR = "mods"                  # <game>\mods\ - where the stub looks
DATA_DIR = "_patcher"              # everything disposable lives here
PROFILE = "profile.json"


class Failure(Exception):
    pass


def _iat_dir(path):
    """(rva, size) of the IAT data directory."""
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    return struct.unpack_from("<II", d, pe + 24 + 96 + 12 * 8)


def _text_section(path):
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    off = pe + 24 + optsz
    for _ in range(nsec):
        nm = d[off:off + 8].rstrip(b"\0").decode("latin1")
        vs, va, rs, ro = struct.unpack_from("<IIII", d, off + 8)
        if nm == ".text":
            return d, ro, min(rs, vs), va
        off += 40
    raise Failure("no .text section in %s" % os.path.basename(path))


def _call_tool(mod, argv, log):
    """Run a vendored CLI tool in-process, echoing its output into the log."""
    old_argv, old_out = sys.argv, sys.stdout
    sys.argv = argv
    sys.stdout = io.StringIO()
    try:
        mod.main()
        code = 0
    except SystemExit as e:
        code = e.code or 0
    finally:
        out = sys.stdout.getvalue()
        sys.argv, sys.stdout = old_argv, old_out
    for line in out.splitlines():
        if line.strip():
            log("    " + line.rstrip())
    return code


# --------------------------------------------------------------------------- steps
def step_preflight(ctx, log):
    folder = ctx["folder"]
    for f in (GAME_EXE, "client_x86.dll"):
        if not os.path.isfile(os.path.join(folder, f)):
            raise Failure("%s not found - is this the game folder?" % f)
    probe = os.path.join(folder, ".patcher_write_test")
    try:
        open(probe, "wb").close()
        os.remove(probe)
    except OSError as e:
        raise Failure("folder is not writable: %s" % e)
    clean = os.path.join(folder, CLEAN_EXE)
    if os.path.isfile(clean):
        try:
            os.rename(clean, clean)          # cheap in-use check on Windows
        except OSError:
            raise Failure("%s is running - close the game first" % CLEAN_EXE)
    os.makedirs(os.path.join(folder, DATA_DIR), exist_ok=True)
    log("game folder OK: %s" % folder)
    log("artifacts -> %s\\  (disposable)" % DATA_DIR)


def _banner(log, lines, level="nudge"):
    """A loud ASCII box. Used only for things the operator must act on."""
    width = max(len(x) for x in lines) + 8      # "##  " + text + "  ##"
    rule = "#" * width
    log("", level)
    log(rule, level)
    log("##" + " " * (width - 4) + "##", level)
    for x in lines:
        log("##  " + x.ljust(width - 8) + "  ##", level)
    log("##" + " " * (width - 4) + "##", level)
    log(rule, level)
    log("", level)


def _ask_to_interact(log, retry=False):
    lines = [">>>  CLICK THE GAME WINDOW AND MOVE THE MOUSE AROUND  <<<", ""]
    if retry:
        lines[0] = ">>>  NOTHING HAPPENED - CLICK THE GAME WINDOW NOW  <<<"
    lines += [
        "Uriel decrypts the code lazily: a page is only unscrambled",
        "when the program actually runs it. Sitting idle at the login",
        "screen it barely runs anything, so this wait can drag on or",
        "time out entirely.",
        "",
        "Clicking around the client - login screen, options, menus -",
        "forces that code to execute and usually finishes the wait in",
        "a few seconds instead of 90.",
        "",
        "Do NOT close the client; the patcher stops it by itself.",
    ]
    _banner(log, lines)


def step_launch(ctx, log):
    folder = ctx["folder"]
    d, ro, size, rva = _text_section(os.path.join(folder, GAME_EXE))
    ctx["enc_text"] = (d, ro, size)

    def waiting(elapsed, left):
        log("      still encrypted after %ds (%ds left) - interact with the "
            "client window" % (elapsed, left), "nudge")

    base, proc = None, None
    for attempt in (1, 2):
        proc = winproc.launch(folder, GAME_EXE, ["--game"], log)
        ctx["proc"] = proc
        _ask_to_interact(log, retry=(attempt == 2))
        log("waiting for Uriel to decrypt .text (up to 90s)")
        base = winproc.wait_for_decrypt(proc.pid, GAME_EXE, d[ro:ro + 4096], rva,
                                        log, on_wait=waiting, proc=proc)
        if base:
            break
        log("      no decryption on attempt %d - restarting the client" % attempt,
            "fail")
        step_stop_game(ctx, log)
    if not base:
        raise Failure("client never decrypted .text after two attempts - launch it "
                      "by hand once to check it starts, then re-run the patcher")
    ctx["pid"], ctx["base"] = proc.pid, base
    log("pid %d, base 0x%08X" % (proc.pid, base))
    rva, size = _iat_dir(os.path.join(folder, GAME_EXE))
    log("waiting for the IAT to be fully resolved (rva 0x%X, %d bytes)" % (rva, size))
    log("      keep clicking around the client until step 3 is done", "nudge")

    def iat_waiting(elapsed, left):
        log("      imports still settling after %ds (%ds left) - keep the client "
            "busy" % (elapsed, left), "nudge")

    winproc.wait_for_iat(proc.pid, base, rva, size, log, on_wait=iat_waiting)


def step_harvest(ctx, log):
    folder = ctx["folder"]
    out = os.path.join(folder, DATA_DIR, PROFILE)
    seen = []

    def tee(msg, level="info"):
        seen.append(msg)
        log(msg, level)

    rc = _call_tool(unuriel, ["unuriel", "harvest", os.path.join(folder, GAME_EXE),
                              str(ctx["pid"]), hex(ctx["base"]), "-o", out], tee)
    if rc or not os.path.isfile(out):
        raise Failure("harvest failed")
    # A partially-resolved IAT yields a client that crashes on the first call
    # through a NULL slot, so treat it as fatal rather than shipping it.
    for line in seen:
        m = re.search(r"(\d+)\s+resolved,\s*(\d+)\s+unresolved", line)
        if m and int(m.group(2)):
            raise Failure("%s IAT slots unresolved - the client was sampled too "
                          "early; re-run the patcher" % m.group(2))
    ctx["profile"] = out


def step_stop_game(ctx, log):
    p = ctx.get("proc")
    if p and p.poll() is None:
        p.kill()
        log("client stopped")
    time.sleep(1.0)


def step_verify_keystream(ctx, log):
    """Recover the keystream from ciphertext alone and compare with the harvest.

    .text is XORed with one 4096-byte key tiled per page, so across ~17k pages
    it is a many-time pad; the modal byte at each page offset is plaintext 0x00.
    """
    import json
    import base64 as b64
    d, ro, size = ctx["enc_text"]
    total = size // 4096
    step = max(1, total // 2500)
    pages = range(0, total, step)
    static = bytearray(4096)
    for j in range(4096):
        c = collections.Counter(d[ro + p * 4096 + j] for p in pages)
        static[j] = c.most_common(1)[0][0]

    prof = json.load(open(ctx["profile"]))
    ks = None
    for k in ("key_b64", "keystream", "ks", "key"):
        if k in prof:
            ks = prof[k]
            break
    if ks is None:
        log("    profile has no keystream field - skipping cross-check")
        return
    harvested = b64.b64decode(ks) if isinstance(ks, str) else bytes(ks)
    if len(harvested) != 4096:
        raise Failure("harvested keystream is %d bytes, expected 4096" % len(harvested))
    same = sum(1 for i in range(4096) if harvested[i] == static[i])
    log("    static vs harvested keystream: %d/4096 agree" % same)
    if prof.get("key_unanimous") is not None:
        log("    harvest reported %s/4096 offsets unanimous" % prof["key_unanimous"])
    if same < 4096:
        bad = [i for i in range(4096) if harvested[i] != static[i]]
        log("    NOTE: %d byte(s) differ (first at offset %d)" % (len(bad), bad[0]))
        log("    the static value is derived from ~2500 pages and is the safer one")


def step_derive(ctx, log):
    """Everything the live harvest used to read out of memory, from the file.

    Keystream: .text is one 4096-byte key XORed over ~17k pages of x86 code,
    a many-time pad whose modal byte per offset is the key. Imports: Uriel
    left the original IAT on disk with the names XOR-obfuscated by that same
    key. OEP: found by shape in the decrypted text. See vendor/unuriel.py."""
    folder = ctx["folder"]
    out = os.path.join(folder, DATA_DIR, PROFILE)
    seen = []

    def tee(msg, level="info"):
        seen.append(msg)
        log(msg, level)

    rc = _call_tool(unuriel, ["unuriel", "derive", os.path.join(folder, GAME_EXE), "-o", out], tee)
    if rc or not os.path.isfile(out):
        raise Failure("derive failed - this build may need the live method (--live)")
    import json
    prof = json.load(open(out))
    if prof.get("key_weak_offsets", 0):
        log("    note: %d keystream offset(s) had a weak majority" % prof["key_weak_offsets"],
            "nudge")
    inherited = prof.get("iat_inherited_names") or []
    if inherited:
        log("    note: %d import name(s) unknown to imports_db.json were attributed by "
            "their group: %s" % (len(inherited), ", ".join(inherited[:8])), "nudge")
    ctx["profile"] = out


def step_rebuild(ctx, log):
    folder = ctx["folder"]
    out = os.path.join(folder, CLEAN_EXE)
    rc = _call_tool(unuriel, ["unuriel", "rebuild", os.path.join(folder, GAME_EXE),
                              ctx["profile"], "-o", out, "--stub-dll", "uriel_stub"], log)
    if rc or not os.path.isfile(out):
        raise Failure("rebuild failed")
    ctx["clean"] = out
    log("wrote %s (%.1f MB)" % (CLEAN_EXE, os.path.getsize(out) / 1048576.0))


# Anchors the anti-cheat stub does not need. Losing one on a new build disables
# the mod host and nothing else, so it must not be able to block a patch - the
# stub reads these as an optional block and skips mod loading when absent.
OPTIONAL_OFFSETS = {
    "kPyRunLine", "kPyRunFile", "kPyRunStringFlags", "kPyLauncherInst",
    "kPackMgrInst", "kPackGet",
}


def step_offsets(ctx, log):
    # Resolving is the slow part (it scans a 68 MB .text repeatedly), and
    # step_natives needs the same values to place the typed fields. Resolve
    # once, stash on ctx, and the two files are guaranteed to agree.
    vals = mkoffsets.resolve_all(ctx["clean"])
    ctx["offsets"] = vals
    missing = [k for k, v in vals.items() if not v and k not in OPTIONAL_OFFSETS]
    absent_opt = [k for k, v in vals.items() if not v and k in OPTIONAL_OFFSETS]
    for k in sorted(vals):
        note = "NOT FOUND"
        if k in OPTIONAL_OFFSETS and not vals[k]:
            note = "not found (optional - mod host disabled)"
        log("    %-24s %s" % (k, ("0x%08X" % vals[k]) if vals[k] else note))
    if missing:
        raise Failure("%d offset(s) unresolved: %s" % (len(missing), ", ".join(missing)))
    if absent_opt:
        log("    note: %d optional offset(s) unresolved - mods will not load"
            % len(absent_opt))
    path = os.path.join(ctx["folder"], INI)
    with open(path, "w") as f:
        f.write("; generated by the Triarch patcher - regenerate after every game update\n")
        f.write("[offsets]\n")
        for k in sorted(vals):
            if vals[k]:
                f.write("%s=%08X\n" % (k, vals[k]))
    log("wrote %s" % INI)


def _res_path(*parts):
    """Absolute path to a bundled resource, frozen or not.

    importlib.resources gives a Traversable that is not a real path under
    PyInstaller's onefile layout, so fall back to __file__ - which PyInstaller
    does materialise into the extraction dir."""
    try:
        from importlib.resources import files
        p = files("triarch_patcher").joinpath("resources", *parts)
        if os.path.exists(str(p)):
            return str(p)
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", *parts)


def step_natives(ctx, log):
    """Emit uriel_natives.ini: the whole triarch_native gateway.

    Generated from the SAME decrypted exe as uriel_offsets.ini, one step
    earlier, so the two can never describe different builds.

    A miss here is reported loudly and fails the run. That is deliberate: the
    stub treats an absent gateway as "natives off" and carries on, so a silent
    skip would ship a client that looks fine, logs nothing unusual, and has no
    working mods."""
    reg = _res_path(NATIVES_REG)
    if not os.path.isfile(reg):
        raise Failure("bundled %s is missing - rebuild the patcher" % NATIVES_REG)
    out = os.path.join(ctx["folder"], NATIVES_INI)
    rows, problems, missing = mkoffsets.write_natives_ini(
        ctx["clean"], reg, out, vals=ctx.get("offsets"), log=lambda m: log("    " + m))
    for p in problems:
        log("    rejected: %s" % p, "nudge")
    if missing:
        raise Failure("native gateway incomplete - %s unresolved; mods would "
                      "silently do nothing" % ", ".join(missing))
    log("    %d native(s) armed, %d rejected" % (len(rows), len(problems)))
    log("wrote %s" % NATIVES_INI)


def _copy_tree(src, dst, log, skip_existing=()):
    """Copy a directory, counting files. Never removes anything already there."""
    n = 0
    for root, dirs, fnames in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in fnames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            d = os.path.join(target, fn)
            if fn in skip_existing and os.path.isfile(d):
                log("    kept your existing %s" % fn)
                continue
            with open(os.path.join(root, fn), "rb") as a, open(d, "wb") as b:
                b.write(a.read())
            n += 1
    return n


def step_mods(ctx, log):
    """Install <game>\\mods\\ - the mod host, the api and the shipped mods.

    config.json is deliberately NOT overwritten when it already exists: it is
    the one file in here a user edits, and silently resetting which mods are
    enabled on every game update would be its own bug."""
    src = _res_path(MODS_DIR)
    if not os.path.isdir(src):
        raise Failure("bundled %s\\ is missing - rebuild the patcher" % MODS_DIR)
    dst = os.path.join(ctx["folder"], MODS_DIR)
    n = _copy_tree(src, dst, log, skip_existing=("config.json",))
    log("installed %d file(s) -> %s\\" % (n, MODS_DIR))
    # Name every mod folder that went in, and point out the ones the installed
    # config.json does not mention: the host enables those by default, which
    # is how a stray checkout folder once shipped and armed itself at login.
    mods = sorted(d for d in os.listdir(src)
                  if os.path.isfile(os.path.join(src, d, "main.py")))
    for m in mods:
        log("    mod %s" % m)
    try:
        import json
        with open(os.path.join(dst, "config.json")) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    unlisted = [m for m in mods if m not in cfg]
    if unlisted:
        log("    note: no config.json section, so enabled by default: %s"
            % ", ".join(unlisted), "nudge")
    _stamp_build_constants(ctx, dst, log)


# Mod settings that are NOT preferences: they are facts about the build, and a
# stale one is a silently broken mod rather than a wrong-looking option. They
# are resolved per build and written into config.json even when it already
# exists, unlike everything else in that file.
BUILD_CONSTANTS = {
    # nocollide gates pass-through on "is the player in the auto-move state",
    # because auto_move_active is never cleared when a route is cancelled. The
    # state value is read out of the engine's own `cmp [player+0x58], <state>`
    # in AutoMoveToPosition; if a game update changes it, a hardcoded copy in
    # the mod would silently stop detecting cancellation and leave collisions
    # off for ever.
    ("nocollide", "WALK_STATE"): "kPlayerWalkState",
    # autohunt2 uses the same state to notice a boss route has died, instead of
    # waiting out BOSS_REPATH_S. Same fact, two consumers, one source.
    ("autohunt2", "WALK_STATE"): "kPlayerWalkState",
    ("keeproute", "WALK_STATE"): "kPlayerWalkState",
    # routes gates leg re-issue on "is the player still walking" - same fact.
    ("routes", "WALK_STATE"): "kPlayerWalkState",
}


def _stamp_build_constants(ctx, mods_dir, log):
    import json
    vals = ctx.get("offsets") or {}
    path = os.path.join(mods_dir, "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except Exception as e:
        log("    note: cannot read config.json (%s) - build constants not stamped"
            % type(e).__name__)
        return

    changed = []
    for (mod, key), offset_name in BUILD_CONSTANTS.items():
        v = vals.get(offset_name)
        if not v:
            log("    note: %s unresolved - %s.%s left as shipped"
                % (offset_name, mod, key))
            continue
        sect = cfg.setdefault(mod, {})
        if sect.get(key) != v:
            sect[key] = v
            changed.append("%s.%s=0x%X" % (mod, key, v))
    if not changed:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)
    log("    stamped build constants: %s" % ", ".join(changed))


def _expected_stub_sha():
    """sha256 recorded next to the DLL at build time, or None if absent."""
    for loader in (lambda: __import__("importlib.resources", fromlist=["files"])
                   .files("triarch_patcher").joinpath("resources", STUB_DLL + ".sha256")
                   .read_text().strip(),
                   lambda: open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "resources", STUB_DLL + ".sha256")).read().strip()):
        try:
            return loader()
        except Exception:
            continue
    return None


def step_deploy(ctx, log):
    try:
        from importlib.resources import files
        blob = (files("triarch_patcher") / "resources" / STUB_DLL).read_bytes()
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        blob = open(os.path.join(here, "resources", STUB_DLL), "rb").read()
    if len(blob) < 4096 or blob[:2] != b"MZ":
        raise Failure("embedded %s is empty or not a PE (%d bytes) - rebuild the patcher"
                      % (STUB_DLL, len(blob)))
    want = _expected_stub_sha()
    got = hashlib.sha256(blob).hexdigest()
    if want and got != want:
        raise Failure("embedded %s is not the DLL that was built (sha %s.. != %s..) - "
                      "PyInstaller may have UPX-packed it; rebuild with --noupx"
                      % (STUB_DLL, got[:12], want[:12]))
    if not want:
        # Worth saying out loud. The hash is recorded at build time from the same
        # file it checks, so it can only ever prove the bytes survived freezing -
        # never that the right stub was built. With the hash absent it proves
        # nothing at all, and silence there reads as "verified".
        log("    note: no recorded sha256 - stub integrity NOT verified", "nudge")
    # A stub without the gateway loads fine and silently has no natives, which
    # is the failure this whole file exists to prevent shipping.
    if b"triarch_native" not in blob:
        raise Failure("embedded %s has no triarch_native gateway - it predates the "
                      "native API; rebuild the stub (build.bat stub)" % STUB_DLL)
    dst = os.path.join(ctx["folder"], STUB_DLL)
    with open(dst, "wb") as f:
        f.write(blob)
    if os.path.getsize(dst) != len(blob):
        raise Failure("short write to %s" % STUB_DLL)
    log("wrote %s (%d bytes)" % (STUB_DLL, len(blob)))


_TAIL = [
    ("rebuild", step_rebuild),
    ("resolve offsets", step_offsets),
    ("resolve natives", step_natives),
    ("deploy stub", step_deploy),
    ("install mods", step_mods),
]

STEPS = [
    ("preflight", step_preflight),
    ("derive (offline)", step_derive),
] + _TAIL

# The original method. Needs the client to run once and the operator to click
# around in it; kept for builds where the offline derivation stops working.
STEPS_LIVE = [
    ("preflight", step_preflight),
    ("launch client", step_launch),
    ("harvest", step_harvest),
    ("stop client", step_stop_game),
    ("verify keystream", step_verify_keystream),
] + _TAIL


def run(folder, log_ui, live=False):
    """Execute every step. Returns True on success.

    Everything printed also lands in patcher_run.log so a closed window or a
    dead GUI never loses the evidence.
    """
    ctx = {"folder": folder}
    steps = STEPS_LIVE if live else STEPS
    t0 = time.time()
    try:
        os.makedirs(os.path.join(folder, DATA_DIR), exist_ok=True)
        fh = open(os.path.join(folder, DATA_DIR, "patcher_run.log"), "w")
    except OSError:
        fh = None

    def log(msg, level="info"):
        log_ui(msg, level)
        if fh:
            fh.write(msg + "\n")
            fh.flush()
    for i, (name, fn) in enumerate(steps, 1):
        log("[%d/%d] %s" % (i, len(steps), name), "step")
        try:
            fn(ctx, log)
            log("      OK", "ok")
        except Failure as e:
            log("      FAILED: %s" % e, "fail")
            step_stop_game(ctx, log)
            return False
        except Exception as e:
            log("      ERROR: %s: %s" % (type(e).__name__, e), "fail")
            step_stop_game(ctx, log)
            return False
    log("")
    log("")
    log("keep in the game folder : %s, %s, %s, %s, %s\\"
        % (CLEAN_EXE, STUB_DLL, INI, NATIVES_INI, MODS_DIR), "info")
    log("safe to delete anytime  : %s\\ and TriarchPatcher.exe" % DATA_DIR, "info")
    log("patched in %.0fs - launch %s --game" % (time.time() - t0, CLEAN_EXE), "ok")
    if fh:
        fh.close()
    return True
