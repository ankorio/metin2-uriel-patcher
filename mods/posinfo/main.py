"""posinfo - print your position, and your target's if one is selected.

The smallest useful mod: it proves the whole chain works end to end (stub ->
RunLine -> modhost -> api -> native bindings) and touches nothing but reads.

Edit this file while the game is running; the host notices the mtime change and
reloads it within a second. No restart, no rebuild.
"""

import sys

CAPABILITIES = ["read"]

INTERVAL = 2.0          # seconds between reports
PROBE = False           # flip on to dump the native module surface on load

_last = 0.0
_last_line = ""


def _probe(api):
    """Report what the client actually exposes.

    The module names are anti-cheat renames (stock Metin2 player/net/chr are
    playerm2g2/m2netm2g/pack_chr) and can change on any patch, so never assume -
    measure. Runs on load; hot reload makes this cheap to iterate on."""
    api.log("---- probe: native module surface ----")

    wanted = ("playerm2g2", "pack_chr", "chat", "chrmgr", "app", "background")
    for name in wanted:
        m = sys.modules.get(name)
        api.log("    %-14s %s" % (name, "OK" if m is not None else "MISSING"))

    # Anything that looks like a game module rather than stdlib/CPython internals
    cands = [n for n in sorted(sys.modules)
             if "." not in n and not n.startswith("_")]
    api.log("    sys.modules (%d): %s" % (len(cands), ", ".join(cands)))

    # If the player module is there, is the function we need on it?
    pm = sys.modules.get("playerm2g2")
    if pm is not None:
        for fn in ("GetMainCharacterIndex", "GetTargetVID"):
            api.log("    playerm2g2.%-22s %s"
                    % (fn, "yes" if hasattr(pm, fn) else "NO"))
        try:
            api.log("    GetMainCharacterIndex() -> %r" % (pm.GetMainCharacterIndex(),))
        except Exception as e:
            api.log("    GetMainCharacterIndex() raised %s: %s" % (type(e).__name__, e))

    cm = sys.modules.get("pack_chr")
    if cm is not None:
        for fn in ("SelectInstance", "GetPixelPosition", "GetNameByVID"):
            api.log("    pack_chr.%-24s %s" % (fn, "yes" if hasattr(cm, fn) else "NO"))
        # GetPixelPosition() returned None rather than a tuple, so the call shape
        # differs from stock Metin2. Try every plausible spelling and record
        # what each one actually does.
        vid = pm.GetMainCharacterIndex()

        def attempt(label, fn):
            try:
                api.log("    %-38s -> %r" % (label, fn()))
            except Exception as e:
                api.log("    %-38s !! %s: %s" % (label, type(e).__name__, e))

        attempt("chr.SelectInstance(vid)", lambda: cm.SelectInstance(vid))
        attempt("chr.GetName()", lambda: cm.GetName())
        attempt("chr.GetNameByVID(vid)", lambda: cm.GetNameByVID(vid))
        attempt("chr.GetPixelPosition()", lambda: cm.GetPixelPosition())
        attempt("chr.GetPixelPosition(vid)", lambda: cm.GetPixelPosition(vid))
        attempt("chr.GetActorPixelPosition()", lambda: cm.GetActorPixelPosition())
        attempt("chr.GetActorPixelPosition(vid)", lambda: cm.GetActorPixelPosition(vid))
        attempt("chr.GetInstanceType(vid)", lambda: cm.GetInstanceType(vid))

        for modname in ("pack_chr", "playerm2g2", "chrmgr", "background", "app"):
            mm = sys.modules.get(modname)
            if mm is None:
                continue
            names = [n for n in dir(mm)
                     if ("pos" in n.lower() or "coord" in n.lower())]
            api.log("    %s position-ish: %s" % (modname, ", ".join(sorted(names)) or "-"))

    api.log("    api.player.vid() -> %r   position -> %r"
            % (api.player.vid(), api.player.position()))
    api.log("---- end probe ----")


def on_load(api):
    api.log("posinfo: loaded (every %.1fs)" % INTERVAL)
    if PROBE:
        try:
            _probe(api)
        except Exception as e:
            api.log("posinfo: probe failed %s: %s" % (type(e).__name__, e))


def on_unload(api):
    api.log("posinfo: unloaded")


def on_update(api, dt):
    global _last, _last_line

    now = api.now()
    if now - _last < INTERVAL:
        return
    _last = now

    me = api.player.position()
    if me is None:
        return                      # not in the world yet - login/select phase

    line = "pos  %.0f %.0f %.0f" % me

    if api.target.exists():
        tp = api.target.position()
        name = api.target.name() or "?"
        if tp is None:
            line += "   |   target %s (no position)" % name
        else:
            dx, dy = tp[0] - me[0], tp[1] - me[1]
            dist = (dx * dx + dy * dy) ** 0.5
            line += "   |   target %s  %.0f %.0f %.0f  d=%.0f" % (
                name, tp[0], tp[1], tp[2], dist)
    else:
        line += "   |   no target"

    if line == _last_line:
        return                      # standing still with nothing selected
    _last_line = line

    api.log(line)
