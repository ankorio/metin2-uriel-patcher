"""keeproute - resume a walk that combat interrupted. Nothing else.

THE PROBLEM
-----------
Walking to a waypoint and getting jumped by a pack ends the walk. The client
cancels the route to retaliate, and it never resumes it: you stop where you were
hit. With nocollide that is doubly annoying, because collision correctly comes
back the moment you stop travelling, so the pack that interrupted you is now
also blocking you.

Measured, not assumed - BlockMovement caller trace over 65 sample windows while
autopathing into packs:

    stops while state=0x8A (auto-moving) : 0
    stops from 0x195865 (mob collision)  : 45
    stops from any terrain site          : 0

Not once blocked while actually travelling. Every stop happened AFTER the client
had already cancelled the route to fight. So nothing was misfiring - the walk
simply ends, and this puts it back.

THE ONE HARD PART
-----------------
Telling "combat interrupted my walk, resume it" apart from "I cancelled this
walk on purpose, leave me alone". Resuming a deliberate cancel would drag the
character back to a waypoint every time the player took manual control, which is
far worse than the problem being solved.

The discriminator is the state AT THE MOMENT the route died:

    route ended while auto_attack_vid != 0   -> combat took it   -> RESUME
    route ended with no attack target        -> the player did it -> LEAVE IT

That is read off the engine, not timed or guessed. A deliberate cancel while
also fighting is the one ambiguous case, and it resolves as "resume" - which is
why RESUME_LIMIT exists rather than resuming for ever.

WHAT IT DOES NOT DO
-------------------
It never invents a destination. It re-sends the one the engine was already
walking to, which is why it cannot send the character anywhere the player did
not ask to go.
"""

CAPABILITIES = ["read", "move"]

# The engine's auto-move state. Stamped per build by the patcher from
# kPlayerWalkState; see nocollide for the full derivation.
WALK_STATE = 0x8A

# Re-issue no more often than this. A pack that re-engages every tick would
# otherwise restart the path continuously, and a route that is constantly
# restarted covers no ground at all.
RESUME_EVERY = 1.5

# Give up after this many resumes for one destination. Not a timeout on the
# journey - a bound on thrashing. If a fight genuinely cannot be walked away
# from, continuing to re-issue forever just spams the pathfinder.
RESUME_LIMIT = 12

# How close counts as arrived, so a resume is not issued for the last few units.
ARRIVE_DIST = 300.0

VERBOSE = True

_dest = None             # destination of the route we are protecting
_armed = False           # the route died to COMBAT, so it may be resumed
_next = 0.0
_count = 0
_failed = False


def on_load(api):
    global _dest, _armed, _next, _count, _failed
    _dest, _armed, _next, _count, _failed = None, False, 0.0, 0, False
    if api.VERSION < 18:
        api.log("keeproute: needs api v18 (have v%s) - not loading" % api.VERSION)
        _failed = True
        return
    api.log("keeproute: ready - resumes walks that COMBAT interrupted, "
            "never ones you cancelled yourself")


def _f(api, name, default=None):
    try:
        v = api.field(name)
        return default if v is None else v
    except Exception:
        return default


def on_update(api, dt):
    global _dest, _armed, _next, _count

    if _failed or not api.in_game():
        return

    active = _f(api, "auto_move_active")
    state = _f(api, "player_state")
    if active is None or state is None:
        return

    travelling = bool(active) and int(state) == WALK_STATE

    if travelling:
        # Remember where this route is going, and reset the thrash counter when
        # the destination changes - a new journey gets a fresh budget.
        d = _f(api, "auto_move_dest")
        if d:
            d = (float(d[0]), float(d[1]))
            if _dest is None or abs(d[0] - _dest[0]) > 1 or abs(d[1] - _dest[1]) > 1:
                _dest, _count = d, 0
        _armed = False
        return

    # Not travelling. Did the route just die, and did combat do it?
    if not active:
        _dest, _armed = None, False       # arrived, or no route at all
        return
    if _dest is None:
        return

    if not _armed:
        # This is the decision, and it is made ONCE, at the moment the route
        # ends - not re-evaluated later when the fight may already be over.
        if _f(api, "auto_attack_vid"):
            _armed = True
            if VERBOSE:
                api.log("keeproute: combat interrupted the walk to (%.0f, %.0f) "
                        "- will resume" % _dest)
        else:
            _dest = None                  # the player cancelled it; respect that
        return

    pos = api.player.position()
    if pos is not None:
        dx, dy = _dest[0] - pos[0], _dest[1] - pos[1]
        if (dx * dx + dy * dy) ** 0.5 <= ARRIVE_DIST:
            if VERBOSE:
                api.log("keeproute: already at the destination - done")
            _dest, _armed = None, False
            return

    now = api.now()
    if now < _next:
        return
    if _count >= RESUME_LIMIT:
        api.log("keeproute: gave up after %d resumes to (%.0f, %.0f) - the fight "
                "is not walkable" % (_count, _dest[0], _dest[1]))
        _dest, _armed = None, False
        return

    _next = now + RESUME_EVERY
    _count += 1
    try:
        api.move_to(_dest[0], _dest[1])
    except Exception as e:
        api.log("keeproute: move_to failed %s: %s - disarming"
                % (type(e).__name__, e))
        _dest, _armed = None, False
        return
    if VERBOSE:
        api.log("keeproute: resumed walk to (%.0f, %.0f) [%d/%d]"
                % (_dest[0], _dest[1], _count, RESUME_LIMIT))
