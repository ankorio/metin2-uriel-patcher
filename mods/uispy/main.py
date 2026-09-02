"""uispy - record what the UI actually does when a human clicks.

Guessing handler names off dir() has been expensive and wrong: __OnTapToPlay
was right, StartGameButton was right but called too early, GetPhaseWindow(2)
turned out to be an int, and SelectButton wanted an argument nobody knew about.

So stop guessing. This wraps the click dispatch in `ui` and logs, for every
press: the widget, the callback's name, its __self__ class, and the arguments
bound to it. Click a button by hand once and the log says exactly what to call.

Read-only in effect - every wrapper calls straight through, so the UI behaves
normally while being observed.
"""

CAPABILITIES = ["read", "ui"]

_patched = []
_dumped = False


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


def _describe(fn):
    """Name the callback a button will fire.

    Metin2 wraps handlers in ui.__mem_func__, so the useful name is one level
    down: repr() of the wrapper is just an address. Unwrap through whichever
    attribute this build uses, then report Class.method - which is exactly the
    string that can be called back later."""
    if fn is None:
        return "None"
    inner = fn
    # This build's ui.__mem_func__ exposes exactly one attribute: `call`.
    for attr in ("call", "im_func", "func", "method", "__func__", "_func"):
        got = getattr(fn, attr, None)
        if got is not None:
            inner = got
            break
    name = getattr(inner, "__name__", None)
    owner = getattr(inner, "__self__", None) or getattr(fn, "im_self", None) \
        or getattr(fn, "obj", None) or getattr(fn, "_obj", None)
    if name and owner is not None:
        return "%s.%s" % (type(owner).__name__, name)
    if name:
        return name
    # last resort: say what the wrapper exposes, so the next run can unwrap it
    return "%s attrs=%s" % (type(fn).__name__,
                            [a for a in dir(fn) if not a.startswith("__")][:12])


def _wrap(api, cls, meth, label):
    orig = getattr(cls, meth, None)
    if orig is None or not callable(orig):
        return False

    def spy(self, *a, **k):
        try:
            ev = getattr(self, "event", None) or getattr(self, "eventFunc", None)
            api.log("uispy: %s on %s(name=%r) -> event=%s args=%r"
                    % (label, type(self).__name__,
                       getattr(self, "GetWindowName", lambda: "?")(),
                       _describe(ev), a[:3]))
        except Exception:
            pass
        return orig(self, *a, **k)

    try:
        setattr(cls, meth, spy)
    except Exception as e:
        api.log("uispy: cannot patch %s.%s (%s)" % (cls.__name__, meth, type(e).__name__))
        return False
    _patched.append((cls, meth, orig))
    return True


def on_load(api):
    global _dumped
    ui = _m("ui")
    if ui is None:
        api.log("uispy: no 'ui' module")
        return

    if not _dumped:
        _dumped = True
        b = getattr(ui, "Button", None)
        if b is not None:
            api.log("uispy: ui.Button callables = %s"
                    % sorted(x for x in dir(b) if not x.startswith("__")
                             and callable(getattr(b, x, None)))[:60])

    hits = []
    for cname, meths in (("Button", ("CallEvent", "OnMouseLeftButtonUp", "Click")),
                         ("Window", ("OnMouseLeftButtonUp",))):
        cls = getattr(ui, cname, None)
        if cls is None:
            continue
        for mth in meths:
            if _wrap(api, cls, mth, "%s.%s" % (cname, mth)):
                hits.append("%s.%s" % (cname, mth))
    api.log("uispy: watching %s - click something and it will be logged"
            % (hits or "NOTHING"))


def on_unload(api):
    while _patched:
        cls, meth, orig = _patched.pop()
        try:
            setattr(cls, meth, orig)
        except Exception:
            pass
    api.log("uispy: unhooked")
