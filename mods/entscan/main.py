"""entscan - mark every loaded metin stone on the atlas, and (optionally) put a
red target mark on any monster whose name matches a configured list.

TWO JOBS, TWO RATES
-------------------
Finding a stone (or a named mob) is expensive: there is no enumeration binding,
so it means sweeping the VID space asking HasInstance about each id. That is the
sweep, and it runs every SWEEP_EVERY seconds.

Watching something you already found is nearly free: you have its VID, so it is
one HasInstance call. That runs EVERY PUMP, which is why a destroyed stone or a
killed mob comes off the map in about a second instead of waiting for the sweep.

TWO KINDS OF MARK
-----------------
Stones use api.atlas_mark - the type-6 waypoint swirl, atlas only. Named mobs use
api.mark_mob - the type-13 TARGET mark, which draws on the minimap AND the atlas
and, because it carries the mob's VID, auto-tracks the mob as it moves (the
engine rewrites its position every frame). Nothing here repositions a mob mark;
it only has to remove it when the mob dies. The two use disjoint mark-id ranges
so neither can clobber the other.

Everything here is live: no memory of things seen earlier. The mark set is
exactly what this client currently has loaded, which is what the minimap draws.
"""

CAPABILITIES = ["read", "ui"]

# ---- configuration --------------------------------------------------------
SWEEP_EVERY = 5.0       # seconds between discovery sweeps
CHUNK = 100000          # VIDs per pump. The sweep is pump-rate bound, not CPU
                        # bound: it costs ceil(width/CHUNK) pumps. Measured on
                        # a 550k window: 6000 -> 39s, 20000 -> 21s, 100000 ->
                        # 4.2s, no frame hitch.

# Sweep window, relative to our own VID. Asymmetric: the core hands out VIDs in
# ascending order and never reuses them, so everything that existed before we
# arrived is below us and only what spawned since is above.
RADIUS_LO = 500000
RADIUS_HI = 50000

MAX_MARKS = 60          # cap, so a stone field cannot flood the atlas
MARK_BASE = 991000

# Named-monster red dots. TARGET_NAMES is a COMMA-SEPARATED list of
# case-insensitive SUBSTRINGS matched against each instance's in-client
# (localised!) name - so "Arquero Serpiente" tags the Snake Archer and "Arquero
# Escorpi" the Scorpion Archer without needing the accented o. A plain comma
# string is what the UI edit box writes; a JSON list works too. Empty = feature
# off, stones only.
TARGET_NAMES = "Arquero Serpiente, Arquero Escorpi"
MAX_TARGET_MARKS = 40
TARGET_MARK_BASE = 992000       # disjoint from the metin range above

# ---- state ----------------------------------------------------------------
_live = {}              # vid -> [mark id, x, y]   (metin stones)
_targets = {}           # vid -> mark id            (named monsters)
_key = None             # map:channel we are marking
_state = "idle"
_lo = _hi = _at = 0
_hits = []
_t0 = 0.0
_next = 0.0
_warned_native = False


def _cur_key(api):
    return "%s:%s" % (api.map_name() or "?", api.channel() or 0)


def _is_stone(rec):
    """`stone` is IsStone, the authoritative flag. The type name is checked too
    because IsStone is one binding and could go missing on a patch."""
    return bool(rec.get("stone")) or rec.get("type_name") == "STONE"


def _norm(s):
    """Lower-cased bytes, tolerant of the client's Python 2 unicode config
    strings and of the non-ASCII bytes in a localised mob name. The accented
    characters are dropped rather than decoded - the configured substrings are
    ASCII, so matching still lands."""
    if not isinstance(s, str):
        try:
            s = s.encode("ascii", "ignore")
        except Exception:
            s = str(s)
    return s.lower()


def _wanted():
    """TARGET_NAMES as a list of non-empty substrings, whether it arrived as a
    comma string (UI edit box, or a profile) or a JSON list (config default)."""
    v = TARGET_NAMES
    if isinstance(v, (list, tuple)):
        parts = v
    else:
        parts = _norm_str(v).split(",")
    return [p.strip() for p in parts if p and p.strip()]


def _norm_str(s):
    """A str, tolerant of Python 2 unicode config values."""
    if isinstance(s, str):
        return s
    try:
        return s.encode("ascii", "ignore")
    except Exception:
        return str(s)


def _target_name(rec):
    """The configured substring this record matches, or None. Only living,
    non-stone characters are eligible."""
    wanted = _wanted()
    if not wanted or _is_stone(rec) or rec.get("dead"):
        return None
    name = _norm(rec.get("name") or "")
    if not name:
        return None
    for want in wanted:
        if _norm(want) in name:
            return want
    return None


def _free_id(taken, base, cap):
    for i in range(cap):
        if base + i not in taken:
            return base + i
    return None


def _drop_all(api):
    global _live, _targets
    n = len(_live) + len(_targets)
    for m in _live.values():
        api.atlas_unmark(m[0])
    for mid in _targets.values():
        api.unmark_mob(mid)
    _live, _targets = {}, {}
    return n


# ---- the cheap half: watch what we already found ---------------------------
def _prune(api):
    """One HasInstance per marked entity, every pump. A destroyed instance loses
    its CInstanceBase, so this catches it within a pump."""
    gone_stone = _prune_stones(api)
    gone_target = _prune_targets(api)
    return gone_stone + gone_target


def _prune_stones(api):
    if not _live:
        return 0
    has = api.has_instance
    gone = []
    for vid in _live:
        if not has(vid):
            gone.append(vid)
            continue
        rec = api.describe(vid)
        # Destroyed but still animating its death, or no longer a stone at all.
        if not rec or rec.get("dead") or not _is_stone(rec):
            gone.append(vid)
    for vid in gone:
        api.atlas_unmark(_live.pop(vid)[0])
    if gone:
        api.log("entscan: -%d destroyed, %d stone(s) marked" % (len(gone), len(_live)))
    return len(gone)


def _prune_targets(api):
    if not _targets:
        return 0
    has = api.has_instance
    gone = []
    for vid in _targets:
        if not has(vid):
            gone.append(vid)
            continue
        rec = api.describe(vid)
        # Dead, despawned, or no longer matches (name changes are unheard of,
        # but a dying mob flips `dead` and should lose its dot at once).
        if not rec or rec.get("dead") or _target_name(rec) is None:
            gone.append(vid)
    for vid in gone:
        api.unmark_mob(_targets.pop(vid))
    if gone:
        api.log("entscan: -%d target(s) gone, %d mob(s) marked" % (len(gone), len(_targets)))
    return len(gone)


# ---- the expensive half: find new ones -------------------------------------
def _start(api, now):
    global _state, _lo, _hi, _at, _hits, _t0
    mine = api.player.vid()
    if not mine:
        return
    _lo, _hi = max(1, mine - RADIUS_LO), mine + RADIUS_HI
    _at, _hits, _t0 = _lo, [], now
    _state = "sweeping"


def _step(api):
    global _at
    has = api.has_instance
    end = min(_at + CHUNK, _hi)
    v = _at
    while v < end:
        if has(v):
            _hits.append(v)
        v += 1
    _at = end
    return _at >= _hi


def _finish(api, now):
    global _state, _warned_native
    _state = "idle"

    added = 0
    added_t = 0
    for vid in _hits:
        rec = None
        # --- metin stones ---
        if vid not in _live:
            rec = api.describe(vid)
            if rec and _is_stone(rec) and not rec.get("dead") and rec.get("pos"):
                mid = _free_id(set(m[0] for m in _live.values()), MARK_BASE, MAX_MARKS)
                if mid is None:
                    api.log("entscan: MAX_MARKS=%d reached - not marking the rest" % MAX_MARKS)
                else:
                    x, y = rec["pos"][0], rec["pos"][1]
                    if api.atlas_mark(mid, x, y, rec.get("name") or "Metin"):
                        _live[vid] = [mid, x, y]
                        added += 1
                        api.log("entscan: +metin %r at (%.0f,%.0f) vid=%d" % (
                            rec.get("name") or "Metin", x, y, vid))

        # --- named monsters ---
        if TARGET_NAMES and vid not in _targets:
            if rec is None:
                rec = api.describe(vid)
            if rec and _target_name(rec) is not None:
                mid = _free_id(set(_targets.values()), TARGET_MARK_BASE, MAX_TARGET_MARKS)
                if mid is None:
                    api.log("entscan: MAX_TARGET_MARKS=%d reached" % MAX_TARGET_MARKS)
                elif api.mark_mob(mid, vid, rec.get("name") or ""):
                    _targets[vid] = mid
                    added_t += 1
                    api.log("entscan: +target %r vid=%d" % (rec.get("name") or "?", vid))
                elif not _warned_native:
                    _warned_native = True
                    api.log("entscan: mark_mob unavailable - stub has no minimap_mark "
                            "native; stones still marked. Rebuild uriel_stub.dll.")

    if added or added_t:
        api.log("entscan: sweep %.1fs - %d stone(s), %d mob(s) marked"
                % (now - _t0, len(_live), len(_targets)))


# ---- lifecycle ------------------------------------------------------------
def on_load(api):
    global _state, _next, _key, _live, _targets, _warned_native
    _state, _next, _key = "idle", 0.0, None
    _live, _targets, _warned_native = {}, {}, False
    api.log("entscan: loaded (api v%s) sweep every %.0fs, chunk %d, targets=%r"
            % (api.VERSION, SWEEP_EVERY, CHUNK, TARGET_NAMES))


def on_unload(api):
    api.log("entscan: unloaded, %d mark(s) cleared" % _drop_all(api))


def on_update(api, dt):
    global _state, _next, _key

    if not api.in_game():
        if _live or _targets:
            _drop_all(api)
        return

    # Map or channel change: entirely different entity set, and the VIDs are
    # reassigned by the new game core, so drop everything and re-sweep now.
    k = _cur_key(api)
    if k != _key:
        if _key is not None:
            api.log("entscan: %s -> %s, %d mark(s) cleared" % (_key, k, _drop_all(api)))
        _key, _state, _next = k, "idle", 0.0

    now = api.now()

    _prune(api)

    if _state == "sweeping":
        if _step(api):
            _finish(api, now)
        return

    if now >= _next:
        _next = now + SWEEP_EVERY
        _start(api, now)
