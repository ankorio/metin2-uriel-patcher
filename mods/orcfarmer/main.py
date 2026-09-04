"""orcfarmer - one switch for an Orc-drop farm.

WHAT IT IS
----------
Not a fourth hunting engine. It is ribfarmer pointed at different mobs and
different loot: the same `autohunt2` and `autoloot` code that ships in this
folder, run in private namespaces with the Orc filters already applied.

    autohunt2   HUNT_RACES = ORC_RACES      PREFER anything called "Orc"
                HUNT_FALLBACK               ...but clear the area when there
                                            are none, instead of standing idle
                SEEK_WANTED                 walk to them instead of waiting

    autoloot    WANTED = ORC_VNUMS+ORC_NAMES  take the Orc drops
                ANNOUNCE_DROPS                pop up and mark every one on the
                                              floor, however far away
                FETCH_UNOWNED + CAMP_S        walk to ones dropped for OTHER
                                              players and wait them out

HUNTING BY NAME, NOT BY RACE NUMBER
-----------------------------------
ribfarmer names its two races as numbers because it wants exactly those two.
This wants "anything Orc", which is a different question and has a different
answer: autohunt2 resolves a STRING through the client's own
nonplayer.GetMonsterDataByNamePart, so one entry covers every Orc the server
has without anybody enumerating them.

That is a SUBSTRING lookup, and the log names what it resolved to on every
load - read that line before trusting this. Worth knowing that this server has
items called "Orchid Sword" which contain "orc"; those are items, not monsters,
so they cannot appear in a monster lookup, but a monster with "orc" buried in
its name would be hunted and the resolve line is where that would show up.

THE LOOT, AND WHAT IS MEASURED
------------------------------
Two of the five have been seen on the ground here and their vnums are read
straight off this server's own log lines:

    vnum 30006 'Orc Tooth'
    vnum 30076 'Orc Amulet+'

The rest ride on names. The "+70" offset that maps Esoteric Primer 30008 to
30078 does NOT hold for these - 30006 + 70 is Orc Amulet+, not Orc Tooth+ - so
the missing vnums are left unset rather than guessed. autoloot substring-matches
any fragment of NAME_MIN_LEN or more, so "orc tooth" already covers "Orc Tooth"
and "Orc Tooth+", and "orc amulet" covers both amulets.

ALL FIVE ARE TREATED AS THE PRIZE. Unlike ribfarmer, where only the blade was
worth crossing the map for and everything else was collected in passing, every
item on this list is scanned for, announced, pinned to the atlas, walked to and
camped on. That is what PASSING_ONLY being empty means here.

EITHER / OR, NEVER BOTH
-----------------------
`autohunt2` and `autoloot` still ship unfiltered and still work exactly as they
did - this is additive. But two hunting engines driving one client is not a
configuration, it is a fight, so this REFUSES TO RUN when either of them, or
ribfarmer, is enabled for this character, says which one and how to fix it,
and stands down.

WHY THE ENGINES ARE EXEC'D RATHER THAN COPIED
---------------------------------------------
A copy would be two files to fix for every bug found in either, and the copy is
the one that would be forgotten. modhost already loads a mod by exec'ing its
main.py into a fresh dict; this does the same with a dict of its own, so the
hosted engine has genuinely separate module state and cannot interfere with a
directly-loaded copy of itself.
"""

import json
import os

CAPABILITIES = ["read", "target", "attack", "ui", "loot", "move"]

# ---- what "orc" means -------------------------------------------------------
# A NAME, resolved by the client. See the docstring: one string covers every Orc
# on the server, and autohunt2 logs the race numbers it resolved to.
#
# Mobs seen here so far: 'Black Orc', 'Black Orc Giant', 'Bold Black Orc',
# 'Elite Orc Fighter', 'Elite Orc Sorcerer'. Numbers may be added alongside the
# string if a particular one ever needs pinning.
ORC_RACES = ["orc"]

# The loot. Vnums where this server has actually shown us one, names for the
# rest - and the names alone would be enough, since they substring-match.
ORC_VNUMS = [
    30006,      # Orc Tooth     seen on the ground
    30076,      # Orc Amulet+   seen on the ground
]
ORC_NAMES = [
    "orc tooth",        # covers "Orc Tooth" and "Orc Tooth+"
    "orc amulet",       # covers "Orc Amulet" and "Orc Amulet+"
    "orc molar",
]

# Our own alts are not competition. Without this the avoidance machinery counts
# the buffer standing on top of us as a crowd and walks the character instead of
# killing - measured at 14 relocations and near-zero kills in five minutes.
#
# Fill this in per profile with the names of your own characters that stand
# nearby (buffer, lure, shop, etc.). Empty = every player counts as competition.
IGNORE_PLAYERS = []

# What the in-game chat popup says when the hunt starts and stops.
LABEL = "orcfarmer"

# ---- everything else worth bending down for ---------------------------------
# Nothing, deliberately. ribfarmer carries a list of commons (carrot, hay,
# bellflower...) because it is parked in one spot for hours; this one starts
# empty so that what it picks up is exactly what it was switched on for. Add
# names or vnums here and they are taken when they are underfoot.
EXTRA_NAMES = []
EXTRA_VNUMS = []

SEEK_RANGE = 12000.0            # how far to walk for a wanted mob, world units
TAKE_MONEY = False              # yang is somebody else's job
LEARN_DROPS = True              # keep measuring which race actually drops what

# PRIORITISE the Orcs - do not hunt ONLY them.
#
# A hard filter starves itself: every non-Orc is refused, so nothing kills them,
# so they pile up and the area's spawn budget goes to mobs we will not touch.
# With this on the Orcs stay the priority - still sought to SEEK_RANGE, still
# taken first - but once the seek finds none for BLANK_S the filter lifts and
# the character hunts normally until one turns up.
HUNT_ANYTHING_WHEN_NONE = True  # off = the old behaviour, Orcs or nothing
BLANK_S = 8.0                   # how long with none in range before it does

# ---- the loot radar ---------------------------------------------------------
# Every listed drop gets the full treatment: announced in chat the moment it is
# seen, pinned to the atlas, walked to, and camped on until it is picked up or
# gone - including drops that belong to other players, which stop being theirs
# after a while.
BLADE_RADAR = True
RADAR_RANGE = 0.0       # 0 = every drop the client has loaded. A REPORT, not a
                        # walk, so there is no cost to seeing far.
TAKE_OTHERS = True
CAMP_S = 45.0           # how long to wait an unowned one out before giving up

# How far to WALK for one. Seeing across the map is free; walking there is not.
FETCH_RANGE = SEEK_RANGE
FETCH_EVERYTHING = True

# Taken underfoot but never scanned for, walked to or camped on. Empty here:
# every item on this mod's list IS the prize, so none of them is "in passing".
PASSING_ONLY = []

# Free-form escape hatches: anything else to push into either engine, by its own
# constant name. Empty by default - the named settings above cover the intent.
HUNT = {}
LOOT = {}

VERBOSE = True

# ---- who owns the character this tick ---------------------------------------
# Three things want to drive one body, and until now they only half-agreed about
# who was driving. What each one wants:
#
#   the fight   whatever has aggroed onto us has to die, or it follows
#   the loot    a wanted drop is on a despawn timer and will not wait
#   the hunt    there is always another mob somewhere
#
# The order is FIGHT > LOOT > HUNT, and it is that order because of what each
# one loses by waiting. A pulled mob follows the character anywhere, so walking
# away from it does not end the fight - it just moves it, and adds the next
# pull on top. A drop expires. The next target does not: it is the one thing
# here that is genuinely replaceable, so it yields to both.
#
# The mechanism is two DEADLINES on os.environ, which is how this codebase has
# always signalled between mods:
#
#   TRIARCH_LOOT_BUSY   published by autoloot while it is walking to a drop.
#                       autohunt2 stands down. Already existed.
#   TRIARCH_FIGHT_BUSY  published HERE while anything is on us. autoloot will
#                       not START a fetch. New.
#   TRIARCH_AGGRO       published HERE: the vids we have watched come for us,
#                       so autohunt2 can tell a mob that pulled from a mob that
#                       happens to be standing nearby. New.
#
# Deadlines rather than flags for the reason autoloot's _hold already documents:
# a flag left set by a mod that crashed or was disabled is a character that
# stops hunting one day and nothing anywhere says why.
FIGHT_FIRST = True      # False: no TRIARCH_FIGHT_BUSY, no aggro feed - the
                        # engines coordinate on the loot hold alone, as before

# ---- looting is part of the hunt, not a background service ------------------
# autoloot ticks whenever it is loaded, and while it is hosted here it is loaded
# for as long as orcfarmer is. So a stopped hunt still swept, still announced,
# and still walked 12000 units to camp on somebody else's drop - with nothing
# killing anything, which is not a farm, it is a character wandering off on its
# own while the player thought it was parked.
#
# The hunt being ENABLED is not the test; the hunt actually RUNNING is. That is
# autohunt2's own _on, set by start() and cleared by stop(), read straight out
# of the hosted engine's globals - the same value the engine itself branches on,
# so the two can never disagree.
#
# Turning the hunt off now stands the collector down with it, and that includes
# abandoning a fetch in flight and clearing its atlas marks: a hunt stopped
# mid-fetch would otherwise leave the character walking to a drop with the mod
# that started the walk no longer being ticked, and nothing to end it.
LOOT_WHILE_IDLE = False

# ---- telling a mob that has pulled from a mob that is just standing there ----
# There is no aggro accessor on this build. autohunt2 says so at _mobs_on_us and
# uses proximity as the proxy, which is right as far as it goes: a pack on top of
# us really is on top of us. The failure is the other direction - an idle spawn
# eight steps away is counted as part of the fight, so on a populated field the
# "still on us" count never reaches zero. That is what made the character look
# indecisive: PACK_FIRST refused to let a seek start, PACK_LOCK re-pressed
# whatever was nearest every tick, and the loot fetch was never given a turn.
#
# What IS measurable is MOTION. A mob that has aggroed runs at the player; a mob
# that has not, does not. One position sample cannot see that and a series of
# them can, so this keeps a short history per vid and asks three questions:
#
#   1. is the client auto-attacking it          -> on us, by definition
#   2. has it CLOSED on us by AGGRO_CLOSE units -> it is coming for us
#   3. has it sat inside AGGRO_MELEE for
#      AGGRO_MELEE_S                            -> it is on top of us
#
# Any one of them is enough. Test 2 is the one that does the real work; test 3
# catches a mob that spawned or was pulled onto us already at melee range and so
# never had a chance to close; test 1 is the ground truth we already have.
#
# It stops being on us when it gives up: back off AGGRO_LEAVE units from its own
# closest approach AND outside melee range. Leashing and death both end that way,
# and death also drops it from actors() entirely.
#
# The feed only ever NARROWS what autohunt2 already believed - a mob has to be
# both near and on the list - so the failure mode of a bad answer here is the
# old behaviour, never something worse.
AGGRO_RADIUS = 2500.0   # how far out to track. Beyond this a mob is not on us
                        # in any sense that should hold up a decision.
AGGRO_SAMPLE_S = 0.4    # how often to sample. Costs one api.actors() walk plus
                        # a few binding calls per mob, so not every frame.
AGGRO_CLOSE = 250.0     # closed this much on us since we started watching it
AGGRO_MELEE = 400.0     # this close is "on top of us"...
AGGRO_MELEE_S = 1.0     # ...and it has to stay there this long, so a mob we
                        # merely walked past is not counted
AGGRO_LEAVE = 700.0     # backed off this far from its closest approach and it
                        # has lost us
AGGRO_FORGET_S = 4.0    # not seen at all for this long: dead, despawned or out
                        # of range. Drop it rather than remember it for ever.
AGGRO_MAX = 32          # cap on the published list. An environment variable is
                        # not a data structure; past a couple of dozen the exact
                        # membership stops mattering to any decision made from it.
FIGHT_HOLD_S = 2.0      # how far ahead TRIARCH_FIGHT_BUSY is kept. Comfortably
                        # more than AGGRO_SAMPLE_S so it never flickers between
                        # samples, and short enough to lapse fast if we stop.

# The one hard ceiling on FIGHT > LOOT.
#
# "Kill what is on us before collecting anything" is the right priority and it
# is also an unbounded one: a mob that has aggroed, cannot be reached and will
# not leash holds the fight open for ever, and the collector would wait for ever
# behind it. That is a farm that quietly stops farming, which is the failure
# this codebase keeps trying not to ship.
#
# So the fight blocks looting for at most this long at a stretch. Past it, only
# TRIARCH_FIGHT_BUSY is dropped - TRIARCH_AGGRO keeps being published, so
# autohunt2 still knows what to kill and goes on killing it. The collector is
# simply allowed to get on with its own job at the same time, which is what it
# was doing before any of this existed.
#
# 45s deliberately matches autohunt2's FINISH_TIMEOUT_S: it is the same problem
# ("adds can arrive forever") answered with the same number, and two different
# ceilings on one situation would be a thing to reconcile later.
FIGHT_MAX_S = 45.0
AGGRO_DEBUG = False     # log every change in the set. mods.log is already large;
                        # this is for working on the tracker, not for running it.

# The mods whose code we run, and the mods we therefore refuse to run beside.
_ENGINES = ("autohunt2", "autoloot")
_RIVALS = _ENGINES + ("ribfarmer",)  # ...and what we must not run BESIDE

MAX_FAULTS = 5          # per engine, mirroring modhost - one broken engine
                        # must not take the other down with it

_loaded = {}            # name -> globals dict of a live engine
_faults = {}            # name -> consecutive faults
_checked = False        # the either/or check has reached a verdict
_blocked = ""           # ...and this is why, if it refused
_next_check = 0.0
_mtimes = {}            # engine main.py -> mtime when we exec'd it
_next_watch = 0.0

ENGINE_POLL_S = 2.0     # how often to look for an edited engine; see _watch

# ---- arbitration state ------------------------------------------------------
_agg = {}               # vid -> {ref, best, dist, seen, near_since, on}
_agg_next = 0.0         # next sample due
_agg_on = 0             # size of the aggro set at the last sample, for logging
_fight_since = 0.0      # when the set last went empty -> non-empty; 0 = empty
_fight_capped = False   # FIGHT_MAX_S has been reached and said so once
_looting = False        # autoloot was ticked last time round; the falling edge
                        # is what has to stand the collector down


# ---- reading files the way this client allows -------------------------------
def _read(path):
    """Read a file with no `with` and no keyword arguments.

    builtins.open here is a Cython `open_` returning a system.pack_file, which
    supports neither the context-manager protocol nor encoding=. Same constraint
    modhost.read_bytes documents; repeated rather than imported because mods
    cannot import modhost."""
    f = open(path, "rb")
    try:
        return f.read()
    finally:
        try:
            f.close()
        except Exception:
            pass


def _read_json(path):
    try:
        return json.loads(_read(path).decode("utf-8", "replace"))
    except Exception:
        return None


def _mods_dir():
    return os.path.dirname(__mod_dir__)


# ---- the either/or check ----------------------------------------------------
def _enabled_for(api, mod):
    """Is `mod` switched on for the character actually logged in?

    Reads config.json and the character's profile in the same order modhost
    merges them, because the answer has to match what modhost DID, not what the
    base config says. A profile that enables autohunt2 on this character is a
    real conflict even when config.json has it off."""
    base = _read_json(os.path.join(_mods_dir(), "config.json")) or {}
    on = None
    sect = base.get(mod)
    if isinstance(sect, dict) and "enabled" in sect:
        on = bool(sect["enabled"])

    name = ""
    try:
        name = api.player.name() or ""
    except Exception:
        pass
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if safe:
        prof = _read_json(os.path.join(_mods_dir(), "profiles", safe + ".json"))
        if isinstance(prof, dict):
            sect = prof.get(mod)
            if isinstance(sect, dict) and "enabled" in sect:
                on = bool(sect["enabled"])
    # modhost's own default when a mod has no `enabled` key at all is ON, so an
    # unmentioned autohunt2 IS a conflict. Guessing the friendlier answer here
    # would mean two engines running and no explanation anywhere.
    return True if on is None else on


def _build_constant(mod, key):
    """A per-BUILD value the patcher stamped into config.json, or None.

    WALK_STATE is the one that matters: the patcher resolves it out of the
    client's own `cmp [player+0x58], <state>` on every install, and modhost
    refuses to let a profile override it precisely because a stale copy is a
    silently broken mod rather than a wrong-looking option. A hosted engine
    reads its config through us, so it would get the module default and miss
    the stamp entirely - it has to be forwarded, and it has to come from the
    BASE config, never from a profile."""
    base = _read_json(os.path.join(_mods_dir(), "config.json")) or {}
    sect = base.get(mod)
    if isinstance(sect, dict) and key in sect:
        return sect[key]
    return None


def _conflict(api):
    """"" when it is safe to run, or the name of the mod in the way."""
    for mod in _RIVALS:
        if not os.path.isfile(os.path.join(_mods_dir(), mod, "main.py")):
            continue                    # not installed: nothing to collide with
        if _enabled_for(api, mod):
            return mod
    return ""


# ---- hosting an engine ------------------------------------------------------
def _fetchable():
    """The entries a FETCH (and the radar) may act on.

    An EMPTY list means "anything in WANTED" to autoloot, so this only returns
    empty when nothing is excluded - otherwise the exclusions would be silently
    ignored, which is the failure mode worth guarding against here."""
    if not FETCH_EVERYTHING:
        return list(ORC_VNUMS) + list(ORC_NAMES)
    want = (list(ORC_VNUMS) + list(ORC_NAMES)
            + list(EXTRA_VNUMS) + list(EXTRA_NAMES))
    if not PASSING_ONLY:
        return []                       # everything; let autoloot use WANTED
    skip = set()
    for p in PASSING_ONLY:
        skip.add(p if isinstance(p, int) else str(p).strip().lower())
    return [w for w in want
            if (w if isinstance(w, int) else str(w).strip().lower()) not in skip]


def _overrides(mod):
    """The settings this mod pushes into one engine.

    Order matters: build constants first, our filters over them, and the
    free-form HUNT/LOOT dicts last so an experiment can always win."""
    if mod == "autohunt2":
        o = {}
        ws = _build_constant("autohunt2", "WALK_STATE")
        if ws is not None:
            o["WALK_STATE"] = ws
        o.update({
            # The game's chat popup on start/stop. The engine is hosted, so it
            # must name the mod the player actually switched on - "autohunt2 on"
            # would name a mod that is, correctly, disabled.
            "LABEL": str(LABEL),
            "HUNT_RACES": list(ORC_RACES),
            "SEEK_WANTED": True,
            "HUNT_FALLBACK": bool(HUNT_ANYTHING_WHEN_NONE),
            "HUNT_FALLBACK_AFTER_S": float(BLANK_S),
            "SEEK_RANGE": float(SEEK_RANGE),
            "IGNORE_PLAYERS": list(IGNORE_PLAYERS),
            "LEARN_DROPS": bool(LEARN_DROPS),
            "LEARN_VNUMS": list(ORC_VNUMS),
            # The drop learner writes next to mods.log; a separate file keeps a
            # orcfarmer run's tally from being merged with a plain autohunt2 one.
            "LEARN_FILE": "orcfarmer_drops.json",
        })
        o.update(HUNT or {})
        return o
    if mod == "autoloot":
        o = {
            "WANTED": (list(ORC_VNUMS) + list(ORC_NAMES)
                       + list(EXTRA_VNUMS) + list(EXTRA_NAMES)),
            # WHAT IS WORTH WALKING FOR.
            #
            # This used to be the blade and nothing else, so everything else was
            # collected only if the sweep happened to reach it. Now the whole
            # list gets the full treatment: seen on the ground anywhere in
            # range, announced, pinned to the atlas, walked to, and camped on
            # until it is picked up or gone.
            #
            # autoloot reads an EMPTY FETCH_ONLY as "anything in WANTED", so
            # this is one value rather than a second copy of the list that could
            # drift out of step with the first.
            #
            # The trade, stated plainly: FETCH_RANGE is SEEK_RANGE (12000), and
            # that now applies to a Bellflower as much as to a blade. If the
            # character starts spending its time couriering cheap drops across
            # the map, FETCH_RANGE is the knob - not this.
            "FETCH_ONLY": _fetchable(),
            "TAKE_ITEMS": True,
            "TAKE_MONEY": bool(TAKE_MONEY),
            # The popup has to name the mod the player switched on.
            "LABEL": str(LABEL),
            "ANNOUNCE_DROPS": bool(BLADE_RADAR),
            "ANNOUNCE_RANGE": float(RADAR_RANGE),
            "FETCH_UNOWNED": bool(TAKE_OTHERS),
            "CAMP_S": float(CAMP_S) if TAKE_OTHERS else 0.0,
            "FETCH_RANGE": float(FETCH_RANGE),
            # An allow-list is only half a filter without this: a blade that
            # lands outside the client's ~1000-unit pickup radius is otherwise
            # left on the floor until it despawns.
            "FETCH_WANTED": True,
        }
        o.update(LOOT or {})
        return o
    return {}


def _spawn(api, mod):
    """exec one engine into a private namespace and apply our overrides."""
    path = _engine_path(mod)
    g = {"__name__": "mod_orcfarmer_" + mod,
         "__file__": path,
         "__mod_dir__": os.path.dirname(path)}
    # Stamped BEFORE the exec. A file edited while we are reading it must look
    # stale on the next poll and be picked up again, not be recorded as the
    # version we ran.
    _mtimes[mod] = _mtime(path)
    exec(compile(_read(path), path, "exec"), g)

    unknown = []
    for k, v in _overrides(mod).items():
        if k in g:
            g[k] = v
        else:
            unknown.append(k)
    if unknown:
        # Loud, because a setting that matches nothing is a filter that is NOT
        # being applied - which looks exactly like the mod working.
        api.log("orcfarmer: %s has no setting(s) %s - THOSE FILTERS ARE NOT "
                "ACTIVE. The engine has changed shape; fix _overrides()."
                % (mod, ", ".join(sorted(unknown))))
    return g


def _engine_path(mod):
    return os.path.join(_mods_dir(), mod, "main.py")


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _watch(api, now):
    """Reload a hosted engine whose file changed on disk.

    modhost hot-reloads a mod when ITS OWN main.py changes, which is exactly
    what it should do - and it means editing autoloot/main.py does nothing here,
    because orcfarmer's main.py is untouched and the copy we exec'd is already
    in memory. Measured the confusing way round: a fix went into autoloot, the
    log kept showing the old behaviour, and the file on disk said otherwise.

    So watch the files we actually run. Same contract as modhost's own reload -
    unload, re-exec, re-apply the overrides - and on the same 1-2s cadence, so
    editing an engine behaves identically whether it is loaded directly or by
    us."""
    global _next_watch
    if now < _next_watch:
        return
    _next_watch = now + ENGINE_POLL_S
    changed = [m for m in _ENGINES
               if m in _loaded and _mtime(_engine_path(m)) != _mtimes.get(m)]
    if not changed:
        return
    api.log("orcfarmer: %s changed on disk - reloading" % ", ".join(changed))
    _stop(api)
    _start(api)


def _hook(api, mod, name, *args):
    """Call one engine's hook, containing a fault to that engine."""
    g = _loaded.get(mod)
    if g is None:
        return
    fn = g.get(name)
    if fn is None:
        return
    try:
        fn(*args)
        _faults[mod] = 0
    except Exception as e:
        _faults[mod] = _faults.get(mod, 0) + 1
        api.log("orcfarmer: %s raised in %s (%d/%d) %s: %s"
                % (mod, name, _faults[mod], MAX_FAULTS, type(e).__name__, e))
        if _faults[mod] >= MAX_FAULTS:
            del _loaded[mod]
            api.log("orcfarmer: %s switched off after %d consecutive faults - "
                    "the other engine keeps running" % (mod, MAX_FAULTS))


def _start(api):
    """Load both engines. Called once the either/or check has passed."""
    for mod in _ENGINES:
        try:
            _loaded[mod] = _spawn(api, mod)
            _faults[mod] = 0
        except Exception as e:
            api.log("orcfarmer: could not load %s - %s: %s"
                    % (mod, type(e).__name__, e))
            continue
        _hook(api, mod, "on_load", api)
    if not _loaded:
        api.log("orcfarmer: NOTHING LOADED - neither engine could be read from "
                "%s. Re-run the patcher to reinstall mods\\." % _mods_dir())
        return
    # Two lines, because one had grown into an unreadable wall of vnums - and a
    # banner nobody reads is the same as no banner. The split also states the
    # thing that is easy to get wrong: what it WALKS for is not what it TAKES.
    api.log("orcfarmer: ON - %s races %r, walking to %r%s. Engines: %s"
            % ("prioritising" if HUNT_ANYTHING_WHEN_NONE else "hunting ONLY",
               list(ORC_RACES), list(ORC_VNUMS) + list(ORC_NAMES),
               "" if TAKE_MONEY else ", no yang",
               ", ".join(sorted(_loaded))))
    if HUNT_ANYTHING_WHEN_NONE:
        api.log("orcfarmer: with none of %r within %.0f for %.0fs it hunts "
                "anything - a cleared area is what lets them respawn"
                % (list(ORC_RACES), SEEK_RANGE, BLANK_S))
    if BLADE_RADAR:
        api.log("orcfarmer: blade radar ON - every %r on the floor%s is "
                "announced and pinned to the atlas"
                % (list(ORC_NAMES)[:1] or ["blade"],
                   "" if not RADAR_RANGE else " within %.0f" % RADAR_RANGE))
    if TAKE_OTHERS:
        api.log("orcfarmer: will also walk up to %.0f for a blade dropped for "
                "SOMEONE ELSE and stand on it for %.0fs waiting for the "
                "ownership to lapse" % (FETCH_RANGE, CAMP_S))
    if EXTRA_NAMES or EXTRA_VNUMS:
        api.log("orcfarmer: also taking%s: %r"
                % ("" if FETCH_EVERYTHING else ", in passing only",
                   list(EXTRA_NAMES) + list(EXTRA_VNUMS),))
    if FETCH_EVERYTHING:
        # _fetchable() returns EMPTY to mean "everything in WANTED" - printing
        # that raw reads as "nothing", which is the opposite of the truth.
        _f = _fetchable()
        api.log("orcfarmer: scanned for, walked to and camped on, up to %.0f away: %s"
                % (FETCH_RANGE,
                   "every item on the list" if not _f else repr(_f)))
    if PASSING_ONLY:
        api.log("orcfarmer: taken ONLY in passing - no scan, no atlas mark, no "
                "walk: %r" % (list(PASSING_ONLY),))


def _stop(api):
    for mod in reversed(_ENGINES):
        if mod in _loaded:
            _hook(api, mod, "on_unload", api)
    _loaded.clear()
    _faults.clear()
    _mtimes.clear()
    # Before the engines are gone rather than after: a fight hold left published
    # by a mod that is no longer running would keep the next autoloot - hosted or
    # loaded directly - from fetching anything for FIGHT_HOLD_S, with nothing
    # left anywhere to explain it.
    _aggro_reset(api)


# ---- who is on us -----------------------------------------------------------
def _attackable(api, vid):
    """Borrowed from the hosted autohunt2 rather than reimplemented.

    It is four raw binding calls with a guard on each, and a second copy would
    be the one that goes stale when a build stops exporting IsStone. We own that
    namespace - we exec'd it - so reading a function out of it is not a trick,
    it is the same access the engine has to itself.

    No engine, no answer: the caller then publishes nothing, and every consumer
    of the feed falls back to plain proximity. That is the right way round -
    guessing "attackable" here would put NPCs and corpses into the aggro set and
    hold the character in a fight that does not exist."""
    g = _loaded.get("autohunt2")
    fn = g.get("_attackable") if g else None
    if fn is None:
        return None
    try:
        return bool(fn(api, vid))
    except Exception:
        return None


def _engaged_vid(api):
    """The client's own auto-attack VID, or 0. Ground truth, such as it is."""
    try:
        return int(api.natives.module().field("auto_attack_vid") or 0)
    except Exception:
        return 0


def _aggro_scan(api, now):
    """One sample of the aggro tracker. See the AGGRO_* block for the rules."""
    global _agg_next, _agg_on
    if now < _agg_next:
        return
    _agg_next = now + AGGRO_SAMPLE_S
    try:
        acts = api.actors(radius=AGGRO_RADIUS)
    except Exception:
        return                          # cannot see: keep the last answer
    engaged = _engaged_vid(api)

    for a in acts:
        vid, d = a["vid"], a["dist"]
        if d is None:
            continue
        ok = _attackable(api, vid)
        if ok is None:
            return                      # no engine to ask; leave the set alone
        if not ok:
            continue
        e = _agg.get(vid)
        if e is None:
            # ref is where we FIRST saw it. Closing is measured from there, so a
            # mob that was already next to us cannot register as having charged
            # - that case is test 3's, not test 2's.
            e = {"ref": d, "best": d, "near_since": 0.0, "on": False}
            _agg[vid] = e
        e["seen"] = now
        e["dist"] = d
        if d < e["best"]:
            e["best"] = d

        if d <= AGGRO_MELEE:
            if not e["near_since"]:
                e["near_since"] = now
        else:
            e["near_since"] = 0.0

        if not e["on"]:
            if vid == engaged:
                e["on"] = True                              # 1. we are on it
            elif e["ref"] - d >= AGGRO_CLOSE:
                e["on"] = True                              # 2. it closed on us
            elif (e["near_since"]
                  and now - e["near_since"] >= AGGRO_MELEE_S):
                e["on"] = True                              # 3. it is on top of us
        elif (vid != engaged
              and d > AGGRO_MELEE
              and d - e["best"] >= AGGRO_LEAVE):
            # It has given up, been leashed, or we walked out of its range.
            # Re-base so a second approach reads as a fresh charge rather than
            # being measured against where it was ten seconds ago.
            e["on"] = False
            e["ref"], e["best"], e["near_since"] = d, d, 0.0

    # Anything we have not seen for a while is dead, despawned or out of range.
    # Death is the common one and it leaves actors() outright, so this is also
    # what stops a killed mob holding the fight open.
    for vid in list(_agg.keys()):
        if now - _agg[vid].get("seen", 0.0) > AGGRO_FORGET_S:
            del _agg[vid]

    on = [v for v in _agg if _agg[v]["on"]]
    if AGGRO_DEBUG and len(on) != _agg_on:
        api.log("orcfarmer: %d on us (tracking %d within %.0f)"
                % (len(on), len(_agg), AGGRO_RADIUS))
    _agg_on = len(on)
    _publish(api, now, on)


def _publish(api, now, on):
    """Publish what is on us, and for how much longer it may block looting.

    The two channels are deliberately separable: past FIGHT_MAX_S the fight
    stops being a reason to postpone collection, but it does not stop being a
    fight, so TRIARCH_AGGRO keeps flowing and autohunt2 keeps killing the right
    mobs. See the FIGHT_MAX_S block."""
    global _fight_since, _fight_capped
    if not on:
        _fight_since, _fight_capped = 0.0, False
        _publish_clear()
        return
    if not _fight_since:
        _fight_since, _fight_capped = now, False
    capped = (now - _fight_since) >= FIGHT_MAX_S
    if capped and not _fight_capped:
        _fight_capped = True
        api.log("orcfarmer: %d mob(s) have been on us for %.0fs - letting the "
                "collector work alongside the fight from here (FIGHT_MAX_S). "
                "Still killing them; just no longer waiting for them."
                % (len(on), FIGHT_MAX_S))
    try:
        os.environ["TRIARCH_FIGHT_BUSY"] = (
            "0" if capped else "%.3f" % (now + FIGHT_HOLD_S))
        os.environ["TRIARCH_AGGRO"] = (
            "%.3f %s" % (now + FIGHT_HOLD_S,
                         ",".join(str(v) for v in on[:AGGRO_MAX])))
    except Exception:
        pass


def _publish_clear():
    """Stop publishing. Consumers fall back to what they did before us.

    Written as "0" rather than deleted: a deadline in the past and an absent key
    mean the same thing to every reader, and overwriting is the operation that
    cannot fail halfway."""
    try:
        os.environ["TRIARCH_FIGHT_BUSY"] = "0"
        os.environ["TRIARCH_AGGRO"] = "0"
    except Exception:
        pass


def _aggro_reset(api):
    global _agg_next, _agg_on, _fight_since, _fight_capped
    _agg.clear()
    _agg_next, _agg_on = 0.0, 0
    _fight_since, _fight_capped = 0.0, False
    _publish_clear()


# ---- who gets ticked --------------------------------------------------------
def _hunting():
    """Is the hunt actually RUNNING - not merely enabled?

    autohunt2's own _on, read out of the namespace we exec'd it into. It is the
    flag start() sets and stop() clears, and it is what the engine's own
    on_update branches on, so this cannot disagree with the engine about whether
    it is hunting."""
    g = _loaded.get("autohunt2")
    return bool(g.get("_on")) if g else False


def _loot_stand_down(api, why):
    """End anything autoloot has in flight, and release its hold.

    Called on the falling edge of the hunt. Skipping the tick alone would not do
    it: a fetch in progress is a live waypoint plus a published hold, and with
    nothing ticking the mod that owns them the character would keep walking to
    the drop and autohunt2 would stay held down for FETCH_HOLD_S after it."""
    g = _loaded.get("autoloot")
    if g is None:
        return
    try:
        if g.get("_fetch_id"):
            g["_end_fetch"](api, api.now(),
                            "orcfarmer: %s - abandoning the fetch" % why)
        g["_clear_marks"](api)
        g["_release"]()
    except Exception as e:
        api.log("orcfarmer: could not stand autoloot down (%s: %s)"
                % (type(e).__name__, e))


# ---- lifecycle --------------------------------------------------------------
def on_load(api):
    global _checked, _blocked, _next_check
    _loaded.clear()
    _faults.clear()
    _checked, _blocked, _next_check = False, "", 0.0
    _mtimes.clear()
    global _next_watch, _looting
    _next_watch = 0.0
    _looting = False
    _aggro_reset(api)
    if VERBOSE:
        api.log("orcfarmer: loaded - waiting for a character before deciding "
                "whether autohunt2/autoloot are already running")


def on_unload(api):
    _stop(api)
    api.log("orcfarmer: unloaded")


def on_update(api, dt):
    global _checked, _blocked, _next_check

    if not _checked:
        # The check needs a character name to read the right profile, and there
        # is none until the world has loaded. Poll slowly rather than deciding
        # early on a name of "" - which would read only config.json and could
        # reach the opposite verdict from the one modhost acted on.
        if not api.in_game():
            return
        now = api.now()
        if now < _next_check:
            return
        _next_check = now + 1.0
        try:
            if not (api.player.name() or ""):
                return
        except Exception:
            return
        _checked = True
        _blocked = _conflict(api)
        if _blocked:
            api.log("orcfarmer: NOT RUNNING - %r is enabled for this character "
                    "and orcfarmer already runs its code with the Orc "
                    "filters applied. Two of them would fight over every "
                    "target. Disable %r (or orcfarmer) in mods\\config.json or "
                    "this character's profile - one or the other, never both."
                    % (_blocked, _blocked))
            return
        _start(api)
        return

    if _blocked:
        return
    global _looting
    now = api.now()
    _watch(api, now)

    # 1. WHO IS ON US. Sampled before either engine runs, so both of them see
    #    the same answer on the same tick rather than one acting on the state
    #    the other has already moved on from.
    hunting = _hunting()
    if FIGHT_FIRST and hunting:
        _aggro_scan(api, now)
    elif _agg or _agg_on:
        # Not hunting: nothing here is entitled to hold a fight open, and a
        # stale set would keep TRIARCH_FIGHT_BUSY alive against a character
        # that is standing still.
        _aggro_reset(api)

    # 2. DOES THE COLLECTOR GET A TURN. The hunt running is the whole test -
    #    see LOOT_WHILE_IDLE. Everything on the falling edge has to be undone
    #    explicitly; see _loot_stand_down.
    loot = hunting or LOOT_WHILE_IDLE
    if loot != _looting:
        _looting = loot
        if not loot:
            _loot_stand_down(api, "the hunt stopped")
        if VERBOSE:
            api.log("orcfarmer: looting %s (hunt %s)"
                    % ("ON" if loot else "OFF",
                       "running" if hunting else "stopped"))

    # 3. TICK. autohunt2 first, so the fight state it acts on is this tick's;
    #    autoloot second, so its hold lands on an engine that has already had
    #    its say. Both still stand down on the other's deadline - the ordering
    #    is about latency, not about correctness.
    for mod in _ENGINES:
        if mod == "autoloot" and not loot:
            continue
        _hook(api, mod, "on_update", api, dt)
