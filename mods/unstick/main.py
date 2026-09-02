"""unstick - break the auto-move deadlock when the client wedges on geometry
or on a mob.

WHY IT HAPPENS
--------------
AutoPathToDestPosition is not a pathfinder. It is four calls:

    NEW_GetPixelPosition -> __AutoPathSegment(x, y)
                         -> NEW_MoveToDestPixelPositionDirection -> NEW_Stop

It steps straight at the destination and lets collision stop it. There is no
navmesh and no obstacle awareness, so a concave corner is a trap: it pushes
into the wall, slides, re-aims at the same destination, and pushes into the
same wall again. A mob standing in the doorway does exactly the same thing,
because actors block movement too.

WHAT THIS DOES
--------------
Detects the deadlock and walks a short leg PERPENDICULAR to the blocked
heading - plain wall-following - then lets whatever was driving the move
re-issue its own destination. autohunt re-targets every loop and follow
re-issues every tick, so nothing has to be remembered or restored.

Collision stays on. This uses AutoMoveToPosition, the client's own mover, so
the server sees ordinary movement.

ARMING
------
The hard part is telling "wedged" from "arrived", because both look like a
position that stopped changing. The discriminator is the target: if you have
one, it is further away than you could possibly be attacking from, and you are
not moving, then you are stuck. Standing still with nothing selected, or next
to what you are hitting, is not.
"""

CAPABILITIES = ["read", "move"]

# ---- configuration --------------------------------------------------------
CHECK_EVERY = 0.5       # seconds between position samples
STUCK_S = 2.5           # no progress for this long while wanting to move
MIN_PROGRESS = 60.0     # units in STUCK_S that counts as "still moving"
NEAR_DIST = 400.0       # closer than this to the target: we have arrived
SIDESTEP = 1100.0       # how far to walk perpendicular
SETTLE_S = 2.5          # let the sidestep run before judging again
MAX_TRIES = 3           # then give up and stop fighting it
COOLDOWN_S = 15.0       # ...for this long
VERBOSE = True

# ---- state ----------------------------------------------------------------
_next_check = 0.0
_anchor = None          # position at the start of the current stall window
_anchor_t = 0.0
_tries = 0
_side = 1               # alternate perpendicular directions between attempts
_busy_until = 0.0
_cooldown_until = 0.0
_target = None


def _dist(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (dx * dx + dy * dy) ** 0.5


def _reset(why=None, api=None):
    global _anchor, _anchor_t, _tries
    _anchor, _anchor_t, _tries = None, 0.0, 0
    if why and api and VERBOSE:
        api.log("unstick: %s" % why)


def on_load(api):
    global _next_check, _busy_until, _cooldown_until, _target, _side
    _next_check = _busy_until = _cooldown_until = 0.0
    _target, _side = None, 1
    _reset()
    api.log("unstick: loaded (api v%s) - %.1fs stall, %.0f unit sidestep, %d tries"
            % (api.VERSION, STUCK_S, SIDESTEP, MAX_TRIES))


def on_unload(api):
    api.log("unstick: unloaded")


def on_update(api, dt):
    global _next_check, _anchor, _anchor_t, _tries, _side
    global _busy_until, _cooldown_until, _target

    if not api.in_game():
        return

    now = api.now()
    if now < _next_check:
        return
    _next_check = now + CHECK_EVERY

    me = api.player.position()
    if me is None:
        return

    # A new target means a new journey: whatever we learned about the old one
    # no longer applies.
    tv = api.target.vid() if api.target.exists() else None
    if tv != _target:
        _target = tv
        _reset()
        _cooldown_until = 0.0

    if tv is None:
        _reset()
        return

    tp = api.target.position()
    if tp is None:
        _reset()
        return

    if _dist(me, tp) <= NEAR_DIST:
        _reset()                            # arrived; nothing to fix
        return

    if now < _busy_until or now < _cooldown_until:
        return

    if _anchor is None:
        _anchor, _anchor_t = me, now
        return

    if _dist(_anchor, me) >= MIN_PROGRESS:
        _anchor, _anchor_t = me, now        # still making headway
        return

    if now - _anchor_t < STUCK_S:
        return

    # Wedged: we want to be somewhere, and we have not moved.
    if _tries >= MAX_TRIES:
        api.log("unstick: gave up after %d tries, backing off %.0fs "
                "(target %r at %.0f units)"
                % (MAX_TRIES, COOLDOWN_S, api.target.name() or "?",
                   _dist(me, tp)))
        _cooldown_until = now + COOLDOWN_S
        _reset()
        return

    dx, dy = tp[0] - me[0], tp[1] - me[1]
    n = (dx * dx + dy * dy) ** 0.5 or 1.0
    # Perpendicular to the blocked heading, alternating sides so a failed
    # attempt tries the other way round the obstacle.
    px = me[0] + (-dy / n) * SIDESTEP * _side
    py = me[1] + (dx / n) * SIDESTEP * _side

    _tries += 1
    _side = -_side
    if api.move_to(px, py):
        if VERBOSE:
            api.log("unstick: stuck %.1fs at (%.0f,%.0f), %s sidestep #%d "
                    "-> (%.0f,%.0f)"
                    % (now - _anchor_t, me[0], me[1],
                       "left" if _side < 0 else "right", _tries, px, py))
        _busy_until = now + SETTLE_S
    else:
        api.log("unstick: move_to failed - is the 'move' capability granted?")
        _cooldown_until = now + COOLDOWN_S

    # Re-anchor from nothing rather than from here-and-now. Anchoring at `now`
    # means the stall window is already satisfied the instant SETTLE_S expires,
    # so the next tick fires a second sidestep without ever having watched the
    # first one - which burned two of MAX_TRIES per real stall.
    _anchor, _anchor_t = None, 0.0
