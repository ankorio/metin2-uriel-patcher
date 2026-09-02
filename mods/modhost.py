"""Triarch mod host - discovery, lifecycle, isolation, hot reload.

Bootstrapped by uriel_stub.dll, which execs this file with MODS_DIR and LOG_DIR
already in globals, then calls boot() once and pump() at roughly 10 Hz from the
client's per-frame anti-cheat slot.

Two hard rules, because this runs inside a live game client:

  1. pump() and boot() MUST NOT raise. Anything that escapes reaches
     CPythonLauncher::RunLine, which logs a traceback every single pump.
  2. A broken mod disables itself. It never takes the client down and never
     stops the other mods.

Layout:
    <exe>\\mods\\modhost.py      this file
    <exe>\\mods\\api.py          the façade handed to mods
    <exe>\\mods\\<name>\\main.py a mod
"""

import json
import os
import sys
import time

# NOTE: `import traceback` is NOT available here.
#
# The client replaces __import__ with Metin2's hybrid importer (system.py:461),
# which falls through to zipimport on the stdlib appended to the executable.
# That zip is intact in the shipped triarch.exe but NOT in the unuriel-rebuilt
# triarch_clean.exe: zipimport reports "bad local file header". So any module
# that is not already resident in sys.modules cannot be imported at all.
#
# os / sys / time are already loaded by the client and are safe. traceback is
# not, so exceptions are formatted by hand below using only builtins.

MODS_DIR = globals().get("MODS_DIR", "")
LOG_DIR = globals().get("LOG_DIR", "")

LOG_PATH = os.path.join(LOG_DIR, "mods.log")
CONFIG_PATH = os.path.join(MODS_DIR, "config.json")
PROFILES_DIR = os.path.join(MODS_DIR, "profiles")

_prof_path = None       # the profile currently merged in, or None
_prof_mtime = 0.0

# Settings a profile is NOT allowed to override. These are facts about the BUILD,
# resolved by the patcher and stamped into config.json on every install - not
# preferences. A profile pinning one would survive a game update and silently
# feed a stale constant to the mod that depends on it, which is exactly the
# failure the stamping exists to prevent.
BUILD_KEYS = ("WALK_STATE",)
UIKIT_PATH = os.path.join(MODS_DIR, "uikit.py")
MAX_FAULTS = 5              # consecutive faults before a mod is switched off
RELOAD_POLL = 1.0           # seconds between mtime checks

_mods = {}                  # name -> record
_api = None

# Both clients run from the same folder and therefore share one mods.log, so
# every line is tagged. The PID is available immediately and is enough to tell
# the two processes apart from the very first line; the character name is
# appended once a character is actually in the world.
_tag = str(os.getpid())
_tag_name = ""
_last_pump = 0.0
_last_poll = 0.0
_api_mtime = 0.0
_uikit = None
_uikit_mtime = 0.0
_cfg = {}
_cfg_mtime = 0.0
_booted = False


def log(msg):
    """Never raises: logging must not be able to break the caller.

    Deliberately tries plain 2-arg open() before the encoding= form. Every read
    that is known to work in this client uses 2-arg open; if the client supplies
    its own pack-aware open(), the keyword form raises TypeError - which, being
    swallowed here, would silently discard every log line. That exact failure
    cost a debugging round, so the fallback chain stays."""
    try:
        line = "%s [%s] %s\n" % (time.strftime("%H:%M:%S"), _tag, msg)
    except Exception:
        line = "<unformattable log line>\n"
    for mode, payload in (("a", line), ("ab", line.encode("utf-8", "replace"))):
        try:
            f = open(LOG_PATH, mode)
        except Exception:
            continue
        try:
            f.write(payload)
            return
        except Exception:
            pass
        finally:
            try:
                f.close()
            except Exception:
                pass


def fmt_exc():
    """What traceback.format_exc() would give us - see the note above.

    Builtins only: sys.exc_info() plus frame/code attributes."""
    try:
        et, ev, tb = sys.exc_info()
        name = getattr(et, "__name__", str(et))
        lines = ["%s: %s" % (name, ev)]
        while tb is not None:
            code = tb.tb_frame.f_code
            lines.append("    %s:%d in %s"
                         % (code.co_filename, tb.tb_lineno, code.co_name))
            tb = tb.tb_next
        return "\n".join(lines)
    except Exception:
        return "<exception while formatting exception>"


def read_bytes(path):
    """Read a file WITHOUT a `with` statement.

    builtins.open in this client is a Cython `open_` returning a
    `system.pack_file`, which supports neither keyword arguments nor the context
    manager protocol:

        TypeError: 'system.pack_file' object does not support the
                   context manager protocol (missed __exit__ method)

    So: no `with open(...)`, no encoding=/errors=, anywhere in mod code."""
    f = open(path, "rb")
    try:
        return f.read()
    finally:
        try:
            f.close()
        except Exception:
            pass


def safe_name(name):
    """A character name reduced to something that is safe as a filename.

    Deliberately strict: anything outside [A-Za-z0-9_-] is dropped rather than
    escaped. A character name comes from the server, so it is untrusted input on
    a path - and "sanitise by whitelist" is the only version of this that cannot
    be talked into traversing a directory."""
    return "".join(c for c in (name or "") if c.isalnum() or c in "-_")


def profile_path(name=None):
    """mods/profiles/<character>.json for the active character, or None.

    Returns a path only when the file EXISTS. Profiles are opt-in: dropping a
    json in there switches that character over, and deleting it switches back.
    Nothing is created automatically, so a shared setup stays shared until
    somebody deliberately makes it not."""
    nm = safe_name(name if name is not None else _tag_name)
    if not nm:
        return None
    p = os.path.join(PROFILES_DIR, nm + ".json")
    return p if os.path.isfile(p) else None


def _read_json_file(path, what):
    try:
        raw = read_bytes(path)
    except Exception:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(d, dict):
            raise ValueError("%s must be an object" % what)
        return d
    except Exception:
        log("%s is invalid - ignoring it\n%s" % (what, fmt_exc()))
        return None


def load_config():
    """{modname: {setting: value}}, base config plus this character's profile.

    Several clients share one mods folder, so a single config.json meant every
    modui change on one character silently rewrote the others. A profile named
    after the active character overrides it.

    The profile is MERGED OVER the base, per mod section, not swapped for it.
    Merging is what keeps a profile small and durable: it holds only what
    differs, so a mod added to config.json later still gets sensible defaults on
    every character instead of vanishing from all of them at once.

    A missing or broken file means "everything on, defaults" rather than an
    error - a typo in config must never take the mod host down."""
    global _cfg, _cfg_mtime, _prof_path, _prof_mtime
    base = _read_json_file(CONFIG_PATH, "config.json")
    if base is None:
        _cfg, _cfg_mtime = {}, 0.0
        _prof_path, _prof_mtime = None, 0.0
        return _cfg

    _prof_path = profile_path()
    if _prof_path:
        over = _read_json_file(_prof_path, os.path.basename(_prof_path))
        if over:
            merged = {}
            for k, v in base.items():
                merged[k] = dict(v) if isinstance(v, dict) else v
            for mod, sect in over.items():
                if isinstance(sect, dict) and isinstance(merged.get(mod), dict):
                    merged[mod].update(sect)
                else:
                    merged[mod] = sect
            # Build constants always come from the base, whatever the profile says.
            for mod, sect in merged.items():
                if not isinstance(sect, dict):
                    continue
                b = base.get(mod)
                for k in BUILD_KEYS:
                    if isinstance(b, dict) and k in b:
                        sect[k] = b[k]
            base = merged
        _prof_mtime = _mtime(_prof_path)
    else:
        _prof_mtime = 0.0

    _cfg = base
    _cfg_mtime = _mtime(CONFIG_PATH)
    return _cfg


def mod_config(name):
    c = _cfg.get(name)
    return c if isinstance(c, dict) else {}


def is_enabled(name):
    return bool(mod_config(name).get("enabled", True))


def _load_uikit():
    """The shared UI renderer, handed to mods as api.ui.

    Optional: a missing uikit.py just means mods get api.ui = None, and the
    ones that do not draw anything carry on unaffected."""
    global _uikit, _uikit_mtime
    _uikit_mtime = _mtime(UIKIT_PATH)
    if not _uikit_mtime:
        _uikit = None
        return None
    g = {"__name__": "triarch_uikit", "__file__": UIKIT_PATH}
    exec(compile(read_bytes(UIKIT_PATH), UIKIT_PATH, "exec"), g)
    _uikit = type("uikit", (object,), g)
    return g.get("UIKIT_VERSION", 0)


def _load_api():
    global _api, _api_mtime
    path = os.path.join(MODS_DIR, "api.py")
    g = {"__name__": "triarch_api", "__file__": path}
    exec(compile(read_bytes(path), path, "exec"), g)
    _api = g["Api"](log, LOG_DIR)
    try:
        _api.ui = _uikit
    except Exception:
        pass
    _api_mtime = _mtime(path)
    return g.get("API_VERSION", 0)


def _reload_api():
    """api.py changed: rebuild the façade, then every mod bound to it.

    Mods hold no reference to the api object between calls - it is passed into
    each hook - so replacing it is enough, but they are reloaded anyway so a
    façade change and a mod change behave identically."""
    log("api.py changed - reloading api and all mods")
    try:
        v = _load_api()
    except Exception:
        log("api reload FAILED - keeping the previous api\n%s" % fmt_exc())
        return
    log("api reloaded, v%s" % v)
    for name in list(_mods):
        rec = _mods.pop(name)
        try:
            _unload_mod(rec)
        except Exception:
            pass
    for name, path in _mod_paths():
        if is_enabled(name):
            _try_load(name, path)


def _mod_paths():
    try:
        names = sorted(os.listdir(MODS_DIR))
    except Exception:
        return []
    out = []
    for n in names:
        if n.startswith((".", "_")):
            continue
        main = os.path.join(MODS_DIR, n, "main.py")
        if os.path.isfile(main):
            out.append((n, main))
    return out


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _call(rec, hook, *args):
    """Invoke a mod hook. Returns False if the mod was disabled by this call."""
    fn = rec["g"].get(hook)
    if fn is None:
        return True
    try:
        fn(*args)
        rec["faults"] = 0
        return True
    except Exception:
        rec["faults"] += 1
        log("mod '%s' raised in %s (%d/%d)\n%s"
            % (rec["name"], hook, rec["faults"], MAX_FAULTS, fmt_exc()))
        if rec["faults"] >= MAX_FAULTS:
            rec["enabled"] = False
            log("mod '%s' disabled after %d consecutive faults" % (rec["name"], MAX_FAULTS))
            return False
        return True


def _load_mod(name, path):
    g = {"__name__": "mod_" + name, "__file__": path,
         "__mod_dir__": os.path.dirname(path)}
    exec(compile(read_bytes(path), path, "exec"), g)

    # Settings whose names match a module-level constant in the mod override
    # it. That keeps mods plain - they declare defaults as normal constants and
    # need no config plumbing of their own - and gives the UI a single file to
    # write rather than any way to reach into running code.
    applied = {}
    for k, v in mod_config(name).items():
        if k == "enabled":
            continue
        if k in g:
            g[k] = v
            applied[k] = v
        else:
            log("mod '%s': config key %r matches nothing - ignored" % (name, k))
    if applied:
        # Secrets must never reach the log (mods.log gets copied around), so
        # the filter below redacts config values whose key looks like a
        # password/token.
        shown = dict(applied)
        for k in shown:
            if any(w in k.upper() for w in ("PW", "PASS", "SECRET", "TOKEN")):
                shown[k] = "<redacted>"
        log("mod '%s' config: %r" % (name, shown))

    rec = {"name": name, "path": path, "g": g, "mtime": _mtime(path),
           "faults": 0, "enabled": True,
           "caps": list(g.get("CAPABILITIES", []))}
    _mods[name] = rec
    log("loaded mod '%s' caps=%s" % (name, rec["caps"] or ["read"]))
    _call(rec, "on_load", _api)
    return rec


def _unload_mod(rec):
    _call(rec, "on_unload", _api)


def _try_load(name, path):
    """Load a mod, and on failure record a disabled placeholder.

    Without the placeholder the poll loop sees "not loaded yet" every second and
    retries forever - one traceback per second in mods.log. A broken mod is now
    retried only when its file actually changes."""
    try:
        _load_mod(name, path)
        return True
    except Exception:
        log("mod '%s' failed to load\n%s" % (name, fmt_exc()))
        _mods[name] = {"name": name, "path": path, "g": {},
                       "mtime": _mtime(path), "faults": MAX_FAULTS,
                       "enabled": False, "caps": []}
        return False


def boot():
    """Called once by the stub after the interpreter is live."""
    global _booted
    if _booted:
        return
    _booted = True
    try:
        load_config()
        try:
            uv = _load_uikit()
            log("uikit v%s loaded" % uv if uv else "no uikit.py - api.ui is None")
        except Exception:
            log("uikit.py failed to load - api.ui will be None\n%s" % fmt_exc())
        v = _load_api()
        log("=== mod host up  api=v%s  dir=%s" % (v, MODS_DIR))
        # One-off environment dump. Imports are severely constrained here (see
        # the note at the top), so record exactly what a mod can rely on.
        try:
            log("    sys.path = %r" % (sys.path,))
            for probe in ("os", "sys", "time", "math", "json", "struct",
                          "traceback", "re", "collections", "random"):
                log("    module %-12s %s"
                    % (probe, "resident" if probe in sys.modules else "ABSENT"))
        except Exception:
            pass
    except Exception:
        log("api.py failed to load - no mods will run\n%s" % fmt_exc())
        return
    for name, path in _mod_paths():
        if is_enabled(name):
            _try_load(name, path)
        else:
            log("mod '%s' disabled by config" % name)


def _refresh_tag():
    """Append the character name to the log tag once it is known.

    Runs at the 1 Hz poll rather than per pump, and re-checks so that relogging
    onto a different character retags rather than lying."""
    global _tag, _tag_name
    if _api is None:
        return
    try:
        name = _api.player.name() or ""
    except Exception:
        return
    # the client reports the literal string "None" for a character that exists
    # but is not in the world yet - do not adopt that as an identity
    if name and name != "None" and name != _tag_name:
        _tag_name = name
        _tag = "%s/%s" % (os.getpid(), name)
        log("identified as %s" % name)


def _poll_reload(now):
    """Reload a mod whose main.py changed on disk. Never raises."""
    global _last_poll
    if now - _last_poll < RELOAD_POLL:
        return
    _last_poll = now
    _refresh_tag()

    # A profile switch is a config change. The character is not known at the
    # moment the mods first load - it arrives seconds later, once in the world -
    # so without this the profile would only ever apply from the NEXT edit
    # onward, which reads exactly like profiles not working.
    want_prof = profile_path()
    cm = _mtime(CONFIG_PATH)
    pm = _mtime(want_prof) if want_prof else 0.0
    if want_prof != _prof_path:
        log("profile: %s" % (os.path.basename(want_prof) if want_prof
                             else "none (shared config.json)"))
    if cm != _cfg_mtime or want_prof != _prof_path or pm != _prof_mtime:
        log("config changed - reloading mods")
        load_config()
        for nm in list(_mods):
            rec = _mods.pop(nm)
            try:
                _unload_mod(rec)
            except Exception:
                pass
        for nm, pth in _mod_paths():
            if is_enabled(nm):
                _try_load(nm, pth)
            else:
                log("mod '%s' disabled by config" % nm)
        return

    um = _mtime(UIKIT_PATH)
    if um != _uikit_mtime:
        log("uikit.py changed - reloading")
        try:
            _load_uikit()
        except Exception:
            log("uikit reload failed\n%s" % fmt_exc())
        _reload_api()
        return

    api_path = os.path.join(MODS_DIR, "api.py")
    am = _mtime(api_path)
    if am and _api_mtime and am != _api_mtime:
        _reload_api()
        return

    # A mod whose folder was deleted keeps running otherwise: the scan below
    # only visits paths that still exist, so nothing ever calls its on_unload
    # and anything it put on screen stays there until the client restarts.
    present = dict(_mod_paths())
    for name in list(_mods):
        if name not in present:
            log("mod '%s' removed from disk - unloading" % name)
            rec = _mods.pop(name)
            try:
                _unload_mod(rec)
            except Exception:
                pass

    for name, path in _mod_paths():
        rec = _mods.get(name)
        m = _mtime(path)
        if rec is None:
            if is_enabled(name):
                _try_load(name, path)
            continue
        if m and m != rec["mtime"]:
            log("mod '%s' changed - reloading" % name)
            try:
                _unload_mod(rec)
            except Exception:
                pass
            _try_load(name, path)


def pump():
    """Driven by the stub from the client's per-frame slot. Must not raise."""
    global _last_pump
    try:
        now = time.time()
        dt = (now - _last_pump) if _last_pump else 0.0
        _last_pump = now
        _poll_reload(now)
        for rec in list(_mods.values()):
            if rec["enabled"]:
                _call(rec, "on_update", _api, dt)
    except Exception:
        try:
            log("pump fault\n%s" % fmt_exc())
        except Exception:
            pass
