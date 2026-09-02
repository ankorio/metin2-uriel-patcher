"""autologin - launch to in-game unattended, by pressing the client's own buttons.

THE FLOW IS SHORTER THAN IT LOOKS
When an account has logged in before, the client starts in `mode='quick'`: the
landing screen shows "TOCA PARA JUGAR" instead of a login form, and the saved
credentials are already loaded. So the tap IS the login - there is no id/password
to fill, and __ShowLogin/__PrefillLastAccount/SetText are all beside the point.
An earlier version fought that for several iterations.

    intrologin.MainLoginWindow.__OnTapToPlay()   -> logs in, goes to select
    ...wait for the character list to arrive...
    <select window>.StartGameButton()            -> "ENTRAR AL JUEGO"

WHY THE WAIT MATTERS
The select window object exists before the server has sent the character list.
Pressing too early raises
    AttributeError: 'NoneType' object has no attribute 'GetMyCharacterCount'
which is not a wrong handler - it is the right handler, too soon.

Injected mouse clicks are NOT an option: they move the cursor but this client
ignores them (clicking a different character slot changed nothing).
"""

CAPABILITIES = ["read", "ui"]

ENABLED = True
TAP_AFTER = 5.0         # let the login window finish building
SETTLE = 3.0            # between steps; the client animates and talks to the server
MAX_STEPS = 40
VERBOSE = True

_next = 0.0
_steps = 0
_tapped = False
_done = False


def _m(n):
    import sys
    x = sys.modules.get(n)
    if x is None:
        try:
            __import__(n)
            x = sys.modules.get(n)
        except Exception:
            x = None
    return x


def _win(net, idx):
    """A phase window, or None. GetPhaseWindow is not a stable handle: index 2
    is None before login, a window after it, and an *int* at other times - so
    everything here checks the object rather than trusting the index."""
    try:
        w = net.GetPhaseWindow(idx)
    except Exception:
        return None
    return w if hasattr(w, "GetWindowName") else None


def _call(api, w, name):
    fn = getattr(w, name, None)
    if not callable(fn):
        return False
    try:
        fn()
        api.log("autologin: %s() ok" % name.split("__")[-1])
        return True
    except Exception as e:
        api.log("autologin: %s() -> %s: %s" % (name.split("__")[-1],
                                               type(e).__name__, e))
        return False


def on_load(api):
    global _next, _steps, _tapped, _done
    _next, _steps, _tapped, _done = 0.0, 0, False, False
    api.log("autologin: armed")


def on_update(api, dt):
    global _next, _steps, _tapped, _done
    if _done:
        return
    now = api.now()
    if _next == 0.0:
        _next = now + TAP_AFTER
        return
    if now < _next:
        return
    _next = now + SETTLE
    _steps += 1
    if _steps > MAX_STEPS:
        api.log("autologin: giving up after %d steps" % _steps)
        _done = True
        return

    net = _m("m2netm2g")
    if net is None:
        return
    try:
        if net.IsGamePhase():
            api.log("autologin: IN GAME after %d steps" % _steps)
            _done = True
            return
    except Exception:
        pass

    # 1. the landing gate, once
    if not _tapped:
        w = _win(net, 1)
        if w is not None and _call(api, w, "_MainLoginWindow__OnTapToPlay"):
            _tapped = True
        return

    # 2. the select screen, as soon as it has its characters
    sel = _win(net, 2)
    if sel is None:
        return
    if not callable(getattr(sel, "StartGameButton", None)):
        return
    if getattr(sel, "mycharacters", None) is None:
        if VERBOSE:
            api.log("autologin: select up, character list not delivered yet")
        return
    _call(api, sel, "StartGameButton")
