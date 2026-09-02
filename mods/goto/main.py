"""goto - a /goto chat command that walks the main character to an (x, y).

Type in game and press enter:

    /goto 655,585

Movement is api.move_to = playerm2g2.AutoMoveToPosition (the client's own
pathing, the same auto-move follow/autohunt use). The command is caught by
wrapping m2netm2g.SendChatPacket - exactly how autohunt2 catches /auto_hunt - and
SWALLOWED, so the server never sees an unknown command.

Coordinates AUTO-SCALE so both forms land on the same spot:
  * SITE / display coords - what the market store header shows (e.g. 655,585).
    Small numbers (< 20000); multiplied by 100 into world units.
  * RAW WORLD coords - a shop's stored loc (e.g. 65541,58515). Large; used as-is.
"""
CAPABILITIES = ["read", "move", "net"]

import sys

CMD = "/goto"
SCALE_BELOW = 20000.0   # a target with |x|,|y| under this is a site (x100) coord

_patched = []


def _mod(name):
    m = sys.modules.get(name)
    if m is None:
        try:
            __import__(name)
            m = sys.modules.get(name)
        except Exception:
            m = None
    return m


def _to_world(x, y):
    """Return (world_x, world_y, tag). Small inputs are site coords -> x100."""
    if abs(x) < SCALE_BELOW and abs(y) < SCALE_BELOW:
        return x * 100.0, y * 100.0, "site"
    return x, y, "world"


def _parse(arg):
    parts = arg.replace(",", " ").split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except Exception:
        return None


def _do_goto(api, x, y):
    wx, wy, tag = _to_world(x, y)
    if api.move_to(wx, wy):
        api.log("goto: move_to(%.0f, %.0f) [%s] from %r" % (wx, wy, tag, api.player.position()))
        api.chat("goto: walking to %g, %g" % (x, y))
        return True
    api.log("goto: move_to unavailable")
    return False


def _install_chat(api):
    """Wrap m2netm2g.SendChatPacket so a typed /goto is caught before the wire.

    Same module-level hook autohunt2 uses for /auto_hunt; chaining is safe (each
    guard passes text it does not own through to the next)."""
    n = _mod("m2netm2g") or _mod("net")
    if n is None:
        api.log("goto: net module not found - chat command unavailable")
        return
    orig = getattr(n, "SendChatPacket", None)
    if orig is None:
        api.log("goto: SendChatPacket missing - chat command unavailable")
        return

    def guarded(msg, *a, **k):
        try:
            t = str(msg)
        except Exception:
            return orig(msg, *a, **k)
        s = t.strip()
        if s.lower().startswith(CMD):
            arg = s[len(CMD):].strip()
            pt = _parse(arg)
            if pt is None:
                api.log("goto: bad /goto arg %r" % arg)
                api.chat("goto: usage  /goto x,y")
            else:
                _do_goto(api, pt[0], pt[1])
            return None                          # swallow - never reaches server
        return orig(msg, *a, **k)

    try:
        setattr(n, "SendChatPacket", guarded)
        _patched.append((n, "SendChatPacket", orig))
        api.log("goto: /goto chat command installed on SendChatPacket")
    except Exception as e:
        api.log("goto: cannot install chat hook (%s)" % type(e).__name__)


def on_load(api):
    _install_chat(api)
    api.log("goto: loaded - type '/goto 655,585' in chat")


def on_unload(api):
    while _patched:
        owner, attr, orig = _patched.pop()
        try:
            setattr(owner, attr, orig)
        except Exception:
            pass
    api.log("goto: unloaded, chat hook restored")
