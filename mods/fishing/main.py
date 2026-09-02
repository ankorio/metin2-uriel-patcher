"""fishing - automate the rod-fishing LOOP, armed/disarmed with F5.

The USER handles positioning: stand at fishable water with a rod equipped, then
press F5 to ARM. The mod casts, waits for the bite, reels, and recasts; press F5
again to DISARM.

How the bite is timed (the hard part): the "!" bite is NATIVE-ONLY - no OnFishing*
Python callback fires when the fish bites, so a pure-Python mod can't see it. The
stub watches the GC_FISHING packets on the Recv path and exposes them via
`api.fishing_poll() -> (seq, sub, bite)`:
    seq  ++ on every GC_FISHING packet
    sub  last subheader: 0 start, 1 stop, 2 BITE, 3 catch-ok, 4 catch-fail, 5 notify
    bite ++ on every bite (subheader 2)
`bite` is monotonic, so we reel the instant it grows and can never miss a bite.
Both the CAST and the REEL go through the FISHING SKILL: `ClickSkillSlot` on the
skill's slot is the client's own skill-press path (what the hotbar key calls) -
no injected input. Pressed while idle it casts; pressed while the line is out it
reels. The injected-Space path (`api.key_event` = SendInput) is kept only as the
legacy fallback (CAST_METHOD / REEL_METHOD = "space"): on the 1.0.11 client it
never reached the game - 0/16 reels on 2026-08-26, every fail landing at
bite+6.0s, which is the server's bite window expiring, not a judgement of a reel.

Needs a stub with the `fishing_poll` native (rebuild + client restart). Without
it the mod warns once and stays idle.
"""

import sys

CAPABILITIES = ["read", "control"]

# --- tuning (config-overridable) ---
# PATIENCE is the whole game here: once the bobber is in the water, DO NOT touch
# the attack key until a real bite - a stray tap reels/cancels the cast and
# desyncs the session so the server stops sending bites ("loops without signal").
# So we cast ONCE and wait up to FISH_TIMEOUT for the bite, doing nothing else.
ARM_KEY = 63             # app.DIK_F5 (0x3F); read from `app` at runtime, this is the fallback
# CAST via the FISHING SKILL, not injected input. The skill's slot is resolved
# from its id at runtime (GetSkillSlotIndex, validated with GetSkillIndex, else a
# slot scan) and the cast is `ClickSkillSlot(slot)` - the client's own skill-press
# path, exactly what pressing the skill's hotbar key does. The REEL is the same
# press while the line is out.
CAST_METHOD = "skill"    # "skill" = start via the fishing skill; "space" = legacy Space tap
REEL_METHOD = "skill"    # "skill" = reel via the same skill press; "space" = legacy Space tap
FISH_SKILL_ID = 123      # skill id of the fishing skill in the skill table
FISH_SKILL_SLOT = -1     # pin the skill-window slot directly; -1 = resolve from FISH_SKILL_ID
SKILL_SCAN_MAX = 160     # skill slots to scan / accept. MEASURED on the live 1.0.11
                         #   client (skilldump probe): the support skills sit HIGH -
                         #   fishing (123) is slot 102 (101:combo 122, 103:leadership
                         #   121, 104:mining 124) - so 64 would reject the real slot.
# Legacy "space" path only (SetAttackKeyState/SetMobileAttackKeyState put nothing on
# the wire - see failed-experiments.md; the tap is an OS-level SendInput scancode):
HOLD_S = 0.2             # s to hold Space: >= 1 VM frame (~125ms @ 8fps) but under the
                         # ~250ms key-repeat delay, so each cast/reel fires exactly ONCE
                         # (a 0.4s hold auto-repeated -> doubled reels that missed)
DEBUG_KEYS = False       # log every OnKeyDown key (diagnostic; off once working)
# BOLA DE PESCAR: the "fish-info" consumable (vnum 27610) grants affect 208 for 20 min, which
# is what makes the server send the sub=5 fish NOTIFY. We keep it topped up while fishing.
USE_BOLA = True
BOLA_VNUM = 27610
BOLA_AFFECT = 208        # measured: affect index 208, 1200s (20 min)
BOLA_REFRESH = 120.0     # re-use a Bola when the effect drops below this many seconds
BOLA_CHECK_S = 5.0       # how often to check the effect (throttle so we don't double-use)
# AUTO-USE CAUGHT FISH: open the fish we catch - inventory items of the FISH category (item
# type 12: Lucioperca/Carpa/Salmon/...). The bait (small fish 27802, worm 27801) is type 3,
# so this never touches it. Rate-capped so it isn't a packet storm.
USE_FISH = True
FISH_ITEM_TYPE = 12
USEFISH_MAX = 4          # max fish used per scan (caps the item-use rate)
USEFISH_CHECK_S = 2.0    # scan+use interval
MIN_BITE_DELAY = 1.5     # s after a cast to ignore "bites" - too fast to be real, so
                         #   they are late duplicate packets from the PREVIOUS fish
CAST_CONFIRM_S = 3.0     # s to see a START (fishing began) after casting; else recast -
                         #   the cast did not take (not facing water, skill on cooldown,
                         #   or - legacy space cast - SendInput missed)
# CATCH TIMING: the catch is NOT "reel on the bite" - after the "!" you must WAIT a
# fish-specific delay, THEN press space. Reeling instantly ALWAYS misses (that was
# the 0-catch bug). The delay depends on the fish and rod level; one delay targets
# one tier of loot (waiting longer filters out the quick small fish). Baseline table
# (+0..+2 rod), and ROD_ADJUST adds ~0.075s/level for our +5 rod:
#   Minnow 1-2s | Crucian/Mandarin 2s | Carp/GrassCarp/Clam/Items/Dyes 2.5s
#   Salmon/RainbowTrout 3s | Eel 3.5s | Catfish 4s | Tench/Lotus 4.5s
ROD_ADJUST = 0.225       # +5 rod: ~0.075s/level above +2 (3 levels: 3*0.075=0.225). Higher
                         #   rods hold fish longer, so wait a touch more before reeling.
PIPELINE_LAG = 0.30      # subtract the mod's poll+inject lag so the reel LANDS on the mark
                         #   (calibrated: base 2.5 + 0.15 - 0.30 = 2.35, which caught items)
DEFAULT_DELAY = 2.5      # baseline (s) for unknown vnum / no fish-info item / items+clams
                         #   (2500ms tier). This IS the fixed "range we used before" fallback.
# Junk bites (fish_vnum 0) are UNCATCHABLE at any delay (study: 0/11 across 2.0-3.0s), so
# don't waste the reel-delay wait - reel immediately to resolve the line and recast.
# ONLY while the fish-info effect (Bola) is up: without it EVERY bite reports vnum 0,
# and an instant reel on a real fish is a guaranteed miss - then vnum 0 means "unknown"
# and gets DEFAULT_DELAY instead.
SKIP_JUNK = True
JUNK_GRACE = 0.6         # s to wait after a bite for the vnum to resolve before calling it junk
# Per-fish baseline reel delays (s, +0..+2 rod) by fish vnum, from the old-school timing
# table (fish not in the table estimated by size class). Effective reel delay for a fish =
# base + ROD_ADJUST - PIPELINE_LAG. With the fish-info item, we know the fish at bite time
# (api.fishing_poll -> fish_vnum) and time each species; without it, fish_vnum is 0 ->
# DEFAULT_DELAY (the old fixed behaviour).
FISH_BASE = {
    # Tuned from the study (effective delay = base - 0.15). LOCKED optima (n from the 1583-row
    # study): Minnow 1.5s (35%, marginal - sweep 1.3-1.5 next), Zander 2.5s (77%);
    # Mandarin RE-TUNED 1.5s->2.0s (was 24/151=16% at 1.5s, a mispin to Minnow's value;
    # 1.85s eff = 50% and the baseline table says 2s); LargeZander 27805 -> 4.5s (peak
    # 12/24=50%, its ceiling); Carp 27806 -> 2.5s; Salmon 27807 -> 2.5s.
    # BrookTrout 27809 re-centered upward (base 3.65 -> sweep spans 2.5-4.5, its 4.0s hits).
    27802: 1.65, 27803: 2.65, 27804: 2.15, 27805: 4.65, 27806: 2.65, 27807: 2.65,
    27808: 2.5,  27809: 3.65, 27810: 3.5,  27811: 3.0,  27812: 3.0,  27813: 2.0,
    27814: 2.0,  27815: 4.5,  27816: 4.0,  27817: 2.0,  27818: 4.5,  27819: 2.5,
    27820: 1.5,  27821: 2.0,  27822: 2.5,  27823: 2.0,
    # Hair dyes with a resolved optimum get their own base + a LOCK (below). The rest of the
    # dyes/loot stay in the ITEM_DELAY sweep (they peak ~1.0-2.0s, where that sweep lives).
    70201: 3.15, 70207: 3.15,   # Bleach -> 3.0s eff (2.5/3.0 both 60%, n=24)
    70205: 3.15, 70208: 3.15,   # BrownDye -> 3.0s eff (3/3 at 3.0, n=14)
    70206: 2.65,                 # BlackDye -> 2.5s eff (4/5 at 2.5, n=11)
}
# The valuable NON-fish loot (all the "Clam / Items / Hair Dyes" tier of the table)
# shares ONE tuning knob. Table says 2-3s; they missed at 2.5, and a +4 rod holds them
# longer, so we're testing the upper end. Clam/Pearls/Map/Pouches (27987-27999) + Hair
# Dyes/Bleach (70201-70208).
ITEM_DELAY = 2.15        # baseline (s) for all fishing ITEMS - the one knob to tune them.
                         #   Study (n~17): items peak at ~2.0s effective (3/8) and miss at
                         #   2.5/3.0+, so 2.15 base (~2.0s eff). Applies to ANY non-fish loot.
FISH_NAME = {
    27802: "Minnow", 27803: "Zander", 27804: "Mandarin", 27805: "LargeZander",
    27806: "Carp", 27807: "Salmon", 27808: "GrassCarp", 27809: "BrookTrout",
    27810: "Eel", 27811: "RainbowTrout", 27812: "RiverTrout", 27813: "Rudd",
    27814: "Perch", 27815: "Tenchi", 27816: "Catfish", 27817: "Loach",
    27818: "LotusFish", 27819: "Sweetfish", 27820: "Smelt", 27821: "Shiri",
    27822: "MirrorCarp", 27823: "Goldfish",
    27987: "Clam", 27988: "TreasureMap", 27989: "MetinCompass", 27990: "StonePiece",
    27991: "WaterStone", 27992: "WhitePearl", 27993: "BluePearl", 27994: "RedPearl",
    27995: "EmptyBottle", 27996: "PoisonBottle", 27997: "VitalityMarble",
    27998: "AlchemyPouch", 27999: "SpiritPouch",
    70201: "Bleach", 70202: "WhiteDye", 70203: "BlondeDye", 70204: "RedDye",
    70205: "BrownDye", 70206: "BlackDye", 70207: "Bleach", 70208: "BrownDye",
}

# BAIT: before each round, bait the rod with a "small fish" (vnum 27802, Spanish
# "Pez Pequeno") if we have one - it works like a Worm but improves the odds of a
# bigger catch. Uses the client's own use-item path (net.SendItemUsePacket) on the
# inventory cell; if we have none, we just cast with whatever's on the rod.
USE_BAIT = True
BAIT_VNUMS = [27802, 27801]   # bait to use, in preference order: small fish (Pez Pequeño,
                              #   better catches) then Worm (basic). Casting with NO bait
                              #   just cancels, so if none are held we PAUSE instead.
BAIT_SETTLE = 0.5        # s to let the bait apply before casting
FAIL_LIMIT = 4           # consecutive casts with no start (or no bait) before pausing
FAIL_BACKOFF = 20.0      # s to pause after too many dead casts (out of bait / bad spot)
MIN_BAIT_GAP = 5.0       # s HARD floor between two bait uses (a round is far longer);
                         #   with the _baited flag, makes a double-bait impossible
INV_CELLS = 180          # main inventory cells to scan (0..179), per selltest

# STUDY: write a dedicated fishing_study.csv (species,delay,wait,result per bite) and, for
# UNKNOWN species only, sweep the reel delay by SWEEP_OFF around their base so the log maps rate vs
# delay per species - the only way to tell a real timing peak from a flat RNG floor. LOCKED
# species always use their tuned delay. Set STUDY_SWEEP=False to lock everything for prod.
STUDY_LOG = True
STUDY_SWEEP = True       # sweep only the UNKNOWN species to map their window; LOCKED species
                         #   (nailed optima) always use their tuned delay - no sacrifice there.
LOCKED_VNUMS = {27802, 27803, 27804,   # Minnow 1.5s(35%), Zander 2.5s(77%), Mandarin 2.0s (was 1.5s=16%, retuned)
                27805, 27806, 27807,   # LargeZander 4.5s, Carp 2.5s, Salmon 2.5s (study-confirmed)
                70201, 70205, 70206, 70207, 70208}  # Bleach 3.0, BrownDye 3.0, BlackDye 2.5
# unknowns (LargeZander, items, rare fish) sweep AROUND their base by these offsets - now that
# each base is a good guess, this concentrates exploration (and catches) near the real window.
SWEEP_OFF = [-1.0, -0.5, 0.0, 0.5, 1.0]
# NO-REEL DETECTION: the server judges the catch when the reel ARRIVES - a result
# 0-1s after our reel is a real judgement (every real-fish result on 2026-08-15 was).
# A FAIL that lands long after our reel (~bite+6s whatever we did) is the server's
# bite window expiring = our reel never reached it. Count those separately and PAUSE
# after a few instead of feeding a bait per round into a dead loop. Junk bites are
# exempt: they sometimes get a late fail even when the reel works.
NOREEL_AFTER_S = 2.0     # a fail this long after our reel was not a judgement of it (used when
                         #   the stub has no cast_seq; with it, "no 0x34 went out" is measured)
NOREEL_LIMIT = 3         # consecutive no-reel fails before pausing (FAIL_BACKOFF)
# AUTOARM: arm by itself AUTOARM_AFTER s after entering the game, no F5 - for a
# character parked at the water. Fires ONCE per load; F5 toggles normally after.
# Per-character PROFILE only; the shared config keeps it off.
AUTOARM = False
AUTOARM_AFTER = 5.0
FISH_TIMEOUT = 55.0      # s to wait for the bite AFTER fishing has started, else recast
                         #   (real bites land in ~20-40s, so 55s is a safe upper bound)
REEL_TIMEOUT = 10.0      # s to wait for a result after reeling before waiting again
RECAST_DELAY = 3.5       # s pause after a result before the next cast. At 2.0 the first
                         #   skill press after a result was ignored ~half the time (the
                         #   catch/fail motion still playing) -> a wasted 3s confirm timeout.

# --- state ---
_armed = False
_state = "idle"          # idle -> waiting -> reeling -> cooldown
_t = 0.0                 # time we entered the current state
_t_cast = 0.0            # time of the last cast (for MIN_BITE_DELAY)
_last_seq = 0
_last_bite = 0
_catches = 0
_misses = 0
_wrapped = None
_orig_key = None
_our_kd = None           # the wrapper WE installed, so _unwrap only undoes our own
_warned_native = False
_t_bola = -999.0         # throttle for the Bola de Pescar effect check
_warned_bola = False     # warned once that we're out of Bolas
_t_usefish = -999.0      # throttle for auto-using caught fish
_hold_until = 0.0        # if >0, release the held attack key at this time
_noreel = 0              # consecutive fails that came too late to be a judgement of our reel
_autoarm_at = 0.0        # AUTOARM: when to arm (0 = not scheduled)
_autoarmed = False       # AUTOARM fired for this load (arms ONCE; F5 then toggles normally)
_fishinfo = False        # the fish-info (Bola) effect is up, so fish_vnum is meaningful
_logged_vid = False      # log our latched VID once
_reel_delay = DEFAULT_DELAY  # the delay chosen for the CURRENT bite (per-fish)
_logged_unknown = set()  # vnums we've already flagged as having no defined timing
_bite_vnum = 0           # fish vnum captured at the current bite (for the study log)
_bite_abs = 0.0          # swept absolute delay for the current bite (>0 = a swept sample)
_bite_t = 0.0            # time of the current bite
_reel_t = 0.0            # time the current reel fired
_reel_seq = 0            # stub cast_seq baselined at our reel: a later increment = the reel's
                         #   0x34 reached the wire (cast_seq counts EVERY outgoing CG_FISHING)
_reel_wire = False       # this bite's reel was seen on the wire
_logged_wire = False     # logged the first wire-confirmed reel once
_sweep_i = 0             # sweep cycle index
_skill_slot = None       # resolved skill-window slot of the fishing skill (cached)
_warned_skill = False    # warned once that the fishing skill was not found
_baited = False          # a small-fish bait is on the rod, not yet consumed by a cast
_last_bait_t = -999.0    # time of the last bait use (hard floor against double-baiting)
_fail_casts = 0          # consecutive casts with no start / no bait (drives the back-off)
_cast_seq = 0            # stub cast counter baselined at our cast (VID-free confirm)


def _m(name):
    x = sys.modules.get(name)
    if x is None:
        try:
            __import__(name)
            x = sys.modules.get(name)
        except Exception:
            x = None
    return x


def _game_window():
    n = _m("m2netm2g")
    if n is None:
        return None
    try:
        return n.GetPhaseWindow(5)          # phase 5 = in-game
    except Exception:
        return None


def _arm_key():
    return getattr(_m("app"), "DIK_F5", ARM_KEY)


def _fish_name(vnum):
    if vnum in FISH_NAME:
        return FISH_NAME[vnum]
    if not vnum:
        return "junk" if _fishinfo else "unknown(no fish-info)"
    return "vnum#%d" % vnum


def _use_caught_fish(api, now):
    """Auto-use (open) caught fish - inventory items whose category is FISH (item type 12).
    Bait (small fish/worm) is type 3, so it is never touched. Rate-capped."""
    global _t_usefish
    if not USE_FISH or now - _t_usefish < USEFISH_CHECK_S:
        return
    _t_usefish = now
    it = getattr(getattr(api, "raw", None), "item", None)
    p = _m("playerm2g2")
    net = _m("m2netm2g")
    if it is None or p is None or net is None:
        return
    used = 0
    for c in range(0, INV_CELLS):
        if used >= USEFISH_MAX:
            break
        try:
            v = int(p.GetItemIndex(c))
            cnt = int(p.GetItemCount(c))
            if not v or cnt <= 0:
                continue
            it.SelectItem(v)
            if int(it.GetItemType()) != FISH_ITEM_TYPE:
                continue
            for _ in range(min(cnt, USEFISH_MAX - used)):
                net.SendItemUsePacket(c)
                used += 1
        except Exception:
            continue
    if used:
        api.log("fishing: used %d caught fish" % used)


def _find_cell(api, vnum):
    """Inventory cell holding `vnum` (count>0), or None. Read-only."""
    p = _m("playerm2g2")
    if p is None:
        return None
    try:
        for c in range(0, INV_CELLS):
            if int(p.GetItemIndex(c)) == vnum and int(p.GetItemCount(c)) > 0:
                return c
    except Exception:
        pass
    return None


def _bait_cell(api):
    """Cell of the preferred bait (small fish, then worm), or None."""
    for want in BAIT_VNUMS:
        c = _find_cell(api, want)
        if c is not None:
            return c
    return None


def _maintain_bola(api, now):
    """Keep the Bola de Pescar effect (affect BOLA_AFFECT) active while fishing - it's what
    makes the server send the sub=5 fish NOTIFY. Re-use a Bola (BOLA_VNUM) when it runs low."""
    global _t_bola, _warned_bola, _fishinfo
    if not USE_BOLA or now - _t_bola < BOLA_CHECK_S:
        return
    _t_bola = now
    try:
        left = api.affect_seconds(BOLA_AFFECT)
    except Exception:
        return
    was, _fishinfo = _fishinfo, left > 0
    if was != _fishinfo:
        api.log("fishing: fish-info effect %s - %s" % (
            "ON" if _fishinfo else "OFF",
            "per-species timing + junk skip" if _fishinfo else "DEFAULT_DELAY for every bite"))
    if left >= BOLA_REFRESH:
        return
    cell = _find_cell(api, BOLA_VNUM)
    if cell is None:
        if not _warned_bola:
            api.log("fishing: no Bola de Pescar in bag - fish-info (per-species timing) will lapse")
            _warned_bola = True
        return
    _warned_bola = False
    net = _m("m2netm2g")
    try:
        net.SendItemUsePacket(int(cell))
        api.log("fishing: used Bola de Pescar (fish-info %s, was %ds left)"
                % ("ON" if not left else "refresh", left))
    except Exception as e:
        api.log("fishing: Bola use failed: %s" % e)


def _use_bait(api):
    """Use one small fish as bait via the client's own use-item packet. True if used."""
    cell = _bait_cell(api)
    if cell is None:
        return False
    net = _m("m2netm2g")
    try:
        net.SendItemUsePacket(int(cell))
        return True
    except Exception as e:
        api.log("fishing: bait use failed: %s" % e)
        return False


def _next_off():
    """Next reel-delay sweep offset, cycled evenly."""
    global _sweep_i
    o = SWEEP_OFF[_sweep_i % len(SWEEP_OFF)]
    _sweep_i += 1
    return o


def _reel_delay_for(api, vnum):
    """(reel_delay, swept) for this bite. LOCKED species (and sweep-off) use the tuned base;
    unknowns sweep base+offset to explore. swept>0 marks a swept sample."""
    base = _delay_for(api, vnum)
    if STUDY_SWEEP and vnum and vnum not in LOCKED_VNUMS:
        d = max(0.3, base + _next_off())
        return d, d
    return base, 0.0


def _study_log(api, vnum, name, delay, wait, result):
    """Append one row to fishing_study.csv (in the mods dir). Best-effort."""
    if not STUDY_LOG:
        return
    try:
        import os
        path = os.path.join(os.path.dirname(api.config_path()), "fishing_study.csv")
        new = not os.path.exists(path)
        f = open(path, "a")
        try:
            if new:
                f.write("t,vnum,name,delay,wait,result\n")
            f.write("%.1f,%d,%s,%.2f,%.2f,%s\n" % (api.now(), vnum, name, delay, wait, result))
        finally:
            f.close()
    except Exception:
        pass


def _delay_for(api, vnum):
    """Reel delay for a fish vnum: table base + rod adj - pipeline lag. Unknown vnums
    (fish not in the table, or ITEMS/clams) fall back to DEFAULT_DELAY and are logged
    ONCE so we can read the vnum and define its proper timing."""
    global _logged_unknown
    if vnum in FISH_BASE:
        base = FISH_BASE[vnum]
    elif vnum:                                 # ANY non-fish, non-junk vnum is loot -> item tier
        base = ITEM_DELAY                       # (dyes, clam, pearls, rings, Sage King's, ...)
        if vnum not in _logged_unknown:
            _logged_unknown.add(vnum)
            api.log("fishing: item vnum %d -> item delay %.2fs"
                    % (vnum, max(0.3, base + ROD_ADJUST - PIPELINE_LAG)))
    else:
        base = DEFAULT_DELAY                    # vnum 0 = junk (skipped in the biting state)
    return max(0.3, base + ROD_ADJUST - PIPELINE_LAG)


# ---- in-game toast (client-side only, no network) ----
def _toast(api, msg):
    c = _m("chat")
    try:
        if c and hasattr(c, "AppendChat"):
            t = getattr(c, "CHAT_TYPE_NOTICE", getattr(c, "CHAT_TYPE_INFO", 1))
            c.AppendChat(t, msg)
            return
    except Exception:
        pass
    try:
        api.chat(msg)
    except Exception:
        pass


# ---- fishing-skill cast ----
def _resolve_skill_slot(api):
    """Skill-window slot holding the fishing skill, cached after the first hit.
    FISH_SKILL_SLOT pins it; otherwise id -> slot via GetSkillSlotIndex (validated
    back with GetSkillIndex, since not every build maps it the same way), else a
    plain slot scan."""
    global _skill_slot
    if _skill_slot is not None:
        return _skill_slot
    if FISH_SKILL_SLOT >= 0:
        _skill_slot = FISH_SKILL_SLOT
        return _skill_slot
    p = _m("playerm2g2")
    if p is None:
        return None
    try:
        s = int(p.GetSkillSlotIndex(FISH_SKILL_ID))
        if 0 <= s < SKILL_SCAN_MAX and int(p.GetSkillIndex(s)) == FISH_SKILL_ID:
            _skill_slot = s
    except Exception:
        pass
    if _skill_slot is None:
        for s in range(SKILL_SCAN_MAX):
            try:
                if int(p.GetSkillIndex(s)) == FISH_SKILL_ID:
                    _skill_slot = s
                    break
            except Exception:
                break
    if _skill_slot is not None:
        api.log("fishing: fishing skill %d resolved to slot %d" % (FISH_SKILL_ID, _skill_slot))
    return _skill_slot


def _skill_press(api):
    """ClickSkillSlot on the fishing skill's slot - the client's own skill-press
    path, no injected input. Idle -> cast; line out -> reel. Skill NOT found ->
    warn once and do nothing (no silent fallback to injection), so the cast-
    confirm timeout drives the normal retry/backoff."""
    global _warned_skill
    slot = _resolve_skill_slot(api)
    if slot is None:
        if not _warned_skill:
            api.log("fishing: fishing skill (id %d) not in any skill slot - learn it "
                    "or set FISH_SKILL_SLOT/FISH_SKILL_ID; not casting" % FISH_SKILL_ID)
            _warned_skill = True
        return False
    p = _m("playerm2g2")
    try:
        p.ClickSkillSlot(int(slot))
        return True
    except Exception as e:
        api.log("fishing: ClickSkillSlot(%d) failed: %s" % (slot, e))
        return False


def _cast(api):
    """Start fishing (CAST_METHOD)."""
    return _skill_press(api) if CAST_METHOD == "skill" else _tap(api)


def _reel(api):
    """Reel the line in (REEL_METHOD)."""
    return _skill_press(api) if REEL_METHOD == "skill" else _tap(api)


# ---- legacy injected Space tap ----
def _tap(api):
    # Inject a real SPACE key event at the OS level - the engine POLLS Space
    # (it never reaches OnKeyDown), so key-state setters and OnKeyDown(Space) both
    # fail from a mod. keybd_event with the SCANCODE flag is what DirectInput reads.
    # This reproduces exactly what the user's physical Space press does.
    global _hold_until
    try:
        # release FIRST, so a dropped key-up from a previous tap can't leave Space
        # logically held (a DOWN on an already-down key is a no-op = a missed cast,
        # which is what caused the double-casts). Then a clean DOWN edge.
        api.key_event(0x20, 0x39, 0)                # UP (idempotent clean-up)
        n = api.key_event(0x20, 0x39, 1)            # DOWN
        _hold_until = api.now() + HOLD_S            # release (key-up) after a short hold
        if not n:                                   # SendInput injected nothing - say so
            api.log("fishing: key_event(space) injected 0 events")
        return True
    except Exception as e:
        api.log("fishing: key_event(space) failed: %s" % e)
        return False


def _space_up(api):
    try:
        api.key_event(0x20, 0x39, 0)                # key UP
    except Exception:
        pass


def _release_key(api=None):
    """Undo an outstanding Space press. The skill path holds nothing, so there is
    nothing to release - and a stray key-up is a real edge the foreground window
    (not necessarily this client) would receive."""
    global _hold_until
    held, _hold_until = _hold_until, 0.0
    if api is not None and held:
        _space_up(api)


# ---- F5 arm/disarm: wrap the game window's OnKeyDown ----
def _reset(api):
    """Return every transient to its INITIAL state and release the key. Called on load
    and whenever the toggle turns off, so a fresh arm always starts clean."""
    global _state, _t, _t_cast, _last_seq, _last_bite, _reel_delay, _hold_until, _baited, _last_bait_t, _fail_casts, _noreel
    _release_key(api)                        # drop any held Space (mid-cast/reel/hold)
    _state, _t, _t_cast = "idle", (api.now() if api else 0.0), 0.0
    _last_seq = _last_bite = 0
    _reel_delay, _hold_until = DEFAULT_DELAY, 0.0
    _baited, _last_bait_t = False, -999.0
    _fail_casts = _noreel = 0


def _toggle_armed(api):
    global _armed, _catches, _misses, _last_seq, _last_bite
    _armed = not _armed
    if _armed:
        _reset(api)
        try:                                 # baseline so a stale bite can't fire a reel
            _last_seq, _, _last_bite, _, _ = api.fishing_poll()
        except Exception:
            _last_seq, _last_bite = 0, 0
    else:
        api.log("fishing: session caught=%d missed=%d" % (_catches, _misses))
        _reset(api)                          # OFF -> full initial state
        _catches = _misses = 0
    _toast(api, "Fishing started" if _armed else "Fishing stopped")
    api.log("fishing: %s (F5)" % ("ARMED" if _armed else "disarmed"))


def _wrap_keys(api):
    global _wrapped, _orig_key
    gw = _game_window()
    if gw is None or gw is _wrapped:
        return
    _unwrap()
    okd = getattr(gw, "OnKeyDown", None)
    if okd is None:
        return
    _orig_key = okd

    def w(key, *a, **kw):
        try:
            if DEBUG_KEYS:
                api.log("fishing: KEYDOWN %s" % key)   # capture which key casts/reels
            if key == _arm_key():
                _toggle_armed(api)
                return 1                    # consume F5
        except Exception:
            pass
        return okd(key, *a, **kw)
    global _our_kd
    try:
        setattr(gw, "OnKeyDown", w)
        _wrapped, _our_kd = gw, w
        api.log("fishing: F5 bound on the game window")
    except Exception:
        _wrapped, _our_kd = None, None


def _unwrap():
    """Restore OnKeyDown - but ONLY if ours is still the installed one. modui wraps
    the same handler and loads after us (alphabetical), so it usually sits ON TOP:
    restoring blindly would install the pre-modui handler we captured and silently
    kill modui's key. If somebody wrapped us, leave the chain alone - our wrapper
    stays in it and just forwards once the mod is gone."""
    global _wrapped, _orig_key, _our_kd
    if _wrapped is not None and _orig_key is not None:
        try:
            if _our_kd is not None and getattr(_wrapped, "OnKeyDown", None) is _our_kd:
                setattr(_wrapped, "OnKeyDown", _orig_key)
        except Exception:
            pass
    _wrapped, _orig_key, _our_kd = None, None, None


def on_load(api):
    global _armed, _catches, _misses, _warned_native, _skill_slot, _warned_skill, _autoarm_at, _autoarmed, _fishinfo
    _armed = False
    _autoarm_at, _autoarmed, _fishinfo = 0.0, False, False
    _catches = _misses = 0
    _warned_native = False
    _skill_slot, _warned_skill = None, False     # re-resolve the skill slot per load
    _reset(api)
    api.log("fishing: loaded, DISARMED - equip a rod, face water, press F5 (cast=%s reel=%s%s)"
            % (CAST_METHOD, REEL_METHOD, " AUTOARM" if AUTOARM else ""))


def on_unload(api):
    _unwrap()
    _release_key(api)
    api.log("fishing: unloaded (catches=%d misses=%d)" % (_catches, _misses))


def on_update(api, dt):
    global _state, _t, _t_cast, _last_seq, _last_bite, _catches, _misses, _warned_native, _hold_until, _reel_delay, _baited, _last_bait_t, _bite_vnum, _bite_abs, _bite_t, _reel_t, _fail_casts, _cast_seq, _noreel, _autoarm_at, _autoarmed, _reel_seq, _reel_wire, _logged_wire
    try:
        if not api.in_game():
            if _hold_until:
                _release_key(api)
            return
        _wrap_keys(api)
        now = api.now()
        if AUTOARM and not _armed and not _autoarmed:
            if not _autoarm_at:
                _autoarm_at = now + AUTOARM_AFTER
            elif now >= _autoarm_at:
                _autoarmed = True                # once per load; F5 toggles from here on
                api.log("fishing: AUTOARM (no F5)")
                _toggle_armed(api)
        if not _armed:
            return
        _maintain_bola(api, now)             # keep the fish-info effect (Bola de Pescar) topped up
        _use_caught_fish(api, now)           # open the caught fish (category fish, item type 12)
        # release the held key once the hold elapses (completes the cast/reel)
        if _hold_until and now >= _hold_until:
            _space_up(api)
            _hold_until = 0.0

        try:
            _poll = api.fishing_poll()
            seq, sub, bite, our_vid, fish_vnum = _poll[:5]
            cast_seq = _poll[5] if len(_poll) > 5 else 0   # 0 if stub predates cast_seq
        except Exception as e:
            if not _warned_native:
                api.log("fishing: %s - rebuild+restart the stub; staying idle" % e)
                _warned_native = True
            return
        global _logged_vid
        if our_vid and not _logged_vid:
            api.log("fishing: our VID latched = %d (now filtering to our own bites)" % our_vid)
            _logged_vid = True

        # 1. BITE -> start the reel TIMER (do NOT reel yet). The catch needs a
        # fish-specific wait after the "!" - reeling instantly always misses. We enter
        # 'biting' and the drive loop reels after REEL_DELAY. Ignore bites in the first
        # MIN_BITE_DELAY after a cast (late duplicates from the previous fish).
        if bite > _last_bite:
            _last_bite, _last_seq = bite, seq
            if _state == "fishing" and (now - _t_cast) >= MIN_BITE_DELAY:
                _bite_vnum = fish_vnum
                _reel_delay, _bite_abs = _reel_delay_for(api, fish_vnum)   # locked base or swept
                if not fish_vnum and not _fishinfo:
                    _reel_delay, _bite_abs = _delay_for(api, 0), 0.0       # no fish-info: fixed default, no sweep
                _bite_t, _reel_t, _reel_wire = now, 0.0, False
                _state, _t = "biting", now
                api.log("fishing: BITE %s -> reel in %.2fs" % (_fish_name(fish_vnum), _reel_delay))
            return

        # 2. a new packet -> react to its subheader. A START (sub 0) after our cast
        # means fishing began -> switch to 'fishing'. Results (3/4) are guarded by
        # state so the server's 2-3x duplicates only count once.
        if seq != _last_seq:
            _last_seq = seq
            if sub == 0 and _state == "casting":   # our cast took - fishing started
                _state, _t = "fishing", now
                _baited = False                     # the cast consumed the bait
                _fail_casts = 0                     # a good cast clears the failure streak
            elif sub == 3 and _state in ("reeling", "fishing", "biting"):   # catch success
                _catches += 1
                _noreel = 0
                _study_log(api, _bite_vnum, _fish_name(_bite_vnum), _reel_delay,
                           (_reel_t - _bite_t) if _reel_t else 0.0, "catch")
                api.log("fishing: CATCH (catches=%d misses=%d)" % (_catches, _misses))
                _toast(api, "Caught a fish! (%d)" % _catches)
                _state, _t = "cooldown", now
            elif sub == 4 and _state in ("reeling", "fishing", "biting"):   # fish escaped
                _misses += 1
                _study_log(api, _bite_vnum, _fish_name(_bite_vnum), _reel_delay,
                           (_reel_t - _bite_t) if _reel_t else 0.0, "miss")
                late = (now - _reel_t) if _reel_t else 0.0
                if cast_seq and not _reel_wire and cast_seq > _reel_seq:   # result and 0x34 in one poll
                    _reel_wire = True
                if cast_seq:                       # stub counts 0x34: MEASURED, not inferred
                    dead = bool(_reel_t and _bite_vnum and not _reel_wire)
                else:                              # old stub: fall back to the timing tell
                    dead = bool(_reel_t and _bite_vnum and late >= NOREEL_AFTER_S)
                if dead:
                    _noreel += 1
                    api.log("fishing: missed - our reel put NO 0x34 on the wire (fail %.1fs after "
                            "reel, bite+%.1fs) (%d/%d) (catches=%d misses=%d)"
                            % (late, now - _bite_t, _noreel, NOREEL_LIMIT, _catches, _misses))
                else:
                    _noreel = 0
                    api.log("fishing: missed (catches=%d misses=%d)" % (_catches, _misses))
                if _noreel >= NOREEL_LIMIT:
                    api.log("fishing: %d reels in a row never reached the server - pausing %.0fs "
                            "(REEL_METHOD=%s)" % (_noreel, FAIL_BACKOFF, REEL_METHOD))
                    _toast(api, "Fishing paused - reel not registering")
                    _state, _t = "backoff", now
                else:
                    _state, _t = "cooldown", now
            # sub 1 (stop) / 5 (notify) / duplicates in cooldown: nothing
            return

        # 3. drive the loop.
        if _state == "idle":
            if USE_BAIT and not _baited:
                # need fresh bait before casting - casting unbaited just cancels.
                # HARD double-guard: only when no unconsumed bait AND past MIN_BAIT_GAP.
                if (now - _last_bait_t) >= MIN_BAIT_GAP and _use_bait(api):
                    _baited, _last_bait_t = True, now
                    _state, _t = "baiting", now
                    api.log("fishing: baited")
                elif _bait_cell(api) is None:        # genuinely out of bait -> pause, don't spam
                    api.log("fishing: OUT OF BAIT (small fish / worm) - pausing %.0fs" % FAIL_BACKOFF)
                    _toast(api, "Fishing paused - out of bait")
                    _state, _t = "backoff", now
            elif _cast(api):                         # already baited / bait disabled -> cast
                _t_cast, _cast_seq = now, cast_seq
                _last_bite, _last_seq = bite, seq
                _state, _t = "casting", now
                api.log("fishing: cast")
        elif _state == "baiting" and now - _t >= BAIT_SETTLE:
            if _cast(api):                           # bait applied -> cast
                _t_cast, _cast_seq = now, cast_seq
                _last_bite, _last_seq = bite, seq
                _state, _t = "casting", now
                api.log("fishing: cast")
        elif _state == "backoff" and now - _t >= FAIL_BACKOFF:
            _fail_casts = _noreel = 0
            _state = "idle"                          # retry (bait may be restocked / re-aimed)
        elif _state == "biting":
            if fish_vnum != _bite_vnum:        # vnum resolved a poll after the bite - recompute
                _bite_vnum = fish_vnum
                if _bite_abs > 0:              # already a swept sample - keep it
                    _reel_delay = _bite_abs
                else:                          # locked (or sweep off) - recompute the base
                    _reel_delay = _delay_for(api, fish_vnum)
            if SKIP_JUNK and _fishinfo and _bite_vnum == 0 and (now - _t) >= JUNK_GRACE:
                _reel_seq = cast_seq
                _reel(api)                     # junk (uncatchable) - reel NOW, skip the delay
                _reel_t = now
                _state, _t = "reeling", now
                api.log("fishing: junk bite - reel now (skip delay)")
            elif now - _t >= _reel_delay:
                _reel_seq = cast_seq
                _reel(api)                     # the timed reel at bite + per-fish delay
                _reel_t = now
                _state, _t = "reeling", now
                api.log("fishing: reel")
        elif _state == "casting":
            if cast_seq > _cast_seq:           # our 0x34 reached the wire -> fishing began.
                _state, _t = "fishing", now    # VID-free confirm: a mis-latch can't trap us.
                _baited, _fail_casts = False, 0    # the cast consumed the bait
            elif now - _t >= CAST_CONFIRM_S:   # no start seen -> the cast didn't take
                _fail_casts += 1
                if _fail_casts >= FAIL_LIMIT:  # persistently not registering -> pause
                    api.log("fishing: %d casts didn't reach the server - pausing %.0fs"
                            % (_fail_casts, FAIL_BACKOFF))
                    _toast(api, "Fishing paused - casts not registering")
                    _state, _t = "backoff", now
                else:
                    _state = "idle"            # quick recast (keeps the bait)
        elif _state == "fishing" and now - _t >= FISH_TIMEOUT:
            _state = "idle"                    # started but no bite in time -> recast
        elif _state == "reeling":
            if cast_seq > _reel_seq and not _reel_wire:   # our reel's 0x34 went out
                _reel_wire = True
                if not _logged_wire:
                    api.log("fishing: reel confirmed on the wire (cast_seq %d -> %d)" % (_reel_seq, cast_seq))
                    _logged_wire = True
            if now - _t >= REEL_TIMEOUT:
                _state = "idle"                # reel gave no result - recast, don't stall
        elif _state == "cooldown" and now - _t >= RECAST_DELAY:
            _state = "idle"
    except Exception as e:
        api.log("fishing EXC %s: %s" % (type(e).__name__, e))
        _state, _t = "cooldown", api.now()
