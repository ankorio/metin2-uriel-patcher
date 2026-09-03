"""autoloot - pick up everything that dropped for you, without the keypress.

HOW IT WORKS
------------
The client already has the whole mechanism; nothing here re-implements it.
CPythonPlayer::PickCloseItemVector walks the ground-item list and, for each
item, keeps it only if BOTH of these hold:

    01230db4  cmp  w8, w25       ; w25 = 999999
    01230db8  b.hi -> skip       ; squared distance, so radius ~ 1000 units

    01230dbc  ldrb w10, [x9, #0x3d0]   ; the item's ownership string
    ...       compared byte for byte against our own GetNameString()

then sends one SendClickItemPacket per survivor. The ownership test is not a
parameter we control - PickCloseItemVector passes our own name - so this can
never take a drop that belongs to another player. It is the same call the
pickup key makes, just on a timer.

WHY THE INTERVAL MATTERS
------------------------
One packet goes out per item picked. A sweep standing in a pile of drops sends
a burst, so this runs on a timer rather than every pump. 0.5s is comfortably
faster than items can accumulate and nowhere near packet-spam territory.

FILTERING
---------
Superseded: this file used to say an item-type filter was unreachable, because
GetCloseItemVector's string argument is the OWNER name rather than the item
name. That was true of the BINDINGS and false of the engine. The stub now walks
CPythonItem's ground-item map directly, so each drop yields an id, a vnum, a
display name and an ownership string, and WANTED can name what to keep.

THREE MODES
-----------
  WANTED empty            batch pickup on the kill trigger        (default)
  WANTED filled           targeted pickup of listed items only
  LEGACY_MODE true        the original constant-interval sweep    (opt in)

The third overrides the other two - see the note on LEGACY_MODE. It is the
pre-2026-08-04 implementation, recovered verbatim and kept as its own code
path so it stays trustworthy as a fallback.
"""

import os
import random

CAPABILITIES = ["read", "loot", "move"]

# ---- configuration --------------------------------------------------------
# WHEN TO SWEEP - the important part.
#
# The old behaviour was pick_up_items() every 0.5s forever, which is a
# metronome: a fixed-period burst of SendClickItemPacket whether or not
# anything dropped. That is the shape of a macro, and it is noise the server
# does not need to see.
#
# Drops appear when something DIES, so that is the trigger. The auto-attack VID
# going non-zero -> zero is the client's own "engagement ended" edge, readable
# through the native field layer, and it is exactly when loot hits the ground.
# Everything else is a slow, jittered fallback for drops walked over.
TRIGGER_ON_KILL = True
KILL_DELAY = 0.6        # drops land a beat after the death animation starts
KILL_SWEEPS = 2         # a couple, spaced - not a burst
KILL_SPACING = 1.2

IDLE_INTERVAL = 8.0     # fallback when nothing died; 16x slower than before
IDLE_JITTER = 3.0       # +/- this, so the period is never a clean multiple

INTERVAL = 0.5          # legacy name, kept so old configs still parse

# ---- LEGACY MODE ----------------------------------------------------------
# The simple pickup loop: one fixed-interval timer, no kill trigger, no jitter,
# no allow-list. Based on the pre-2026-08-04 08:49 rollback snapshot, with one
# change - it no longer sweeps blindly. Each tick it enumerates the ground and:
#
#     floor empty  -> send nothing
#     N drops      -> a burst of one targeted click per item (N packets),
#                     then 5 blind mop-up sweeps
#
# _legacy_sweep() below is that loop, kept deliberately literal rather than
# folded into the new code path - the point of a legacy mode is that it behaves
# like the simple thing, so it should be readable as the simple thing.
#
# Turning it on OVERRIDES the kill trigger AND the WANTED allow-list. A
# fixed-period sweep is what "legacy" means here; quietly keeping the filter on
# would make it neither the simple behaviour nor the new one.
#
# The floor-empty gate is deliberate: the old version was a metronome of
# SendClickItemPacket every 0.5s whether or not anything had dropped, which
# grabs at bare ground. Empty pickups are the exact signal the v15 auto-hunt
# brake arms on (notePickupAttempt), and a perfectly constant period is a macro
# tell besides. Gating on drops-present keeps legacy off both. It is still the
# heavier mode - a full N-click burst per tick over any pile - so it stays off
# by default and logs a warning whenever it is armed.
LEGACY_MODE = False
LEGACY_INTERVAL = 0.5   # the original INTERVAL

# Back-compat: SPAM_PICKUP was this switch's first name. Old configs and the
# ones already deployed keep working rather than silently doing nothing.
SPAM_PICKUP = False
SPAM_INTERVAL = 0.5

TAKE_ITEMS = True
TAKE_MONEY = True

# WHAT TO PICK UP.
#
# WANTED empty  -> blind batch pickup, the client's own PickCloseItemVector.
# WANTED filled -> enumerate the ground, match on vnum, and send one
#                  SendItemPickUpPacket for the survivors only.
#
# Entries may be either:
#   an int    -> vnum, the item TYPE. Stable and language-independent.
#                Yang reads as vnum 1 here; a bow read as 2080.
#   a string  -> matched case-insensitively against the item's display NAME,
#                as a substring: "bow" matches "Red Eye Bow+0".
#                Convenient, but the name is LOCALISED - a client in another
#                language will not match. Prefer vnums for anything permanent.
#
# Money is deliberately NOT subject to the list: TAKE_MONEY keeps using the
# client's own PickCloseMoney, so "always pick yang" holds whatever the filter
# says. That also keeps the money path on the code the client already uses.
#
# Discovering vnums: look the item up in the client's locale item table
# (item_names.txt in the locale pack, one "vnum<TAB>name" per line). Prefer
# vnums here - names are localised.
WANTED = []
NAME_MIN_LEN = 4        # name fragments shorter than this must match a WHOLE
                        # WORD of the item name rather than any part of it.
                        # 'hay' as a substring matches a surprising number of
                        # unrelated items, and with FETCH_WANTED on every false
                        # positive is a wasted walk across the map.
IGNORED_LOGGED = True   # say once per vnum what is being left behind, so a
                        # wrong list is visible instead of silently lossy
SET_CLIENT_FLAG = True  # also turn on the client's own AUTO_PICK option, so
                        # the setting in the options window agrees with what
                        # is actually happening. It does not gate anything.
VERBOSE = False         # log every sweep - noisy, for debugging only

# ---- fetching drops that fell out of range ---------------------------------
# A mob killed at the far end of the pull drops its loot where IT died, which is
# routinely outside the client's ~1000-unit pickup radius. Blind pickup can
# never reach those and neither could the filtered sweep, because both only
# ever look inside that radius.
#
# So: when WANTED is set, look further out, and if something on the list is
# lying there, WALK TO IT. Only ever for listed items - a fetch costs seconds of
# travel, which is worth it for the thing you are farming and absurd for a
# potion.
#
# Coordinating with autohunt2 is the hard half. Both mods drive
# AutoMoveToPosition, and that REPLACES the route in flight rather than queueing
# it - two movers means neither arrives. The handshake is in _hold(): we publish
# a DEADLINE that autohunt2 honours by standing down.
FETCH_WANTED = True
FETCH_RANGE = 3000.0    # look this far for a listed drop. Deliberately local:
                        # at 6000 it spent whole minutes crossing the map and
                        # timing out, which reads as the character wandering off
                        # for no reason. 0 = no limit.
FETCH_ARRIVE = 700.0    # inside this, the ordinary pickup call reaches it
FETCH_TIMEOUT_S = 10.0  # base allowance, PLUS travel time - see _deadline().
                        # A flat timeout fails every distant fetch by
                        # construction: 12s is not enough to walk 5000 units,
                        # so it gave up, re-picked the same item and looped.
FETCH_SPEED = 250.0     # assumed walking speed, units/sec, for that allowance
FETCH_REPATH_S = 3.0    # re-check existence, and re-issue a stalled walk
FETCH_PROGRESS = 150.0  # movement below this over FETCH_REPATH_S = stalled
FETCH_COOLDOWN_S = 1.5  # between fetches, so a pile is taken one at a time
FETCH_RETRY_S = 45.0    # after giving up on an item, leave THAT ITEM alone this
                        # long. Without it the next scan picks the same drop
                        # straight back up and the whole failure repeats - which
                        # is exactly what the log showed it doing.
FETCH_HOLD_S = 3.0      # how far ahead the autohunt hold is kept. Short on
                        # purpose: it is refreshed every tick while we walk, so
                        # if this mod stops the hold lapses within seconds.

# Leave a fight to go and collect something?
#
# TRUE by default, and the reasoning is about drop lifetime rather than tidiness.
# Waiting for the fight to end sounds politer, but ground items despawn, and
# autohunt2 re-acquires within a quarter of a second of each kill - so "wait
# until we are not fighting" can mean waiting for the whole pull, by which point
# the drop this feature exists to save is gone.
#
# Set False if you would rather never break off a fight; the sweep will still
# take anything that dies within normal pickup range.
FETCH_WHILE_FIGHTING = True

_next = 0.0
_armed = False
_sweeps = 0
_last_vid = 0
_queue = []
_seen_ignored = set()

_fetch_id = 0           # ground-item id we are walking to, 0 when idle
_fetch_pos = None       # where it lies
_fetch_at = 0.0         # when the fetch started, for FETCH_TIMEOUT_S
_fetch_mark = 0.0       # last progress/existence check
_fetch_from = None      # where we were at that check
_fetch_next = 0.0       # no new fetch before this
_fetch_due = 0.0        # deadline for the fetch in progress
_fetch_bad = {}         # ground-item id -> when we may try it again
_fetches = 0


def on_load(api):
    global _next, _armed, _sweeps, _last_vid, _queue
    global _fetch_id, _fetch_pos, _fetch_from, _fetch_next, _fetch_due, _fetches
    _next, _armed, _sweeps = 0.0, False, 0
    _fetch_id, _fetch_pos, _fetch_from = 0, None, None
    _fetch_next, _fetch_due, _fetches = 0.0, 0.0, 0
    _fetch_bad.clear()
    # A reload mid-fetch would otherwise leave autohunt2 held down by a deadline
    # nothing is refreshing any more.
    _release()
    if api.VERSION < 13:
        api.log("autoloot: needs api v13 (have v%s) - pickup calls are missing"
                % api.VERSION)
        return
    _seen_ignored.clear()
    if _legacy_on():
        # Say it plainly and say it every load. Someone reading the log a week
        # from now needs to know the noisy mode was on without going to look at
        # the config, and needs to know the allow-list is not in effect.
        api.log("autoloot: LEGACY MODE ON - fixed-period pickup every %.1fs, "
                "no kill trigger%s. Fires only when drops are present, then a "
                "burst of one click per item + 5; empty floor sends nothing. "
                "Heavier than the default; off by default."
                % (_legacy_interval(), ", WANTED IGNORED" if WANTED else ""))
        api.log("autoloot: loaded (api v%s) mode=legacy items=%s money=%s"
                % (api.VERSION, TAKE_ITEMS, TAKE_MONEY))
        return
    if WANTED and api.VERSION < 15:
        api.log("autoloot: WANTED needs api v15 (have v%s) - filtering DISABLED, "
                "falling back to blind pickup" % api.VERSION)
    api.log("autoloot: loaded (api v%s) trigger=%s idle=%.0fs items=%s money=%s filter=%s"
            % (api.VERSION,
               "kill" if TRIGGER_ON_KILL else "timer",
               IDLE_INTERVAL, TAKE_ITEMS, TAKE_MONEY,
               ("%d entr%s" % (len(WANTED), "y" if len(WANTED) == 1 else "ies"))
               if WANTED else "off (take all)"))
    if FETCH_WANTED and WANTED:
        api.log("autoloot: will WALK to listed drops up to %s away (autohunt2 "
                "stands down while it does)"
                % ("any distance" if not FETCH_RANGE else "%.0f" % FETCH_RANGE))
    short = sorted(set(str(v).lower() for v in WANTED
                       if not isinstance(v, int) and len(str(v)) < NAME_MIN_LEN))
    if short:
        api.log("autoloot: %s %s under %d characters, so %s matched as whole "
                "words only - a bare substring that short picks up unrelated "
                "items, and each one would be a wasted walk"
                % (", ".join(repr(s) for s in short),
                   "is" if len(short) == 1 else "are", NAME_MIN_LEN,
                   "it is" if len(short) == 1 else "they are"))


def on_unload(api):
    # Release the hold explicitly. The deadline would lapse on its own within
    # FETCH_HOLD_S, but leaving autohunt2 standing still for no reason - with
    # the mod that asked for it already gone - is not a state worth shipping.
    _release()
    api.log("autoloot: unloaded after %d sweep(s), %d fetch(es)"
            % (_sweeps, _fetches))


def on_update(api, dt):
    global _next, _armed, _sweeps, _last_vid, _queue

    if api.VERSION < 13 or not api.in_game():
        _armed = False
        return

    # Do this once per session in the world, not per sweep: it writes a
    # setting, and the options window reads it.
    if SET_CLIENT_FLAG and not _armed:
        _armed = True
        was = api.auto_pickup()
        if was is False:
            api.auto_pickup(True)
            api.log("autoloot: client AUTO_PICK was off - turned on")

    now = api.now()

    # The escape hatch, checked before anything else so it genuinely overrides
    # the trigger and the filter rather than layering on top of them.
    if _legacy_on():
        _legacy_sweep(api, now)
        return

    # Walking to an out-of-range drop. Deliberately NOT exclusive with the
    # sweeps below: the fetch owns MOVEMENT, the sweeps own PICKUP, so anything
    # we happen to walk past on the way is collected for free.
    _fetch(api, now)

    # the kill edge: engaged -> not engaged
    fired = False
    if TRIGGER_ON_KILL:
        vid = _engaged(api)
        if _last_vid and not vid:
            _queue = [now + KILL_DELAY + i * KILL_SPACING
                      for i in range(KILL_SWEEPS)]
            if VERBOSE:
                api.log("autoloot: vid %d died - %d sweep(s) queued"
                        % (_last_vid, KILL_SWEEPS))
        _last_vid = vid
        while _queue and now >= _queue[0]:
            _queue.pop(0)
            _sweep(api, "kill")
            fired = True

    if not fired and now >= _next:
        _next = now + IDLE_INTERVAL + random.uniform(-IDLE_JITTER, IDLE_JITTER)
        _sweep(api, "idle")


def _legacy_on():
    """Either name turns it on. LEGACY_MODE is the one the UI checkbox binds."""
    return bool(LEGACY_MODE or SPAM_PICKUP)


def _legacy_interval():
    """LEGACY_INTERVAL wins; SPAM_INTERVAL honoured for configs that set it."""
    if not LEGACY_MODE and SPAM_PICKUP:
        return SPAM_INTERVAL
    return LEGACY_INTERVAL


def _legacy_sweep(api, now):
    """The simple pickup loop, fixed-interval, no kill trigger/jitter/allow-list.

    ITEMS are gated on floor drops: an empty floor sends nothing, N drops send a
    burst of one click per item + 5 blind mop-ups. That gate is the point -
    empty ITEM pickups are the signal the v15 auto-hunt brake arms on (the
    client counts failed pickup attempts), so legacy never grabs at bare ground.

    MONEY is INDEPENDENT of that gate. PickCloseMoney does not feed the brake
    (only item pickups do), and a money-only config (TAKE_ITEMS off) must keep
    collecting yang whether or not it shows up in the ground-item map - gating it
    on ground_items() would silently stop money pickup, which was the bug.

    Deliberately NOT routed through _sweep() - if this is the fallback people
    trust when the clever version misbehaves, it must not share code with it."""
    global _next, _sweeps
    if now < _next:
        return
    _next = now + _legacy_interval()

    swept = False
    n_items = 0

    # ITEMS: only when there are drops on the floor.
    if TAKE_ITEMS:
        try:
            items = api.ground_items()
        except Exception as e:
            api.pick_up_items()             # old blind fallback on an old stub
            swept = True
            if VERBOSE:
                api.log("autoloot: legacy item sweep blind fallback (%s: %s)"
                        % (type(e).__name__, e))
        else:
            if items:
                n_items = len(items)
                for it in items:            # N targeted clicks, one per drop
                    api.pick_up(it["id"])
                for _ in range(5):          # +5 blind mop-up for late-landing drops
                    api.pick_up_items()
                swept = True

    # MONEY: independent of the item gate (see docstring).
    if TAKE_MONEY:
        api.pick_up_money()
        swept = True

    if swept:
        _sweeps += 1
        if VERBOSE:
            parts = []
            if n_items:
                parts.append("%d item(s) + 5" % n_items)
            if TAKE_MONEY:
                parts.append("money")
            api.log("autoloot: sweep %d (legacy) - %s"
                    % (_sweeps, ", ".join(parts) or "nothing"))


def _engaged(api):
    """The auto-attack VID, or 0. Read through the native field layer - the
    client's own notion of "I am fighting this", not a guess from positions."""
    try:
        return int(api.natives.module().field("auto_attack_vid") or 0)
    except Exception:
        return 0


def _sweep(api, why):
    global _sweeps
    _sweeps += 1
    took = 0
    filtering = bool(WANTED) and api.VERSION >= 15 and not _legacy_on()
    if TAKE_ITEMS:
        if filtering:
            took = _sweep_filtered(api)
        else:
            api.pick_up_items()
    if TAKE_MONEY:
        api.pick_up_money()
    if VERBOSE:
        api.log("autoloot: sweep %d (%s)%s"
                % (_sweeps, why, " took %d" % took if filtering else ""))


def _sweep_filtered(api):
    """Pick only the vnums on the list, one targeted packet each.

    Note the packet count: blind batch sends one SendClickItemPacket per item
    within ~1000 units regardless of what it is. This sends strictly fewer -
    one per WANTED item - so filtering also reduces traffic rather than adding
    to it. Ownership is still enforced server-side; a pickup for someone else's
    drop is simply refused."""
    vnums = set(v for v in WANTED if isinstance(v, int))
    names = [str(v).lower() for v in WANTED if not isinstance(v, int)]
    try:
        # Defaults already apply the client's own radius and the ownership
        # test, so this asks the server only about drops it would accept.
        items = api.ground_items()
    except Exception as e:
        # Never fall back to blind pickup here: taking everything is the exact
        # opposite of what the list asked for, and silently doing so would be
        # worse than picking nothing up at all.
        api.log("autoloot: ground_items() failed (%s: %s) - skipping this sweep"
                % (type(e).__name__, e))
        return 0
    took = 0
    for it in items:
        if _matches(it, vnums, names):
            if api.pick_up(it["id"]):
                took += 1
        elif IGNORED_LOGGED and it["vnum"] not in _seen_ignored:
            _seen_ignored.add(it["vnum"])
            api.log("autoloot: leaving vnum %d %r (not on the list)"
                    % (it["vnum"], it["name"]))
    return took


# ---- the allow-list test, in ONE place -------------------------------------
# The sweep and the fetch MUST agree about what is wanted. Two copies of this
# rule would drift, and the failure would be silent and slow: walking across the
# map for something the sweep then refuses to pick up.

def _wanted_sets():
    """WANTED split into the vnum set and the lowercased name fragments."""
    return (set(v for v in WANTED if isinstance(v, int)),
            [str(v).lower() for v in WANTED if not isinstance(v, int)])


def _words(name):
    """Lowercased alphanumeric tokens of an item name.

    'Red Iron Blade+0' -> ['red', 'iron', 'blade', '0']"""
    out, cur = [], ""
    for ch in name:
        if ch.isalnum():
            cur += ch
        elif cur:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _matches(it, vnums, names):
    """Is this drop on the list?

    Short fragments match WHOLE WORDS ONLY, and that is not fussiness - it is a
    real bug that sends the character running across the map. A three-letter
    entry like 'hay' is a substring of plenty of unrelated item names, and with
    fetching enabled every false positive becomes a walk. Anything shorter than
    NAME_MIN_LEN therefore has to be a word of the name rather than merely
    inside it; longer fragments keep the convenient substring behaviour."""
    if it["vnum"] in vnums:
        return True
    raw = it["name"] or ""
    low = raw.lower()
    toks = None
    for nm in names:
        if len(nm) >= NAME_MIN_LEN:
            if nm in low:
                return True
        else:
            if toks is None:
                toks = _words(low)
            if nm in toks:
                return True
    return False


# ---- walking to a drop that fell out of range ------------------------------

def _filtering(api):
    """Is the allow-list actually in force? Fetching only makes sense if so."""
    return bool(WANTED) and api.VERSION >= 15 and not _legacy_on()


def _hold(api, seconds):
    """Ask autohunt2 to stand still while we collect something.

    Publishes a DEADLINE, not a flag. A boolean would freeze hunting for ever if
    this mod were disabled, reloaded or simply crashed mid-fetch, and that
    failure would be completely silent - the character would just stop hunting
    one day. A deadline expires on its own, so the worst case is a few wasted
    seconds.

    os.environ is the established in-process channel in this codebase:
    api.attack() already signals the stub through TRIARCH_ATTACK the same way."""
    try:
        os.environ["TRIARCH_LOOT_BUSY"] = "%.3f" % (api.now() + seconds)
    except Exception:
        pass


def _release():
    try:
        os.environ["TRIARCH_LOOT_BUSY"] = "0"
    except Exception:
        pass


def _should_wait(api):
    """Hold off starting a fetch?

    Deliberately NOT gated on "is the character already walking". autohunt2 is
    walking to a target almost continuously, so that test starves fetching
    completely - which is exactly the bug it looks like it prevents. The hold in
    _hold() IS the coordination: autohunt2 stands down the moment we publish it,
    so there is never a second mover to collide with.

    The only real question is whether to abandon a fight in progress, and that
    is FETCH_WHILE_FIGHTING."""
    if FETCH_WHILE_FIGHTING:
        return False
    return _engaged(api) != 0


def _far_wanted(api):
    """The nearest wanted drop lying OUTSIDE normal pickup range, or None."""
    vnums, names = _wanted_sets()
    try:
        # radius=0 means "no distance filter"; ownership still applies, so this
        # can only ever return drops the server would let us take.
        items = api.ground_items(radius=FETCH_RANGE or 0)
    except Exception as e:
        api.log("autoloot: fetch scan failed (%s: %s)" % (type(e).__name__, e))
        return None
    now = api.now()
    for it in items:                    # ground_items() sorts nearest-first
        d = it["dist"]
        if d is None or d <= api.PICKUP_RADIUS:
            continue                    # the ordinary sweep already covers it
        if _fetch_bad.get(it["id"], 0) > now:
            continue                    # tried and failed recently; leave it
        if _matches(it, vnums, names):
            return it
    return None


def _disengage(api):
    """Stop the client walking to its own target.

    THE reason distant fetches used to fail. __Update_AutoAttack sustains the
    swing and walks the character toward its target every frame, natively - so
    while an engagement is live, the engine is a second mover pulling against
    our waypoint. The log showed a fetch that started 2354 units out and ended
    5252 units away, having been dragged the other way the whole time.

    Holding autohunt2 off is not enough, because this mover is the ENGINE, not a
    mod. The engagement itself has to go."""
    try:
        api.natives.module().drop_engagement()
        return True
    except Exception:
        return False


def _deadline(dist):
    """How long this particular fetch is allowed to take.

    Scaled by distance, because a flat timeout fails every distant fetch by
    construction - 12s cannot cover 5000 units at any walking speed, so it gave
    up, re-scanned, picked the same drop and looped."""
    return FETCH_TIMEOUT_S + (dist / FETCH_SPEED if FETCH_SPEED > 0 else 0.0)


def _end_fetch(api, now, why=None, blacklist=False):
    global _fetch_id, _fetch_pos, _fetch_from, _fetch_next
    if why:
        api.log("autoloot: %s" % why)
    if blacklist and _fetch_id:
        _fetch_bad[_fetch_id] = now + FETCH_RETRY_S
    _fetch_id, _fetch_pos, _fetch_from = 0, None, None
    _fetch_next = now + FETCH_COOLDOWN_S
    _release()


def _fetch(api, now):
    """Walk to an out-of-range wanted drop. True while we own the character."""
    global _fetch_id, _fetch_pos, _fetch_at, _fetch_mark, _fetch_from
    global _fetch_next, _fetch_due, _fetches

    if not (FETCH_WANTED and _filtering(api)):
        return False
    here = api.player.position()
    if here is None:
        return False

    # ---- already going somewhere ----
    if _fetch_id:
        dx, dy = _fetch_pos[0] - here[0], _fetch_pos[1] - here[1]
        d = (dx * dx + dy * dy) ** 0.5

        if d <= FETCH_ARRIVE:
            got = api.pick_up(_fetch_id)
            _sweep(api, "fetch")        # and anything else now within reach
            _end_fetch(api, now,
                       "reached the drop at %.0f - %s"
                       % (d, "collected" if got else "pick_up refused it"))
            return True

        if now > _fetch_due:
            _end_fetch(api, now, "gave up on the drop after %.0fs (%.0f away)"
                       % (now - _fetch_at, d), blacklist=True)
            return False

        if now - _fetch_mark >= FETCH_REPATH_S:
            moved = 0.0
            if _fetch_from is not None:
                mx, my = here[0] - _fetch_from[0], here[1] - _fetch_from[1]
                moved = (mx * mx + my * my) ** 0.5
            _fetch_from, _fetch_mark = (here[0], here[1]), now
            # Somebody else may have taken it, or it may have despawned.
            # Checked on this cadence rather than every tick: it is a native
            # walk over the whole ground map.
            try:
                alive = any(i["id"] == _fetch_id
                            for i in api.ground_items(radius=0))
            except Exception:
                alive = True            # cannot tell: keep going
            if not alive:
                _end_fetch(api, now, "the drop is gone - back to hunting")
                return False
            if moved < FETCH_PROGRESS:
                # Almost always the engine walking us back to its target, so
                # clear that before re-issuing or the next 3s go the same way.
                api.log("autoloot: fetch stalled (%.0f in %.0fs) - dropping the "
                        "engagement and re-issuing" % (moved, FETCH_REPATH_S))
                _disengage(api)
                api.move_to(_fetch_pos[0], _fetch_pos[1])

        # Keep the engine off the wheel for the whole journey, not just at the
        # start: autohunt2 is held, but a mob that aggros us mid-walk engages
        # through the client's own path and starts pulling again.
        if _engaged(api):
            _disengage(api)
        _hold(api, FETCH_HOLD_S)        # refreshed every tick while we walk
        return True

    # ---- idle: is anything worth walking for? ----
    if now < _fetch_next or _should_wait(api):
        return False
    it = _far_wanted(api)
    if it is None:
        return False
    if not api.move_to(it["pos"][0], it["pos"][1]):
        _fetch_next = now + FETCH_COOLDOWN_S
        api.log("autoloot: cannot fetch - move_to unavailable")
        return False
    _fetch_id, _fetch_pos = it["id"], it["pos"]
    _fetch_at, _fetch_mark = now, now
    _fetch_due = now + _deadline(it["dist"])
    _fetch_from = (here[0], here[1])
    _fetches += 1
    _disengage(api)                     # before anything else - see _disengage
    _hold(api, FETCH_HOLD_S)
    api.log("autoloot: fetching vnum %d %r at %.0f units - walking to it (%.0fs)"
            % (it["vnum"], it["name"], it["dist"], _deadline(it["dist"])))
    return True
