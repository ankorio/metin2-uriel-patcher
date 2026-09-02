"""nocollide - walk through monsters. Terrain still stops you.

WHAT IT SOLVES
--------------
Walk-to-waypoint could round a corner but not get past a mob. Monsters do not
STOP the character - they DISPLACE it. Every frame the engine tests each nearby
actor and, on a hit, pushes your position back out of it, which is why the
character walks on the spot rather than halting, and why sidestepping traps you
in the middle of a pack instead of escaping it.

That distinction is why two earlier attempts failed:

  * Marking mobs with the skip-collision sentinel did nothing. The flag means
    "I ignore collisions", is read off the MOVER, and is only consulted for the
    main instance. 90 actors marked, zero change.
  * Setting it on ourselves worked - and took TERRAIN with it, because
    CheckAdvancing does actors and the map attribute in one function. Walking
    where the map forbids is self-reporting, so that was never shippable.
  * NOP-ing BlockMovement fixed wall autopathing and did nothing for mobs,
    because BlockMovement is the *stop* - the thing terrain uses. Mobs never
    call it until the retry loop has already given up.

HOW IT WORKS
------------
The engine ships this feature. The actor-collision test is a RACE WHITELIST
with two hardcoded exempt ranges (mount vnums on the build it was found on).
Widening those ranges is the same mechanism mounts already use: immediates of
DATA inside an otherwise untouched function, not a rewritten prologue. Having
TWO of them is what lets a band be carved out of the middle, so metin stones
keep their collision exactly rather than approximately - see SOLID_RACES.

WHEN IT IS ACTIVE
-----------------
By default, only while walking to a waypoint (ONLY_ON_WAYPOINT). Body-blocking
only ever mattered while travelling, and gating on that is far more predictable
than trying to list every situation where pass-through would be unwanted.

Verified live: the displacement stops being applied ENTIRELY (the adjuster went
from 23 calls per 2s window to 0), the collision-loop BlockMovement disappears
with it, and the movement state machine keeps ticking normally.

TERRAIN IS NOT AFFECTED. Terrain is a different predicate with its own callers
and is not reachable from the range this touches. That separation is the whole
point - it is what the skip-collision sentinel could not give us.

WHAT IT DOES NOT DO
-------------------
Nothing is transmitted. There is no collision bit in any packet; the client
reports its own x/y about 2.5 times a second regardless, and walking through a
monster is not an illegal position the way walking through scenery is.

The patch lives in memory only. If the client dies, it dies with it - there is
no on-disk change to leave behind.

THE TERRAIN SWITCH IS DIFFERENT AND IS NOT SAFE
-----------------------------------------------
NO_TERRAIN is a second, separate switch that neuters BlockMovement - the actual
*stop*, which is what terrain uses. It is deliberately not part of the main
toggle, because the risk profile is not remotely the same:

    DISABLING TERRAIN COLLISION MAY LEAD YOU TO ILLEGAL ZONES!!!!

Walking through a monster is invisible to the server. Walking through a WALL is
not: the client reports its own x/y about 2.5 times a second, so standing where
the map forbids is self-reporting even though no packet carries a "collision
off" bit. Walking manually into illegal terrain has already raised an in-client
error dialog - evidence of a legality check we have not fully mapped. Use it for
a stuck character, not as a permanent setting.
"""

CAPABILITIES = ["read", "move"]

# There is deliberately no ENABLED flag here. modhost consumes `enabled` from
# config.json to decide whether to load a mod at all and does NOT pass it
# through, so a module-level ENABLED would sit at its default forever - which is
# exactly what happened the first time: the mod loaded, the checkbox was ticked,
# and nothing was ever applied. Being loaded IS being enabled; on_unload is the
# off switch.
NO_TERRAIN = False       # walls too - NOT safe, read the warning above

# Metin stones keep their collision, always. This is EXACT, not a proximity
# guess: the engine's test has two hardcoded exempt ranges, so the band is
# carved out of the middle - range 1 covers everything below it, range 2
# everything above. Stones sit at 8005 on this server, a band of their own well
# clear of mobs (401-599) and of the mount (20101).
#
# Measured, not assumed: 'Metin Negro' reports IsStone(vid)=1,
# GetInstanceType(vid)=2 (CHAR_TYPE STONE) and race 8005. The mod re-checks that
# at runtime and complains if a stone ever shows up outside this band, because a
# silently wrong band would just quietly make stones walkable again.
SOLID_RACES = [8000, 8999]      # [] or None to let stones become passable too

# Pass through actors ONLY while travelling to a waypoint. Everything else -
# standing, fighting, walking by hand - keeps normal collision.
#
# This replaced a SOLID_WHILE_ATTACKING flag, and the reason is worth keeping:
# "solid while attacking" tries to enumerate the situations where you do NOT
# want pass-through, and that list is never finished. This states the one
# situation where you DO want it. Body-blocking only ever mattered while
# walking somewhere.
#
# The signal is the client's own flag, set by CPythonPlayer::AutoMoveToPosition
# when a route is accepted and cleared on arrival, on cancel and when no path
# can be built. Every route goes through that function, so this covers a mod
# calling api.move_to AND the player clicking the atlas, with no bookkeeping
# and nothing for a mod to announce.
ONLY_ON_WAYPOINT = True

VERBOSE = True

# Diagnostics for "is the waypoint gate actually working right now?".
#
# DEBUG prints one line every DEBUG_S with the raw engine state - BOTH flags,
# the destination, the position, and the collision state set from them. Logging
# both flags separately is the point: they disagree, and which one is set tells
# you what happened.
#
#   active=1 enabled=1 -> a route is live
#   active=1 enabled=0 -> CANCELLED (moved by hand, attacked, cast). ACTIVE is
#                         stale and stays set forever; only ENABLED tells you.
#   active=0           -> ARRIVED. The updater cleared it.
DEBUG = True
DEBUG_S = 3.0

# There are deliberately NO timeout, arrival-radius or "has it moved lately"
# knobs here any more. They existed to paper over reading only ACTIVE, which
# misses every cancellation; reading ACTIVE and ENABLED answers the question
# outright. If this ever needs a timer again, the flag pair is wrong - find the
# right state, do not add a fudge factor.

_actors = None           # what we last told the engine (None = unknown)
_terrain = False
_failed = False          # a hard failure disarms us for the session
_warned_race = []        # stone races already reported as outside SOLID_RACES
_warned = []             # one-shot warning keys, so a 4Hz loop logs once
_dbg_next = 0.0          # next debug line due


def on_load(api):
    global _actors, _terrain, _failed, _warned_race, _warned
    global _dbg_next
    # _actors starts UNKNOWN, not False. The patch is global and survives a
    # reload, so assuming "off" would skip the first write and leave the engine
    # in whatever state the previous instance left behind.
    _actors, _terrain, _failed = None, False, False
    _warned_race, _warned = [], []
    _dbg_next = 0.0
    if api.VERSION < 18:
        api.log("nocollide: needs api v18 (have v%s) - not loading" % api.VERSION)
        _failed = True
        return
    api.log("nocollide: ready - %s%s, terrain untouched"
            % ("passable ONLY while walking to a waypoint" if ONLY_ON_WAYPOINT
               else "passable at all times",
               ", races %s always solid" % (list(_band()),) if _band() else ""))


def _warn_once(api, key, msg):
    """Log `msg` the first time only. A per-tick warning at 4Hz buries the log
    and says nothing the first line did not."""
    if key not in _warned:
        _warned.append(key)
        api.log(msg)


def _f(api, name, default=None):
    try:
        return api.field(name)
    except Exception:
        return default


def _diag(api, now, moving):
    """One line of raw engine state. Reporting only - the decision to treat a
    stale flag as 'not travelling' lives in _travelling, so that the log and the
    behaviour can never disagree about what happened."""
    global _dbg_next
    if not DEBUG or now < _dbg_next:
        return
    _dbg_next = now + DEBUG_S

    pos = None
    try:
        pos = api.player.position()
    except Exception:
        pass
    dest = _f(api, "auto_move_dest")
    d = ""
    if dest and pos is not None:
        try:
            dx, dy = dest[0] - pos[0], dest[1] - pos[1]
            d = " dist=%.0f" % ((dx * dx + dy * dy) ** 0.5)
        except Exception:
            pass
    # player_state (+0x58) is in the log because AutoMoveToPosition itself
    # branches on it (`cmp [player+0x58], 0x8A`), which makes it the leading
    # candidate for what changes when a route is cancelled by hand - the one
    # transition none of the flags has been shown to model.
    api.log("nocollide dbg: active=%s state=%s -> travelling=%s | dest=%s "
            "pos=%s%s collision=%s atk=%s"
            % (_f(api, "auto_move_active"), _f(api, "player_state"),
               moving, dest,
               ("(%.0f, %.0f)" % (pos[0], pos[1])) if pos else None, d,
               "PASSABLE" if _actors else "solid",
               _f(api, "auto_attack_vid")))


def _band():
    """SOLID_RACES as a validated (lo, hi), or None."""
    try:
        if not SOLID_RACES:
            return None
        lo, hi = int(SOLID_RACES[0]), int(SOLID_RACES[1])
        if lo <= 0 or hi < lo:
            return None
        return (lo, hi)
    except Exception:
        return None


# The engine's auto-move state. MEASURED live, not read off a header:
#
#   state=138 (0x8A) while a waypoint route is actually walking
#   state=133 (0x85) the instant it is cancelled, and from then on
#
# and CPythonPlayer::AutoMoveToPosition itself branches on exactly this value
# (`cmp dword [player+0x58], 0x8A`), so it is the engine's own notion of "I am
# auto-moving" rather than a number that happened to look useful.
#
# This default is only a fallback. mkoffsets resolves the constant out of that
# comparison as kPlayerWalkState, and the patcher STAMPS it into config.json on
# every install - so a game update that renumbers the states updates this
# automatically. It is a fact about the build, not a preference, which is why it
# is the one setting the patcher overwrites in an existing config.
WALK_STATE = 0x8A


def _travelling(api):
    """Is a walk-to-waypoint in progress? Both conditions, both measured.

    A route must be accepted (auto_move_active) AND the player must actually be
    in the auto-move state (player_state == WALK_STATE).

    Why both, from live capture of a cancel:

        active=1 state=138 walking
        active=1 state=133 CANCELLED - and active stays 1 for ever after

    auto_move_active is never cleared on cancellation. Only the state moves. So
    active alone gets completion right and cancellation wrong, which is exactly
    the reported bug; the state alone would be true for any movement; together
    they are "a route exists and we are following it".

    Three earlier readings were wrong, recorded so they are not retried:

      * +0x4FFE0 is not an "enabled" flag - it reads 0 for the whole of a normal
        walk, because its store sits behind this same 0x8A branch.
      * The path vector never empties; waypoints stays at its last value.
      * No timeout, arrival radius or movement heuristic is needed. Every one of
        those was compensating for watching the wrong field.

    Returns None when a field cannot be read, which must NOT be confused with
    False: reporting False there would strand collisions on and look exactly
    like the mod being off, so the caller holds its previous state.
    """
    try:
        active = api.field("auto_move_active")
        state = api.field("player_state")
    except Exception:
        return None
    if active is None or state is None:
        return None
    return bool(active) and int(state) == WALK_STATE


# Only the states actually OBSERVED are named. Everything else is reported as a
# raw value rather than guessed at: a wrong label in a diagnostic is worse than
# no label, because it stops people looking.
_STATE_NAMES = {
    0x85: "idle",           # measured: the instant a route is cancelled by hand
    0x8A: "auto-moving",    # measured: while a waypoint route walks
    0x89: "charge skill",   # from the __OnPressActor research, not re-measured
}


def _state_name(st):
    try:
        st = int(st)
    except Exception:
        return "state %r" % (st,)
    nm = _STATE_NAMES.get(st)
    return ("state 0x%02X (%s)" % (st, nm)) if nm else ("state 0x%02X (unknown)" % st)


def _why_stopped(api):
    """Name the reason, since the two flags distinguish it for free.

    "arrived" and "cancelled" look identical from the outside and were the
    whole confusion here, so the log says which one it was rather than leaving
    it to be inferred."""
    a, st = _f(api, "auto_move_active"), _f(api, "player_state")
    if a and st is not None and int(st) != WALK_STATE:
        return "route interrupted - %s" % _state_name(st)
    if not a:
        return "arrived"
    return "not travelling"


def _check_stone_band(api):
    """Warn if a metin turns up outside SOLID_RACES.

    The band is the whole basis for stones keeping collision. If this server
    puts one somewhere unexpected, the failure is silent - the stone simply
    becomes walkable - so it is checked against the client's own IsStone rather
    than trusted. Only the currently targeted actor is tested: one binding call
    when the target changes, not a sweep every tick."""
    band = _band()
    if not band:
        return
    try:
        vid = api.target.vid()
        if not vid:
            return
        if not api.raw.pack_chr.IsStone(vid):
            return
        for a in api.actors(radius=0):
            if a["vid"] != vid:
                continue
            race = a["race"]
            if race is None or band[0] <= race <= band[1]:
                return
            if race in _warned_race:
                return
            _warned_race.append(race)
            api.log("nocollide: !! metin %r has race %s, OUTSIDE SOLID_RACES %s "
                    "- that stone is passable. Widen the band."
                    % (a["name"], race, list(band)))
            return
    except Exception:
        pass                    # a diagnostic must never break the mod


def on_unload(api):
    _apply(api, False, False, "unloaded")


def _set(api, fn, want, label):
    """One switch. Never silent on failure: a mod that reports nothing and
    leaves collision off is the one bug that is invisible until the character is
    somewhere it should not be."""
    global _failed
    try:
        fn(want)
    except Exception as e:
        _failed = True
        api.log("nocollide: %s(%s) failed %s: %s - disarming"
                % (label, want, type(e).__name__, e))
        return None
    return want


def _apply(api, want_actors, want_terrain, why):
    global _actors, _terrain, _failed

    if want_actors != _actors:
        band = _band() if want_actors else None
        try:
            api.actor_pass(want_actors, band)
        except Exception as e:
            _failed = True
            api.log("nocollide: actor_pass(%s, %s) failed %s: %s - disarming"
                    % (want_actors, band, type(e).__name__, e))
            return
        _actors = want_actors
        if VERBOSE or not want_actors:
            api.log("nocollide: actors are now %s (%s)%s"
                    % ("PASSABLE" if want_actors else "solid again", why,
                       " - races %s stay solid" % (list(band),) if band else ""))

    if want_terrain != _terrain:
        got = _set(api, api.terrain_pass, want_terrain, "terrain_pass")
        if got is None:
            return
        _terrain = got
        # Always logged, never gated on VERBOSE. If this is on, it belongs in
        # the log regardless of how quiet the user asked the mod to be.
        api.log("nocollide: !! TERRAIN is now %s (%s)%s"
                % ("PASSABLE" if want_terrain else "solid again", why,
                   " - DISABLING TERRAIN COLLISION MAY LEAD YOU TO ILLEGAL ZONES!!!!"
                   if want_terrain else ""))


def on_update(api, dt):
    if _failed:
        return

    # Both patches are global, not per-actor, so there is nothing to re-assert
    # on warp, respawn or mount change - unlike the sentinel version, which went
    # quietly inert whenever the actor was recreated. Act only on a change.
    if not api.in_game():
        # Leaving the world does not undo a code patch, so these must still be
        # cleared rather than merely forgotten.
        _apply(api, False, False, "left the world")
        return

    _check_stone_band(api)

    # Loaded means enabled. Terrain remains its own opt-in on top of that, and
    # on_unload clears both, so unticking the mod cannot leave walls off behind
    # it.
    #
    if not ONLY_ON_WAYPOINT:
        _apply(api, True, bool(NO_TERRAIN), "config")
        return

    moving = _travelling(api)
    if moving is None:
        # Could not read the flag. Hold whatever we already had rather than
        # guessing: guessing False strands the character solid and looks like
        # the mod is off, guessing True leaves it passable while standing still.
        # Say what actually fixes it. The stub reads uriel_natives.ini once, at
        # DLL load, so regenerating the ini under a RUNNING client changes
        # nothing - the field table is already in memory. The first version of
        # this message said "regenerate the ini", which had just been done, and
        # would have sent the reader in a circle.
        _warn_once(api, "nofield",
                   "nocollide: auto_move_active is not in this client's field "
                   "table - holding the current collision state. The ini is "
                   "read once at DLL load, so RESTART THE CLIENT to pick it up "
                   "(regenerating the ini alone will not).")
        return
    _diag(api, api.now(), moving)
    _apply(api, moving, bool(NO_TERRAIN),
           "walking to waypoint" if moving else _why_stopped(api))
