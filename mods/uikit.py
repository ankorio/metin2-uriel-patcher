"""uikit - build mod UI from a JSON spec.

Loaded by modhost beside api.py and handed to every mod as `api.ui`, so this is
the shared way mods create UI rather than each one writing layout code.

    rendered = api.ui.render(spec, host, values, on_change, log)
    ...
    rendered.destroy()

`spec` is a parsed ui.json; `values` is {key: current value}; `on_change(key,
value)` fires on every edit. uikit never writes files and never reads config -
the caller owns persistence.

Three constraints of this client shape the code, each learned the hard way:

  * A widget with NO artwork has zero size and can never be picked. Bare
    ui.Button renders its text and silently ignores clicks, so anything
    clickable gets real art or a ui.Window with SetPickAlways() plus
    SetMouseLeftButtonDownEvent.
  * Art must come in MATCHED pairs - checkbox_disabled.png is a large X meaning
    "unavailable", not "unchecked".
  * Widgets are collected the moment nothing references them. Rendered.widgets
    holds every one for as long as the page is up.
"""

import sys

UIKIT_VERSION = 1

CHECK_ON = "d:/ymir work/ui/checkbox/checkbox_new_selected.tga"
CHECK_OFF = "d:/ymir work/ui/checkbox/checkbox_new_unselected.tga"
# btn_07_* is 166x54 - far too large for a form button. The btn_05 family is
# the only one with small sized variants; 76x40 matches the shipped settings
# window. "active" doubles as the pressed state.
BTN_UP = "assets/button/btn_05_deactive_76x40.png"
BTN_OVER = "assets/button/btn_05_deactive-hover_76x40.png"
BTN_DOWN = "assets/button/btn_05_active_76x40.png"
INPUT_BG = "assets/input/bg_input_02_190x36.png"

ROW_H = 24
GAP = 4
LABEL_X = 10
CTRL_X = 140
SLIDER_W = 110


def _ui():
    return sys.modules.get("ui")


class Rendered(object):
    """Everything one page created. Keeps widgets alive; destroys on switch."""

    def __init__(self, log):
        self.widgets = []
        self.height = 0
        self.log = log

    def keep(self, w):
        if w is not None:
            self.widgets.append(w)
        return w

    def destroy(self):
        for w in reversed(self.widgets):
            for meth in ("Hide", "Destroy"):
                try:
                    getattr(w, meth)()
                except Exception:
                    pass
        del self.widgets[:]


class _Ctx(object):
    def __init__(self, host, values, on_change, log, width):
        self.host = host
        self.values = values
        self.on_change = on_change
        self.log = log
        self.width = width
        self.out = Rendered(log)

    def try_(self, label, fn):
        try:
            return fn()
        except Exception as e:
            self.log("uikit: %s failed - %s: %s" % (label, type(e).__name__, e))
            return None


# ---- primitives -----------------------------------------------------------

def _text(ctx, parent, x, y, text, color=None):
    ui = _ui()
    t = ctx.try_("TextLine", lambda: ui.TextLine())
    if t is None:
        return None
    ctx.try_("SetParent", lambda: t.SetParent(parent))
    if color is not None:
        # Both spellings exist across builds; whichever lands first wins and the
        # other is a no-op. A row that cannot be coloured must still be a row.
        for meth in ("SetPackedFontColor", "SetFontColor"):
            if hasattr(t, meth):
                if ctx.try_(meth, lambda m=meth: getattr(t, m)(color)) is not None:
                    break
    for meth in ("SetHorizontalAlignLeft", "SetWindowHorizontalAlignLeft"):
        if hasattr(t, meth):
            ctx.try_(meth, lambda m=meth: getattr(t, m)())
    ctx.try_("SetPosition", lambda: t.SetPosition(x, y))
    ctx.try_("SetText", lambda: t.SetText(str(text)))
    ctx.try_("Show", lambda: t.Show())
    return ctx.out.keep(t)


def _hitbox(ctx, parent, x, y, w, h, onclick):
    """A pickable region. Needs no artwork - this is how the shipped ItemRow
    does its checkbox behaviour too."""
    ui = _ui()
    cls = None
    for name in ("Window", "Box", "Bar"):
        if getattr(ui, name, None) is not None:
            cls = getattr(ui, name)
            break
    if cls is None:
        return None
    r = ctx.try_("hitbox", lambda: cls())
    if r is None:
        return None
    ctx.try_("SetParent", lambda: r.SetParent(parent))
    ctx.try_("SetPosition", lambda: r.SetPosition(x, y))
    ctx.try_("SetSize", lambda: r.SetSize(w, h))
    ctx.try_("SetPickAlways", lambda: r.SetPickAlways())
    ctx.try_("SetMouseLeftButtonDownEvent",
             lambda: r.SetMouseLeftButtonDownEvent(onclick))
    ctx.try_("Show", lambda: r.Show())
    return ctx.out.keep(r)


def _checkimage(ctx, parent, x, y, on):
    ui = _ui()
    b = ctx.try_("ImageBox", lambda: ui.ImageBox())
    if b is None:
        return None
    ctx.try_("SetParent", lambda: b.SetParent(parent))
    ctx.try_("LoadImage", lambda: b.LoadImage(CHECK_ON if on else CHECK_OFF))
    ctx.try_("SetPosition", lambda: b.SetPosition(x, y))
    ctx.try_("Show", lambda: b.Show())
    return ctx.out.keep(b)


# ---- row builders ---------------------------------------------------------
# each returns the height it consumed

def _b_label(ctx, row, y):
    # "color": 0xAARRGGBB, optional. Used for warnings that must not read as
    # ordinary help text.
    _text(ctx, ctx.host, LABEL_X, y, row.get("text", ""), row.get("color"))
    return ROW_H


def _b_separator(ctx, row, y):
    ui = _ui()
    line = None
    if getattr(ui, "Line", None) is not None:
        line = ctx.try_("Line", lambda: ui.Line())
        if line is not None:
            ctx.try_("SetParent", lambda: line.SetParent(ctx.host))
            ctx.try_("SetPosition", lambda: line.SetPosition(LABEL_X, y + 6))
            ctx.try_("SetSize", lambda: line.SetSize(ctx.width - 2 * LABEL_X, 1))
            ctx.try_("Show", lambda: line.Show())
            ctx.out.keep(line)
    if line is None:
        _text(ctx, ctx.host, LABEL_X, y, "-" * 34)
    return GAP + 10


def _b_value(ctx, row, y):
    key = row.get("key", "")
    _text(ctx, ctx.host, LABEL_X, y, row.get("label", key))
    _text(ctx, ctx.host, CTRL_X, y, ctx.values.get(key, ""))
    return ROW_H


def _b_checkbox(ctx, row, y):
    key = row.get("key", "")
    label = row.get("label", key)
    state = {"on": bool(ctx.values.get(key, False))}

    img = _checkimage(ctx, ctx.host, LABEL_X, y, state["on"])
    _text(ctx, ctx.host, LABEL_X + 24, y + 1, label)

    def clicked():
        state["on"] = not state["on"]
        if img is not None:
            try:
                img.LoadImage(CHECK_ON if state["on"] else CHECK_OFF)
            except Exception:
                pass
        ctx.on_change(key, state["on"])

    _hitbox(ctx, ctx.host, LABEL_X - 2, y - 2, ctx.width - 20, ROW_H - 2, clicked)
    return ROW_H


def _b_edit(ctx, row, y):
    ui = _ui()
    key = row.get("key", "")
    _text(ctx, ctx.host, LABEL_X, y, row.get("label", key))

    bg = ctx.try_("input bg", lambda: ui.ImageBox())
    if bg is not None:
        ctx.try_("bg.SetParent", lambda: bg.SetParent(ctx.host))
        ctx.try_("bg.LoadImage", lambda: bg.LoadImage(INPUT_BG))
        ctx.try_("bg.SetPosition", lambda: bg.SetPosition(CTRL_X - 4, y - 5))
        ctx.try_("bg.Show", lambda: bg.Show())
        ctx.out.keep(bg)

    e = ctx.try_("EditLine", lambda: ui.EditLine())
    if e is None:
        _text(ctx, ctx.host, CTRL_X, y, ctx.values.get(key, ""))
        return ROW_H
    ctx.try_("SetParent", lambda: e.SetParent(ctx.host))
    ctx.try_("SetPosition", lambda: e.SetPosition(CTRL_X, y))
    ctx.try_("SetSize", lambda: e.SetSize(row.get("width", 120), ROW_H - 6))
    for meth in ("SetMax", "SetMaxLength"):
        if hasattr(e, meth):
            ctx.try_(meth, lambda m=meth: getattr(e, m)(int(row.get("max_length", 32))))
    if row.get("numeric"):
        ctx.try_("SetNumberMode", lambda: e.SetNumberMode())
    ctx.try_("SetText", lambda: e.SetText(str(ctx.values.get(key, ""))))
    ctx.try_("Show", lambda: e.Show())
    ctx.out.keep(e)

    def changed(_e=e, _k=key):
        try:
            ctx.on_change(_k, _e.GetText())
        except Exception:
            pass

    # not all builds expose every hook; whichever exists wins
    for hook in ("SetReturnEvent", "SetEscapeEvent"):
        if hasattr(e, hook):
            ctx.try_(hook, lambda h=hook: getattr(e, h)(changed))
    ctx.out.keep(changed)
    return ROW_H


def _b_slider(ctx, row, y):
    ui = _ui()
    key = row.get("key", "")
    lo = float(row.get("min", 0))
    hi = float(row.get("max", 100))
    step = float(row.get("step", 0)) or 0.0
    suffix = row.get("suffix", "")
    cur = float(ctx.values.get(key, lo) or lo)
    span = (hi - lo) or 1.0

    _text(ctx, ctx.host, LABEL_X, y, row.get("label", key))
    readout = _text(ctx, ctx.host, CTRL_X + SLIDER_W + 12, y,
                    "%g%s" % (cur, suffix))

    s = ctx.try_("SliderBar", lambda: ui.SliderBar())
    if s is None:
        return ROW_H
    ctx.try_("SetParent", lambda: s.SetParent(ctx.host))
    ctx.try_("SetPosition", lambda: s.SetPosition(CTRL_X, y))
    ctx.try_("SetSize", lambda: s.SetSize(SLIDER_W, ROW_H - 6))
    ctx.try_("SetSliderPos", lambda: s.SetSliderPos((cur - lo) / span))
    ctx.try_("Show", lambda: s.Show())
    ctx.out.keep(s)

    def moved(_s=s, _k=key):
        try:
            pos = float(_s.GetSliderPos())
        except Exception:
            return
        val = lo + pos * span
        if step:
            val = round(val / step) * step
        if float(int(val)) == val:
            val = int(val)
        if readout is not None:
            try:
                readout.SetText("%g%s" % (val, suffix))
            except Exception:
                pass
        ctx.on_change(_k, val)

    ctx.try_("SetEvent", lambda: s.SetEvent(moved))
    ctx.out.keep(moved)
    return ROW_H


def _b_button(ctx, row, y):
    ui = _ui()
    label = row.get("label", "?")
    action = row.get("action", "")
    w = int(row.get("width", 90))

    def clicked():
        ctx.on_change("__action__", action)

    b = ctx.try_("Button", lambda: ui.Button())
    ok = False
    if b is not None:
        ctx.try_("SetParent", lambda: b.SetParent(ctx.host))
        ctx.try_("SetPosition", lambda: b.SetPosition(row.get("x", LABEL_X), y))
        # A button with no visuals has zero size and cannot be picked.
        up = ctx.try_("SetUpVisual", lambda: b.SetUpVisual(BTN_UP))
        ctx.try_("SetOverVisual", lambda: b.SetOverVisual(BTN_OVER))
        ctx.try_("SetDownVisual", lambda: b.SetDownVisual(BTN_DOWN))
        ctx.try_("SetText", lambda: b.SetText(label))
        ctx.try_("SetEvent", lambda: b.SetEvent(clicked))
        ctx.try_("Show", lambda: b.Show())
        ctx.out.keep(b)
        ok = up is not None or True
    if not ok or b is None:
        _text(ctx, ctx.host, row.get("x", LABEL_X) + 6, y + 2, "[ %s ]" % label)
        _hitbox(ctx, ctx.host, row.get("x", LABEL_X), y, w, ROW_H - 4, clicked)
    return ROW_H


_BUILDERS = {
    "label": _b_label,
    "separator": _b_separator,
    "value": _b_value,
    "checkbox": _b_checkbox,
    "edit": _b_edit,
    "slider": _b_slider,
    "button": _b_button,
}


def render(spec, host, values, on_change, log, width=300, top=6, zone="body"):
    """Build one page. Returns a Rendered whose .destroy() tears it down.

    Unknown row types are logged and skipped rather than raising, so a spec
    written for a newer uikit still renders everything it can."""
    ctx = _Ctx(host, values or {}, on_change, log, width)
    y = top
    for row in (spec or {}).get("rows", []):
        if not isinstance(row, dict):
            continue
        if row.get("zone", "body") != zone:
            continue
        fn = _BUILDERS.get(row.get("type"))
        if fn is None:
            log("uikit: unknown row type %r - skipped" % (row.get("type"),))
            continue
        try:
            y += fn(ctx, row, y) + GAP
        except Exception as e:
            log("uikit: row %r raised %s: %s" % (row.get("type"), type(e).__name__, e))
    ctx.out.height = y - top
    return ctx.out
