"""The stable surface mods are written against.

Mods never touch the client's native modules directly. Two reasons that matter
in practice, plus one that matters on principle:

  * The core modules are already renamed as an anti-cheat measure - stock
    Metin2's `player`/`net`/`chr` are `playerm2g2`/`m2netm2g`/`pack_chr` here.
    They can be renamed again on any patch. Behind this façade that is a
    one-file fix; in front of it, every mod breaks.
  * The bindings are thin C wrappers. A wrong argument type is an access
    violation, not a TypeError.
  * Capability boundary, chosen deliberately rather than by accident.

v2 adds movement (`move_to`), channel awareness (`channel`) and cross-client
IPC (`link`). That was a considered widening: the client itself ships
`playerm2g2.CreateAutoBotSettings`, `Get/SetAutoPotionInfo`,
`systemSetting.Get/SetAutoPickup`, `miniMap.SetAutoHuntStatus` and switchbot -
i.e. auto-hunt that fights, loots, potions and casts. Auto-walking a second
character is strictly less capable than the sanctioned feature set.

Still NOT wrapped: `m2netm2g` packet sending. Movement goes through
`AutoMoveToPosition`, the client's own pathing, so the server sees ordinary
movement rather than synthesised packets.
"""

import json
import os
import sys
import time

API_VERSION = 23       # fishing_poll +cast_seq (VID-free cast confirmation)

_CACHE = {}


def _mod(name):
    """Resolve a native module lazily. They are registered during Py init, but
    resolving on first use keeps import order out of the picture."""
    if name in _CACHE:
        return _CACHE[name]
    m = sys.modules.get(name)
    if m is None:
        try:
            m = __import__(name)
        except Exception:
            m = None
    # Do NOT cache a miss. Most client modules exist from Py init, but
    # triarch_native is registered by the stub a second or two into the run, so
    # caching None here made it permanently unavailable to anything that asked
    # too early - which is exactly what made autohunt2 report native_drive=False
    # while the gateway was up and working.
    if m is not None:
        _CACHE[name] = m
    return m


class _Entity(object):
    """A character in the world, addressed by VID."""

    def __init__(self, api, vid_fn):
        self._api = api
        self._vid_fn = vid_fn

    def vid(self):
        try:
            v = self._vid_fn()
            return int(v) if v else 0
        except Exception:
            return 0

    def exists(self):
        return self.vid() != 0

    def name(self):
        c = _mod("pack_chr")
        v = self.vid()
        if c is None or not v:
            return ""
        try:
            return c.GetNameByVID(v) or ""
        except Exception:
            return ""

    def position(self):
        """(x, y, z) in world units, or None if not resolvable.

        This build takes the VID directly - GetPixelPosition(vid) - and the
        no-argument form returns None rather than raising. Stock Metin2 instead
        wants SelectInstance(vid) followed by a bare GetPixelPosition(), so both
        shapes are attempted; measured, not assumed.

        Do NOT use GetActorPixelPosition here: it exists, takes a VID, and
        returns uninitialised garbage (nan, 5.7e-268, ...)."""
        c = _mod("pack_chr")
        v = self.vid()
        if c is None or not v:
            return None

        pos = None
        try:
            pos = c.GetPixelPosition(v)
        except TypeError:
            pass                        # older signature - fall through
        except Exception:
            return None

        if pos is None:
            try:
                # SelectInstance returns None, never a bool - do not test it.
                c.SelectInstance(v)
                pos = c.GetPixelPosition()
            except Exception:
                return None

        try:
            if pos is None or len(pos) < 3:
                return None
            xyz = (float(pos[0]), float(pos[1]), float(pos[2]))
        except Exception:
            return None

        # (-100, -100, -100) is the client's "instance exists but is not placed
        # in the world" sentinel - seen while a map loads and again on logout.
        # Reporting it as a position produces a bogus teleport in the log, so
        # treat it the same as having no position at all.
        if xyz == (-100.0, -100.0, -100.0):
            return None
        return xyz


class Link(object):
    """Filesystem IPC between two clients on the same machine.

    Both clients run from the same folder, so state is keyed by CHARACTER name,
    not by install. Each client publishes <link>/<name>.json and reads the
    others. Writes go to a temp file then os.replace, which is atomic on NTFS -
    a reader never sees a half-written record.

    json and os are resident; sockets and struct are not reliably importable
    inside the embedded interpreter, so a file is the pragmatic channel."""

    STALE_S = 6.0

    def __init__(self, log_dir, log_fn):
        self._log = log_fn
        self.dir = os.path.join(log_dir or ".", "link")
        try:
            if not os.path.isdir(self.dir):
                os.makedirs(self.dir)
        except Exception:
            pass

    @staticmethod
    def _safe(name):
        return "".join(c for c in (name or "") if c.isalnum() or c in "-_") or "unknown"

    def publish(self, name, data):
        path = os.path.join(self.dir, self._safe(name) + ".json")
        tmp = path + ".tmp"
        try:
            f = open(tmp, "w")
            try:
                f.write(json.dumps(data))
            finally:
                f.close()
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def read(self, name):
        """Latest record for a character, or None if missing/stale."""
        path = os.path.join(self.dir, self._safe(name) + ".json")
        try:
            f = open(path, "r")
            try:
                d = json.loads(f.read())
            finally:
                f.close()
        except Exception:
            return None
        try:
            if time.time() - float(d.get("ts", 0)) > self.STALE_S:
                return None
        except Exception:
            return None
        return d


class Api(object):
    VERSION = API_VERSION

    def __init__(self, log_fn, log_dir=None):
        self._log = log_fn
        self._data_dir = log_dir or "."
        self.link = Link(log_dir, log_fn)
        # the shared UI renderer; modhost injects it after construction
        self.ui = None
        self.player = _Entity(self, lambda: _mod("playerm2g2").GetMainCharacterIndex())
        self.target = _Entity(self, lambda: _mod("playerm2g2").GetTargetVID())
        # v14: semantic namespaces over the natives and the 2202 bindings.
        # Wraps self.player rather than replacing it, so every v13 mod is
        # unaffected - see _install_namespaces.
        try:
            _install_namespaces(self)
        except Exception as e:
            self.log("api: namespace install failed %s: %s" % (type(e).__name__, e))

    # ---- output -----------------------------------------------------------
    def log(self, msg):
        """Append to _patcher\\mods.log. Always safe."""
        self._log(str(msg))

    def chat(self, msg):
        """Write a line into the in-game chat window.

        No-op before the chat window exists (login/select phases), which is why
        this reports success rather than raising."""
        c = _mod("chat")
        if c is None:
            return False
        try:
            c.AppendChat(getattr(c, "CHAT_TYPE_INFO", 1), str(msg))
            return True
        except Exception:
            return False

    # ---- persistence -------------------------------------------------------
    def store_save(self, name, obj):
        """Persist a JSON-able object next to mods.log, atomically.

        Centralised so the client's open() constraints are honoured in exactly
        one place: no `with`, no encoding= keyword - builtins.open here is a
        Cython open_ returning system.pack_file and supports neither."""
        path = self.data_path(name)
        tmp = path + ".tmp"
        try:
            f = open(tmp, "w")
            try:
                f.write(json.dumps(obj))
            finally:
                f.close()
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def store_load(self, name, default=None):
        try:
            f = open(self.data_path(name), "r")
            try:
                return json.loads(f.read())
            finally:
                f.close()
        except Exception:
            return default

    def data_path(self, name):
        return os.path.join(self._data_dir, name)

    # ---- state ------------------------------------------------------------
    def now(self):
        return time.time()

    def in_game(self):
        """True once a character is in the world."""
        return self.player.exists() and self.player.position() is not None

    def channel(self):
        """Current channel number, or 0 if unknown."""
        a = _mod("app")
        try:
            return int(a.GetChannel())
        except Exception:
            return 0

    def map_name(self):
        """Current map, or "" if unknown.

        Needed because world coordinates are global across the map atlas: the
        main's x/y on a different map is a real position somewhere else, so
        following it blindly would walk the alt off to nowhere."""
        b = _mod("background")
        try:
            return str(b.GetCurrentMapName() or "")
        except Exception:
            return ""

    def vid_of(self, name):
        """VID of a named character IN THIS CLIENT, or 0.

        VIDs are per-client instance ids - the number the main client reports
        for itself is meaningless here, so a follower must resolve by name."""
        c = _mod("pack_chr")
        if c is None or not name:
            return 0
        try:
            v = int(c.GetVIDByName(name))
        except Exception:
            return 0
        # GetVIDByName returns -1 for "not here" - passing that on produced
        # SetTarget(-1) calls against a vid that cannot exist.
        return v if v > 0 else 0

    def entity(self, vid):
        """Wrap any VID so it reads like api.player / api.target.

        The VID is captured, not re-resolved, so the wrapper goes stale when the
        instance despawns - .position() returns None rather than lying."""
        v = int(vid or 0)
        return _Entity(self, lambda: v)

    def has_instance(self, vid):
        c = _mod("pack_chr")
        try:
            return bool(c.HasInstance(int(vid)))
        except Exception:
            return False

    def instance_types(self):
        """{value: "PLAYER"} read from pack_chr's own INSTANCE_TYPE_* constants.

        Deliberately NOT hardcoded. The stock Metin2 ordering puts PC at 0, and
        assuming that here is wrong: measured live, a player reports type 6 and
        type 0 is an ordinary monster."""
        c = _mod("pack_chr")
        if c is None:
            return {}
        out = {}
        for n in dir(c):
            if n.startswith("INSTANCE_TYPE_"):
                try:
                    out[int(getattr(c, n))] = n[len("INSTANCE_TYPE_"):]
                except Exception:
                    pass
        return out

    def describe(self, vid):
        """Everything cheaply knowable about one loaded instance, or None.

        Only meaningful for a VID that is actually registered - the accessors
        raise for anything else - so has_instance() gates the whole thing.
        Every field is individually guarded: a build that drops one of these
        bindings should cost that field, not the whole record."""
        vid = int(vid or 0)
        if not vid or not self.has_instance(vid):
            return None

        c = _mod("pack_chr")
        m = _mod("chrmgr")

        def get(fn, *a):
            try:
                return fn(*a)
            except Exception:
                return None

        rec = {"vid": vid}
        rec["name"] = get(c.GetNameByVID, vid) or ""
        rec["type"] = get(c.GetInstanceType, vid)
        rec["type_name"] = self.instance_types().get(rec["type"])
        rec["race"] = get(c.GetRaceByVID, vid)
        rec["level"] = get(c.GetLevel, vid)
        rec["guild"] = get(c.GetGuildID, vid)
        rec["dead"] = get(c.IsDead, vid)
        rec["stone"] = get(c.IsStone, vid)
        rec["npc"] = get(c.IsNPC, vid)
        rec["enemy"] = get(c.IsEnemy, vid)
        rec["pc"] = get(m.IsPC, vid) if m is not None else None
        rec["pos"] = self.entity(vid).position()
        return rec

    def set_target(self, vid):
        """Select a character.

        SetTarget is GATED. From the Android build's playerSetTarget:

            mov  w1, #5
            bl   CPythonNetworkStream::GetPhaseWindow(unsigned int)
            ldr  x8, [sp, #8]        ; the 2nd Python argument, default NULL
            cmp  x0, x8
            b.ne skip                ; mismatch -> SetTarget never runs

        So a one-argument call silently does nothing - which is exactly what we
        measured live (selection 0 -> 0). Passing the phase-5 (game) window as
        the second argument satisfies the check. This is an anti-bot guard: it
        breaks any script that calls SetTarget(vid) the stock way."""
        p = _mod("playerm2g2")
        if p is None or not vid:
            return False
        n = _mod("m2netm2g")
        win = None
        if n is not None:
            try:
                win = n.GetPhaseWindow(5)
            except Exception:
                win = None
        try:
            if win is not None:
                p.SetTarget(int(vid), win)
            else:
                p.SetTarget(int(vid))
            return True
        except Exception:
            return False

    def use_quickslot(self, index):
        """Press hotbar slot `index` (0-based: slot 0 is the "1" key).

        RequestUseLocalQuickSlot is the client's own hotkey path, so this is
        exactly what pressing the key does - no skill ids, no packets."""
        p = _mod("playerm2g2")
        if p is None:
            return False
        try:
            p.RequestUseLocalQuickSlot(int(index))
            return True
        except Exception:
            return False

    def quickslot(self, index):
        """(type, value) of a hotbar slot; (0, 0) means empty. type 2 = skill."""
        p = _mod("playerm2g2")
        try:
            return tuple(p.GetLocalQuickSlot(int(index)))
        except Exception:
            return None

    def clear_target(self):
        p = _mod("playerm2g2")
        try:
            p.ClearTarget()
            return True
        except Exception:
            return False

    def select(self, vid):
        """pack_chr.Select - the world-instance selection, distinct from the
        UI target. Used to shift a selection that SetTarget alone will not."""
        c = _mod("pack_chr")
        try:
            c.Select(int(vid))
            return True
        except Exception:
            return False

    def affect_seconds(self, idx):
        """Seconds remaining on affect `idx`, 0 if absent.

        Measured, not assumed: buffwatch caught indices 94/95/96 appearing at
        70/70/85 and counting down once per second to zero, so sub-field 0 is
        the remaining duration."""
        p = _mod("playerm2g2")
        try:
            return int(p.GetAffectData(int(idx), 0)) or 0
        except Exception:
            return 0

    def active_affects(self, indices):
        """{idx: seconds} for those of `indices` currently active."""
        out = {}
        for i in indices:
            s = self.affect_seconds(i)
            if s:
                out[str(i)] = s
        return out

    def affects(self):
        """Raw affect list for the player, or None.

        Shape is build-specific and not yet pinned down - the probe mod dumps
        it. Exposed raw on purpose so a mod can experiment without an api
        change; it will get a typed wrapper once the shape is confirmed."""
        p = _mod("playerm2g2")
        if p is None:
            return None
        try:
            return p.GetAffectData()
        except Exception:
            return None

    # ---- atlas / minimap --------------------------------------------------
    def map_base(self):
        """(baseX, baseY) of the current map in global atlas units, or None.

        Derived rather than exposed: background.GlobalPositionToLocalPosition
        is a plain `p -= base` (CPythonBackground+0x4600), so feeding it zero
        reads the base back out negated."""
        b = _mod("background")
        if b is None:
            return None
        try:
            lx, ly = b.GlobalPositionToLocalPosition(0, 0)
        except Exception:
            return None
        return (-int(lx), -int(ly))

    def atlas_mark(self, mark_id, x, y, label=""):
        """Place (or move) a pulsing mark on the atlas and minimap.

        Coordinates are the ones api.player.position() reports, passed straight
        through. They are already MAP-LOCAL: AddWayPoint scales by
        atlasSize/mapSize with no origin subtraction, and running them through
        background.GlobalPositionToLocalPosition first subtracts the map base a
        second time. On the map measured that turned (38462, 44033) into
        (-883138, -160768), and RenderAtlas skips any mark whose coordinates
        are not both > 0 - so the call reported success and nothing drew.

        Removes first, always. AddWayPoint scans the mark list and gives up if
        the id is already there:

            cmp  dword ptr [eax + 4], edx    ; entry->id == ours?
            je   0x604953                    ; ...then do nothing at all

        so re-adding an existing id is silently a no-op and the mark would sit
        frozen wherever it was first placed.

        Renders as the 15-frame `waypoint%02d.sub` animation - the same pulsing
        icon the server's quest targets use. That animation applies to every
        mark type except 13, and the binding hardcodes type 6."""
        m = _mod("miniMap")
        if m is None:
            return False
        try:
            m.RemoveWayPoint(int(mark_id))
        except Exception:
            pass
        try:
            m.AddWayPoint(int(mark_id), float(x), float(y), str(label))
            return True
        except Exception:
            return False

    def atlas_unmark(self, mark_id):
        m = _mod("miniMap")
        if m is None:
            return False
        try:
            m.RemoveWayPoint(int(mark_id))
            return True
        except Exception:
            return False

    def mark_mob(self, mark_id, vid, label=""):
        """Put the blinking TARGET mark on a live instance, on the minimap AND
        the atlas, and have it FOLLOW that instance.

        atlas_mark uses the Python miniMap.AddWayPoint binding, which hardcodes
        mark type 6 - the animated swirl, and only on the atlas. This calls the
        native underneath it with type 13, the target mark the server's own
        quest-target system uses: it renders on both surfaces and, because a VID
        is attached, CPythonMiniMap::Update rewrites its position every frame, so
        a wandering mob stays marked with nothing to do here. Proven live before
        the native shipped (a type-13 record with computed screen coords lands in
        the waypoint vector).

        Re-marking the same id updates it in place - AddWayPoint removes the id
        first - so a sweep can call this every pass without accumulating marks.

        Returns True on success. False (never an exception) when the stub has no
        minimap_mark native or the minimap object is not up yet, so a caller can
        treat the red dot as a nice-to-have and carry on marking the atlas."""
        tn = self.natives.module()
        if tn is None or not hasattr(tn, "minimap_mark"):
            return False
        try:
            return bool(tn.minimap_mark(int(mark_id), 0.0, 0.0,
                                        int(vid or 0), str(label or "")))
        except Exception:
            return False

    def unmark_mob(self, mark_id):
        """Remove a mark placed by mark_mob (or any waypoint id). False if the
        native is absent or the call fails."""
        tn = self.natives.module()
        if tn is None or not hasattr(tn, "minimap_unmark"):
            return False
        try:
            return bool(tn.minimap_unmark(int(mark_id)))
        except Exception:
            return False

    def pick_up_items(self):
        """PickCloseItemVector - the client's own batch pickup.

        The filtering is entirely native and cannot be widened from here:
        CPythonItem::GetCloseItemVector keeps a ground item only if

            01230db4  cmp  w8, w25        ; w25 = 999999
            01230db8  b.hi -> skip        ; radius = sqrt(999999) ~ 1000 units

        and only if the item's ownership string at +0x3d0 matches, byte for
        byte, the name PickCloseItemVector passes in - which is our own
        GetNameString(). So this can never take a drop that belongs to someone
        else. One SendClickItemPacket goes out per surviving item, so call it
        on a timer rather than every pump."""
        p = _mod("playerm2g2")
        if p is None:
            return False
        try:
            p.PickCloseItemVector()
            return True
        except Exception:
            return False

    def pick_up_money(self):
        """PickCloseMoney - same idea, for dropped yang."""
        p = _mod("playerm2g2")
        if p is None:
            return False
        try:
            p.PickCloseMoney()
            return True
        except Exception:
            return False

    # The client's own batch pickup keeps an item only if it is within ~1000
    # units AND the ownership string matches our name. A filtered sweep has to
    # reproduce both or it sends the server pickup requests it will refuse -
    # which is worse than useless, it is a bot signature.
    PICKUP_RADIUS = 1000.0

    def actors(self, races=None, radius=None, alive_only=True):
        """Every character the client can see, nearest first.

        Each entry is a dict: vid, race, name, pos, dist.

        There is NO binding for this - the character manager never exposed its
        instance map. The stub walks it directly. It is an unordered_map, so the
        walk is a flat linked list rather than a tree.

        `races` filters by race number (or name substring) before any of the
        per-VID binding calls, which matters: those calls are the expensive part
        and a boss scan runs on a timer.

        This is what "find a specific monster" needs and what the autohunt
        filter could never do - FindAndSetNewTarget only ever offers its own
        choice, so vetting it can reject but never SEEK."""
        m = self.natives.module()
        if m is None:
            return []
        want = None
        if races:
            want = set(r for r in races if isinstance(r, int))
        here = self.player.position()
        n = m.actors()
        out = []
        for i in range(n):
            vid = m.actor_at(i)
            if not vid:
                continue
            try:
                race = self.raw.nonplayer.GetRaceNumByVID(vid)
            except Exception:
                continue
            if want is not None and race not in want:
                continue
            pos = None
            dist = None
            try:
                p = self.raw.pack_chr.GetPixelPosition(vid)
                if p and len(p) >= 2:
                    pos = (float(p[0]), float(p[1]))
                    if here is not None:
                        dx, dy = pos[0] - here[0], pos[1] - here[1]
                        dist = (dx * dx + dy * dy) ** 0.5
            except Exception:
                pass
            if radius and dist is not None and dist > radius:
                continue
            name = ""
            try:
                name = self.raw.pack_chr.GetNameByVID(vid) or ""
            except Exception:
                pass
            out.append({"vid": vid, "race": race, "name": name,
                        "pos": pos, "dist": dist})
        out.sort(key=lambda d: d["dist"] if d["dist"] is not None else 1e9)
        return out

    def ground_items(self, radius=None, mine_only=True):
        """Every drop the client knows about, nearest first.

        Each entry is a dict:

            id     ground item id  -> what pick_up() takes
            vnum   item type       -> stable, language-independent
            name   display name    -> 'Arco Ojo Rojo +0', locale-dependent
            owner  ownership name  -> '' when the drop is free for anyone
            pos    (x, y)
            dist   distance from the player, or None if we have no position

        Defaults match the client's own batch pickup: within PICKUP_RADIUS and
        ours to take. Pass radius=0 to disable the distance filter and
        mine_only=False to see everything, which is what a diagnostic wants and
        a pickup loop does not.

        The client has no binding for this - GetCloseItem and
        GetVirtualNumberOfGroundItem are both INLINED on x86, so there is no
        function to call however callable they look in the Android symbols.
        The stub walks CPythonItem's std::map directly instead: key is the
        ground item id, value is an SGroundItemInstance whose +0x04 is the
        vnum. Verified in-game against live drops.

        id   = WHICH drop this is  -> what SendItemPickUpPacket takes
        vnum = WHAT KIND of item   -> what an allow-list matches on

        Returns [] when nothing is on the floor. Raises only if the native
        layer is missing or unwired - an unresolved offset must never be
        mistaken for an empty field."""
        m = self.natives.module()
        if m is None:
            return []
        if radius is None:
            radius = self.PICKUP_RADIUS
        me = (self.player.name() or "") if mine_only else ""
        here = self.player.position()
        n = m.ground_items()
        out = []
        for i in range(n):
            owner = m.ground_item_str(i, 0) or ""
            # An empty ownership string means the drop is free for anyone -
            # that is how yang arrives - so it is NOT a mismatch.
            if mine_only and owner and me and owner != me:
                continue
            x, y = m.ground_item_pos(i)
            dist = None
            if here is not None:
                dx, dy = x - here[0], y - here[1]
                dist = (dx * dx + dy * dy) ** 0.5
                if radius and dist > radius:
                    continue
            out.append({"id": m.ground_item_at(i),
                        "vnum": m.ground_item_vnum(i),
                        "name": m.ground_item_str(i, 1) or "",
                        "owner": owner,
                        "pos": (x, y),
                        "dist": dist})
        out.sort(key=lambda d: d["dist"] if d["dist"] is not None else 1e9)
        return out

    def pick_up(self, item_id):
        """SendItemPickUpPacket - take ONE specific drop by id.

        This binding has always existed and has always been unusable, because
        nothing in Python could tell you a valid id. ground_items() is what
        makes it callable."""
        n = _mod("m2netm2g")
        if n is None:
            return False
        try:
            n.SendItemPickUpPacket(int(item_id))
            return True
        except Exception:
            return False

    def config_path(self):
        """The config file a mod should WRITE to for this character.

        mods/profiles/<character>.json when one exists, otherwise the shared
        mods/config.json. Several clients run from one mods folder, so a UI that
        always writes config.json rewrote every other character's settings too -
        which is the bug profiles exist to fix, and it is only actually fixed if
        writes follow the same resolution the loader uses.

        Never creates anything: profiles are opt-in, and a shared setup stays
        shared until somebody deliberately makes it not."""
        mods = os.path.join(self._data_dir, "..", "mods")
        mods = os.path.normpath(mods)
        try:
            nm = self.player.name() or ""
        except Exception:
            nm = ""
        nm = "".join(c for c in nm if c.isalnum() or c in "-_")
        if nm:
            p = os.path.join(mods, "profiles", nm + ".json")
            if os.path.isfile(p):
                return p
        return os.path.join(mods, "config.json")

    def field(self, name):
        """Read a typed field declared in natives.json, by name.

        Fields are NOT reachable as api.<area>.<name>: _Ns resolves curated
        helpers, natives and bindings, and a field is none of those, so
        `api.player.auto_move_active` raises AttributeError however correctly
        the field is declared and resolved. Mods were each reimplementing this
        against triarch_native privately; this is that, once, in the open.

        Raises rather than returning a default. A caller that cannot tell
        "unavailable" from "0" will read a missing field as a legitimate false
        and act on it - which is exactly how nocollide ended up holding
        collision on while reporting itself ready.
        """
        tn = self.natives.module()
        if tn is None:
            raise RuntimeError("field(%r) needs triarch_native - check "
                               "uriel_stub.log" % name)
        return tn.field(name)

    def actor_pass(self, on, exclude=None):
        """Walk through other actors. Terrain collision is NOT affected.

        `exclude` is an optional (lo, hi) race range that keeps its collision.
        The engine's test has TWO hardcoded exempt ranges, so a band can be
        carved out exactly: the first range covers everything below it, the
        second everything above. Used to leave metin stones solid - they sit at
        race 8005, a band of their own well clear of mobs (401-599) and of the
        mount (20101) - with no per-frame work and no proximity guessing.

        Monsters body-block by DISPLACEMENT, not by stopping you: the engine
        tests each nearby actor, and on a hit pushes your position back out of
        it, every frame. That is why the character walks on the spot instead of
        halting, and why neutering BlockMovement (the *stop*, which terrain
        uses) fixed wall autopathing and did nothing for mobs.

        The test is a race whitelist with two hardcoded exempt ranges. This
        widens the first to cover every race, which is the same mechanism mounts
        already use. Verified live: the displacement stops being applied at all,
        while walls keep stopping you.

        Raises rather than returning False when the native is missing, because
        "collision is off" and "the call silently did nothing" must never look
        the same to a caller that is about to walk into a pack.
        """
        tn = self.natives.module()
        if tn is None:
            raise RuntimeError("actor_pass needs triarch_native - check uriel_stub.log")
        if not hasattr(tn, "actor_pass"):
            raise RuntimeError("this stub has no actor_pass native - rebuild uriel_stub.dll")
        if not exclude:
            return bool(tn.actor_pass(1 if on else 0))
        lo, hi = int(exclude[0]), int(exclude[1])
        if lo <= 0 or hi < lo:
            raise ValueError("actor_pass exclude must be (lo, hi) with lo > 0 and hi >= lo")
        rc = tn.actor_pass(1 if on else 0, lo, hi)
        # 2 means the band was really carved out. A stub built before bands
        # existed ignores the extra arguments and returns 1 - having made
        # EVERYTHING passable, including the races the caller asked to keep
        # solid. Refuse to report that as success.
        if on and rc != 2:
            raise RuntimeError(
                "actor_pass ignored the exclude range (returned %r) - the stub "
                "predates band support; rebuild and redeploy uriel_stub.dll" % rc)
        return bool(rc)

    def terrain_pass(self, on):
        """Walk through WALLS. Deliberately separate from actor_pass.

        This neuters BlockMovement, the *stop* - which is what terrain uses and
        what mobs reach only after their retry loop has given up. Actor
        pass-through is invisible to the server; this is not. The client reports
        its own x/y about 2.5 times a second, so standing where the map forbids
        is self-reporting, and walking manually into illegal terrain has already
        been seen to raise an in-client error dialog - evidence of a legality
        check that is not fully mapped.

        Kept as its own switch so that nothing can enable it as a side effect of
        wanting to walk past a monster.
        """
        tn = self.natives.module()
        if tn is None:
            raise RuntimeError("terrain_pass needs triarch_native - check uriel_stub.log")
        if not hasattr(tn, "terrain_pass"):
            raise RuntimeError("this stub has no terrain_pass native - rebuild uriel_stub.dll")
        return bool(tn.terrain_pass(1 if on else 0))

    def http_post(self, url, body, auth=None, content_type="text/csv"):
        """POST `body` to `url` through the stub's synchronous WinHTTP native.

        Returns the HTTP status code as an int (e.g. 200/201/401), or a NEGATIVE
        sentinel when the request could not be made at all (uriel_stub HttpPostRaw:
        -1 bad url .. -8 no status header). Pure-Windows, no game offsets. It BLOCKS
        the caller for the request (timeouts bound it), so call it off the hot path
        - once per finished scan is fine. `auth` is the full Authorization header
        value, e.g. "Bearer <key>"; None sends no auth header.
        """
        tn = self.natives.module()
        if tn is None:
            raise RuntimeError("http_post needs triarch_native - check uriel_stub.log")
        if not hasattr(tn, "http_post"):
            raise RuntimeError("this stub has no http_post native - rebuild uriel_stub.dll")
        return int(tn.http_post(str(url), str(auth or ""),
                                str(content_type or "text/csv"), str(body or "")))

    def fishing_poll(self):
        """Rod-fishing packet state from the stub: (seq, sub, bite, our_vid, fish_vnum).

        GC_FISHING is a broadcast, so the stub filters to OUR character - it latches
        our VID from the start that answers our own cast - and only these counters
        move for our own fishing:
          seq       - increments on every GC_FISHING packet for us
          sub       - the LAST subheader for us: 0 start, 1 stop, 2 BITE, 3 catch-ok,
                      4 catch-fail, 5 notify
          bite      - increments on every bite (subheader 2), the reel-now window
          our_vid   - the latched character VID (0 until the first cast->start)
          fish_vnum - with a fish-info item equipped, the vnum of the biting fish from
                      the sub=5 NOTIFY (27802 Minnow .. 27823 Goldfish); 0 = junk, or no
                      item -> use a default delay. Lets the mod time each species.
          cast_seq  - increments on every OUTGOING CG_FISHING (0x34). VID-free, so the
                      mod confirms a cast reached the wire (vs a dropped SendInput)
                      without depending on the fragile own-VID latch.
        The bite is native-only - no OnFishing* Python callback fires for it - so
        this poll is the only way to time the reel. bite is monotonic: remember the
        last value and reel when it grows, and a 4 Hz poll can never miss a bite.
        Raises if the stub lacks the native (rebuild + restart uriel_stub.dll).
        """
        tn = self.natives.module()
        if tn is None:
            raise RuntimeError("fishing_poll needs triarch_native - check uriel_stub.log")
        if not hasattr(tn, "fishing_poll"):
            raise RuntimeError("this stub has no fishing_poll native - rebuild uriel_stub.dll")
        return tuple(int(x) for x in tn.fishing_poll())

    def key_event(self, vk, scan, down):
        """Inject a raw keyboard event (by SCANCODE, DirectInput-visible) via the stub.

        Some keys are POLLED by the engine straight from the device and never reach
        Python OnKeyDown - Space (the fishing cast/reel) is one - so a mod cannot
        press them any other way, and the client's Python has no ctypes. The stub
        uses SendInput; the event goes to the focused window. `down`: 1 down, 0 up.
        Hold across a tick (down, then up next tick) for a real press. Returns the
        number of events injected (1 on success)."""
        tn = self.natives.module()
        if tn is None or not hasattr(tn, "key_event"):
            raise RuntimeError("key_event needs a stub with the native - rebuild uriel_stub.dll")
        return int(tn.key_event(int(vk), int(scan), 1 if down else 0))

    def auto_pickup(self, on=None):
        """The client's own AUTO_PICK setting - CPythonSystem byte at +0x265d,
        the one the options window writes. Pass nothing to read it.

        Worth knowing: this flag does NOT gate PickCloseItemVector. That
        function's early-out is on the network-stream singleton, not on this
        byte - so picking up works whether or not the setting is on. It is
        exposed for consistency with the options window, not because it is
        required."""
        s = _mod("systemSetting")
        if s is None:
            return None
        try:
            if on is None:
                return bool(s.GetAutoPickup())
            s.SetAutoPickup(1 if on else 0)
            return bool(on)
        except Exception:
            return None

    def move_to(self, x, y):
        """Walk the main character toward (x, y) using the client's own pathing.

        This is playerm2g2.AutoMoveToPosition - the same auto-move the shipped
        auto-hunt and quest navigation use. Returns False if unavailable."""
        p = _mod("playerm2g2")
        if p is None:
            return False
        try:
            p.AutoMoveToPosition(float(x), float(y))
            return True
        except Exception:
            return False

    def attack(self, vid):
        """Attack `vid`. Pass 0 (or nothing) to stop.

        There is NO Python binding that attacks a chosen target on this build,
        and the two that look like they would cannot:

          * AttackPickedActor() attacks OLD_GetPickedInstanceVID - whatever the
            MOUSE CURSOR is over - and nothing can set that pick from a script.
          * SetAttackKeyState() never reaches the auto-attack target, and holds
            the key down, which blocks movement.

        The client itself attacks through CPythonPlayer::__OnPressActor, which
        writes an auto-attack target that __Update_AutoAttack then services every
        frame until the target dies. That function is not exposed, so the stub
        bridges it: we publish the VID in TRIARCH_ATTACK and uriel_stub calls
        __OnPressActor on the next frame. The stub only acts when the value
        CHANGES, because __Update_AutoAttack sustains the swing on its own.

        The same environment channel the mod bootstrap already uses to report
        status back through TRIARCH_MODS, so no new mechanism is involved.

        When the developers ship a real binding this becomes a one-line call and
        nothing above it has to change."""
        try:
            os.environ["TRIARCH_ATTACK"] = str(int(vid or 0))
            return True
        except Exception:
            return False

    def attack_available(self):
        """Whether the stub reports the native bridge as armed.

        The stub writes TRIARCH_ATTACK_OK once it has resolved its offsets, so a
        mod can degrade gracefully on a client patched before the bridge existed
        rather than silently failing to attack."""
        try:
            return os.environ.get("TRIARCH_ATTACK_OK") == "1"
        except Exception:
            return False


# ===========================================================================
# v14 - semantic namespaces over BOTH backends
#
# Two kinds of name live side by side, and the case tells you which:
#
#   snake_case   ours. Curated, guarded, stable across client patches.
#                api.player.position(), api.move_to(...)
#   CamelCase    the client's own, raw. Either a native reached through
#                triarch_native, or a Python binding on one of the area's
#                modules.
#
# That split is what lets api.player.position() and api.player.GetTargetVID()
# coexist without either shadowing the other, and it is why every mod written
# against v13 keeps working untouched.
#
# Natives resolve BEFORE bindings, so a native added later could silently
# repoint an existing mod at a different implementation. _shadow() checks for
# that and refuses rather than letting it happen quietly.
# ===========================================================================

_AREAS = {
    "player":  ["playerm2g2", "skill", "petskill"],
    "world":   ["pack_chr", "chrmgr", "nonplayer"],
    "shop":    ["shop", "itemshop", "m2netm2g"],
    "market":  ["offlineshop", "shopSearch"],
    "storage": ["safebox", "cube", "switchbot"],
    "social":  ["guild", "messenger", "whispermgr", "chat"],
    "session": ["app", "m2netm2g", "ServerStateChecker"],
    "ui":      ["wndMgr", "grp", "miniMap", "render_manager"],
    "items":   ["item"],
    "map":     ["background", "fly"],
}

# Where two modules in one area export the same name, say which one wins.
# Without an entry the lookup RAISES rather than picking by list order:
# ordering is easy to change by accident, and a silent repoint is the worst
# possible outcome for a shipped mod.
_COLLISION_OVERRIDES = {
    ("shop", "GetItemPrice"):        "shop",
    ("shop", "GetItemCount"):        "shop",
    ("shop", "GetItemID"):           "shop",
    ("shop", "Open"):                "shop",
    ("shop", "Close"):               "shop",
    ("shop", "IsOpen"):              "shop",
    ("market", "GetItemID"):         "offlineshop",
    ("market", "GetItemVnum"):       "offlineshop",
    ("market", "GetItemCount"):      "offlineshop",
    ("market", "GetItemPrice"):      "offlineshop",
    ("market", "GetItemMetinSocket"): "offlineshop",
    ("player", "GetSkillCoolTime"):  "playerm2g2",
    ("player", "GetIconImage"):      "skill",
    ("world",  "IsDead"):            "pack_chr",
    # Both a native and a binding, and they are the SAME function - the binding
    # is the client's own front door, so prefer it and leave the native for
    # api.raw / direct triarch_native use.
    ("player", "CreateAutoBotSettings"): "playerm2g2",
    # market: offlineshop is the live shop, shopSearch is the browse/search
    # view over other people's shops. Item accessors on the former are what a
    # mod managing ITS OWN shop wants.
    ("market", "GetItemAttribute"):  "offlineshop",
    ("market", "GetItemEventTime"):  "offlineshop",
    ("market", "GetItemEvolution"):  "offlineshop",
    ("market", "GetItemLink"):       "offlineshop",
    ("market", "GetItemRefinePerc"): "offlineshop",
    ("market", "GetItemTimeType"):   "offlineshop",
    ("market", "GetItemTradeLimit"): "offlineshop",
    # map: background is the world; fly is the projectile subsystem that
    # happens to share two generic verbs.
    ("map", "Render"): "background",
    ("map", "Update"): "background",
    # session: app owns process-level lifecycle; ServerStateChecker is a probe.
    ("session", "Create"): "app",
    # social: chat owns the chat window; whispermgr owns whisper sessions.
    # Generic verbs on both, so name the owner of the concept.
    ("social", "Clear"):        "chat",
    ("social", "ClearWhisper"): "whispermgr",
    ("social", "Destroy"):      "guild",
    # storage: safebox is the account bank, cube is the crafting UI.
    ("storage", "Clear"): "safebox",
    # ui: wndMgr is the window system - the generic verbs belong to it, and
    # miniMap/grp keep theirs under api.raw.
    ("ui", "Destroy"):  "wndMgr",
    ("ui", "Hide"):     "wndMgr",
    ("ui", "SetAlpha"): "wndMgr",
    ("ui", "SetColor"): "wndMgr",
    ("ui", "SetDiffuseColor"): "wndMgr",
    ("ui", "SetRotation"):     "wndMgr",
    ("ui", "SetScale"):        "wndMgr",
    ("ui", "Show"):            "wndMgr",
}

# One stack slot per dword. i64/u64 occupy TWO - the reason UseAutoSkills
# cleans 16 bytes for three arguments, and the reason a naive len(args)*4
# check would be wrong.
_WIDE = ("i64", "u64")


class _Natives(object):
    """The native side of a namespace, driven by natives.json.

    Loaded from the file rather than duplicated here so the registry stays the
    single source of truth for the resolver, the stub, the docs and this."""

    def __init__(self, api):
        self._api = api
        self.by_name = {}
        self.by_area = {}
        path = None
        try:
            path = os.path.join(os.path.dirname(__file__), "natives.json")
            reg = json.loads(open(path).read())
        except Exception as e:
            api.log("api: no natives.json (%s) - native calls unavailable"
                    % type(e).__name__)
            return
        for n in reg.get("natives", []):
            self.by_name[n["name"]] = n
            if n.get("area"):
                self.by_area.setdefault(n["area"], set()).add(n["name"])
        api.log("api: %d native(s) registered from natives.json" % len(self.by_name))

    def module(self):
        return _mod("triarch_native")

    def available(self):
        return self.module() is not None

    def call(self, name, args):
        spec = self.by_name.get(name)
        if spec is None:
            raise AttributeError("no native called %r" % name)
        # Arity first. It is a mistake in the CALLER and should surface the
        # same way whether or not the stub happens to be present - otherwise a
        # missing gateway masks a real bug in the mod.
        want = spec.get("args", [])
        if len(args) != len(want):
            raise TypeError("%s expects %d argument(s) %s, got %d"
                            % (name, len(want),
                               [a["name"] for a in want], len(args)))
        tn = self.module()
        if tn is None:
            raise RuntimeError(
                "native %r needs triarch_native, which the stub did not "
                "register - check uriel_stub.log for the NATIVE: lines" % name)
        dwords = []
        for a, v in zip(want, args):
            iv = int(v)
            if a["type"] in _WIDE:
                dwords.append(iv & 0xFFFFFFFF)
                dwords.append((iv >> 32) & 0xFFFFFFFF)
            else:
                dwords.append(iv & 0xFFFFFFFF)
        return tn.call(name, *dwords)


class _Ns(object):
    """One semantic area: curated helpers, then natives, then bindings."""

    def __init__(self, api, area, modules, curated=None):
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_area", area)
        object.__setattr__(self, "_modules", modules)
        object.__setattr__(self, "_curated", curated)
        object.__setattr__(self, "_cache", {})

    def __repr__(self):
        return "<api.%s over %s>" % (self._area, "+".join(self._modules))

    def _shadow(self, name):
        """True if a native name also exists as a binding in this area."""
        for m in self._modules:
            if hasattr(_mod(m), name):
                return m
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        cached = self._cache.get(name)
        if cached is not None:
            return cached

        # 1. curated snake_case - ours, and it wins
        cur = self._curated
        if cur is not None:
            got = getattr(cur, name, None)
            if got is not None:
                return got

        api = self._api
        # 2. natives
        nat = api.natives
        if name in nat.by_area.get(self._area, ()):  # noqa: E501
            clash = self._shadow(name)
            pick = _COLLISION_OVERRIDES.get((self._area, name))
            if clash and pick is None:
                raise AttributeError(
                    "%r is BOTH a native and a %s binding. Preferring one "
                    "silently would repoint existing callers, so this refuses. "
                    "Add ((%r, %r) -> 'native' or %r) to _COLLISION_OVERRIDES, "
                    "or use api.raw.%s.%s."
                    % (name, clash, self._area, name, clash, clash, name))
            if clash and pick != "native":
                pass                      # fall through to the binding below
            else:

                def _bound(*a, **k):
                    if k:
                        raise TypeError(
                            "native %r takes positional arguments only" % name)
                    return nat.call(name, a)
                _bound.__name__ = str(name)
                self._cache[name] = _bound
                return _bound

        # 3. bindings, first module that has it - unless it is ambiguous
        # dedupe: several modules export a name twice from two PyMethodDef
        # tables (IsAntiFlagBySlot, SendPetAttrChangePacket), and "playerm2g2
        # and playerm2g2 disagree" is not a decision anybody can make.
        owners, _seen = [], set()
        for m in self._modules:
            if m not in _seen and hasattr(_mod(m), name):
                _seen.add(m)
                owners.append(m)
        if not owners:
            near = self._suggest(name)
            raise AttributeError(
                "api.%s has no %r%s" % (self._area, name,
                                        (" - did you mean %s?" % near) if near else ""))
        if len(owners) > 1:
            pick = _COLLISION_OVERRIDES.get((self._area, name))
            if pick not in owners:
                raise AttributeError(
                    "%r is exported by %s in api.%s. Which one is a decision, "
                    "not a default - add it to _COLLISION_OVERRIDES, or use "
                    "api.raw.<module>.%s."
                    % (name, " and ".join(owners), self._area, name))
            owners = [pick]
        got = getattr(_mod(owners[0]), name)
        self._cache[name] = got
        return got

    def _suggest(self, name):
        low = name.lower()
        out = []
        for m in self._modules:
            mod = _mod(m)
            if mod is None:
                continue
            for a in dir(mod):
                if a.startswith("_"):
                    continue
                al = a.lower()
                if len(al) < 4 or len(low) < 4:
                    continue
                if low in al or al in low:
                    out.append(a)
                    if len(out) >= 4:
                        return ", ".join(out)
        return ", ".join(out)

    def names(self):
        """Everything reachable here - for discovery from a mod or the console."""
        out = set(self._api.natives.by_area.get(self._area, ()))
        for m in self._modules:
            mod = _mod(m)
            if mod is not None:
                out.update(a for a in dir(mod) if not a.startswith("_"))
        return sorted(out)


class _RawModule(object):
    """api.raw.<module>.<Name> - unambiguous, always available, no policy."""

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, name):
        m = _mod(self._name)
        if m is None:
            raise AttributeError("module %r is not loaded" % self._name)
        return getattr(m, name)

    def __repr__(self):
        return "<api.raw.%s>" % self._name


class _Raw(object):
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _RawModule(name)

    def __repr__(self):
        return "<api.raw - any client module by its real name>"


def _install_namespaces(api):
    """Wire the areas onto an Api instance.

    api.player keeps its v13 _Entity behaviour as the curated layer, so
    api.player.position() and api.player.GetTargetVID() both work."""
    api.natives = _Natives(api)
    api.raw = _Raw()
    for area, mods in _AREAS.items():
        curated = api.player if area == "player" else None
        setattr(api, area, _Ns(api, area, mods, curated))
    api.log("api: v%d namespaces: %s" % (API_VERSION, ", ".join(sorted(_AREAS))))
