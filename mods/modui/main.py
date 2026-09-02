"""modui - mod manager window: tab rail on the left, one mod's page on the right.

Layout mirrors the shipped auto-potion window. The rail is generated from the
installed mods; each page is built by api.ui (uikit) from that mod's
mods/<name>/ui.json. A mod with no ui.json still gets a valid page - just the
enable checkbox - so nothing has to ship a spec to be manageable here.

Edits are held in memory and written by Save, so dragging a slider does not
rewrite config.json (and reload the mod) on every tick.
"""

import json
import os
import sys

CAPABILITIES = ["ui", "config"]

# OPEN_ON_START is the persisted default and is config-overridable.
# _show is the LIVE per-client state and is deliberately never persisted:
# both clients read the same config.json, so a saved "SHOW" key made one
# window's F10 close the other's.
OPEN_ON_START = True
TOGGLE_KEY = 68         # app.DIK_F10
_show = None            # None until on_load seeds it from OPEN_ON_START

WIDTH = 520
HEIGHT = 430
RAIL_X = 10
RAIL_W = 120
RAIL_ROW_H = 26
PANE_X = RAIL_X + RAIL_W + 10
PANE_Y = 30
PANE_W = WIDTH - PANE_X - 12

_win = None
_rail = []              # [(mod name, label widget)]
_railhits = []          # click targets, kept alive alongside the labels
_page = None            # Rendered from uikit, replaced on tab switch
_pagebox = None
_hint = None
_selected = None
_pending = {}           # {mod: {key: value}} unsaved edits
_built = False
_failed = False
_last_show = None
_keybound = None


# ---- config ---------------------------------------------------------------

def _mods_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg_path(api=None):
    """Where THIS character's settings live.

    api.config_path() resolves mods/profiles/<character>.json when one exists,
    falling back to the shared mods/config.json. Several clients run from one
    mods folder, so writing config.json unconditionally rewrote every other
    character's settings - which is exactly what profiles exist to stop, and is
    only actually stopped if the UI writes where the loader reads.

    api is optional so the pre-login paths still work; without it this is the
    shared file, which is the correct answer when no character is known yet."""
    if api is not None:
        try:
            return api.config_path()
        except Exception:
            pass
    return os.path.join(_mods_dir(), "config.json")


def _read_json(path, default):
    try:
        f = open(path, "r")
        try:
            return json.loads(f.read())
        finally:
            f.close()
    except Exception:
        return default


def _read_cfg(api=None):
    return _read_json(_cfg_path(api), {})


def _write_cfg(api, cfg):
    """Atomic: temp file then os.replace, so a reader never sees a partial
    config and the other mods' sections cannot be lost to a torn write."""
    path = _cfg_path(api)
    tmp = path + ".tmp"
    try:
        f = open(tmp, "w")
        try:
            f.write(json.dumps(cfg, indent=2))
        finally:
            f.close()
        os.replace(tmp, path)
        return True
    except Exception:
        api.log("modui: could not write %s" % os.path.basename(path))
        return False


def _mod_names():
    out = []
    try:
        for n in sorted(os.listdir(_mods_dir())):
            if n.startswith((".", "_")):
                continue
            if os.path.isfile(os.path.join(_mods_dir(), n, "main.py")):
                out.append(n)
    except Exception:
        pass
    return out


def _spec_of(name):
    return _read_json(os.path.join(_mods_dir(), name, "ui.json"), {})


def _effective_cfg(api):
    """What the mod host actually sees: shared config with the profile over it.

    The UI has to show the SAME merge the loader performs, or it displays the
    shared value for a setting the profile has overridden - and then writing it
    back would "save" a number that was never on screen. Reading only the
    profile is equally wrong: it holds just the differences, so everything else
    would appear blank."""
    base = _read_json(os.path.join(_mods_dir(), "config.json"), {})
    path = _cfg_path(api)
    if os.path.basename(path).lower() == "config.json":
        return base
    over = _read_json(path, {})
    out = {}
    for k, v in base.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    for mod, sect in over.items():
        if isinstance(sect, dict) and isinstance(out.get(mod), dict):
            out[mod].update(sect)
        else:
            out[mod] = sect
    return out


def _values_of(api, name):
    """Saved values with any unsaved edits laid over the top."""
    v = dict(_effective_cfg(api).get(name, {}))
    v.setdefault("enabled", True)
    v.update(_pending.get(name, {}))
    return v


def _dirty():
    return any(_pending.values())


# ---- key binding ----------------------------------------------------------

def _set_show(api, show):
    """Open/close state is per client and deliberately NOT persisted.

    Both clients share mods/config.json, so writing SHOW there made F10 in one
    window toggle the other as well. The config value is only the startup
    default now."""
    global _show, _last_show
    _show = show
    if _win is not None:
        try:
            _win.Show() if show else _win.Hide()
        except Exception:
            pass
    _last_show = show


def _bind_key(api):
    global _keybound
    n = sys.modules.get("m2netm2g")
    if n is None:
        _keybound = "no m2netm2g"
        return
    try:
        win = n.GetPhaseWindow(5)
    except Exception as e:
        _keybound = "GetPhaseWindow failed: %s" % type(e).__name__
        return
    if win is None:
        _keybound = "no game phase window yet"
        return
    orig = getattr(win, "OnKeyDown", None)
    if orig is None:
        _keybound = "GameWindow has no OnKeyDown - key handling is native"
        api.log("modui: %s" % _keybound)
        return

    def hooked(key, _orig=orig):
        try:
            if key == TOGGLE_KEY:
                _set_show(api, not _show)
                return True
        except Exception:
            pass
        return _orig(key)

    try:
        win.OnKeyDown = hooked
        _keybound = "F10 bound on the phase window instance"
    except Exception:
        try:
            type(win).OnKeyDown = hooked
            _keybound = "F10 bound on GameWindow"
        except Exception as e:
            _keybound = "cannot patch OnKeyDown (%s)" % type(e).__name__
    api.log("modui: %s" % _keybound)


# ---- actions --------------------------------------------------------------

def _title(api):
    try:
        _win.SetTitleName("Mods *" if _dirty() else "Mods")
    except Exception:
        pass


def _on_change(api, key, value):
    """Every widget edit lands here. Nothing is written until Save."""
    if key == "__action__":
        _action(api, value)
        return
    if _selected:
        _pending.setdefault(_selected, {})[key] = value
    _title(api)


def _action(api, action):
    if action == "save":
        _save(api)
    elif action == "reset":
        _pending.pop(_selected, None)
        api.log("modui: reverted unsaved edits for %s" % _selected)
        _open_page(api, _selected)
        _title(api)
    else:
        api.log("modui: unknown action %r" % (action,))


def _save(api):
    if not _dirty():
        api.log("modui: nothing to save")
        return
    cfg = _read_cfg(api)
    for mod, edits in _pending.items():
        if not edits:
            continue
        sec = cfg.setdefault(mod, {})
        sec.update(edits)
        api.log("modui: saved %s %r -> %s"
                % (mod, edits, os.path.basename(_cfg_path(api))))
    _pending.clear()
    _write_cfg(api, cfg)          # one write -> modhost reloads once
    _title(api)


# ---- window ---------------------------------------------------------------

def _open_page(api, name):
    """Swap the right pane to `name`'s page."""
    global _page, _selected
    _selected = name
    if _page is not None:
        _page.destroy()
        _page = None

    spec = _spec_of(name)
    # The enable checkbox is injected, never declared in ui.json - so a mod
    # with no spec at all still gets a usable page.
    rows = [{"type": "checkbox", "key": "enabled", "label": "Enable %s" % name}]
    rows += [r for r in spec.get("rows", []) if isinstance(r, dict)]

    if api.ui is None:
        api.log("modui: api.ui is None - uikit.py missing")
        return
    _page = api.ui.render({"rows": rows}, _pagebox, _values_of(api, name),
                          lambda k, v: _on_change(api, k, v),
                          api.log, width=PANE_W - 16)
    if _hint is not None:
        try:
            _hint.SetText(spec.get("hint", ""))
        except Exception:
            pass
    _refresh_rail()


RAIL_ON = 0xFFFFC89B      # selected: warm highlight, as the settings window uses
RAIL_OFF = 0xFFAAAAAA


def _refresh_rail():
    for name, widget in _rail:
        try:
            widget.SetText(name)
        except Exception:
            pass
        for meth in ("SetPackedFontColor", "SetFontColor"):
            if hasattr(widget, meth):
                try:
                    getattr(widget, meth)(RAIL_ON if name == _selected else RAIL_OFF)
                    break
                except Exception:
                    pass


def _build(api):
    global _win, _built, _failed, _pagebox, _hint
    ui = sys.modules.get("ui")
    if ui is None:
        api.log("modui: 'ui' not loaded - cannot build")
        _failed = True
        return

    def T(label, fn):
        try:
            return fn()
        except Exception as e:
            api.log("modui: %s failed - %s: %s" % (label, type(e).__name__, e))
            return None

    _win = T("BoardWithTitleBar", lambda: ui.BoardWithTitleBar())
    if _win is None:
        _failed = True
        return
    T("SetSize", lambda: _win.SetSize(WIDTH, HEIGHT))
    T("SetTitleName", lambda: _win.SetTitleName("Mods"))
    T("SetCenterPosition", lambda: _win.SetCenterPosition())
    T("AddFlag(float)", lambda: _win.AddFlag("float"))
    T("AddFlag(movable)", lambda: _win.AddFlag("movable"))
    T("SetCloseEvent", lambda: _win.SetCloseEvent(lambda: _set_show(api, False)))
    # selection is shown by colouring the rail label, not by a "> " marker
    T("Show", lambda: _win.Show())

    names = _mod_names()

    # left rail: one clickable row per installed mod
    rowcls = None
    for c in ("Window", "Box", "Bar"):
        if getattr(ui, c, None) is not None:
            rowcls = getattr(ui, c)
            break
    # The rail sits on its own board. A bare ui.Window has no visual, and
    # children parented to one DO NOT RENDER - that is why the first rail was
    # invisible even though every widget was created without error. So: text on
    # the board, hitbox as a sibling on top purely to catch clicks.
    railbox = T("railbox", lambda: (getattr(ui, "ThinBoard", None) or ui.Board)())
    if railbox is not None:
        T("railbox.SetParent", lambda: railbox.SetParent(_win))
        T("railbox.SetPosition", lambda: railbox.SetPosition(RAIL_X, PANE_Y))
        T("railbox.SetSize",
          lambda: railbox.SetSize(RAIL_W, HEIGHT - PANE_Y - 60))
        T("railbox.Show", lambda: railbox.Show())
    y = PANE_Y + 8
    for name in names:
        lbl = T("rail label", lambda: ui.TextLine())
        if lbl is not None:
            T("lbl.SetParent", lambda l=lbl: l.SetParent(_win))
            for meth in ("SetHorizontalAlignLeft", "SetWindowHorizontalAlignLeft"):
                if hasattr(lbl, meth):
                    T(meth, lambda l=lbl, m=meth: getattr(l, m)())
            T("lbl.SetPosition", lambda l=lbl, yy=y: l.SetPosition(RAIL_X + 10, yy))
            T("lbl.SetText", lambda l=lbl, n=name: l.SetText(n))
            T("lbl.Show", lambda l=lbl: l.Show())
            _rail.append((name, lbl))

        hit = T("rail hit", lambda: rowcls()) if rowcls else None
        if hit is not None:
            T("hit.SetParent", lambda h=hit: h.SetParent(_win))
            T("hit.SetPosition", lambda h=hit, yy=y: h.SetPosition(RAIL_X + 4, yy - 4))
            T("hit.SetSize", lambda h=hit: h.SetSize(RAIL_W - 6, RAIL_ROW_H - 2))
            T("hit.SetPickAlways", lambda h=hit: h.SetPickAlways())
            T("hit.SetMouseLeftButtonDownEvent",
              lambda h=hit, n=name: h.SetMouseLeftButtonDownEvent(
                  lambda n=n: _open_page(api, n)))
            T("hit.Show", lambda h=hit: h.Show())
            _railhits.append(hit)
        y += RAIL_ROW_H

    # right pane container
    _pagebox = T("pane", lambda: (getattr(ui, "ThinBoard", None) or ui.Board)())
    if _pagebox is not None:
        T("pane.SetParent", lambda: _pagebox.SetParent(_win))
        T("pane.SetPosition", lambda: _pagebox.SetPosition(PANE_X, PANE_Y))
        T("pane.SetSize", lambda: _pagebox.SetSize(PANE_W, HEIGHT - PANE_Y - 60))
        T("pane.Show", lambda: _pagebox.Show())

    # footer: hint + Save
    _hint = T("hint", lambda: ui.TextLine())
    if _hint is not None:
        T("hint.SetParent", lambda: _hint.SetParent(_win))
        for meth in ("SetHorizontalAlignLeft", "SetWindowHorizontalAlignLeft"):
            if hasattr(_hint, meth):
                T(meth, lambda m=meth: getattr(_hint, m)())
        T("hint.SetPosition", lambda: _hint.SetPosition(PANE_X, HEIGHT - 46))
        T("hint.SetText", lambda: _hint.SetText(""))
        T("hint.Show", lambda: _hint.Show())

    if api.ui is not None:
        api.ui.render({"rows": [{"type": "button", "label": "Guardar",
                                 "action": "save", "x": RAIL_X}]},
                      _win, {}, lambda k, v: _on_change(api, k, v),
                      api.log, width=WIDTH, top=HEIGHT - 44)

    _built = True
    api.log("modui: built rail with %d tabs" % len(names))
    if names:
        _open_page(api, _selected if _selected in names else names[0])


def on_load(api):
    global _show
    if _show is None:                 # first load in this client
        _show = bool(OPEN_ON_START)
    api.log("modui: loaded (open=%s, toggle key %d, api v%s)"
            % (_show, TOGGLE_KEY, api.VERSION))


def on_unload(api):
    global _win, _built, _page, _pagebox, _hint
    if _page is not None:
        _page.destroy()
    if _win is not None:
        for m in ("Hide", "Destroy"):
            try:
                getattr(_win, m)()
            except Exception:
                pass
    _win, _built, _page, _pagebox, _hint = None, False, None, None, None
    del _rail[:]
    del _railhits[:]


def on_update(api, dt):
    global _last_show
    if _failed or not api.in_game():
        return
    if _keybound is None:
        _bind_key(api)
    if not _built:
        if not _show:
            return
        _build(api)
        _last_show = True
        return
    if _show != _last_show:
        _last_show = _show
        try:
            _win.Show() if _show else _win.Hide()
        except Exception:
            pass
