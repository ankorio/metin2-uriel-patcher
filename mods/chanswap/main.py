"""chanswap - programmatic channel change with NO clicks and NO native, by driving
the game's OWN registered ChannelChanger through method calls. Exposed as
`triarch_chan` for other mods:

    import sys
    cs = sys.modules.get("triarch_chan")
    cs.change_to(3)        # 1..5 ; True if the flow started
    cs.current()           # current channel (app.GetChannel)
    cs.state()

HOW (measured working sequence, 2026-08-09)
--------------------------------------------------------------------------
Every lighter path failed (raw net calls drop to login; a FRESH ChannelChanger
never gets channel status; SelectCh with the plain number or 10+ch does nothing).
What works is the LIVE, registered changer plus selecting the ListBox ROW:

    cw = net.GetPhaseWindow(5).interface.channelsw     # the registered instance
    cw.Show()
    cw._ChannelChanger__RefreshServerStateList()
    cw._ChannelChanger__Fill_Up_ChannelList()
    lb  = cw.ChannelList                               # a ui.ListBox
    row = index where lb.keyDict[row] == target        # keyDict {0:1,1:2,...}
    lb.SelectItem(row)                                 # sets lb.selectedLine
    cw._ChannelChanger__OnSelectChannel()              # propagate the selection
    cw._ChannelChanger__OnClickConnectButton()         # commit -> smooth change

The change is in-world -> in-world (a brief load), no re-login, no phase hacks.
Only channels 1..5 are supported (all in the first visible ListBox page; no
scrolling). The registered changer only exists in-game (phase 5).

DIRECT METHOD (2026-08-28, from a static decode of the Cython-compiled
connect-button handler)
--------------------------------------------------------------------------
`ChannelChanger.__OnClickConnectButton` does NO socket work of its own. After
the same-channel / special-map checks it runs exactly:

    name = m2netm2g.GetServerInfo().split(",")[0]        # "Triarch"
    m2netm2g.SetServerInfo(name + ", CH%d" % ch)
    self.Close()
    m2netm2g.SendChatPacket("/kanal " + str(ch))

The server answers the /kanal command with GC_WARP (port of the new channel);
the C++ net stream reconnects and the loading phase runs by itself. So the
whole change is two Python calls and the packet on the wire is byte-identical
to a real button press - no UI, no row selection, no channel-count limit from a
ListBox page. METHOD = "direct" uses this; "ui" keeps the driven-window recipe
above as the fallback.
"""

CAPABILITIES = ["read", "ui", "net"]

METHOD = "direct"       # "direct" (SetServerInfo + /kanal) or "ui" (drive the window)
MIN_CH, MAX_CH = 1, 5
SETTLE = 2.0            # between the select and the connect (client settles/animates)
LAND_TIMEOUT = 40.0    # whole flow budget

_api = None
_req = None            # requested channel, or None
_req_at = 0.0
_stage = 0
_stage_at = 0.0
_row = None
_last = None           # last result string


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


def current():
    app = _m("app")
    try:
        return app.GetChannel() if app else None
    except Exception:
        return None


def state():
    return {"requested": _req, "current": current(),
            "pending": _req is not None, "stage": _stage, "last_result": _last}


def _cw():
    """The live, registered ChannelChanger, or None (only exists in-game)."""
    net = _m("m2netm2g")
    try:
        return getattr(getattr(net.GetPhaseWindow(5), "interface", None), "channelsw", None)
    except Exception:
        return None


def _call(w, name, *args):
    fn = getattr(w, name, None)
    if not callable(fn):
        return False
    try:
        fn(*args)
        return True
    except Exception as e:
        if _api:
            _api.log("chanswap: %s%r -> %s: %s"
                     % (name.split("__")[-1], args, type(e).__name__, e))
        return False


def change_to(ch):
    """Request a change to channel `ch` (1..5). True if the flow started."""
    global _req, _req_at, _stage, _stage_at, _row, _last
    if _api is None:
        return False
    try:
        ch = int(ch)
    except Exception:
        return False
    if not (MIN_CH <= ch <= MAX_CH):
        _api.log("chanswap: CH%s out of range %d..%d" % (ch, MIN_CH, MAX_CH))
        _last = "out-of-range"
        return False
    if current() == ch:
        _api.log("chanswap: already on CH%d" % ch)
        _last = "already-there"
        return True
    if _req is not None:
        _api.log("chanswap: change to CH%s already in flight" % _req)
        return False
    _req, _req_at, _stage, _stage_at, _row, _last = ch, _api.now(), 0, _api.now(), None, "pending"
    _api.log("chanswap: change to CH%d requested" % ch)
    return True


def _direct(api, ch):
    """SetServerInfo + '/kanal N' - what the button does, without the button."""
    net = _m("m2netm2g")
    if net is None or not net.IsGamePhase():
        return False
    try:
        name = str(net.GetServerInfo()).split(",")[0]
    except Exception as e:
        api.log("chanswap: GetServerInfo failed %s: %s" % (type(e).__name__, e))
        return False
    try:
        net.SetServerInfo("%s, CH%d" % (name, ch))
        net.SendChatPacket("/kanal %d" % ch)
    except Exception as e:
        api.log("chanswap: direct change failed %s: %s" % (type(e).__name__, e))
        return False
    api.log("chanswap: direct - SetServerInfo('%s, CH%d') + '/kanal %d' sent" % (name, ch, ch))
    return True


def _reset(result):
    global _req, _stage, _row, _last
    _req, _stage, _row = None, 0, None
    _last = result


def _poll_env(api):
    """Honour a channel request another mod posted through os.environ.

    Mods run in isolated namespaces, so a caller like autohunt2 (LOOP_CHANNELS)
    cannot invoke change_to() directly - it writes the target channel into
    TRIARCH_CHAN_REQUEST, the same environment channel the loot handshake uses.
    Read it once and clear it, so a single request causes exactly one change; a
    swap already in flight (_req set) or a request for the channel we are already
    on is a no-op."""
    import os
    try:
        raw = os.environ.get("TRIARCH_CHAN_REQUEST", "")
    except Exception:
        return
    if not raw or raw == "0":
        return
    try:
        os.environ["TRIARCH_CHAN_REQUEST"] = ""      # consume it, exactly once
    except Exception:
        pass
    try:
        ch = int(raw)
    except Exception:
        return
    if _req is None and current() != ch:
        change_to(ch)


def on_update(api, dt):
    global _req, _stage, _stage_at, _row
    _poll_env(api)
    if _req is None:
        return
    now = api.now()

    # landed?
    net = _m("m2netm2g")
    try:
        if current() == _req and net is not None and net.IsGamePhase():
            api.log("chanswap: landed on CH%d after %.1fs" % (_req, now - _req_at))
            _reset("ok")
            return
    except Exception:
        pass

    if now - _req_at > LAND_TIMEOUT:
        api.log("chanswap: CH%d timed out after %.0fs - now on CH%s"
                % (_req, LAND_TIMEOUT, current()))
        _reset("timeout")
        return

    if METHOD == "direct":
        if _stage == 0:
            _stage = 3 if _direct(api, _req) else 0
            if _stage == 0:
                _reset("direct-failed")
        return

    cw = _cw()
    if cw is None:
        return  # not in-game yet / mid-load; keep waiting

    if _stage == 0:
        _call(cw, "Show")
        _call(cw, "_ChannelChanger__RefreshServerStateList")
        _call(cw, "_ChannelChanger__Fill_Up_ChannelList")
        lb = cw.__dict__.get("ChannelList")
        kd = lb.__dict__.get("keyDict", {}) if lb is not None else {}
        _row = None
        for idx, chnum in kd.items():
            if chnum == _req:
                _row = idx
                break
        if _row is None:
            api.log("chanswap: CH%d not found in changer rows %r" % (_req, kd))
            _reset("no-row")
            return
        _stage, _stage_at = 1, now
        return

    if now - _stage_at < SETTLE:
        return
    _stage_at = now
    lb = cw.__dict__.get("ChannelList")
    if lb is None:
        _reset("no-listbox")
        return

    if _stage == 1:
        # select the target row, then propagate the selection
        if not _call(lb, "SelectItem", _row):
            try:
                lb.selectedLine = _row
            except Exception:
                pass
        _call(cw, "_ChannelChanger__OnSelectChannel")
        if lb.__dict__.get("selectedLine") != _row:
            api.log("chanswap: row select did not take (selectedLine=%r != %r)"
                    % (lb.__dict__.get("selectedLine"), _row))
            _reset("select-failed")
            return
        _stage = 2
        return

    if _stage == 2:
        api.log("chanswap: committing CH%d (row %r)" % (_req, _row))
        _call(cw, "_ChannelChanger__OnClickConnectButton")
        _stage = 3          # now just wait for the landed check to fire
        return


def on_load(api):
    global _api, _req, _stage, _row, _last
    _api = api
    _req, _stage, _row, _last = None, 0, None, None
    import sys

    class _H(object):
        pass
    h = _H()
    h.change_to = change_to
    h.current = current
    h.state = state
    sys.modules["triarch_chan"] = h
    api.log("chanswap: ready - triarch_chan.change_to(1..5)")


def on_unload(api):
    import sys
    try:
        del sys.modules["triarch_chan"]
    except Exception:
        pass
    api.log("chanswap: unloaded")
