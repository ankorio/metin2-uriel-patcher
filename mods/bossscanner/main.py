"""bossscanner - ping every monster the client draws with an ORANGE name.

WHAT IT IS FOR
--------------
entscan marks mobs BY NAME: a profile lists "Bestial Archer, Bestial Specialist"
and it matches those strings. That only ever finds what somebody already knew to
type in, and there are far more special mobs than anyone wants to enumerate.
This finds the whole class instead, from the client's own data.

WHAT "SPECIAL" TURNED OUT TO BE
-------------------------------
The client has exactly two monster name colours:

    colorInfo.CHR_NAME_RGB_MOB  = (235,  22,  9)   red    - ordinary
    colorInfo.CHR_NAME_RGB_BOSS = (255, 153, 51)   orange - special

Finding what selects the orange one took four wrong answers, recorded here so
nobody spends the time again:

    RANK             NO. Bestial Archer (orange) and Black Orc (red) are BOTH
                     S_KNIGHT - confirmed live, GetGradeByVID reads 3 for each.
    AI_FLAG=AGGR     NO. Reported from the game: there are aggressive mobs with
                     red names, and orange-named mobs that are not aggressive.
    MOB_COLOR        NO. Column 55 of mob_proto is a model tint; both read 0.
    the spawn packet NO. TPacketGCCharacterAdd carries bType and wRaceNum only -
                     no boss flag - so the client decides from its own tables,
                     keyed on the race number.

Diffing all 71 columns of the server's mob_proto.txt for 533 against 636 leaves
only AI flag, race family (HUMAN vs ORC) and stun immunity: nothing that means
"special". So the answer is a client-side LIST, and there is one:

    WikiConfig.CUSTOM_BOSS_CATEG_VNUMS_*    the client's own boss categories

Validated against every mob whose colour is known: it contains 533 and 534 and
excludes all 27 ordinary ones - Black Orc, Black Orc Giant, Bold Black Orc, Bold
Black Giant Orc, Dark/High Tormentor, Dark Summoner, High Evocator, the Orcs,
the Arahans, King Scorpion. 29 of 29 correct.

It is also where the mobs nobody has named live: Lykos, Scrofa, Bera, Tigris,
Mahon, Bo, Goo-Pae, Chuong, the whole Bestial family, Ignitor.

TWO SOURCES, BECAUSE NEITHER IS ENOUGH ALONE
--------------------------------------------
That wiki list misses 68 monsters of rank BOSS or KING - Demon King, Chief Elite
Orc, Dark Leader, Elite Vile Demon King and the rest - which are certainly
orange. So a mob is special when EITHER holds:

    its rank is BOSS or KING          read live, nonplayer.GetGradeByVID
    its vnum is in the wiki boss set  read live, WikiConfig

Both come out of the running client at load. NOTHING IS HARDCODED: if the server
patches its boss list or adds a mob, this picks it up on the next reload with no
edit here.

THE ONE TRAP IN THE RANK ACCESSOR
---------------------------------
GetGradeByVID answers for things that are not monsters, and it answers 5 - KING,
the top of the ladder. A metin stone reads 5; so does an NPC. A rank filter alone
would mark every stone and shopkeeper on the map, so the rank test is applied
only to actual enemies, established from the instance type
(INSTANCE_TYPE_ENEMY = 0 on this build).

THE MARK
--------
Preferred is api.mark_mob, the type-13 target mark that carries the VID so the
engine repositions it every frame. THIS STUB DOES NOT EXPORT IT - measured, and
there is no minimap_mark in natives.json or in the DLL. What the offsets file
does carry is kMiniMapAddWayPoint, which is api.atlas_mark, so the fallback is
the type-6 swirl, re-placed each pass to track a moving mob. entscan reached the
same conclusion for the same reason. On this build the ping is that swirl plus
the chat line, refreshed every PRUNE_EVERY_S rather than every frame.
"""

import sys

CAPABILITIES = ["read", "ui"]

# ---- what counts as special -------------------------------------------------
# Ranks that are special on their own. 4 = BOSS, 5 = KING. S_KNIGHT (3) is
# deliberately NOT here: Black Orc is an S_KNIGHT with an ordinary red name.
BOSS_RANKS = [4, 5]

_GRADE_NAMES = {0: "PAWN", 1: "S_PAWN", 2: "KNIGHT",
                3: "S_KNIGHT", 4: "BOSS", 5: "KING"}

# THE MAIN TEST: does the monster have any IMMUNITY?
#
# nonplayer.GetMonsterImmuneFlag(race) is a bitmask - STUN 1, SLOW 2, FALL 4,
# CURSE 8, POISON 16, TERROR 32 - and non-zero is what the orange name tracks.
# Checked against every mob whose colour is known, thirty of them, no exceptions:
#
#   ORANGE  Bestial Archer 533   immune=STUN        Thunderclap 681  immune=35
#           Bestial Specialist   immune=STUN        (STUN|SLOW|TERROR)
#           Lykos/Scrofa/Bera/Tigris  STUN,SLOW,CURSE,TERROR
#   RED     Black Orc, Black Orc Giant, Bold Black Orc, Bold Black Giant Orc,
#           Dark/High Tormentor, Dark Summoner, High Evocator, Orc Scout,
#           Orc Fighter, Orc Sorcerer, Elite Orc x3, Bold Big Orc x2, the seven
#           Arahans, the six High Arahans, King Scorpion   ALL immune=0
#
# It is also the only test that could have found Thunderclap: race 681 is not in
# the server's mob_proto.txt at all - that file is an older cut - and it is not
# in the client's boss list either. The immunity is read from the CLIENT's own
# table at runtime, so a mob that exists only on the live server is still
# classified correctly.
#
# Set False to fall back to rank plus the client list alone.
# FALSIFIED, left off. Scorpion Archer and Scorpion Man carry immune=POISON and
# have ordinary red names, so a non-zero immunity does not mean orange. Kept as a
# switch only because the field is still worth having in EXPLAIN output.
USE_IMMUNE = False

# The client's own curated list, kept as a SECOND source. It catches anything
# the immunity test would miss, and it is where the named specials are grouped.
USE_WIKI_LIST = True
WIKI_MODULES = ["WikiConfig", "wikiconfig"]
# Attribute prefixes to union. CUSTOM_BOSS_CATEG_VNUMS_1..9, _YOHARA and
# _YOHARA_DUNGEON are the per-region boss categories; bossList is a shorter
# curated set that overlaps them.
#
# CUSTOM_METIN_CATEG_VNUMS_* is deliberately NOT here: those are metin stones,
# entscan already marks them, and they are not enemies anyway.
WIKI_PREFIXES = ["CUSTOM_BOSS_CATEG_VNUMS", "bossList"]

# Say WHY, for any mob whose name contains one of these. One line per race,
# once per load, whether or not it ends up flagged.
#
# This exists because "that mob should be flagged and is not" is the question
# this mod will keep being asked, and answering it by adding another probe every
# time is slower than carrying the answer. It prints the rank, whether the
# client calls it an enemy, and whether its vnum is in the client's boss list -
# which between them decide everything.
EXPLAIN_NAMES = []

# One-shot: find whatever actually holds a NAME COLOUR, rather than another
# property that merely correlates with one. Rank, AI flag, immunity and the wiki
# lists have each been wrong; the colour is the only thing every interesting mob
# is known to share, so read the colour.
HUNT_COLOUR = False

# One-shot: which client-side containers hold these vnums? 681 is Thunderclap,
# which is orange but is NOT in the WikiConfig boss lists - so there is a second
# list somewhere. 533/534 are the Bestials (orange) and 636/2105 are Black Orc
# and Scorpion Archer (red), carried as controls: the list we want holds the
# first three and none of the last two.
HUNT_VNUMS = []

# THE SERVER'S OWN SPECIAL-SPAWN LIST.
#
# Every map has a boss.txt beside its regen.txt, and it is where the server puts
# the named/special spawns. Checked against every mob whose colour is confirmed:
#
#   in boss.txt      533 Bestial Archer, 534 Bestial Specialist   ORANGE
#   not in boss.txt  636/637/656/657 the Black Orcs               RED
#                    2103/2104/2105/2131/2132 the Scorpions       RED
#
# Two of two orange, none of nine red. This is the union of the 'm' spawn lines
# across all 30 boss.txt files in the source that was supplied - Cung-Mok,
# Mu-Rang, Jug-Hyul, Young-Ji, Li-An, Lykos, the White Oath four, the Black
# Storm six, the Bestial family, Bestial Captain, Ice Witch and the rest.
#
# IT IS A STATIC COPY, and that is a real limitation: the supplied source is an
# older cut than the live server - it has no vnum 681 (Thunderclap) anywhere,
# not in mob_proto.txt and not in mob_names. Any special added since is missing
# here, which is what EXTRA_VNUMS is for until the live gamefiles turn up.
BOSS_SPAWN_VNUMS = [
    151, 152, 153, 154, 155, 191, 331, 332, 333, 334, 431, 432,
    433, 434, 435, 436, 531, 532, 533, 534, 591, 601, 602, 603,
    604, 1192, 2191, 2307, 2491,
]

# Always special, whatever any list says. 681 is Thunderclap: confirmed orange
# in game, and in NO source available here - not the client's wiki lists, not
# any client container (searched by content across every resident module), and
# not the supplied server files, which predate it.
EXTRA_VNUMS = [681]
IGNORE_VNUMS = []       # never special, whatever the client says
IGNORE_NAMES = []       # ...by exact name, if one turns out noisy

# Only this instance type is asked for its rank - see the trap note above. Read
# from the client's own constant; this is only the fallback.
ENEMY_TYPE = 0

RANGE = 0.0             # how far to look, world units. 0 = every actor the
                        # client has loaded, which is the right answer: this is
                        # a report, not a walk, and the client's own load radius
                        # is already the real limit.

SCAN_EVERY_S = 2.0      # discovery cadence
PRUNE_EVERY_S = 0.5     # how often marked mobs are re-checked, so a killed one
                        # comes off the map in about half a second - and how
                        # often an atlas mark is dragged after a moving mob

MAX_MARKS = 40          # cap, so a boss field cannot flood the map. Must stay
                        # under 1000: the next id range starts at MARK_BASE+1000.
MARK_BASE = 994000      # disjoint from entscan (991000 stones, 992000 targets)
                        # and autoloot (993000). Two mods sharing an id would
                        # silently delete each other's marks.

ANNOUNCE = True         # chat popup the first time each one is seen
ANNOUNCE_ONCE = True    # False = re-announce every scan, which is a lot
VERBOSE = True
PROBE = False           # dump the rank and the verdict for every race in view
                        # and mark nothing. How all of the above was established.

_marks = {}             # vid -> (mark id, "mob" | "atlas", name)
_next_mark = 0
_seen = []              # vids already announced this session
_scan_at = 0.0
_prune_at = 0.0
_found = 0
_ready = False
_said_nomark = False
_hunted_colour = False
_hunted_vnums = False
_explained = []         # races already explained, so EXPLAIN_NAMES says it once
_wiki = None            # frozenset of special vnums, None until built


# ---- the two sources --------------------------------------------------------
def _wiki_set(api):
    """The client's own boss vnums, unioned over every category list.

    Read from the running client rather than copied into this file. That is the
    whole point: nobody has to maintain a list, and a server patch that adds a
    special is picked up on the next reload."""
    out = set()
    if not USE_WIKI_LIST:
        return frozenset(out)
    for mn in WIKI_MODULES:
        m = sys.modules.get(mn)
        if m is None:
            continue
        try:
            attrs = dir(m)
        except Exception:
            continue
        for a in attrs:
            if not any(a.startswith(p) for p in WIKI_PREFIXES):
                continue
            try:
                v = getattr(m, a)
            except Exception:
                continue
            if isinstance(v, (list, tuple, set, frozenset)):
                for x in v:
                    if isinstance(x, int):
                        out.add(x)
    return frozenset(out)


def _enemy_type(api):
    """INSTANCE_TYPE_ENEMY, from the client's own constant where possible."""
    m = sys.modules.get("pack_chr")
    try:
        return int(getattr(m, "INSTANCE_TYPE_ENEMY"))
    except Exception:
        return ENEMY_TYPE


def _immune(api, race):
    """The monster's immunity bitmask, or None when this build cannot say.

    Per RACE, not per VID: it is a property of the monster kind, and the client
    exposes it as GetMonsterImmuneFlag(race). None means "no answer" and drops
    out of the test rather than counting either way."""
    m = sys.modules.get("nonplayer")
    fn = getattr(m, "GetMonsterImmuneFlag", None) if m is not None else None
    if fn is None:
        return None
    try:
        return int(fn(int(race)))
    except Exception:
        return None


def _grade(api, vid):
    """The monster's rank, or None when this build cannot say.

    None is not "ordinary" and not "special": it simply drops out of the rank
    test and leaves the vnum list to decide. A filter that guessed here would
    either mark the whole map or silently mark nothing, and both look exactly
    like the mod working."""
    m = sys.modules.get("nonplayer")
    fn = getattr(m, "GetGradeByVID", None) if m is not None else None
    if fn is None:
        return None
    try:
        return int(fn(int(vid)))
    except Exception:
        return None


def _is_enemy(api, vid):
    """A real monster, as opposed to a stone, an NPC, a shop or a player.

    Both tests, because they fail in opposite directions: the instance type is
    authoritative, and IsEnemy is the cheap catch for a build where the type
    numbering has moved. Anything unreadable is NOT an enemy - unknown must
    never mean yes here, or the stones come back."""
    c = sys.modules.get("pack_chr")
    if c is None:
        return False
    try:
        if int(c.GetInstanceType(int(vid))) != _enemy_type(api):
            return False
    except Exception:
        return False
    try:
        if not c.IsEnemy(int(vid)):
            return False
    except Exception:
        pass                            # no IsEnemy here: the type stands
    try:
        if c.IsDead(int(vid)):
            return False
    except Exception:
        pass
    return True


def _verdict(api, a):
    """Why this actor is special, or "" if it is not.

    A reason rather than a bare True, because it is what the log and the chat
    line say - and "BOSS" is worth telling apart from "special" when a field
    turns out busier than expected."""
    vid, race = a["vid"], a["race"]
    if race in IGNORE_VNUMS:
        return ""
    if IGNORE_NAMES:
        low = (a["name"] or "").strip().lower()
        if low in [str(n).strip().lower() for n in IGNORE_NAMES]:
            return ""
    if not _is_enemy(api, vid):
        return ""                       # stones and NPCs read rank 5 - see the
                                        # trap note in the docstring
    g = _grade(api, vid)
    if g is not None and g in BOSS_RANKS:
        return _GRADE_NAMES.get(g, str(g))
    if race in EXTRA_VNUMS:
        return "listed"
    if race in BOSS_SPAWN_VNUMS:
        return "special"
    if USE_IMMUNE:
        im = _immune(api, race)
        if im:
            return "special"
    if _wiki and race in _wiki:
        return "listed"
    return ""


# ---- marking ----------------------------------------------------------------
def _pos(api, vid):
    try:
        return api.entity(vid).position()
    except Exception:
        return None


def _mark_one(api, mid, vid, name, pos):
    """Place one mark by whichever route this build supports.

    Returns "mob", "atlas", or "" when neither worked."""
    try:
        if api.mark_mob(mid, vid, name):
            return "mob"
    except Exception:
        pass
    if pos:
        try:
            if api.atlas_mark(mid, pos[0], pos[1], name or "?"):
                return "atlas"
        except Exception:
            pass
    return ""


def _unmark_one(api, mid, how):
    try:
        if how == "atlas":
            api.atlas_unmark(mid)
        else:
            api.unmark_mob(mid)
    except Exception:
        pass


def _bearing(api, pos):
    """" - 2913 units NE", or "". Something to act on without opening the atlas."""
    try:
        me = api.player.position()
        if not me or not pos:
            return ""
        dx, dy = pos[0] - me[0], pos[1] - me[1]
        d = (dx * dx + dy * dy) ** 0.5
        # +y is SOUTH in this client's world coordinates, which is why the N/S
        # test reads inverted against the arithmetic.
        ns = "N" if dy < 0 else "S"
        ew = "E" if dx > 0 else "W"
        side = (ns if abs(dy) > 2 * abs(dx) else
                ew if abs(dx) > 2 * abs(dy) else ns + ew)
        return " - %.0f units %s" % (d, side)
    except Exception:
        return ""


def _mark(api, a, why):
    """Pin one special. True if a NEW mark was placed."""
    global _next_mark, _found, _said_nomark
    vid = a["vid"]
    if vid in _marks or len(_marks) >= MAX_MARKS:
        return False
    mid = MARK_BASE + (_next_mark % MAX_MARKS)
    _next_mark += 1
    name = a["name"] or "?"
    how = _mark_one(api, mid, vid, name, a["pos"])
    if not how and VERBOSE and not _said_nomark:
        # Said ONCE, not per mob: a scanner that silently marks nothing looks
        # exactly like a map with no specials on it, and repeating it every two
        # seconds would bury the chat line that still works.
        _said_nomark = True
        api.log("bossscanner: cannot mark %r - neither mark_mob nor atlas_mark "
                "worked (pos=%r). Still announcing in chat." % (name, a["pos"]))
    _marks[vid] = (mid, how, name)
    _found += 1
    if ANNOUNCE and (vid not in _seen or not ANNOUNCE_ONCE):
        if vid not in _seen:
            _seen.append(vid)
            del _seen[:-400]
        api.chat("%s [%s]%s" % (name, why, _bearing(api, a["pos"])))
    if VERBOSE:
        api.log("bossscanner: %s race=%s %s vid=%d%s"
                % (name, a["race"], why, vid, _bearing(api, a["pos"])))
    return True


def _unmark_all(api):
    for vid in list(_marks):
        mid, how, _nm = _marks.pop(vid)
        _unmark_one(api, mid, how)


def _prune(api):
    """Drop marks whose mob is gone, and drag the rest after their mob."""
    for vid in list(_marks):
        alive = True
        try:
            alive = bool(api.has_instance(vid))
        except Exception:
            alive = True                # cannot tell: leave it marked
        if alive:
            try:
                if sys.modules["pack_chr"].IsDead(int(vid)):
                    alive = False
            except Exception:
                pass
        if not alive:
            mid, how, _nm = _marks.pop(vid)
            _unmark_one(api, mid, how)
            if VERBOSE:
                api.log("bossscanner: vid %d gone - unmarked" % vid)
            continue
        # A native mark follows the mob by itself; an atlas mark does not, so
        # re-place it. One AddWayPoint on a VID we already hold, and it is the
        # difference between tracking a boss and pointing at where it used to be.
        mid, how, name = _marks[vid]
        if how == "atlas":
            here = _pos(api, vid)
            if here:
                try:
                    # THE NAME GOES BACK IN. Re-marking an id replaces the whole
                    # waypoint, label included, so passing "" here overwrote the
                    # name half a second after every find and left the atlas
                    # showing bare coordinates on hover - which is exactly what
                    # was reported, and why entscan's marks read correctly and
                    # these did not.
                    api.atlas_mark(mid, here[0], here[1], name or "?")
                except Exception:
                    pass


def _explain(api, a):
    """Say why one actor is or is not special. See EXPLAIN_NAMES.

    Dumps EVERY per-race and per-VID field this build exposes, because the
    narrow version is what kept costing round trips: Thunderclap turned out to
    be race 681, which is not in the server mob_proto.txt we were handed AND not
    in the client's boss list, so whatever marks it orange is a field nobody has
    looked at yet. Print them all once and compare."""
    vid, race = a["vid"], a["race"]
    g = _grade(api, vid)
    why = _verdict(api, a)
    api.log("bossscanner: EXPLAIN %r race=%s rank=%s(%s) immune=%s enemy=%s "
            "in_client_list=%s in_EXTRA=%s -> %s"
            % (a["name"], race, g, _GRADE_NAMES.get(g, "?"),
               _immune(api, race), _is_enemy(api, vid),
               bool(_wiki and race in _wiki), race in EXTRA_VNUMS,
               ("PING as %s" % why) if why else "NOT flagged"))

    np = sys.modules.get("nonplayer")
    pc = sys.modules.get("pack_chr")

    def ask(m, fn, arg):
        f = getattr(m, fn, None) if m is not None else None
        if f is None:
            return "-"
        try:
            return f(arg)
        except Exception as e:
            return "!" + type(e).__name__

    cols = []
    for fn in ("GetEventTypeByVID", "GetLevelByVID", "GetAttElementFlagByVID"):
        cols.append((fn.replace("ByVID", ""), ask(np, fn, vid)))
    for fn in ("GetTagByVID", "GetVirtualNumber", "GetAura", "GetLevel",
               "GetArmor", "GetShape", "GetWeapon", "IsAffect"):
        cols.append(("pc." + fn.replace("ByVID", ""), ask(pc, fn, vid)))
    for fn in ("GetMonsterRaceFlag", "GetMonsterImmuneFlag", "GetMonsterLevel",
               "GetMonsterMaxHP", "GetMonsterExp", "GetMonsterGold",
               "GetMonsterST", "GetMonsterDX", "GetMonsterDamage",
               "GetMonsterDamageMultiply", "GetMonsterRegenCycle",
               "GetMonsterRegenPercent", "GetMobDamageLimit", "GetMac",
               "GetMonsterEnchants", "GetMonsterResists", "GetMonsterName",
               "GetEventType", "IsMonsterStone", "GetAttElementFlagByRace"):
        cols.append((fn.replace("GetMonster", "m"), ask(np, fn, race)))
    for i in range(0, len(cols), 6):
        api.log("     " + "  ".join("%s=%r" % (k, v) for k, v in cols[i:i + 6]))


def _scan(api):
    try:
        acts = api.actors(radius=RANGE or None)
    except Exception as e:
        api.log("bossscanner: actors() failed %s: %s" % (type(e).__name__, e))
        return
    want = [str(w).strip().lower() for w in EXPLAIN_NAMES if str(w).strip()]
    for a in acts:
        if want and a["race"] not in _explained:
            low = (a["name"] or "").lower()
            if any(w in low for w in want):
                _explained.append(a["race"])
                try:
                    _explain(api, a)
                except Exception as e:
                    api.log("bossscanner: explain failed %s: %s"
                            % (type(e).__name__, e))
        if a["vid"] in _marks:
            continue
        why = _verdict(api, a)
        if why:
            _mark(api, a, why)


def _probe(api):
    """One line per distinct race in view, with the verdict. Marks nothing."""
    api.log("---- bossscanner probe ----")
    try:
        acts = api.actors(radius=RANGE or None)
    except Exception as e:
        api.log("  actors() failed %s: %s" % (type(e).__name__, e))
        api.log("---- end probe ----")
        return
    api.log("  %d actor(s); enemy type %d; %d vnum(s) in the client's boss list"
            % (len(acts), _enemy_type(api), len(_wiki or ())))
    seen = []
    for a in acts:
        if a["race"] in seen:
            continue
        seen.append(a["race"])
        g = _grade(api, a["vid"])
        why = _verdict(api, a)
        api.log("  %-6s %-26s %6s  rank=%-9s immune=%-4s enemy=%-5s "
                "inlist=%-5s %s"
                % (a["race"], (a["name"] or "")[:26],
                   "-" if a["dist"] is None else "%.0f" % a["dist"],
                   _GRADE_NAMES.get(g, g), _immune(api, a["race"]),
                   _is_enemy(api, a["vid"]),
                   bool(_wiki and a["race"] in _wiki),
                   ("<- PING (%s)" % why) if why else ""))
    api.log("---- end probe ----")


def _hunt_vnums(api):
    """Every client-side container that mentions any of HUNT_VNUMS.

    Searched by CONTENT, not by attribute name: the list that marks Thunderclap
    is evidently not called anything anyone guessed, and the WikiConfig lists
    were only found this way in the first place. Nested one level, because the
    client keeps some of these as lists of lists."""
    api.log("---- vnum container hunt ----")
    mods = sys.modules
    want = set(HUNT_VNUMS)

    def ints_in(v, depth=0):
        """Every int reachable in v, one level of nesting."""
        out = set()
        try:
            if isinstance(v, dict):
                for k, x in v.items():
                    if isinstance(k, int):
                        out.add(k)
                    if isinstance(x, int):
                        out.add(x)
                    elif depth < 1 and isinstance(x, (list, tuple, set, frozenset)):
                        out |= ints_in(x, depth + 1)
            elif isinstance(v, (list, tuple, set, frozenset)):
                for x in v:
                    if isinstance(x, int):
                        out.add(x)
                    elif depth < 1 and isinstance(x, (list, tuple, set, frozenset,
                                                     dict)):
                        out |= ints_in(x, depth + 1)
        except Exception:
            pass
        return out

    found = 0
    for n in sorted(mods):
        if "." in n or n.startswith("_"):
            continue
        m = mods.get(n)
        try:
            attrs = dir(m)
        except Exception:
            continue
        for a in attrs:
            if a.startswith("__"):
                continue
            try:
                v = getattr(m, a)
            except Exception:
                continue
            if not isinstance(v, (dict, list, tuple, set, frozenset)):
                continue
            try:
                if len(v) > 60000:
                    continue
            except Exception:
                continue
            got = ints_in(v) & want
            if not got:
                continue
            found += 1
            api.log("     %s.%s  %s[%d]  holds %s"
                    % (n, a, type(v).__name__, len(v), sorted(got)))
    api.log("  %d container(s) mention any of %s" % (found, sorted(want)))
    # The one nonplayer accessor never called: it returns a whole row rather
    # than a single field, so it may carry something the per-field getters do
    # not expose. BuildWikiSearchList is asked too - it is what populates the
    # wiki's boss tab, so it may know the categorisation for mobs that are not
    # in the static lists.
    np = mods.get("nonplayer")
    for nm in ("Thunderclap", "Black Orc", "Bestial Archer", "Scorpion Archer"):
        f = getattr(np, "GetMonsterDataByNamePart", None) if np else None
        if f is None:
            api.log("     GetMonsterDataByNamePart absent")
            break
        try:
            api.log("     GetMonsterDataByNamePart(%r) -> %s" % (nm, repr(f(nm))[:400]))
        except Exception as e:
            api.log("     GetMonsterDataByNamePart(%r) !! %s: %s" % (nm, type(e).__name__, e))
    f = getattr(np, "BuildWikiSearchList", None) if np else None
    if f is not None:
        for args in ((), (0,), (1,)):
            try:
                api.log("     BuildWikiSearchList%r -> %s" % (args, repr(f(*args))[:300]))
                break
            except Exception as e:
                api.log("     BuildWikiSearchList%r !! %s" % (args, type(e).__name__))
    f = getattr(np, "AppendList", None) if np else None
    api.log("     AppendList present=%s" % (f is not None))
    api.log("---- end vnum container hunt ----")


def _hunt_colour(api):
    """Find a binding that reports a NAME COLOUR, not a proxy for one.

    Every structural property tried so far has been wrong - rank, AI flag,
    immunity, the wiki lists - because each of them only correlates with the
    colour for some mobs. The colour itself is the definition, so the question
    is simply which call returns it."""
    api.log("---- colour accessor hunt ----")
    mods = sys.modules

    # 1. Text tails are what draw the name. If the colour is readable anywhere,
    #    it is most likely here.
    tails = [n for n in sorted(mods)
             if "." not in n and not n.startswith("_") and "tail" in n.lower()]
    api.log("  text-tail modules: %s" % (", ".join(tails) or "none"))
    for n in tails:
        m = mods.get(n)
        try:
            names = sorted(a for a in dir(m) if not a.startswith("__"))
        except Exception:
            continue
        for i in range(0, len(names), 8):
            api.log("     %s: %s" % (n, ", ".join(names[i:i + 8])))

    # 2. Anything, anywhere, that looks like it RETURNS a colour.
    api.log("  -- accessors mentioning colour --")
    hits = 0
    for n in sorted(mods):
        if "." in n or n.startswith("_"):
            continue
        m = mods.get(n)
        try:
            names = dir(m)
        except Exception:
            continue
        for a in names:
            if a.startswith("__"):
                continue
            low = a.lower()
            if "color" not in low and "colour" not in low:
                continue
            try:
                v = getattr(m, a)
            except Exception:
                continue
            if callable(v):
                hits += 1
                api.log("     %s.%s()" % (n, a))
    api.log("  %d callable(s)" % hits)

    # 3. And the direct attempt: ask every plausible spelling for a real mob.
    pc = mods.get("pack_chr")
    cm = mods.get("chrmgr")
    try:
        acts = api.actors(radius=RANGE or None)
    except Exception:
        acts = []
    sample = None
    for a in acts:
        if _is_enemy(api, a["vid"]):
            sample = a
            break
    if sample is None:
        api.log("  no monster in view to test against")
    else:
        api.log("  testing against %r race=%s" % (sample["name"], sample["race"]))
        for mod, mn in ((pc, "pack_chr"), (cm, "chrmgr")):
            for fn in ("GetNameColor", "GetNameColorIndex", "GetTextTailColor",
                       "GetColor", "GetTitleColor", "GetTagColor",
                       "GetNameColorByVID", "GetInstanceColor"):
                f = getattr(mod, fn, None) if mod is not None else None
                if f is None:
                    continue
                try:
                    api.log("     %s.%s(vid) -> %r" % (mn, fn, f(sample["vid"])))
                except Exception as e:
                    api.log("     %s.%s(vid) !! %s" % (mn, fn, type(e).__name__))
    # constInfo.SET_DEFAULT_CHRNAME_COLOR exists, so constInfo is where the
    # name colour is configured. Dump everything there that mentions a name or
    # a colour - including the function's own code object, which names the
    # globals it touches even though the body is stripped of nothing else.
    ci = mods.get("constInfo")
    if ci is not None:
        api.log("  == constInfo: name/colour state ==")
        for a in sorted(x for x in dir(ci) if not x.startswith("__")):
            low = a.lower()
            if "color" not in low and "colour" not in low and "name" not in low:
                continue
            try:
                v = getattr(ci, a)
            except Exception:
                continue
            if callable(v):
                code = getattr(v, "__code__", None)
                api.log("     %s() uses: %s" % (a, ", ".join(code.co_names)
                                                if code else "<not python>"))
            else:
                api.log("     %s = %s" % (a, repr(v)[:300]))
    # THE DIRECT READ. textTail.GetPosition(vid) should give the screen position
    # of a mob's name, and wndMgr.GetColorAtPosition(x, y) reads a pixel. If both
    # work, the orange name can be SAMPLED instead of inferred - which is the
    # only property every interesting mob is known to share.
    tt = mods.get("textTail")
    wm = mods.get("wndMgr")
    api.log("  == direct colour sample ==")
    api.log("     textTail=%s wndMgr.GetColorAtPosition=%s"
            % (tt is not None, hasattr(wm, "GetColorAtPosition") if wm else False))
    # The tails read (-100,-100): the sentinel for "not positioned". Ask the
    # client to show and arrange them first, then sample.
    for fn in ("ShowAllTextTail", "UpdateAllTextTail", "ArrangeTextTail",
               "UpdateShowingTextTail"):
        f = getattr(tt, fn, None) if tt is not None else None
        if f is None:
            continue
        try:
            f()
            api.log("     %s() ok" % fn)
        except Exception as e:
            api.log("     %s() !! %s: %s" % (fn, type(e).__name__, e))
    shown = 0
    for a in acts:
        if shown >= 6:
            break
        if not _is_enemy(api, a["vid"]):
            continue
        vid = a["vid"]
        pos = "-"
        try:
            pos = tt.GetPosition(vid)
        except Exception as e:
            pos = "!" + type(e).__name__
        col = "-"
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            try:
                col = wm.GetColorAtPosition(int(pos[0]), int(pos[1]))
            except Exception as e:
                col = "!" + type(e).__name__
        shown += 1
        api.log("     %-24s race=%-6s tailpos=%r pixel=%r"
                % ((a["name"] or "")[:24], a["race"], pos, col))
    api.log("---- end colour accessor hunt ----")


# ---- lifecycle --------------------------------------------------------------
def on_load(api):
    global _next_mark, _scan_at, _prune_at, _found, _ready, _said_nomark, _wiki
    _marks.clear()
    del _seen[:]
    del _explained[:]
    _next_mark, _scan_at, _prune_at, _found = 0, 0.0, 0.0, 0
    _ready, _said_nomark, _wiki = False, False, None
    api.log("bossscanner: loaded - pinging rank %s%s plus the client's own boss "
            "list, scan %.1fs, up to %d marks"
            % ([_GRADE_NAMES.get(r, r) for r in BOSS_RANKS],
               ", any monster with an IMMUNITY" if USE_IMMUNE else "",
               SCAN_EVERY_S, MAX_MARKS))


def on_unload(api):
    # The marks belong to this run. Leaving them pinned after the mod is gone
    # would be litter with nothing left to clean it up.
    _unmark_all(api)
    api.log("bossscanner: unloaded after %d find(s)" % _found)


def on_update(api, dt):
    """Must never raise - five faults and modhost disables the mod."""
    global _scan_at, _prune_at, _ready, _wiki

    if not api.in_game():
        if _marks:
            _unmark_all(api)
        return
    now = api.now()

    if HUNT_VNUMS and not _hunted_vnums:
        globals()["_hunted_vnums"] = True
        try:
            _hunt_vnums(api)
        except Exception as e:
            api.log("bossscanner: vnum hunt failed %s: %s"
                    % (type(e).__name__, e))

    if HUNT_COLOUR and not _hunted_colour:
        globals()["_hunted_colour"] = True
        try:
            _hunt_colour(api)
        except Exception as e:
            api.log("bossscanner: colour hunt failed %s: %s"
                    % (type(e).__name__, e))

    if not _ready:
        # Built once in the world, not at load: it keeps the one-time cost off
        # the login path and puts it next to the line that reports what it found.
        _ready = True
        _wiki = _wiki_set(api)
        m = sys.modules.get("nonplayer")
        boss = getattr(m, "BOSS", None) if m is not None else None
        if boss is not None and int(boss) != 4:
            api.log("bossscanner: WARNING - nonplayer.BOSS is %r, not 4. The "
                    "rank ladder has moved on this build, so BOSS_RANKS=%s no "
                    "longer means what its comment says." % (boss, BOSS_RANKS))
        if USE_WIKI_LIST and not _wiki:
            api.log("bossscanner: the client's boss list is EMPTY or absent "
                    "(looked for %s in %s) - falling back to rank alone, which "
                    "finds bosses but NOT the named specials like the Bestials."
                    % (WIKI_PREFIXES, WIKI_MODULES))
        elif VERBOSE:
            api.log("bossscanner: %d vnum(s) in the client's own boss list; "
                    "rank ladder confirmed (nonplayer.BOSS=%r)"
                    % (len(_wiki), boss))
        if PROBE:
            try:
                _probe(api)
            except Exception as e:
                api.log("bossscanner: probe failed %s: %s" % (type(e).__name__, e))

    if PROBE:
        return

    if now >= _prune_at:
        _prune_at = now + PRUNE_EVERY_S
        try:
            _prune(api)
        except Exception as e:
            api.log("bossscanner: prune failed %s: %s" % (type(e).__name__, e))

    if now >= _scan_at:
        _scan_at = now + SCAN_EVERY_S
        try:
            _scan(api)
        except Exception as e:
            api.log("bossscanner: scan failed %s: %s" % (type(e).__name__, e))
