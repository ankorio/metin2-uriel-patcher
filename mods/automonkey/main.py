"""automonkey - run the monkey dungeon loop, for ever, unattended.

THE CYCLE
---------
The dungeon throws you out to Pyungmoo after a while. So this is not a sequence
with a beginning and an end - it is a rule about where to stand, applied to
whatever map the character happens to be on:

    Pyungmoo  (metin2_map_c1)          -> walk to 137,126
    Bakra     (metin2_map_c3)          -> walk to 535,41
    monkeydungeon                      -> walk to the hunting spot, use the
                                          skill on key 2, start the GAME'S OWN
                                          autohunt

Get teleported out mid-hunt and the map changes under us; the rule re-applies
and it walks back in. Nothing needs resetting and there is no state to get
stuck in, because the map IS the state.

COORDINATES
-----------
`move_to` takes WORLD units, and the numbers you read in-game are world/100 with
no map origin offset. Verified rather than assumed: a character standing on the
Bakra target read 53500 world against an in-game 535. So 137,126 -> 13700,12600.

ONE COMMAND PER WALK
--------------------
A move order is issued ONCE per destination, not re-sent on a timer. Re-sending
would fight you every time you took manual control, and AutoMoveToPosition
replaces the route rather than queueing it, so a command per second reads as
"walk, stop, walk, stop".

The exception is the one that matters: combat. Getting jumped ends the route -
the client cancels it to retaliate and never resumes - so a walk that dies while
something is attacking us is resumed. A walk that dies with nothing attacking is
taken as YOU cancelling, and is left alone. That discriminator is keeproute's,
measured there over 65 sample windows: routes never die while travelling, only
after the client has already switched to fighting.

STARTING THE GAME'S OWN AUTOHUNT
--------------------------------
Two steps, because the client's Start button does two things and the native loop
needs both:

    miniMap.SetAutoHuntStatus(True)   the flag AutoHuntLoop tests FIRST - this
                                      is what actually wakes the native drive
    SendChatPacket("/auto_hunt start")  what the button emits

autohunt2 exists precisely to intercept those and take over. It must NOT be
enabled on this character, or it will swallow the trigger and drive the hunt
itself, which is not what "the game's default autohunt" means. Checked at load.
"""

import sys

CAPABILITIES = ["read", "move", "ui", "control"]

# Refuse to run on anyone else. This mod MOVES the character and starts a hunt;
# picking up the wrong profile would be both surprising and hard to notice.
# "" disables the check; set it to your farming character's name in a profile.
ONLY_CHARACTER = ""

# map name -> destination in WORLD units. api.map_name() returns these strings;
# they were read off the live clients rather than guessed.
ROUTE = {
    "maps/metin2_map_c1":            [13700.0, 12600.0],   # Pyungmoo  137,126
    "maps/metin2_map_c3":            [53500.0,  4100.0],   # Bakra     535,41
    "maps/metin2_map_monkeydungeon": [7568.0,  21395.0],   # the hunting spot
}

# Maps where, once standing on the spot, we use the skill and start the hunt.
HUNT_MAPS = ["maps/metin2_map_monkeydungeon"]

SKILL_KEY = 2            # hotbar key to press on ENTERING a hunt map
SKILL_SLOT = None        # explicit 0-based index; None = SKILL_KEY - 1
USE_SKILL = True

START_HUNT = True        # start the game's own autohunt on arrival
HUNT_VIA_SETTINGS = True # playerm2g2.CreateAutoBotSettings() FIRST - the button
                         # does this before anything else, and the native loop
                         # is useless without it

# ONE of these, never both. THIS COMBINATION IS CONFIRMED WORKING - do not
# "improve" it by switching the other one back on.
#
# The autohunt button is a TOGGLE: the same button turns it on and off. So two
# "on" actions in a row are not belt-and-braces, they are on-then-off - and both
# calls return cleanly, so the log still says STARTED while the character stands
# there. That was the bug. With only the chat command firing, it works.
#
# Do NOT trust uiHud.HudControls._IsAutoHuntOn() to check this: it reported
# False two seconds into a hunt that was demonstrably running. It appears to
# track the HUD button's own flag, which the chat command never updates. The
# authority on whether the hunt is running is the character, not that getter.
HUNT_VIA_MINIMAP = False # miniMap.SetAutoHuntStatus(True) - leave OFF
HUNT_VIA_CHAT = True     # SendChatPacket("/auto_hunt start") - what the UI sends
STOP_HUNT_ON_LEAVE = True  # "/auto_hunt end" when we leave a hunt map, so the
                           # native drive is not still steering while we walk

# ---- getting through a dungeon full of aggressive mobs ---------------------
# Two separate problems, and only one of them is already solved.
#
#   BODY BLOCKING  - solved by `nocollide`, which is enabled on this character
#                    and makes actors passable while travelling (states 0x8A /
#                    0x88). Mobs displace you rather than stopping you, and it
#                    switches off the moment you stop, so nothing is left open.
#
#   RETALIATION    - NOT solved by that. Being hit makes the client cancel the
#                    route to fight back, and it never resumes. In a dungeon
#                    where everything is aggressive that happens over and over,
#                    which is what "messes up the pathfinding": the walk is not
#                    being blocked, it is being abandoned.
#
# So on these maps we refuse the fight: any auto-attack target acquired while
# walking is DROPPED immediately, so the client has nothing to turn and swing
# at and the route survives. Killing them instead was the alternative and is
# worse - the dungeon is full of them, it would take far longer than the walk,
# and killing things is what the autohunt is for once we are standing on the
# spot.
#
# ONLY while walking. The instant we arrive this stops, or the autohunt would
# be unable to hold a target at all.
IGNORE_MOBS_MAPS = ["maps/metin2_map_monkeydungeon"]
IGNORE_RESUME_EVERY_S = 1.0   # re-issue faster here than the normal 2.0 - the
                              # route dies often, and each death costs distance
IGNORE_MAX_RESUMES = 200      # and far more often than the normal 25

ARRIVE = 400.0           # world units; 4 in-game units. Close enough to stand.
SETTLE_S = 3.0           # after a map change, wait this long before issuing the
                         # first move - position and zone both need a moment to
                         # settle, and a move sent mid-load is simply dropped
STALL_S = 1.5            # not travelling for this long = the route has died
RESUME_EVERY_S = 2.0     # minimum gap between resumes
MAX_RESUMES = 25         # per destination, so a hopeless spot cannot loop for
                         # ever re-issuing a walk that never completes
COMBAT_MEMORY_S = 6.0    # a route that died within this long of us last having
                         # an attack target counts as combat-interrupted

# Travelling states, same values nocollide measured: 0x8A is the waypoint walk,
# 0x88 the walk-in to an attack target. Anything else means we are not moving.
WALK_STATE = 0x8A
WALK_STATES = [0x8A, 0x88]

# ---- the transformation applied when the dungeon spits you out -------------
# It is applied on arrival in Pyungmoo and changes how the character moves, so
# it is dropped BEFORE the walk is issued.
#
# HOW to drop it is build-specific, so it is a LIST of recipes tried in order
# until one reports success, rather than a guess baked into code. Each entry is
# ("module.Function", args...) and is skipped when the module or function is
# absent. PROBE_TRANSFORM logs every plausible binding once, so the right recipe
# can be read off the log rather than guessed at - and guessing is not free
# here: a wrong argument type to a thin C wrapper is an access violation, not a
# TypeError.
CANCEL_TRANSFORM = True
TRANSFORM_MAPS = ["maps/metin2_map_c1"]
PROBE_TRANSFORM = True   # log the candidates once, on the first transform map
DIAG = False             # the autohunt question is answered; this only added noise
# The affect to drop. 220 is NEW_AFFECT_POLYMORPH, confirmed live: the client's
# own AffectShower.GetAffectDict() returned {574: ..., 220: ...} while
# transformed, and 220 was gone afterwards.
#
# Note skill.GetNewAffectDataCount() reports 0 even while visibly transformed -
# wrong enumerator. GetAffectDict() is the real list.
TRANSFORM_AFFECT = 220

# Cancelling takes TWO steps and they cannot share a tick: clicking the affect
# icon OPENS a confirmation dialog, and that dialog only exists from the next
# frame on. So step two waits.
CANCEL_ANSWER_S = 0.4    # gap between opening the dialog and answering it
CANCEL_TRIES = 3         # attempts before giving up and walking anyway

# ---- HOW THE CHARACTER WALKS ------------------------------------------------
# "auto"  - api.move_to = playerm2g2.AutoMoveToPosition, the call the game makes
#           when you CLICK THE ATLAS.
# "steer" - hold a walk direction and re-aim it every tick, the way a player
#           holding an arrow key walks.
#
# WHY STEER. AutoMoveToPosition is not a pathfinder; unstick's notes have it as
# four calls - NEW_GetPixelPosition, __AutoPathSegment, then
# NEW_MoveToDestPixelPositionDirection and NEW_Stop. It steps at the destination
# and lets COLLISION stop it, then re-aims. That collision-driven segment logic
# is the part that gets confused by bodies, and it stays confused even when
# nocollide has made those bodies passable - the route was already computed
# around them.
#
# Key walking has none of that in it. There is no destination, no segment and
# nothing to recompute: the character walks the way it is pointed, and we point
# it. A mob in the way cannot confuse a mechanism that has no notion of the mob.
#
# MEASURED, on an idle character so nothing else was driving it:
#
#   SetSingleDIKKeyState(200 up)  516 units in 1.2s  heading 74.2, camera 74.2
#   SetSingleDIKKeyState(208 dn)  514 units          heading 254.2  (camera+180)
#   SetMultiDirKeyState U         519 units          heading 75.8, camera 75.8
#   SetMultiDirKeyState L         542 units          heading 345.8  (camera-90)
#   pack_chr.MoveToDestPosition     0 units          accepted, does nothing
#
# So the input is CAMERA-relative with exact 90 degree steps, and
# SetMultiDirKeyState(L,R,U,D) is the clean four-way - the left/right ARROWS
# turn the camera instead of strafing, which is why they are not used here.
#
# The cost, stated plainly: eight directions is 45 degrees of resolution, so the
# approach is a shallow zig-zag rather than a straight line, converging because
# it re-aims at STEER_EVERY_S. It also does not path around WALLS - there is no
# pathfinder to do it - so a concave corner needs STEER_STUCK_S to hand back to
# "auto" for a leg. On the open runs this mod does, that trade is the right way
# round.
# "repath" - RE-ISSUE the same destination continuously, which is what a
#            player does when they keep clicking the spot they want. This is
#            the one that actually works, reported from play and consistent
#            with what the call is: __AutoPathSegment computes ONE segment and
#            then keeps retrying that segment when it wedges. Re-issuing from
#            wherever we now stand replaces it with a fresh straight segment
#            before the stale one can matter. Same mover the client already
#            uses, so nothing here is novel to the server - it is the ordinary
#            auto-move, issued often.
#
#            It also keeps auto_move_active and the walk state continuously
#            fresh, which is exactly the condition nocollide's pass-through
#            gate is watching for.
# WHERE re-issuing applies. A LIST OF MAPS, like HUNT_MAPS and
# IGNORE_MOBS_MAPS above, because this is a property of the place and not of
# the mod.
#
# Only the monkey dungeon earns it. That is where twenty mobs are on the
# character at once and a wedged segment has something to wedge ON; the
# approach legs across c1 and c3 are open ground where the ordinary auto-move
# has always been fine, and re-issuing at 2 Hz there would be 180-odd extra
# calls per leg buying nothing.
#
# Measured on the dungeon leg, same target, same client:
#
#   old path   3m34s / 17 resumes,  1m17s / 6,  1m23s / 10,  2m52s / 9
#   repath     1m36s /  0 resumes, under the heaviest pack of any run (80
#              engagements refused against 63, 63, 2, 1)
#
# The resume count is the result: a resume only happens when the stall watchdog
# diagnoses a dead route, and there is no window for that when the route is
# replaced every half second. The elapsed time sits inside the old spread, so
# it is suggestive, not proven.
REPATH_MAPS = ["maps/metin2_map_monkeydungeon"]

MOVE_METHOD = "auto"     # what to use on maps NOT in REPATH_MAPS: "auto"
                         #   (issue once, stall watchdog resumes), "repath" to
                         #   re-issue everywhere, or "steer" for the key-walking
                         #   experiment - see the note above.
# RE-ISSUE ONLY WHEN IT HAS STOPPED - never on a fixed clock.
#
# The first version fired every REPATH_EVERY_S unconditionally and broke the
# walk outright: reported as "takes one step then stops". AutoMoveToPosition
# STOPS the current movement before starting the new segment (NEW_Stop is the
# last of the four calls unstick names), so re-issuing into a healthy walk
# cancels it. At 2 Hz the character spends its life being stopped and restarted
# and never gets past the first step.
#
# That was a misreading of what clicking repeatedly actually does. A player does
# not interrupt a walk that is working - they click again WHEN IT STOPS. The
# character keeps moving in between, and the extra clicks only land on the
# moments the path has wedged.
#
# So: watch progress, and re-issue only when there is none. Same effect on a
# wedged segment, no effect at all on a healthy walk.
REPATH_CHECK_S = 0.75    # window the progress test is measured over
REPATH_PROGRESS = 60.0   # units in that window that count as still moving.
                         #   Walking speed is ~430 units/s, so a working walk
                         #   covers ~320 in this window - 60 is comfortably
                         #   below that and comfortably above jitter.
REPATH_EVERY_S = 0.5     # floor between two re-issues, so a genuinely stuck
                         #   character cannot be re-issued at tick rate
STEER_EVERY_S = 0.25     # how often to re-aim. The drive ticks at ~4Hz.
STEER_ARRIVE = 350.0     # stop steering this close and let ARRIVE finish it
STEER_STUCK_S = 4.0      # no real progress for this long -> fall back to "auto"
                         #   for this leg, and say so. Almost always geometry.
STEER_PROGRESS = 120.0   # units in STEER_STUCK_S that counts as still moving

# WHICH window counts as "the transformation question".
#
# This is not fussiness, it is the bug. The old test was `"Question" in cn or
# "Dialog" in cn`, over gc.get_objects(), taking the FIRST shown match:
#
#   17:23:04  automonkey: answered WhisperDialog via acceptButton.CallEvent()
#   17:23:06  automonkey: after answering, transformation is STILL ACTIVE
#
# WhisperDialog contains "Dialog", was on screen, and has an acceptButton - so
# it got clicked, the real dialog stayed open, and a modal dialog kills the
# walk. automonkey then read the dead route as a manual cancel and refused to
# resume, turning a 2m36s leg into 4m55s. gc ordering is arbitrary, which is
# exactly why this worked until the day a whisper window happened to be open.
#
# So: rank the candidates and take the best one, rather than the first one.
# QuestionDialog2 is this client's confirm box - measured, it is what answered
# correctly on every run that worked.
CANCEL_PREFER = ["QuestionDialog2", "QuestionDialog", "Question"]

# ...and never touch these, however well they match. A chat, whisper or input
# window with an accept button is a message being sent, not a question being
# answered, and clicking it has consequences we cannot see from here.
CANCEL_NEVER = ["Whisper", "Chat", "Input", "Safebox", "Exchange", "Shop",
                "Mall", "Guild", "Party", "Messenger", "Board", "Login",
                "Password", "Pick", "Refine", "Attach"]

VERBOSE = True

_map = None              # the map we last acted on

_repath_at = 0.0         # earliest next re-issue
_repath_from = None      # position at the last progress check
_repath_check = 0.0      # when that check is due
_repaths = 0             # re-issues on this leg, reported on arrival
_repath_failed = False   # move_to reported failure at least once

_steer_keys = None       # (L, R, U, D) currently held, None = nothing held
_steer_at = 0.0          # next re-aim due
_steer_from = None       # position at the last progress check
_steer_mark = 0.0        # when that check was taken
_steer_off = False       # this leg gave up on steering and fell back to "auto"
_steer_handed = False    # ...and whether that handoff has been done yet
_target = None
_issued = False          # a move order has gone out for this destination
_arrived = False
_skill_done = False
_hunt_on = False
_resumes = 0
_since_map = 0.0         # when the current map was first seen
_last_travel = 0.0       # last time we were in a travelling state
_last_combat = 0.0       # last time an attack target existed
_cancel_said = False     # "you cancelled it" logged once per destination
_transform_done = False  # the transform cancel has been attempted on this map
_cancel_at = 0.0         # when to answer the dialog; 0 = nothing pending
_cancel_tries = 0
_verify_at = 0.0         # when to re-read the affect list after answering
_dropped = 0             # engagements refused while walking, for the log
_drop_said = 0.0
_warned_ah2 = False
_me = ""


def _m(name):
    return sys.modules.get(name)


def _d2(a, b):
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy


def _field(api, name, default=0):
    try:
        return api.field(name)
    except Exception:
        return default


def _walk_states():
    out = set()
    for v in [WALK_STATE] + list(WALK_STATES or []):
        try:
            out.add(int(v))
        except Exception:
            pass
    return out or set([0x8A])


def _travelling(api):
    st = _field(api, "player_state", None)
    if st is None:
        return None                       # cannot tell - never guess "stopped"
    return int(st) in _walk_states()


def _slot():
    if SKILL_SLOT is not None:
        try:
            return int(SKILL_SLOT)
        except Exception:
            pass
    try:
        k = int(SKILL_KEY)
    except Exception:
        return 1
    return 9 if k == 0 else k - 1         # "0" is the tenth slot, not the zeroth


def _use_skill(api):
    """Press the hotbar slot, refusing an empty one.

    RequestUseLocalQuickSlot does no validation of its own, and pressing an
    empty slot is implicated in a crash cluster (19 of 25 crashes in one install
    ended on follow's buff cast). (0, 0) means empty; None means the accessor is
    unavailable, which is NOT the same and is allowed through."""
    slot = _slot()
    try:
        qs = api.quickslot(slot)
    except Exception:
        qs = None
    if qs is not None:
        try:
            if int(qs[0]) == 0 and int(qs[1]) == 0:
                api.log("automonkey: hotbar slot %d (key %s) is EMPTY - not "
                        "pressing it. Put the skill there, or set SKILL_SLOT."
                        % (slot, SKILL_KEY))
                return False
        except Exception:
            pass
    ok = api.use_quickslot(slot)
    api.log("automonkey: used key %s (slot %d)%s"
            % (SKILL_KEY, slot, "" if ok else " - FAILED"))
    return ok


_diag_done = False


def _arity(api, label, fn):
    """Learn a binding's signature WITHOUT running it.

    Calling with the wrong arguments makes PyArg_ParseTuple raise BEFORE the
    function body executes, and the message names the format it wanted. Calling
    with the RIGHT arguments would execute it, which is the thing to avoid while
    still guessing - a bad pointer-typed argument is an access violation, not an
    exception."""
    # DISABLED. Calling a binding with no arguments cannot crash - PyArg
    # rejects it first - but the ones that legitimately take no arguments RUN,
    # and "GetAffect()" style names are not all as read-only as they sound.
    # A signature is not worth a side effect on a live character.
    api.log("automonkey: (arity probe for %s skipped - it can have side "
            "effects on a live client)" % label)


def _probe_button(api):
    """Find the autohunt BUTTON and the object it lives on. READ ONLY.

    The user starts this by clicking a UI button - there is no range slider and
    no settings beyond on/off and use-skills. So the reliable move is the one
    that has worked everywhere else on this client: call the function the button
    calls, instead of re-creating what it is assumed to do. For that we need the
    live WINDOW INSTANCE, not the class: the client binds its buttons with
    SetEvent(ui.__mem_func__(self.__StartBtn)) at CONSTRUCTION time, so a class
    patch never reaches an already-open window.

    Nothing here is called. It lists what exists so the handler can be picked."""
    import sys as _s
    names = sorted(n for n in list(_s.modules)
                   if "autohunt" in n.lower() or "hud" in n.lower()
                   or "taskbar" in n.lower())
    api.log("automonkey: autohunt-ish modules: %s" % (names or "none"))
    for n in names[:6]:
        m = _s.modules.get(n)
        if m is None:
            continue
        for attr in sorted(dir(m)):
            if attr.startswith("__"):
                continue
            try:
                v = getattr(m, attr)
            except Exception:
                continue
            low = attr.lower()
            if isinstance(v, type):
                meths = sorted(a for a in dir(v)
                               if "hunt" in a.lower() or "start" in a.lower()
                               or "toggle" in a.lower())
                if meths:
                    api.log("automonkey: %s.%s (class) -> %s"
                            % (n, attr, ", ".join(meths)))
            elif "hunt" in low or "toggle" in low:
                api.log("automonkey: %s.%s = %r (%s)"
                        % (n, attr, v, "callable" if callable(v) else "value"))
        # A live INSTANCE is what we actually need - look for module-level ones.
        inst = [a for a in dir(m)
                if not a.startswith("__")
                and "hunt" in a.lower()
                and not isinstance(getattr(m, a, None), type)]
        if inst:
            api.log("automonkey: %s module-level hunt names: %s" % (n, inst))


_hud = None              # the live uiHud.HudControls instance, once found


def _find_hud(api, quiet=False):
    """Locate the LIVE HudControls object.

    The autohunt button is an instance method on it, and the client binds its
    buttons at construction - so the class is no use, only the object the HUD
    actually built. It is not exposed as a module global, so sweep the loaded
    modules for an attribute that IS one. Cheap, and done once."""
    global _hud
    if _hud is not None:
        return _hud
    import sys as _s
    ui = _s.modules.get("uiHud")
    cls = getattr(ui, "HudControls", None) if ui else None
    if cls is None:
        if not quiet:
            api.log("automonkey: uiHud.HudControls not loaded")
        return None
    seen = []

    def take(where, v):
        global _hud
        try:
            if isinstance(v, cls):
                seen.append(where)
                if _hud is None:
                    _hud = v
                return True
        except Exception:
            pass
        return False

    # 1. module attributes, then one level INSIDE them - the HUD is owned by
    #    another UI object rather than exposed as a module global.
    for name in list(_s.modules):
        m = _s.modules.get(name)
        if m is None:
            continue
        for attr in dir(m):
            if attr.startswith("__"):
                continue
            try:
                v = getattr(m, attr)
            except Exception:
                continue
            if take("%s.%s" % (name, attr), v):
                continue
            if isinstance(v, type) or callable(v):
                continue                     # classes and functions own nothing
            for sub in dir(v):
                if sub.startswith("__"):
                    continue
                try:
                    take("%s.%s.%s" % (name, attr, sub), getattr(v, sub))
                except Exception:
                    continue

    # 2. gc is a BUILTIN module, so it imports even though this client's
    #    zipimport is broken for everything else. It sees objects no attribute
    #    walk can reach - a HUD held only in a local or a closure, for instance.
    if _hud is None:
        try:
            import gc
            for o in gc.get_objects():
                if take("gc", o):
                    break
        except Exception as e:
            if not quiet:
                api.log("automonkey: gc sweep unavailable (%s)" % type(e).__name__)

    if not quiet:
        api.log("automonkey: HudControls instance(s): %s" % (seen[:5] or "NONE FOUND"))
    return _hud


def _hunt_is_on(api):
    """Ask the HUD whether autohunt is really running. None = cannot tell.

    This is the check that has been missing all along: every previous attempt
    reported success from the fact that our CALLS returned, which is not the
    same thing and is exactly how a silent failure gets logged as STARTED."""
    hud = _find_hud(api, quiet=True)
    fn = getattr(hud, "_IsAutoHuntOn", None) if hud is not None else None
    if not callable(fn):
        return None
    try:
        return bool(fn())
    except Exception:
        return None


def _diagnose(api):
    """Why is the hunt not starting, and what cancels the transformation?

    Two questions, one pass, run once. The autohunt half matters most: this
    server defines NEW_AFFECT_AUTO_HUNT (830), which means auto-hunt may be a
    BUFF rather than a plain client feature - and if the server grants it by
    affect, /auto_hunt start is refused without it while every call we make
    still reports success. That failure is invisible from our side, which is
    exactly the shape of the bug being chased."""
    global _diag_done
    if _diag_done:
        return
    _diag_done = True
    _probe_button(api)
    _find_hud(api)
    api.log("automonkey: autohunt actually on? -> %s" % _hunt_is_on(api))

    # 1. Is the autohunt AFFECT active? If auto-hunt is buff-gated, this is the
    #    answer, and no amount of calling /auto_hunt will help without it.
    for name, idx in (("AUTO_HUNT", 830), ("AUTO_HUNT_AFF", 831),
                      ("AUTO_METIN_FARM", 835), ("POLYMORPH", 220)):
        secs = None
        try:
            secs = api.affect_seconds(idx)
        except Exception as e:
            secs = "err:%s" % type(e).__name__
        api.log("automonkey: affect %-16s (%d) -> %s" % (name, idx, secs))

    # 2. Can we read the minimap autohunt flag BACK? Writing it and never
    #    checking is how "STARTED" ends up in a log for a hunt that never ran.
    mm = _m("miniMap")
    if mm is not None:
        names = sorted(a for a in dir(mm)
                       if not a.startswith("__") and "hunt" in a.lower())
        api.log("automonkey: miniMap autohunt names: %s" % (names or "none"))
        for a in names:
            f = getattr(mm, a, None)
            if callable(f) and a.lower().startswith("get"):
                _arity(api, "miniMap." + a, f)

    # 3. What can even see or drop an affect? The polymorph-remove feature is
    #    compiled IN on this server (app.ENABLE_AFFECT_POLYMORPH_REMOVE = 1),
    #    so something must expose it - most likely the affect UI module.
    import sys as _s
    uimods = sorted(n for n in list(_s.modules)
                    if "affect" in n.lower() or "uiaffect" in n.lower())
    api.log("automonkey: affect-ish modules loaded: %s" % (uimods or "none"))
    for n in uimods[:4]:
        m = _s.modules.get(n)
        if m is None:
            continue
        calls = sorted(a for a in dir(m)
                       if not a.startswith("__") and callable(getattr(m, a, None)))
        api.log("automonkey: %s callables: %s" % (n, ", ".join(calls[:40])))
    for label, mod, fn in (("pack_chr.IsAffect", "pack_chr", "IsAffect"),
                           ("playerm2g2.CheckAffect", "playerm2g2", "CheckAffect"),
                           ("playerm2g2.GetAffect", "playerm2g2", "GetAffect"),
                           ("chrmgr.SetAffect", "chrmgr", "SetAffect")):
        m = _m(mod)
        f = getattr(m, fn, None) if m else None
        if callable(f):
            _arity(api, label, f)


def _probe_transform(api):
    """Name every binding that could plausibly drop a transformation. READ ONLY.

    Nothing here is called. The point is to turn "what cancels this?" into a
    list read off the client, which CANCEL_CALLS can then be pointed at."""
    hits = []
    for modname in ("playerm2g2", "m2netm2g", "skill", "app", "pack_chr",
                    "chrmgr", "item", "uiAffect"):
        mod = _m(modname)
        if mod is None:
            continue
        for attr in dir(mod):
            if attr.startswith("__"):
                continue
            low = attr.lower()
            if any(w in low for w in ("polymorph", "transform", "morph",
                                      "cancel", "affect", "unmount", "ride",
                                      "mount", "remove")):
                try:
                    v = getattr(mod, attr)
                except Exception:
                    continue
                hits.append("%s.%s%s" % (modname, attr,
                                         "" if callable(v) else "=%r" % (v,)))
    api.log("automonkey: transform candidates: %s"
            % (", ".join(sorted(hits)) if hits else "none found"))


FORCE_CANCEL = False     # one-shot: try every cancel route, on load
FORCE_YES = False        # one-shot: answer an OPEN confirm dialog with yes
FORCE_WINDOWS = False    # one-shot: list every SHOWN answerable window
FORCE_BTN = False        # one-shot: dump QuestionDialog2.acceptButton
FORCE_PROBE = False      # set true to run the affect probe once on load,
                         # whatever map we are on - for use while transformed


def _find_instance(api, modname, clsname):
    """The LIVE object of a UI class, via gc. Same trick that found the HUD.

    UI singletons are owned by other objects, not exposed as module globals, so
    an attribute walk misses them. gc is a BUILTIN module and imports even
    though this client's zipimport is broken for everything else."""
    import sys as _s
    m = _s.modules.get(modname)
    cls = getattr(m, clsname, None) if m else None
    if cls is None:
        return None
    try:
        import gc
        for o in gc.get_objects():
            try:
                if isinstance(o, cls):
                    return o
            except Exception:
                continue
    except Exception as e:
        api.log("automonkey: gc unavailable (%s)" % type(e).__name__)
    return None


def _probe_affect_ui(api):
    """What is actually applied, and what can remove it. READ ONLY.

    skill.GetNewAffectDataCount() reported 0 while visibly transformed, so it is
    the wrong enumerator. The affect SHOWER holds the real list."""
    sh = _find_instance(api, "uiAffectShower", "AffectShower")
    api.log("automonkey: AffectShower instance: %s" % ("found" if sh else "NOT FOUND"))
    if sh is None:
        return
    for meth in ("GetAffectDict",):
        fn = getattr(sh, meth, None)
        if callable(fn):
            try:
                api.log("automonkey: %s -> %r" % (meth, fn()))
            except Exception as e:
                api.log("automonkey: %s failed (%s: %s)"
                        % (meth, type(e).__name__, e))
    # The icons themselves - each knows which affect it represents.
    import sys as _s
    ui = _s.modules.get("uiAffectShower")
    img_cls = getattr(ui, "AffectImage", None) if ui else None
    if img_cls is None:
        return
    try:
        import gc
        n = 0
        for o in gc.get_objects():
            try:
                if not isinstance(o, img_cls):
                    continue
            except Exception:
                continue
            n += 1
            if n > 12:
                break
            bits = []
            for meth in ("GetAffect", "IsSkillAffect", "GetImageName", "IsShow"):
                fn = getattr(o, meth, None)
                if callable(fn):
                    try:
                        bits.append("%s=%r" % (meth, fn()))
                    except Exception:
                        bits.append("%s=err" % meth)
            api.log("automonkey: AffectImage#%d %s" % (n, ", ".join(bits)))
    except Exception:
        pass


def _probe_dialog_button(api):
    """Dump QuestionDialog2.acceptButton in full - methods AND stored state.

    Clicking it with the mouse handlers raised nothing and did nothing, so the
    registered accept event is not reached that way. The event has to be stored
    on the button somewhere; find the attribute or the method that fires it."""
    try:
        import gc
    except Exception:
        return
    for o in gc.get_objects():
        try:
            if type(o).__name__ != "QuestionDialog2" or not o.IsShow():
                continue
        except Exception:
            continue
        btn = getattr(o, "acceptButton", None)
        api.log("automonkey: acceptButton is %s" % type(btn).__name__)
        if btn is None:
            return
        meths = sorted(a for a in dir(btn) if not a.startswith("__"))
        api.log("automonkey: acceptButton methods: %s" % ", ".join(meths))
        # Stored callables are the interesting part - one of them IS the event.
        for a in meths:
            try:
                v = getattr(btn, a)
            except Exception:
                continue
            if callable(v) and not a[:1].isupper():
                api.log("automonkey:   btn.%s = %r" % (a, v))
        for a in sorted(x for x in dir(o) if not x.startswith("__")):
            try:
                v = getattr(o, a)
            except Exception:
                continue
            if callable(v) and ("event" in a.lower() or "accept" in a.lower()):
                api.log("automonkey:   dlg.%s = %r" % (a, v))
        return
    api.log("automonkey: QuestionDialog2 is not open right now")


def _find_open_windows(api):
    """Every SHOWN window that looks answerable, with its class and handlers.

    The earlier search only matched class names containing "Question" or
    "Dialog" and logged nothing when it matched none - so a failure looked
    identical to a dialog that was not there. This names what is actually on
    screen instead of assuming what it is called."""
    try:
        import gc
    except Exception:
        api.log("automonkey: gc unavailable")
        return
    seen = {}
    for o in gc.get_objects():
        try:
            show = getattr(o, "IsShow", None)
            if not callable(show) or not show():
                continue
            cn = type(o).__name__
        except Exception:
            continue
        # Only things with an answer-shaped handler - otherwise this is every
        # widget in the UI.
        meths = []
        try:
            for a in dir(o):
                if a.startswith("__"):
                    continue
                if any(w in a.lower() for w in
                       ("accept", "yes", "answer", "confirm", "okbutton",
                        "onclickok", "cancel")):
                    meths.append(a)
        except Exception:
            continue
        if not meths:
            continue
        key = cn
        if key in seen:
            continue
        seen[key] = True
        try:
            txt = ""
            for g in ("GetText", "GetTitleName", "GetWindowName"):
                f = getattr(o, g, None)
                if callable(f):
                    txt = " %s=%r" % (g, f())
                    break
        except Exception:
            txt = ""
        api.log("automonkey: SHOWN %s%s -> %s" % (cn, txt, ", ".join(sorted(meths))))
    if not seen:
        api.log("automonkey: nothing shown has an answer-shaped handler")


def _click_yes(api):
    """Answer the open "break the transformation?" dialog with YES.

    The affect click OPENS a confirmation dialog; the Yes button belongs to that
    dialog object, not to the affect icon - which is why AffectQuestionAnswer
    returned None and nothing happened. Find the dialog that is currently SHOWN
    and fire its accept handler.

    All of this is client PYTHON, so a wrong name or arity raises a TypeError
    rather than corrupting anything - which is what makes trying several
    accept-handler names acceptable here."""
    try:
        import gc
    except Exception:
        api.log("automonkey: gc unavailable")
        return False
    hits = 0
    for o in gc.get_objects():
        try:
            cn = type(o).__name__
        except Exception:
            continue
        if "Question" not in cn and "Dialog" not in cn:
            continue
        # Only the one actually on screen.
        try:
            shown = o.IsShow()
        except Exception:
            continue
        if not shown:
            continue
        hits += 1
        meths = sorted(a for a in dir(o)
                       if any(w in a.lower() for w in
                              ("accept", "ok", "yes", "answer", "confirm")))
        api.log("automonkey: OPEN dialog %s -> %s" % (cn, ", ".join(meths) or "-"))
        for meth in ("OnAccept", "Accept", "OnClickOkButton", "OnClickYesButton",
                     "_QuestionDialog__OnAccept", "OnPressOkKey"):
            fn = getattr(o, meth, None)
            if not callable(fn):
                continue
            try:
                r = fn()
                api.log("automonkey: %s.%s() -> %r" % (cn, meth, r))
                return True
            except Exception as e:
                api.log("automonkey: %s.%s() -> %s: %s"
                        % (cn, meth, type(e).__name__, e))
    if not hits:
        api.log("automonkey: no dialog is currently open")
    return False


def _force_cancel_now(api):
    """Emulate: click the affect icon, then click cancel. One shot, on load.

    Safe to try blind because uiAffectShower is PYTHON, not a C binding - a
    wrong argument raises a TypeError instead of an access violation. The
    every-recipe-in-order approach is therefore fine here in a way it would
    never be against playerm2g2."""
    import sys as _s
    ui = _s.modules.get("uiAffectShower")
    if ui is None:
        api.log("automonkey: uiAffectShower not loaded")
        return
    try:
        import gc
    except Exception:
        api.log("automonkey: gc unavailable - cannot reach the affect icons")
        return

    img_cls = getattr(ui, "AffectImage", None)
    sh_cls = getattr(ui, "AffectShower", None)
    imgs, showers = [], []
    for o in gc.get_objects():
        try:
            if img_cls is not None and isinstance(o, img_cls):
                imgs.append(o)
            elif sh_cls is not None and isinstance(o, sh_cls):
                showers.append(o)
        except Exception:
            continue
    api.log("automonkey: %d affect icon(s), %d shower(s)" % (len(imgs), len(showers)))

    # 1. The click chain, per icon: open the polymorph question, then answer it.
    for i, o in enumerate(imgs[:16]):
        what = ""
        try:
            g = getattr(o, "GetAffect", None)
            if callable(g):
                what = "affect=%r " % (g(),)
        except Exception:
            pass
        for meth, args in (("_AffectImage__OnClickAffectIcon", ()),
                           ("OnPolymorphQuestionDialog", ()),
                           ("AffectQuestionAnswer", (True,)),
                           ("AffectQuestionAnswer", (1,))):
            fn = getattr(o, meth, None)
            if not callable(fn):
                continue
            try:
                r = fn(*args)
                api.log("automonkey: icon#%d %s%s%r -> %r"
                        % (i, what, meth, args, r))
            except Exception as e:
                api.log("automonkey: icon#%d %s%r -> %s: %s"
                        % (i, meth, args, type(e).__name__, e))

    # 2. The shower's own removal, by affect index.
    for sh in showers[:2]:
        for meth in ("RemoveAffect", "_AffectShower__RemoveAffect"):
            fn = getattr(sh, meth, None)
            if not callable(fn):
                continue
            for idx in (220, 129):          # NEW_AFFECT_POLYMORPH, SKILL_INDEX
                try:
                    api.log("automonkey: shower %s(%d) -> %r" % (meth, idx, fn(idx)))
                except Exception as e:
                    api.log("automonkey: shower %s(%d) -> %s: %s"
                            % (meth, idx, type(e).__name__, e))


def _dump_affects(api):
    """List every affect ACTIVE RIGHT NOW, with its index.

    This is the question that matters and the earlier probe could not answer:
    which affect IS the transformation? Reading NEW_AFFECT_POLYMORPH while
    standing in the dungeon proved nothing - there is no transformation there.
    Run on the transform map, this names the thing to remove."""
    sk = _m("skill")
    cnt = getattr(sk, "GetNewAffectDataCount", None) if sk else None
    get = getattr(sk, "GetNewAffectData", None) if sk else None
    if callable(cnt) and callable(get):
        try:
            n = int(cnt())
        except Exception as e:
            api.log("automonkey: GetNewAffectDataCount failed (%s)" % type(e).__name__)
            n = 0
        rows = []
        for i in range(min(n, 40)):
            try:
                rows.append("%s" % (get(i),))
            except Exception:
                break
        api.log("automonkey: %d active affect(s): %s" % (n, "; ".join(rows) or "-"))
    # Cross-check the indices we already suspect, by name.
    for name, idx in (("POLYMORPH", 220), ("MOV_SPEED", 200),
                      ("AUTO_HUNT", 830), ("PREMIUM_USER", 572)):
        try:
            api.log("automonkey: affect %-13s (%d) -> %s"
                    % (name, idx, api.affect_seconds(idx)))
        except Exception:
            pass
    # "Click the effect, then click cancel" - so the removal lives in the affect
    # UI. Dump the classes and their methods so the click handler can be read
    # off rather than guessed at.
    ui = _m("uiAffectShower")
    if ui is not None:
        for cname in ("AffectListRow", "AffectListWindow", "AffectShower",
                      "AffectImage"):
            cls = getattr(ui, cname, None)
            if cls is None:
                continue
            meths = sorted(a for a in dir(cls) if not a.startswith("__"))
            api.log("automonkey: uiAffectShower.%s: %s" % (cname, ", ".join(meths)))
    p = _m("playerm2g2")
    for label, fn in (("IsMountingHorse", getattr(p, "IsMountingHorse", None)),):
        if callable(fn):
            try:
                api.log("automonkey: %s -> %s" % (label, fn()))
            except Exception as e:
                api.log("automonkey: %s -> %s" % (label, type(e).__name__))


def _affect_dict(api):
    """{affect index: icon} from the client's own affect shower, or None."""
    sh = _find_instance(api, "uiAffectShower", "AffectShower")
    fn = getattr(sh, "GetAffectDict", None) if sh is not None else None
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def _is_transformed(api):
    """True/False, or None when the affect list cannot be read.

    This is the VERIFICATION the earlier attempts lacked: whether the affect is
    still in the client's own list, rather than whether our calls returned."""
    d = _affect_dict(api)
    if d is None:
        return None
    try:
        return TRANSFORM_AFFECT in d
    except Exception:
        return None


def _cancel_transform(api):
    """Step 1: click the affect icon, which opens the confirmation dialog."""
    global _cancel_at, _cancel_tries
    if PROBE_TRANSFORM:
        _dump_affects(api)
    d = _affect_dict(api)
    if d is None:
        api.log("automonkey: cannot read the affect list - skipping the cancel")
        return False
    icon = None
    try:
        icon = d.get(TRANSFORM_AFFECT)
    except Exception:
        pass
    if icon is None:
        if VERBOSE:
            api.log("automonkey: no transformation (affect %d) active"
                    % TRANSFORM_AFFECT)
        return False
    fn = getattr(icon, "_AffectImage__OnClickAffectIcon", None)
    if not callable(fn):
        fn = getattr(icon, "OnPolymorphQuestionDialog", None)
    if not callable(fn):
        api.log("automonkey: the affect icon exposes no click handler")
        return False
    try:
        fn()
    except Exception as e:
        api.log("automonkey: clicking the affect icon failed (%s: %s)"
                % (type(e).__name__, e))
        return False
    _cancel_at = api.now() + CANCEL_ANSWER_S
    _cancel_tries = 0
    if VERBOSE:
        api.log("automonkey: opened the break-transformation dialog - "
                "answering in %.1fs" % CANCEL_ANSWER_S)
    return True


def _dialog_rank(cn):
    """How good a match this class name is for the transformation question.

    Lower is better; None means never touch it. Keeping the deny-list ahead of
    the preference list matters: "GuildQuestionDialog" would otherwise score as
    a Question and be answered."""
    for bad in CANCEL_NEVER:
        if bad.lower() in cn.lower():
            return None
    for i, want in enumerate(CANCEL_PREFER):
        if want.lower() == cn.lower():
            return i
    for i, want in enumerate(CANCEL_PREFER):
        if want.lower() in cn.lower():
            return len(CANCEL_PREFER) + i
    if "dialog" in cn.lower():
        return 99                      # last resort, and only if nothing better
    return None


def _answerable(api):
    """Every shown window that may be answered, best candidate first.

    Sorted rather than first-found: gc.get_objects() has no meaningful order,
    so "the first one that matched" was really "whichever the collector happened
    to list first", and that is not a decision anybody made."""
    try:
        import gc
    except Exception:
        return []
    out = []
    for o in gc.get_objects():
        try:
            cn = type(o).__name__
        except Exception:
            continue
        rank = _dialog_rank(cn)
        if rank is None:
            continue
        try:
            if not o.IsShow():
                continue
        except Exception:
            continue
        out.append((rank, cn, o))
    out.sort(key=lambda e: e[0])
    return out


def _cancel_answer(api, now):
    """Step 2: click Yes on the dialog the icon opened.

    The Yes button belongs to the DIALOG, not to the affect icon - which is why
    AffectImage.AffectQuestionAnswer() returned None and changed nothing. The
    dialog is a normal client-Python window, so a wrong handler name raises
    rather than doing damage."""
    global _cancel_at, _cancel_tries
    if not _cancel_at or now < _cancel_at:
        return
    _cancel_at = 0.0

    still = _is_transformed(api)
    if still is False:
        api.log("automonkey: transformation cleared")
        return

    cands = _answerable(api)
    if not cands:
        api.log("automonkey: no answerable dialog is open")
    elif len(cands) > 1:
        # Say what was on screen and what was chosen. The whole failure was an
        # invisible choice between two windows.
        api.log("automonkey: %d answerable window(s) open %r - answering %r"
                % (len(cands), [c[1] for c in cands], cands[0][1]))
    clicked = False
    for _rank, cn, o in cands:

        # This client's confirm box is uiCommon.QuestionDialog2. It has NO
        # OnAccept - the "yes" action is stored ON THE BUTTON as
        #   acceptButton.eventFunc = <lambda from AffectImage.__OnClickAffectIcon>
        # so the way to answer is to FIRE THAT EVENT, not to simulate a mouse.
        #
        # Simulating the mouse (OnMouseLeftButtonDown/Up) raised nothing and did
        # nothing - the up-handler only fires the event for a widget the mouse
        # has actually picked. That silent no-op is why this looked answered
        # while the affect stayed put.
        btn = getattr(o, "acceptButton", None)
        if btn is not None:
            fn = getattr(btn, "CallEvent", None)
            if callable(fn):
                try:
                    fn()
                    api.log("automonkey: answered %s via acceptButton.CallEvent()"
                            % cn)
                    clicked = True
                except Exception as e:
                    api.log("automonkey: CallEvent failed (%s: %s)"
                            % (type(e).__name__, e))
            if not clicked:
                ev = getattr(btn, "eventFunc", None)
                if callable(ev):
                    args = getattr(btn, "eventArgs", None) or ()
                    try:
                        ev(*args)
                        api.log("automonkey: answered %s via acceptButton."
                                "eventFunc%r" % (cn, tuple(args)))
                        clicked = True
                    except Exception as e:
                        api.log("automonkey: eventFunc failed (%s: %s)"
                                % (type(e).__name__, e))
        if clicked:
            break
        # Fallback: a dialog that does expose a handler directly.
        for meth in ("OnAccept", "Accept", "OnClickOkButton", "OnClickYesButton",
                     "_QuestionDialog__OnAccept", "OnPressOkKey"):
            fn = getattr(o, meth, None)
            if not callable(fn):
                continue
            try:
                fn()
                api.log("automonkey: answered %s via %s()" % (cn, meth))
                clicked = True
                break
            except Exception:
                continue
        if clicked:
            break

    _cancel_tries += 1
    if not clicked and _cancel_tries < CANCEL_TRIES:
        _cancel_at = now + CANCEL_ANSWER_S   # dialog may not be up yet
        return
    if clicked:
        # Do NOT declare success here. Removing the affect is a server round
        # trip, and "the call returned" has been wrong every single time this
        # session. Re-read the client's own affect list in a moment instead.
        global _verify_at
        _verify_at = now + 1.2
        return
    api.log("automonkey: could not answer the dialog after %d attempt(s) - "
            "walking anyway" % _cancel_tries)


def _hunt(api, on):
    """Start or stop the client's OWN autohunt, the way its button does.

    THREE steps, and the first is the one whose absence made this silently do
    nothing. CreateAutoBotSettings populates the client's autohunt settings
    block from its config; the shipped Start button calls it BEFORE sending
    /auto_hunt. Without it the native loop wakes with no settings to work from,
    the minimap flag reads set, the chat command goes out, the log says
    "STARTED", and the character stands there. Which is exactly what happened."""
    done = []
    if on and HUNT_VIA_SETTINGS:
        p = _m("playerm2g2")
        fn = getattr(p, "CreateAutoBotSettings", None) if p else None
        if callable(fn):
            try:
                fn()
                done.append("settings")
            except Exception as e:
                api.log("automonkey: CreateAutoBotSettings failed (%s: %s)"
                        % (type(e).__name__, e))
        else:
            api.log("automonkey: CreateAutoBotSettings not exposed - the native "
                    "hunt may not start")
    if HUNT_VIA_MINIMAP:
        mm = _m("miniMap")
        fn = getattr(mm, "SetAutoHuntStatus", None) if mm else None
        if callable(fn):
            try:
                # Sets the gate AND, unconditionally, re-centres the search on
                # the player's current position - which is what we want, since
                # we have just walked to the spot we mean to hunt from.
                fn(bool(on))
                done.append("minimap flag")
            except Exception as e:
                api.log("automonkey: SetAutoHuntStatus failed (%s)" % type(e).__name__)
    if HUNT_VIA_CHAT:
        n = _m("m2netm2g")
        fn = getattr(n, "SendChatPacket", None) if n else None
        if callable(fn):
            try:
                fn("/auto_hunt %s" % ("start" if on else "end"))
                done.append("/auto_hunt %s" % ("start" if on else "end"))
            except Exception as e:
                api.log("automonkey: SendChatPacket failed (%s)" % type(e).__name__)
    api.log("automonkey: autohunt %s via %s"
            % ("STARTED" if on else "stopped", ", ".join(done) or "nothing"))
    return bool(done)


# ---- steering ---------------------------------------------------------------
def _camera():
    """Camera yaw in degrees, or None. This is the frame the keys work in."""
    a = _m("app")
    try:
        return float(a.GetCameraRotation())
    except Exception:
        return None


def _heading(here, there):
    """World compass heading from `here` to `there`, 0 = north, clockwise.

    +y is SOUTH in this client's world coordinates - the same inversion
    entscan and autoloot account for in their bearings."""
    import math
    dx, dy = there[0] - here[0], there[1] - here[1]
    return math.degrees(math.atan2(dx, -dy)) % 360.0


# Camera-relative octant -> (L, R, U, D). Index 0 is straight ahead; each step
# is 45 degrees clockwise. U is the camera heading and L is camera-90, both
# measured rather than assumed - see the note on MOVE_METHOD.
_OCTANTS = [
    (False, False, True,  False),   #   0  forward
    (False, True,  True,  False),   #  45  forward-right
    (False, True,  False, False),   #  90  right
    (False, True,  False, True),    # 135  back-right
    (False, False, False, True),    # 180  back
    (True,  False, False, True),    # 225  back-left
    (True,  False, False, False),   # 270  left
    (True,  False, True,  False),   # 315  forward-left
]


def _keys(api, quad):
    """Hold exactly this combination of walk keys. None releases everything.

    RE-ASSERTED EVERY CALL, never skipped when unchanged. Setting it once and
    trusting it to stick is what the first live run got wrong:

        18:06:46  steering to 7568,21395 - holding a walk direction
        18:06:54  steering made 82 units in 4s

    against 516 units in 1.2s measured on an idle character. The client polls
    the real keyboard every frame and writes that state over ours, so a
    synthetic hold survives only until the next poll - the character moved in
    brief spurts, one per direction CHANGE, which is exactly 82 units' worth.
    The call is cheap; the optimisation was not."""
    global _steer_keys
    p = _m("playerm2g2")
    if p is None:
        return False
    want = quad or (False, False, False, False)
    try:
        p.SetMultiDirKeyState(bool(want[0]), bool(want[1]),
                              bool(want[2]), bool(want[3]))
    except Exception as e:
        api.log("automonkey: SetMultiDirKeyState failed %s: %s"
                % (type(e).__name__, e))
        return False
    _steer_keys = want if quad else None
    return True


def _steer_stop(api):
    """Release the walk keys. Cheap, idempotent, and must never be skipped.

    A held direction outlives whatever set it: the character keeps walking with
    nothing driving it, which is the one failure here the player would feel and
    not be able to explain."""
    global _steer_at, _steer_from, _steer_mark
    if _steer_keys is not None:
        _keys(api, None)
    _steer_at, _steer_from, _steer_mark = 0.0, None, 0.0


def _steer(api, now, here, target):
    """Point the character at `target` and hold. True while it owns the walk.

    Returns False when steering cannot or should not drive this leg, so the
    caller falls back to the ordinary route."""
    global _steer_at, _steer_from, _steer_mark, _steer_off

    if _steer_off:
        return False
    cam = _camera()
    if cam is None:
        if not _steer_off:
            _steer_off = True
            api.log("automonkey: no camera rotation to steer by - using the "
                    "ordinary route for this leg")
        return False

    d = _d2(here, target) ** 0.5
    if d <= STEER_ARRIVE:
        _steer_stop(api)
        return False                    # close enough; ARRIVE finishes it

    # Progress watchdog. Steering has no pathfinder, so geometry that needs one
    # shows up as simply not getting closer.
    if _steer_from is None:
        _steer_from, _steer_mark = here, now
    elif now - _steer_mark >= STEER_STUCK_S:
        moved = _d2(_steer_from, here) ** 0.5
        _steer_from, _steer_mark = here, now
        if moved < STEER_PROGRESS:
            _steer_off = True
            _steer_stop(api)
            # Deliberately does NOT name a cause. The first version of this line
            # said "that is geometry, not bodies" and was wrong: the character
            # was on open ground and the real fault was our own key handling.
            # A diagnostic that guesses sends the reader to the wrong file.
            api.log("automonkey: steering made only %.0f units in %.0fs (%.0f "
                    "still to go) - handing this leg to the ordinary route"
                    % (moved, STEER_STUCK_S, d))
            return False
        elif VERBOSE:
            api.log("automonkey: steering - %.0f units in %.0fs, %.0f to go"
                    % (moved, STEER_STUCK_S, d))

    if now < _steer_at:
        return True                     # holding the current direction
    _steer_at = now + STEER_EVERY_S

    rel = (_heading(here, target) - cam) % 360.0
    idx = int((rel + 22.5) // 45.0) % 8
    _keys(api, _OCTANTS[idx])
    return True


def _read_json(path):
    """Read a json file without `with` or encoding= - see the modhost header."""
    import json
    try:
        f = open(path, "rb")
    except Exception:
        return None
    try:
        return json.loads(f.read().decode("utf-8", "replace"))
    except Exception:
        return None
    finally:
        try:
            f.close()
        except Exception:
            pass


def _check_hunt_conflict(api):
    """Warn if a mod that hijacks /auto_hunt is enabled for this character.

    autohunt2 patches SendChatPacket and SWALLOWS "/auto_hunt start", and it
    blocks miniMap.SetAutoHuntStatus outright - both of the things this mod uses
    to start the native drive. ribfarmer hosts that same engine, so it counts
    too. Neither would produce an error; the hunt would simply be someone
    else's, which is not what "the game's default autohunt" means.

    Read from config.json + this character's profile, in the order modhost
    merges them, because the answer has to match what modhost DID."""
    import os
    mods = os.path.dirname(__mod_dir__)
    base = _read_json(os.path.join(mods, "config.json")) or {}
    safe = "".join(c for c in (_me or "") if c.isalnum() or c in "-_")
    prof = _read_json(os.path.join(mods, "profiles", safe + ".json")) or {}         if safe else {}
    bad = []
    for name in ("autohunt2", "ribfarmer"):
        on = None
        for src in (base, prof):
            sect = src.get(name)
            if isinstance(sect, dict) and "enabled" in sect:
                on = bool(sect["enabled"])
        if on:                              # modhost defaults absent->on, but an
            bad.append(name)                # absent section here means untouched
    if bad:
        api.log("automonkey: WARNING - %s enabled for this character. It "
                "intercepts /auto_hunt and blocks SetAutoHuntStatus, so the "
                "GAME'S autohunt will not start - that mod will hunt instead. "
                "Disable it, or set START_HUNT false." % " and ".join(bad))


def _reset(api, mp, now):
    """Everything that is per-destination. Called on every map change."""
    global _map, _target, _issued, _arrived, _skill_done, _hunt_on
    global _resumes, _since_map, _cancel_said, _last_travel, _last_combat
    global _transform_done
    # Leaving a hunt map: stop the native drive, or it keeps steering while we
    # are trying to walk somewhere.
    if STOP_HUNT_ON_LEAVE and _hunt_on and mp not in HUNT_MAPS:
        _hunt(api, False)
    _map, _target = mp, ROUTE.get(mp)
    _issued = _arrived = _skill_done = _hunt_on = False
    _transform_done = False
    _resumes, _since_map, _cancel_said = 0, now, False
    global _dropped, _drop_said
    _dropped, _drop_said = 0, 0.0
    # Per-DESTINATION, so they belong here rather than in on_load: the re-issue
    # count is reported on arrival and would otherwise accumulate across the
    # whole route, and a steering handoff must not carry over to the next leg.
    global _repath_at, _repaths, _repath_failed, _repath_from, _repath_check, _steer_off, _steer_handed
    _repath_at, _repaths, _repath_failed = 0.0, 0, False
    _repath_from, _repath_check = None, 0.0
    _steer_stop(api)
    _steer_off, _steer_handed = False, False
    _last_travel = _last_combat = now
    if _target:
        api.log("automonkey: on %s -> walking to %.0f,%.0f (in-game %.0f,%.0f)"
                % (mp, _target[0], _target[1], _target[0] / 100.0,
                   _target[1] / 100.0))
    else:
        api.log("automonkey: on %s - no destination for this map, standing by"
                % mp)


def on_load(api):
    global _me, _warned_ah2, _map, _steer_off, _steer_handed
    global _repath_at, _repaths, _repath_failed, _repath_from, _repath_check
    _map = None                            # force a fresh decision
    # A held direction must never survive a reload. The keys are engine state,
    # not module state - re-exec'ing this file does not release them.
    _steer_stop(api)
    _steer_off, _steer_handed = False, False
    _repath_at, _repaths, _repath_failed = 0.0, 0, False
    _repath_from, _repath_check = None, 0.0
    _warned_ah2 = False
    try:
        _me = api.player.name() or ""
    except Exception:
        _me = ""
    if FORCE_BTN:
        try:
            _probe_dialog_button(api)
        except Exception as e:
            api.log("automonkey: button probe failed (%s)" % type(e).__name__)
    if FORCE_WINDOWS:
        try:
            _find_open_windows(api)
        except Exception as e:
            api.log("automonkey: window probe failed (%s)" % type(e).__name__)
    if FORCE_YES:
        try:
            _click_yes(api)
        except Exception as e:
            api.log("automonkey: click-yes failed (%s)" % type(e).__name__)
    if FORCE_CANCEL:
        try:
            _force_cancel_now(api)
        except Exception as e:
            api.log("automonkey: force cancel failed (%s)" % type(e).__name__)
    if FORCE_PROBE:
        try:
            _probe_affect_ui(api)
        except Exception as e:
            api.log("automonkey: affect probe failed (%s)" % type(e).__name__)
    api.log("automonkey: loaded - %d route(s), arrive within %.0f, skill key %s, "
            "hunt %s" % (len(ROUTE), ARRIVE, SKILL_KEY,
                         "on arrival" if START_HUNT else "off"))


def on_unload(api):
    # A HELD WALK KEY MUST NOT SURVIVE THE MOD. Unlike the hunt below, this is
    # not game state the player chose - it is a key we pressed, and leaving it
    # down would walk the character away with nothing driving it and nothing
    # left to explain why.
    _steer_stop(api)
    # DELIBERATELY does not stop the hunt.
    #
    # It used to, and that was harmful: every reload of this mod - of which
    # there are many while it is being written - sent "/auto_hunt end" and
    # switched off a hunt the player had started by hand. A mod being replaced
    # is not a reason to change the game's state, and the next load re-decides
    # everything from the map anyway.
    api.log("automonkey: unloaded")


def on_update(api, dt):
    global _me, _issued, _arrived, _skill_done, _hunt_on, _resumes
    global _last_travel, _last_combat, _cancel_said, _warned_ah2
    global _transform_done, _cancel_at, _cancel_tries, _verify_at
    global _dropped, _drop_said, _steer_handed
    global _repath_at, _repaths, _repath_failed, _repath_from, _repath_check

    if not api.in_game():
        return
    now = api.now()

    if _cancel_at:
        _cancel_answer(api, now)
    if _verify_at and now >= _verify_at:
        _verify_at = 0.0
        still = _is_transformed(api)
        api.log("automonkey: after answering, transformation is %s"
                % ("STILL ACTIVE" if still else
                   "GONE" if still is False else "unreadable"))
        # STILL ACTIVE means we answered the WRONG window - the whole failure
        # mode above. Previously this was reported and then dropped, so one bad
        # click ended the attempt for the map. Go round again; _answerable's
        # ranking makes the retry likely to land somewhere better, and
        # CANCEL_TRIES still bounds it.
        if still and _cancel_tries < CANCEL_TRIES:
            _cancel_at = now + CANCEL_ANSWER_S
            api.log("automonkey: retrying the transformation dialog (%d/%d)"
                    % (_cancel_tries + 1, CANCEL_TRIES))

    if not _me:
        try:
            _me = api.player.name() or ""
        except Exception:
            return
        if not _me:
            return
    if ONLY_CHARACTER and _me != ONLY_CHARACTER:
        return                              # silent: wrong character, not an error

    if not _warned_ah2 and START_HUNT:
        _warned_ah2 = True
        _check_hunt_conflict(api)
    mp = api.map_name() or ""
    if mp != _map:
        _reset(api, mp, now)
        return                              # let the zone settle before acting
    if not _target:
        return
    if now - _since_map < SETTLE_S:
        return

    pos = api.player.position()
    if not pos:
        return
    here = (pos[0], pos[1])

    # Remember combat and movement continuously - the discriminator below needs
    # to know what was true just BEFORE the route died, not after.
    if _field(api, "auto_attack_vid", 0):
        _last_combat = now
    trav = _travelling(api)
    if trav:
        _last_travel = now

    # ENTERING a hunt map: cast first, walk second. The skill is wanted for the
    # whole run, not just from the moment we happen to reach the spot, and the
    # walk takes ~20s.
    if USE_SKILL and not _skill_done and _map in HUNT_MAPS:
        _skill_done = True
        _use_skill(api)
        return                              # one action per tick, in order

    # ENTERING a transform map: drop the transformation before walking. It is
    # applied by the teleport out of the dungeon and it changes how the
    # character moves, so it has to go before the route is issued rather than
    # after.
    if CANCEL_TRANSFORM and not _transform_done and _map in TRANSFORM_MAPS:
        _transform_done = True
        _cancel_transform(api)
        return

    ignoring = _map in IGNORE_MOBS_MAPS and not _arrived

    # Refuse every fight while walking here. Dropping the engagement leaves the
    # client with nothing to turn and swing at, so the route it would otherwise
    # cancel simply carries on. nocollide handles the bodies in the way.
    if ignoring and _field(api, "auto_attack_vid", 0):
        tn = api.natives.module() if hasattr(api, "natives") else None
        fn = getattr(tn, "drop_engagement", None) if tn is not None else None
        if callable(fn):
            try:
                fn()
                _dropped += 1
                # Count it as combat even though we just cancelled it - the
                # resume logic below keys off "was something attacking us", and
                # dropping the target would otherwise hide the very thing that
                # justifies resuming.
                _last_combat = now
                if VERBOSE and now - _drop_said > 10.0:
                    _drop_said = now
                    api.log("automonkey: walking through the pack - %d "
                            "engagement(s) refused so far" % _dropped)
            except Exception:
                pass

    if not _arrived:
        if _d2(here, _target) <= ARRIVE * ARRIVE:
            _arrived = True
            _steer_stop(api)            # never arrive with a key still held
            api.log("automonkey: arrived at %.0f,%.0f on %s%s%s"
                    % (_target[0], _target[1], _map,
                       " (refused %d engagement(s), %d resume(s) on the way)"
                       % (_dropped, _resumes) if _dropped or _resumes else "",
                       " [%d re-issue(s)]" % _repaths if _repaths else ""))
            return
        # RE-ISSUE THE DESTINATION. Deliberately ahead of everything else and
        # deliberately returning: the stall/resume machinery below exists to
        # notice a dead route and start a new one, and re-issuing continuously
        # IS that, done on a fixed clock instead of after a 1.5s diagnosis.
        if _map in REPATH_MAPS or MOVE_METHOD == "repath":
            if not _issued:
                _issued = True
                api.log("automonkey: walking to %.0f,%.0f - re-issuing "
                        "whenever it stops making progress (<%.0f units in "
                        "%.2fs), like clicking the spot again (%s)"
                        % (_target[0], _target[1], REPATH_PROGRESS,
                           REPATH_CHECK_S,
                           "%s is in REPATH_MAPS" % _map
                           if _map in REPATH_MAPS else "MOVE_METHOD"))
            # Progress test. Re-issuing is a STOP followed by a start, so it
            # must only ever happen to a walk that is not going anywhere.
            if _repath_from is None:
                _repath_from, _repath_check = here, now + REPATH_CHECK_S
            elif now >= _repath_check:
                moved = _d2(_repath_from, here) ** 0.5
                _repath_from, _repath_check = here, now + REPATH_CHECK_S
                if moved < REPATH_PROGRESS and now >= _repath_at:
                    _repath_at = now + REPATH_EVERY_S
                    _repaths += 1
                    if (not api.move_to(_target[0], _target[1])
                            and not _repath_failed):
                        _repath_failed = True
                        api.log("automonkey: move_to FAILED while re-issuing - "
                                "AutoMoveToPosition is unavailable on this build")
            # We are driving, so the watchdog below must not call this leg dead
            # and start a second mover underneath us.
            _last_travel = now
            return

        # STEERING OWNS THE WALK while it can. Before the route, not after:
        # issuing an AutoMoveToPosition first and steering second would put two
        # movers on one character, which is the fight nocollide and the fetch
        # hold already exist to avoid.
        if MOVE_METHOD == "steer" and _steer(api, now, here, _target):
            if not _issued:
                _issued = True
                api.log("automonkey: steering to %.0f,%.0f - holding a walk "
                        "direction and re-aiming every %.2fs, no route to get "
                        "confused" % (_target[0], _target[1], STEER_EVERY_S))
            # Steering IS travelling. Without this the resume watchdog below
            # sees no auto-move state, calls the leg dead and starts issuing
            # routes underneath us.
            _last_travel = now
            return

        # Steering stood down mid-leg (geometry, or no camera). Hand the leg
        # over cleanly: the route has to be ISSUED, and _issued is still set
        # from the steering announcement.
        if MOVE_METHOD == "steer" and _steer_off and not _steer_handed:
            _steer_handed = True
            _issued = False

        if not _issued:
            _issued = True
            ok = api.move_to(_target[0], _target[1])
            api.log("automonkey: walking to %.0f,%.0f%s"
                    % (_target[0], _target[1], "" if ok else " - move_to FAILED"))
            return
        # The walk was issued. Has it died?
        if trav is None or trav or (now - _last_travel) < STALL_S:
            return                          # still going, or too soon to judge
        # On the ignore maps there is no "you cancelled it" case to respect -
        # the character is unattended in a dungeon, and every stop there is the
        # mobs. Always resume.
        # ...unless WE are the reason it stopped. Opening the transformation
        # dialog is a modal window, and a modal window kills the route - so a
        # route that died while our own dialog handling is in flight is our
        # doing, not the player's. Measured: one wrong click on 2026-08-25
        # stalled the c1 leg for the rest of the map (4m55s against 2m36s)
        # because it was filed as a manual cancel and never resumed.
        ours = bool(_cancel_at or _verify_at)
        if not ignoring and not ours and now - _last_combat > COMBAT_MEMORY_S:
            # Nothing was attacking us when it stopped -> you cancelled it.
            if not _cancel_said:
                _cancel_said = True
                api.log("automonkey: the walk stopped with nothing attacking - "
                        "treating that as YOUR cancel and leaving it. It "
                        "resumes on the next map change.")
            return
        cap = IGNORE_MAX_RESUMES if ignoring else MAX_RESUMES
        gap = IGNORE_RESUME_EVERY_S if ignoring else RESUME_EVERY_S
        if _resumes >= cap:
            return
        if now - _last_travel < gap:
            return
        _resumes += 1
        _last_travel = now                  # do not re-fire until it has moved
        ok = api.move_to(_target[0], _target[1])
        # Quiet on the ignore maps: it resumes constantly there by design, and a
        # line per resume would bury everything else in the log.
        if VERBOSE and (not ignoring or _resumes % 10 == 1):
            api.log("automonkey: combat interrupted the walk - resuming "
                    "(%d/%d)%s" % (_resumes, cap, "" if ok else " FAILED"))
        return

    # Arrived. The skill already went off on entry; all that is left is the hunt.
    if _map not in HUNT_MAPS:
        return
    if START_HUNT and not _hunt_on:
        _hunt_on = True
        _hunt(api, True)
        if DIAG:
            _diagnose(api)
