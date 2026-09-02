"""probe_ui - can we detour the game's own autohunt button into a mod?

THE QUESTION
------------
For autohunt2 we want the shipped autohunt button to drive OUR controller
instead of sending `/auto_hunt start` to the server. That is worth doing twice
over: it is the simplest possible test harness, and it also *improves*
isolation - a button that no longer emits the chat command cannot pollute the
developers' autohunt telemetry.

To do it we need to know, on the live Windows client:

  1. which autohunt UI modules are actually loaded (uiAutoHunt / uiAutoHuntNew
     / uiHud all exist as strings in the exe, but the HUD is mobile-oriented
     and may never be instantiated here);
  2. how to reach the live window INSTANCE, not just the class;
  3. whether the click handler is patchable - Cython `cdef class` extension
     types refuse attribute assignment, plain compiled Python classes allow it.
     modui already patches OnKeyDown on the phase-window instance, so the
     technique works somewhere; the question is whether it works here;
  4. what the button state depends on, since the visual on/off latch is driven
     by the server affect we deliberately never acquire.

Read-only apart from one setattr/delattr round-trip using a dummy attribute
name, which is undone immediately. Nothing here clicks anything, sends a
packet, or starts a hunt.
"""

CAPABILITIES = ["read"]

DELAY_S = 3.0
DUMMY = "_probe_ui_patch_test"
CHUNK = 5

_done = False
_t = 0.0


def _emit(api, label, names):
    names = sorted(set(names))
    if not names:
        api.log("probe_ui: %s: (none)" % label)
        return
    api.log("probe_ui: %s: %d" % (label, len(names)))
    for i in range(0, len(names), CHUNK):
        api.log("probe_ui:    %s" % ", ".join(names[i:i + CHUNK]))


def _safe_dir(o):
    try:
        return dir(o)
    except Exception:
        return []


def _interesting(names):
    out = []
    for n in names:
        low = n.lower()
        if ("hunt" in low or "click" in low or "toggle" in low
                or "event" in low or "interface" in low):
            out.append(n)
    return out


def _patchable(api, obj, what):
    """Can we assign an attribute here? Restore immediately either way."""
    try:
        setattr(obj, DUMMY, 1)
    except Exception as e:
        api.log("probe_ui:   %s NOT patchable (%s: %s)" % (what, type(e).__name__, e))
        return False
    try:
        delattr(obj, DUMMY)
    except Exception:
        api.log("probe_ui:   %s patchable but delattr failed - dummy left behind" % what)
        return True
    api.log("probe_ui:   %s PATCHABLE" % what)
    return True


def on_load(api):
    global _done, _t
    _done, _t = False, 0.0
    api.log("probe_ui: armed, reporting in %.0fs" % DELAY_S)


def on_update(api, dt):
    global _done, _t
    if _done:
        return
    _t += dt
    if _t < DELAY_S:
        return
    _done = True

    import sys

    api.log("probe_ui: ================== BEGIN ==================")
    try:
        # ---- 1. which autohunt UI modules are live ----------------------
        for name in ("uiAutoHunt", "uiAutoHuntNew", "uiHud", "interfaceModule",
                     "huntdialog", "hunt_dialog_window"):
            m = sys.modules.get(name)
            if m is None:
                try:
                    __import__(name)
                    m = sys.modules.get(name)
                    state = "imported on demand"
                except Exception as e:
                    api.log("probe_ui: module %-18s ABSENT (%s)" % (name, type(e).__name__))
                    continue
            else:
                state = "already loaded"
            classes = [n for n in _safe_dir(m) if not n.startswith("__")]
            api.log("probe_ui: module %-18s %s, %d names" % (name, state, len(classes)))

        # ---- 2. the AutoHunting classes and their click handlers --------
        for modname in ("uiAutoHunt", "uiAutoHuntNew"):
            m = sys.modules.get(modname)
            if m is None:
                continue
            cls = getattr(m, "AutoHunting", None)
            if cls is None:
                api.log("probe_ui: %s has no AutoHunting class" % modname)
                continue
            _emit(api, "%s.AutoHunting interesting members" % modname,
                  _interesting(_safe_dir(cls)))
            _patchable(api, cls, "%s.AutoHunting (class)" % modname)

        # ---- 3. reach the live instance via the phase window ------------
        n = sys.modules.get("m2netm2g")
        gw = None
        if n is not None:
            try:
                gw = n.GetPhaseWindow(5)
            except Exception as e:
                api.log("probe_ui: GetPhaseWindow(5) failed: %s" % type(e).__name__)
        if gw is None:
            api.log("probe_ui: no game phase window - is the client in game?")
        else:
            api.log("probe_ui: GameWindow = %s" % type(gw).__name__)
            _emit(api, "GameWindow interesting members", _interesting(_safe_dir(gw)))

            iface = getattr(gw, "interface", None)
            if iface is None:
                api.log("probe_ui: GameWindow has no .interface")
            else:
                api.log("probe_ui: interface = %s" % type(iface).__name__)
                _emit(api, "interface interesting members", _interesting(_safe_dir(iface)))
                _patchable(api, iface, "interface (instance)")

                # any attribute of interface that smells like the hunt window
                for a in _safe_dir(iface):
                    if "hunt" not in a.lower():
                        continue
                    try:
                        v = getattr(iface, a)
                    except Exception:
                        continue
                    api.log("probe_ui:   interface.%s = %s" % (a, type(v).__name__))
                    if v is not None and not callable(v):
                        _emit(api, "     its members", _interesting(_safe_dir(v)))
                        _patchable(api, v, "interface.%s (instance)" % a)

        # ---- 4. the HUD toggle, if the mobile HUD exists here -----------
        hud = sys.modules.get("uiHud")
        if hud is not None:
            hc = getattr(hud, "HudControls", None)
            if hc is None:
                api.log("probe_ui: uiHud has no HudControls")
            else:
                _emit(api, "uiHud.HudControls interesting members",
                      _interesting(_safe_dir(hc)))
                _patchable(api, hc, "uiHud.HudControls (class)")

    except Exception as e:
        api.log("probe_ui: FAILED %s: %s" % (type(e).__name__, e))
    api.log("probe_ui: =================== END ===================")
