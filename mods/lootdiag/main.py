"""lootdiag - prove the ground-item walk returns real drops.

Enumerates every ground item the client knows about, straight out of the
std::map at CPythonItem+4. Read-only: it never picks anything up.

The map is only populated while something is actually lying on the floor, so a
run over an empty field legitimately reports 0. That is why this samples
continuously and only speaks when the answer *changes* - a wall of "0 ground
item(s)" proves nothing and drowns the one line that matters.
"""
CAPABILITIES = ["read"]
ENABLED = True

EVERY = 1.0             # ground items despawn; sample faster than that
QUIET_REPEAT = 30.0     # re-state an unchanged answer this often, at most

_next = 0.0
_last = None
_last_said = 0.0
_peak = 0


def on_load(api):
    global _next, _last, _last_said, _peak
    _next, _last, _last_said, _peak = 0.0, None, 0.0, 0
    api.log("lootdiag: armed (continuous)")


def on_update(api, dt):
    global _next, _last, _last_said, _peak
    if not api.in_game():
        return
    now = api.now()
    if now < _next:
        return
    _next = now + EVERY

    tn = api.natives.module()
    if tn is None:
        return
    try:
        n = tn.ground_items()
    except Exception as e:
        api.log("lootdiag: ground_items failed %s: %s" % (type(e).__name__, e))
        _next = now + 10.0
        return

    ids = []
    for i in range(min(n, 16)):
        try:
            iid = tn.ground_item_at(i)
        except Exception as e:
            api.log("lootdiag: ground_item_at(%d) failed %s: %s"
                    % (i, type(e).__name__, e))
            break
        # vnum is the whole point of the exercise, but it is a newer native -
        # stay useful against a stub that predates it rather than dying.
        try:
            vnum = tn.ground_item_vnum(i)
        except Exception:
            vnum = None
        # Which of the two trailing std::strings is the ownership name is the
        # open question - print both, with the player's own name alongside, and
        # the answer is whichever column matches on our own drops.
        try:
            s1 = tn.ground_item_str(i, 0)
            s2 = tn.ground_item_str(i, 1)
            x, y = tn.ground_item_pos(i)
        except Exception:
            s1 = s2 = None
            x = y = None
        if s1 is None:
            ids.append("%d" % iid if vnum is None else "%d:v%d" % (iid, vnum))
        else:
            ids.append("%d:v%s @(%s,%s) s1=%r s2=%r" % (iid, vnum, x, y, s1, s2))

    key = (n, tuple(ids))
    if key == _last and now - _last_said < QUIET_REPEAT:
        return
    _last, _last_said = key, now
    if n > _peak:
        _peak = n
    me = ""
    try:
        me = api.player.name()
    except Exception:
        pass
    api.log("lootdiag: %d ground item(s) peak=%d me=%r pos=%s %s"
            % (n, _peak, me, api.player.position(), ids))
