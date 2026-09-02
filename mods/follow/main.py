"""follow - keep a second character near your main, across channels.

Every client running this publishes its own state to <_patcher>\\link\\<name>.json
(name, channel, position, heartbeat). A client whose character is NOT the main
also reads the main's record and walks toward it using the client's own pathing.

Set MAIN below to your main character's name. The same file runs unmodified in
both clients: whichever one is the main just publishes and does nothing else.

When FOLLOW_CHANNEL is on, a channel split also triggers an automatic swap onto
the main's channel (via the chanswap mod); otherwise it is only reported.
"""



CAPABILITIES = ["read", "move", "link"]

# ---- configuration --------------------------------------------------------
# REQUIRED, and it belongs in config.json, not here. Set it from the F10 UI
# ("Main character") or as follow.MAIN. Two clients share one mods folder, so a
# name baked in here would apply to both and would also ship in the
# distributable. This default stayed set to a real character locally while the
# deployed copy was "", which is how a follower sat there reporting
# MAIN='<UNSET>' while the source on disk looked correctly configured.
MAIN = ""               # your main character's name, exactly as in game
CAST_DIST = 600.0       # inside this, stop moving entirely - we can cast
FOLLOW_DIST = 900.0     # outside this, start walking again (hysteresis band)
REPATH_MOVE = 300.0     # minimum goal shift before re-issuing a move
REPATH_FRAC = 0.25      # ...or this fraction of the distance still to walk,
                        # whichever is larger. A running main shifts more than
                        # REPATH_MOVE every single tick, so at long range the
                        # old rule re-issued constantly - and every
                        # AutoMoveToPosition restarts pathing from scratch, so
                        # the follower never finished a leg and wedged on the
                        # first corner. Far away, a 300-unit shift barely
                        # changes our heading and is not worth re-aiming for.
ARRIVE_DIST = 250.0     # committed waypoint counts as reached within this
STUCK_S = 4.0           # ... or if we have not made progress for this long
PUBLISH_S = 0.5         # how often to publish our own state
# When the main is on a different channel, swap to join it. The main publishes
# its channel in the link record, so the target is already known; the request
# rides the TRIARCH_CHAN_REQUEST env channel that the chanswap mod consumes
# (mods run in isolated namespaces and cannot call each other directly, so this
# is the same handshake autohunt2's channel loop uses). Needs chanswap loaded;
# without it this is a no-op. Off by default - opt in. Channels 1..5 only.
FOLLOW_CHANNEL = False
CHAN_SETTLE_S = 15.0    # after requesting a swap, wait this long before another,
                        # so the change + load can finish instead of re-firing
CHAN_FOLLOW_DELAY = 30.0 # the main must HOLD a new channel this long before we
                         # swap to join it; a hop during the wait restarts the
                         # timer, so brief channel-bouncing no longer drags us along
TARGET_MAIN = True      # keep the main selected so buffs land on it
RETARGET_S = 3.0        # don't re-issue SetTarget faster than this
BUFF_SLOTS = [0, 1, 2]  # hotbar keys 1/2/3 - your three buffs
# Affect indices of your three buffs, measured with the buffwatch mod:
# 94 and 95 run 70s, 96 runs 85s. GetAffectData(idx, 0) is seconds remaining.
BUFF_AFFECTS = [94, 95, 96]
BUFF_REFRESH_AT = 15.0  # re-cast when the shortest one drops below this
BUFF_PERIOD = 55.0      # fallback only, used when the main publishes no timers
BUFF_GAP = 2.5          # seconds between individual casts

_last_pub = 0.0
_last_state = None
_warned_channel = None
_chan_next = 0.0        # no channel-follow swap request before this
_pending_ch = None      # main's channel we're waiting on to settle (or None)
_pending_since = 0.0    # when the main first appeared on _pending_ch
_dest = None            # last destination we actually commanded
_last_progress = 0.0    # when we last got measurably closer
_last_dist = None
_targeted_vid = 0
_last_target_try = 0.0
_had_main = False
_buff_cycle = 0.0       # when the current cycle started
_buff_i = None          # index into BUFF_SLOTS while a cycle is running
_buff_next = 0.0


def _me(api):
    return api.player.name() or ""


# MAIN is deliberately NOT auto-detected. "Follow the other character I can
# see" makes both clients follow each other: the main walks to the alt while
# the alt walks to the main, and they chase each other around the map. Caught
# in simulation. An explicit name gives each client an unambiguous role, and
# the same file can then run unmodified in both.


def on_load(api):
    api.log("follow: loaded (api v%s) MAIN=%r" % (api.VERSION, MAIN or "<UNSET>"))


def on_unload(api):
    api.log("follow: unloaded")


def _publish(api, name, pos, ch):
    api.link.publish(name, {
        "name": name,
        "channel": ch,
        "map": api.map_name(),
        "affects": api.active_affects(BUFF_AFFECTS),
        "x": pos[0], "y": pos[1], "z": pos[2],
        "ts": api.now(),
    })


def _note_channel(api, mine, theirs, main_name):
    """Report a channel split once - but keep walking.

    Following continues across channels ON PURPOSE. Map coordinates are the
    same in every channel, so the alt can shadow the main's position while on
    a different channel; then a manual channel change puts both characters in
    the same spot with no walking afterwards. Buffing is still suppressed,
    since the main is not a real instance here."""
    global _warned_channel
    key = (mine, theirs)
    if _warned_channel == key:
        return
    _warned_channel = key
    msg = ("follow: %s is on CH%s, you are on CH%s - shadowing position, "
           "no buffs until you rejoin" % (main_name, theirs, mine))
    api.log(msg)
    api.chat(msg)


def _follow_channel(api, now, their_ch, main_name):
    """Ask chanswap to move us onto the main's channel.

    The main's channel comes straight from the link record. We swap only once the
    main has HELD a channel for CHAN_FOLLOW_DELAY: the main bouncing between
    channels used to drag us through a swap on every hop, so now each hop restarts
    the settle timer and we join only after it stops moving.

    The request rides os.environ - mods run in isolated namespaces and cannot call
    chanswap directly - and chanswap honours it once and clears it. Channels 1..5
    only; anything else is left alone."""
    global _chan_next, _pending_ch, _pending_since
    try:
        ch = int(their_ch)
    except Exception:
        _pending_ch = None
        return
    if not (1 <= ch <= 5):
        _pending_ch = None
        return

    # A different target than we were counting on (first sight, or the main
    # hopped again) -> (re)start the settle timer and wait it out.
    if ch != _pending_ch:
        _pending_ch = ch
        _pending_since = now
        api.log("follow: %s on CH%d - waiting %.0fs for it to settle"
                % (main_name, ch, CHAN_FOLLOW_DELAY))
        return
    if now - _pending_since < CHAN_FOLLOW_DELAY:
        return                              # still settling on this channel
    if now < _chan_next:
        return                              # a swap just fired; let it load

    import os
    try:
        os.environ["TRIARCH_CHAN_REQUEST"] = str(ch)
    except Exception as e:
        api.log("follow: could not post channel request %s: %s"
                % (type(e).__name__, e))
        return
    _chan_next = now + CHAN_SETTLE_S
    _pending_ch = None
    api.log("follow: %s settled on CH%d for %.0fs - requesting a swap to join"
            % (main_name, ch, CHAN_FOLLOW_DELAY))


def _needs_buff(rec, api):
    """(bool, why) from the MAIN's published timers.

    The shaman cannot read another character's affects, and since the update it
    no longer gets a mirrored copy when buffing someone else - so the timers
    come from the main's own client over the link. Falls back to a fixed period
    only when the main publishes nothing (old build, or not yet in world)."""
    aff = rec.get("affects")
    if aff is None:
        return None, "no timers published"
    missing = [i for i in BUFF_AFFECTS if str(i) not in aff]
    if missing:
        return True, "missing %s" % missing
    low = {i: aff[str(i)] for i in BUFF_AFFECTS if aff[str(i)] <= BUFF_REFRESH_AT}
    if low:
        return True, "expiring %r" % low
    return False, "ok %r" % ({i: aff[str(i)] for i in BUFF_AFFECTS},)


def _buffs(api, now, main_name, main_vid, rec):
    """Run a buff cycle: press each hotbar slot in turn, spaced by BUFF_GAP.

    Only fires while in casting range and with the main actually selected -
    casting at a mob would waste the buff. Timing is a fixed period for now:
    the affect API takes (idx, sub) but returned 0 with nothing active, so the
    real remaining-time shape still needs a capture while buffed.

    Since the update no longer mirrors buffs onto the caster, the shaman cannot
    read these timers locally at all - they will have to come from the main
    over the link, which is why the record already carries a slot for them."""
    global _buff_cycle, _buff_i, _buff_next

    if not BUFF_PERIOD or not BUFF_SLOTS:
        return
    if not main_vid or api.target.vid() != main_vid:
        return                              # not selected - do not waste a cast

    if _buff_i is None:
        need, why = _needs_buff(rec, api)
        if need is None:
            if now - _buff_cycle < BUFF_PERIOD:   # no timers: fall back to a period
                return
        elif not need:
            return
        elif now - _buff_cycle < BUFF_GAP * (len(BUFF_SLOTS) + 2):
            return                                # a cycle just ran; let it land
        _buff_cycle = now
        api.log("follow: buffing %s (%s)" % (main_name, why))
        _buff_i = 0
        _buff_next = now

    if now < _buff_next:
        return
    slot = BUFF_SLOTS[_buff_i]
    if api.use_quickslot(slot):
        api.log("follow: cast slot %d (key %d)" % (slot, slot + 1))
    else:
        api.log("follow: quickslot %d unavailable" % slot)
    _buff_i += 1
    _buff_next = now + BUFF_GAP
    if _buff_i >= len(BUFF_SLOTS):
        _buff_i = None


def on_update(api, dt):
    global _last_pub, _last_state, _warned_channel, _pending_ch
    global _dest, _last_progress, _last_dist, _targeted_vid, _last_target_try
    global _had_main, _buff_cycle

    if not api.in_game():
        return

    name = _me(api)
    pos = api.player.position()
    if not name or pos is None:
        return
    ch = api.channel()

    now = api.now()
    if now - _last_pub >= PUBLISH_S:
        _last_pub = now
        _publish(api, name, pos, ch)

    if not MAIN:
        if _last_state != "unconfigured":
            _last_state = "unconfigured"
            api.log("follow: MAIN is not set - publishing only. Set the main "
                    "character's name in the F10 UI, or as follow.MAIN in "
                    "mods/config.json")
        return
    main_name = MAIN
    if main_name == name:
        return                              # we are the main: publish only

    rec = api.link.read(main_name)
    if rec is None:
        if _last_state != "waiting":
            _last_state = "waiting"
            api.log("follow: no fresh record for %r" % main_name)
        return

    # Different MAP means the coordinates are somewhere else entirely - do not
    # chase them. Different CHANNEL is fine: same map, same coordinates.
    their_map = rec.get("map", "")
    my_map = api.map_name()
    if their_map and my_map and their_map != my_map:
        if _last_state != "othermap":
            _last_state = "othermap"
            api.log("follow: %s is on map %s, you are on %s - waiting"
                    % (main_name, their_map, my_map))
        return

    their_ch = rec.get("channel", 0)
    same_channel = not (their_ch and ch and their_ch != ch)
    if same_channel:
        _warned_channel = None
        _pending_ch = None                  # on the main's channel; nothing pending
    else:
        _note_channel(api, ch, their_ch, main_name)
        if FOLLOW_CHANNEL:
            _follow_channel(api, now, their_ch, main_name)

    try:
        tx, ty = float(rec["x"]), float(rec["y"])
    except Exception:
        return

    dx, dy = tx - pos[0], ty - pos[1]
    dist = (dx * dx + dy * dy) ** 0.5

    # Keep the main selected whenever it is a real instance in THIS client.
    # VIDs are per-client, so resolve by name rather than trusting the record.
    #
    # Rate-limited: SetTarget does not always stick (auto-hunt re-targets mobs),
    # and retrying every tick produced a log line per tick and hammered the
    # binding. Try at most every RETARGET_S and report only on change.
    # On another channel the main is not an instance here, so vid_of returns 0
    # and both targeting and buffing are skipped automatically.
    main_vid = api.vid_of(main_name) if (TARGET_MAIN and same_channel) else 0

    # Re-acquiring the main - after a relog, a channel change, or simply coming
    # back into view - gives it a NEW vid and means its buffs are unknown. Force
    # a cycle rather than waiting out the remainder of BUFF_PERIOD.
    if main_vid and not _had_main:
        _had_main = True
        _buff_cycle = 0.0
        # Say which mode we are actually in. This read "- buffing" unconditionally,
        # which is wrong for a non-caster follower with BUFF_SLOTS cleared and
        # made it look like buffs were still being attempted when they were not.
        api.log("follow: acquired %s (vid %d) - %s"
                % (main_name, main_vid,
                   "buffing" if BUFF_SLOTS else "following only (buffs off)"))
    elif not main_vid and _had_main:
        _had_main = False
    if main_vid and api.target.vid() != main_vid and now - _last_target_try >= RETARGET_S:
        _last_target_try = now
        api.set_target(main_vid)
        if api.target.vid() != main_vid:
            # A stale selection (an instance that no longer exists after a
            # relog) is not displaced by SetTarget alone - the old vid stays
            # selected and reads back as name "None" with no position. Clear
            # it, select the world instance, then set the target.
            api.clear_target()
            api.select(main_vid)
            api.set_target(main_vid)
        if api.target.vid() == main_vid:
            if _targeted_vid != main_vid:
                _targeted_vid = main_vid
                api.log("follow: targeted %s (vid %d)" % (main_name, main_vid))
        elif _targeted_vid != -1:
            _targeted_vid = -1
            api.log("follow: SetTarget(%d) did not stick - selection is %d"
                    % (main_vid, api.target.vid()))

    # Inside casting range: hold position. Re-issuing a move here is what made
    # the character stutter - every AutoMoveToPosition restarts pathing, so a
    # command per second reads as "walk, stop, walk, stop".
    if dist <= CAST_DIST:
        if _dest is not None:
            _dest = None
        if _last_state != "close":
            _last_state = "close"
            api.log("follow: in range of %s (%.0f) - holding" % (main_name, dist))
        _buffs(api, now, main_name, main_vid, rec)
        return

    if _dest is not None and dist < FOLLOW_DIST:
        pass                                # already walking, still in band
    elif dist < FOLLOW_DIST:
        return                              # hysteresis: do not start yet

    # Progress watchdog: if we are not getting closer, the path is blocked and
    # the goal needs re-issuing even though it has not moved.
    if _last_dist is None or dist < _last_dist - 20.0:
        _last_dist = dist
        _last_progress = now
    stuck = (now - _last_progress) > STUCK_S

    if _dest is not None and not stuck:
        gx, gy = _dest
        shift = ((tx - gx) ** 2 + (ty - gy) ** 2) ** 0.5
        left = ((pos[0] - gx) ** 2 + (pos[1] - gy) ** 2) ** 0.5
        # Commit to the leg we already started: only re-aim once the goal has
        # moved enough to matter at this range, or once we have actually got
        # to where we were heading. Then correct from there.
        need = REPATH_MOVE
        if dist > FOLLOW_DIST:
            need = max(need, dist * REPATH_FRAC)
        if shift < need and left > ARRIVE_DIST:
            return

    if api.move_to(tx, ty):
        _dest = (tx, ty)
        _last_progress = now
        _last_dist = dist
        if _last_state != "moving":
            _last_state = "moving"
            api.log("follow: walking to %s (%.0f away)%s"
                    % (main_name, dist, " [unstick]" if stuck else ""))
    else:
        api.log("follow: move_to unavailable")
