"""routes - scripted travel that coordinates the other mods.

WHAT IT SOLVES
--------------
autohunt2 farms ONE anchor. Moving the character between spots, across a map
exit, or to another channel was either manual or a single hard-wired hop
(autohunt2 LOOP_CHANNELS). This mod runs a ROUTE - a list of steps - and hands
control to a worker mod (autohunt2 today, automining tomorrow) at the points
where something is to be done, then takes it back and walks on.

A route is plain data (config `ROUTES` or `_patcher/routes.json`, the latter
written by the `/route save` recorder), so a new farming circuit is a file, not
a code change:

    {"repeat": true,
     "steps": [
        {"op": "channel", "ch": 2},
        {"op": "path",  "points": [[655, 585], [660, 590]]},
        {"op": "loop",  "points": [[655,585],[670,585],[670,600],[655,600]],
                        "hunt": true, "channels": [1,2,3,4,5], "laps": 0},
        {"op": "warp",  "x": 700, "y": 640, "map": "metin2_map_b1"},
        {"op": "goto",  "x": 123, "y": 456},
        {"op": "hunt",  "idle_s": 8},
        {"op": "task",  "name": "mine", "max_s": 600},
        {"op": "wait",  "s": 5}]}

Steps
  goto     walk to (x, y). Coordinates auto-scale exactly like /goto: values
           under 20000 are SITE coords (x100), larger ones are raw world units.
  path     walk the points in order (a one-way route between two places).
  loop     a CLOSED circuit. At every node, if "hunt" is true, autohunt2 is
           started right there (its anchor = the node) and the route waits until
           it reports the area clear, then walks to the next node. "laps" (0 =
           forever) and "channels" (after each full lap, hop to the next listed
           channel) are optional.
  channel  change channel through the chanswap mod (no re-login).
  warp     walk onto a map exit at (x, y) and wait for the map to change
           (optionally to "map"). The server does the warp when the character
           reaches the exit; we only walk and watch background.GetCurrentMapName.
  hunt     start autohunt2 here and wait for "clear" - a loop node without walking.
  task     hand control to ANY worker mod by name (future automining) - see the
           protocol below - and wait for it to report done (or "max_s").
  wait     stand still for "s" seconds.

HOW MODS TALK
-------------
Mods run in isolated namespaces, so requests ride os.environ, the channel the
loot handshake and chanswap already use. Every request key is consumed exactly
once by its reader.

    TRIARCH_ROUTE_ACTIVE   "1" while a route runs. autohunt2 suppresses its own
                           LOOP_CHANNELS hop while this is set - the route owns
                           positioning and channel choice.
    TRIARCH_HUNT_REQUEST   "start" | "stop"  -> autohunt2 (consumed once)
    TRIARCH_HUNT_STATE     "off" | "busy" | "idle" | "clear"  <- autohunt2, every
                           tick. "clear" = on, nothing engaged, and the client's
                           own FindVictim has come back empty RETURN_AFTER_FAILS
                           times in a row. "idle" = on, nothing engaged, but the
                           empty-search counter has not tripped (ONLY_* profiles
                           never search for trash, so they never reach "clear").
    TRIARCH_CHAN_REQUEST   "<ch>" -> chanswap (consumed once)
    TRIARCH_TASK_REQUEST   "<name> start" | "<name> stop" -> the worker mod
    TRIARCH_TASK_STATE     "<name>:off|busy|done"  <- the worker mod, every tick

A worker that is not loaded never answers; the route logs it and moves on rather
than waiting for ever (HUNT_START_TIMEOUT_S / TASK_START_TIMEOUT_S).

WALKING
-------
api.move_to is playerm2g2.AutoMoveToPosition - NOT a pathfinder, a straight
step-and-slide toward the destination (see unstick). So a leg is re-issued while
we are away and not travelling, "travelling" being the measured two-flag test
from nocollide (auto_move_active AND player_state == WALK_STATE), and a leg
that makes no progress for STUCK_S gets a perpendicular sidestep, sides
alternating, before it is re-issued. Routes should use enough points that each
leg is a clear straight line; the recorder (`/route mark`) makes that cheap.

CHAT COMMANDS (swallowed, never reach the server)
  /route list                     routes known (config + routes.json)
  /route run <name>               start
  /route stop                     stop (also stops a hunt we started)
  /route status
  /route show <name>
  /route mark                     append the current position to the draft
  /route mark undo | clear
  /route save <name> loop|path [hunt]   draft -> a one-step route, persisted
  /route delete <name>
  /route hop <ch>                 channel change by itself (chanswap)
"""

CAPABILITIES = ["read", "move", "net"]

import sys
import os
import json

# ---- configuration (every key overridable from config / profile) ----------
WALK_STATE = 138            # build constant, stamped by the patcher - do not pin
ARRIVE_DIST = 250.0         # a leg is done within this many world units
REISSUE_S = 3.0             # re-issue the walk while away and not travelling
STUCK_S = 4.0               # no progress for this long -> sidestep
MIN_PROGRESS = 60.0         # units per STUCK_S that still count as moving
SIDESTEP = 1100.0           # length of the perpendicular escape leg
MAX_STUCK_TRIES = 4         # sidesteps before the leg is given up
LEG_TIMEOUT_S = 180.0       # a single leg never takes longer than this
CHANNEL_TIMEOUT_S = 60.0    # chanswap flow budget
WARP_TIMEOUT_S = 90.0       # walk-to-exit + load budget
SETTLE_S = 3.0              # after a channel/map load, before walking again
HUNT_IDLE_S = 6.0           # "clear" reported this long -> node is done
HUNT_IDLE_NOCLEAR_S = 20.0  # "idle" (never "clear") this long -> node is done
HUNT_MAX_S = 0.0            # cap per hunt node, 0 = none
HUNT_START_TIMEOUT_S = 8.0  # autohunt2 did not pick the request up -> skip
TASK_START_TIMEOUT_S = 8.0
AUTORUN = ""                # route to run once in-game (use a profile for this)
VERBOSE = True
ROUTES = {}                 # routes from config; routes.json entries override

CMD = "/route"
SCALE_BELOW = 20000.0
STORE = "routes.json"

ENV_ROUTE = "TRIARCH_ROUTE_ACTIVE"
ENV_HUNT_REQ = "TRIARCH_HUNT_REQUEST"
ENV_HUNT_STATE = "TRIARCH_HUNT_STATE"
ENV_CHAN = "TRIARCH_CHAN_REQUEST"
ENV_TASK_REQ = "TRIARCH_TASK_REQUEST"
ENV_TASK_STATE = "TRIARCH_TASK_STATE"

# ---- state ----------------------------------------------------------------
_api = None
_patched = []
_run = None             # {"name", "route", "i", "st", "since"} or None
_draft = []             # recorded points (site coords)
_saved = {}             # routes.json content
_autorun_at = 0.0
_autorun_done = False
_hunt_ours = False      # we asked autohunt2 to start; stop it on abort
_task_ours = ""         # worker we asked to start
_side = 1               # alternate sidestep directions
_warned = set()


# ---- helpers --------------------------------------------------------------
def _mod(name):
    m = sys.modules.get(name)
    if m is None:
        try:
            __import__(name)
            m = sys.modules.get(name)
        except Exception:
            m = None
    return m


def _log(msg):
    if _api is not None:
        _api.log("routes: " + msg)


def _say(msg):
    _log(msg)
    if _api is not None:
        _api.chat("routes: " + msg)


def _warn_once(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    _log(msg)


def _env_get(k):
    try:
        return os.environ.get(k, "") or ""
    except Exception:
        return ""


def _env_set(k, v):
    try:
        os.environ[k] = str(v)
        return True
    except Exception:
        return False


def _to_world(x, y):
    x, y = float(x), float(y)
    if abs(x) < SCALE_BELOW and abs(y) < SCALE_BELOW:
        return x * 100.0, y * 100.0
    return x, y


def _d(a, b):
    dx, dy = a[0] - b[0], a[1] - b[1]
    return (dx * dx + dy * dy) ** 0.5


def _pos(api):
    try:
        p = api.player.position()
    except Exception:
        return None
    return (p[0], p[1]) if p else None


def _travelling(api):
    """auto_move_active AND player_state == WALK_STATE (nocollide's measured
    test). Unreadable -> None, and the caller falls back to timed re-issue."""
    try:
        return bool(api.field("auto_move_active")) and \
            int(api.field("player_state")) == int(WALK_STATE)
    except Exception:
        _warn_once("fields", "auto_move_active/player_state unreadable - "
                   "re-issuing legs on the timer alone")
        return None


def _routes_all():
    r = dict(ROUTES or {})
    r.update(_saved or {})
    return r


def _persist(api):
    if not api.store_save(STORE, _saved):
        _log("could not write %s" % STORE)


# ---- leg walker -----------------------------------------------------------
def _leg(api, now, st, x, y):
    """Walk toward world (x, y). True = arrived, False = in progress,
    "fail" = given up (st["fail"] says why)."""
    global _side
    pos = _pos(api)
    if pos is None:
        return False                             # loading / not placed
    if _d(pos, (x, y)) <= ARRIVE_DIST:
        return True
    if "t0" not in st:
        st.update({"t0": now, "mark": pos, "mark_t": now, "tries": 0,
                   "next": 0.0, "issued": 0})
    if now - st["t0"] > LEG_TIMEOUT_S:
        st["fail"] = "timeout"
        return "fail"

    if _d(pos, st["mark"]) >= MIN_PROGRESS:
        st["mark"], st["mark_t"] = pos, now
    elif now - st["mark_t"] >= STUCK_S and st["issued"] >= 2:
        # Two issues without progress, not one: the first walk is often issued
        # while the character is still swinging at something (measured: a
        # 7900-unit leg "stuck" twice in its first 8s, then ran at 440 u/s).
        st["tries"] += 1
        if st["tries"] > MAX_STUCK_TRIES:
            st["fail"] = "stuck"
            return "fail"
        hx, hy = x - pos[0], y - pos[1]
        n = (hx * hx + hy * hy) ** 0.5 or 1.0
        px, py = -hy / n * _side, hx / n * _side
        _side = -_side
        api.move_to(pos[0] + px * SIDESTEP, pos[1] + py * SIDESTEP)
        _log("stuck %.0f from (%.0f,%.0f) - sidestep #%d"
             % (_d(pos, (x, y)), x, y, st["tries"]))
        st["mark"], st["mark_t"] = pos, now
        st["next"] = now + STUCK_S               # let the sidestep run
        return False

    if now >= st["next"] and _travelling(api) is not True:
        st["next"] = now + REISSUE_S
        st["issued"] += 1
        if not api.move_to(x, y):
            st["fail"] = "move_to unavailable"
            return "fail"
    return False


# ---- workers --------------------------------------------------------------
def _hunt_here(api, now, st, idle_s=None, max_s=None):
    """Start autohunt2 on the spot and wait for it to run dry."""
    global _hunt_ours
    idle_s = HUNT_IDLE_S if idle_s is None else float(idle_s)
    max_s = HUNT_MAX_S if max_s is None else float(max_s)
    state = _env_get(ENV_HUNT_STATE)

    if "t0" not in st:
        st.update({"t0": now, "clear_t": 0.0, "idle_t": 0.0, "stopping": 0.0})
        _env_set(ENV_HUNT_REQ, "start")
        _hunt_ours = True
        _log("hunt: requested start (state was %r)" % state)
        return False

    if st["stopping"]:
        if state == "off" or now - st["stopping"] > 5.0:
            _hunt_ours = False
            return True
        return False

    if state == "off" or not state:
        if now - st["t0"] > HUNT_START_TIMEOUT_S:
            _say("hunt: autohunt2 did not start (state %r) - moving on" % state)
            _hunt_ours = False
            return True
        return False

    done = False
    if state == "clear":
        st["clear_t"] = st["clear_t"] or now
        done = now - st["clear_t"] >= idle_s
    else:
        st["clear_t"] = 0.0
    if state in ("clear", "idle"):
        st["idle_t"] = st["idle_t"] or now
        done = done or now - st["idle_t"] >= HUNT_IDLE_NOCLEAR_S
    else:
        st["idle_t"] = 0.0
    if max_s and now - st["t0"] >= max_s:
        done = True
    if done:
        _log("hunt: node done after %.0fs (state %s)" % (now - st["t0"], state))
        _env_set(ENV_HUNT_REQ, "stop")
        st["stopping"] = now
    return False


def _task_here(api, now, st, name, max_s=0.0):
    """Generic worker hand-off: '<name> start' -> wait for '<name>:done'."""
    global _task_ours
    state = _env_get(ENV_TASK_STATE)
    mine = state.startswith(name + ":")
    sub = state.split(":", 1)[1] if mine else ""
    if "t0" not in st:
        st.update({"t0": now, "stopping": 0.0})
        _env_set(ENV_TASK_REQ, "%s start" % name)
        _task_ours = name
        _log("task %s: requested start" % name)
        return False
    if st["stopping"]:
        if sub in ("off", "") or now - st["stopping"] > 5.0:
            _task_ours = ""
            return True
        return False
    if not mine or sub == "off":
        if now - st["t0"] > TASK_START_TIMEOUT_S:
            _say("task %s: no worker answered - moving on" % name)
            _task_ours = ""
            return True
        return False
    if sub == "done" or (max_s and now - st["t0"] >= float(max_s)):
        _env_set(ENV_TASK_REQ, "%s stop" % name)
        st["stopping"] = now
    return False


def _channel_to(api, now, st, ch):
    ch = int(ch)
    cur = api.channel()
    if "t0" not in st:
        st.update({"t0": now, "landed": 0.0})
        if cur == ch:
            return True
        _env_set(ENV_CHAN, str(ch))
        _log("channel: requested CH%d (on CH%s)" % (ch, cur))
        return False
    if cur == ch and api.in_game():
        st["landed"] = st["landed"] or now
        if now - st["landed"] >= SETTLE_S:
            _log("channel: landed on CH%d" % ch)
            return True
        return False
    st["landed"] = 0.0
    if now - st["t0"] > CHANNEL_TIMEOUT_S:
        st["fail"] = "channel change did not land (chanswap loaded?)"
        return "fail"
    return False


# ---- step handlers --------------------------------------------------------
def _pt(step, key_x="x", key_y="y"):
    return _to_world(step.get(key_x, 0), step.get(key_y, 0))


def _step_goto(api, now, step, st):
    x, y = _pt(step)
    return _leg(api, now, st, x, y)


def _step_path(api, now, step, st):
    pts = step.get("points") or []
    i = st.setdefault("i", 0)
    if i >= len(pts):
        return True
    x, y = _to_world(pts[i][0], pts[i][1])
    r = _leg(api, now, st.setdefault("leg", {}), x, y)
    if r is True or r == "fail":
        if r == "fail":
            _say("path: point %d (%g,%g) %s - skipping" % (i, pts[i][0], pts[i][1], st["leg"].get("fail")))
        st["i"], st["leg"] = i + 1, {}
    return False


def _step_loop(api, now, step, st):
    pts = step.get("points") or []
    if not pts:
        return True
    laps = int(step.get("laps", 0) or 0)
    chans = step.get("channels") or []
    if "i" not in st:
        st.update({"i": 0, "lap": 0, "phase": "walk", "sub": {}, "ci": -1})
        if chans:
            cur = api.channel()
            st["ci"] = chans.index(cur) if cur in chans else -1
    i = st["i"]
    if st["phase"] == "walk":
        x, y = _to_world(pts[i][0], pts[i][1])
        r = _leg(api, now, st["sub"], x, y)
        if r == "fail":
            _say("loop: node %d %s - skipping" % (i, st["sub"].get("fail")))
        if r is True and step.get("hunt", True):
            st["phase"], st["sub"] = "hunt", {}
            _log("loop: node %d/%d lap %d - hunting" % (i + 1, len(pts), st["lap"] + 1))
            return False
        if r is True or r == "fail":
            st["phase"], st["sub"] = "next", {}
        return False
    if st["phase"] == "hunt":
        if _hunt_here(api, now, st["sub"], step.get("idle_s"), step.get("max_s")):
            st["phase"], st["sub"] = "next", {}
        return False
    if st["phase"] == "chan":
        r = _channel_to(api, now, st["sub"], chans[st["ci"]])
        if r == "fail":
            _say("loop: %s - continuing on CH%s" % (st["sub"].get("fail"), api.channel()))
        if r is True or r == "fail":
            st["phase"], st["sub"], st["i"] = "walk", {}, 0
        return False
    # phase "next": advance node / lap / channel
    i += 1
    if i < len(pts):
        st["i"], st["phase"], st["sub"] = i, "walk", {}
        return False
    st["lap"] += 1
    _log("loop: lap %d complete" % st["lap"])
    if laps and st["lap"] >= laps:
        return True
    if chans:
        st["ci"] = (st["ci"] + 1) % len(chans)
        st["phase"], st["sub"] = "chan", {}
        return False
    st["i"], st["phase"], st["sub"] = 0, "walk", {}
    return False


def _step_channel(api, now, step, st):
    return _channel_to(api, now, st, step.get("ch", 1))


def _step_warp(api, now, step, st):
    want = str(step.get("map", "") or "")
    if "map0" not in st:
        st.update({"map0": api.map_name(), "t0": now, "landed": 0.0, "leg": {}})
        _log("warp: from %r toward (%g,%g)%s"
             % (st["map0"], step.get("x", 0), step.get("y", 0),
                " expecting %r" % want if want else ""))
    cur = api.map_name()
    changed = bool(cur) and cur != st["map0"] and (not want or cur == want)
    if changed:
        if not api.in_game():
            return False
        st["landed"] = st["landed"] or now
        if now - st["landed"] >= SETTLE_S:
            _say("warp: now on %s" % cur)
            return True
        return False
    if now - st["t0"] > WARP_TIMEOUT_S:
        st["fail"] = "map did not change (still %r)" % cur
        return "fail"
    x, y = _pt(step)
    r = _leg(api, now, st["leg"], x, y)
    if r == "fail":
        st["fail"] = "walk to exit %s" % st["leg"].get("fail")
        return "fail"
    if r is True and "at_exit" not in st:
        st["at_exit"] = now
        _log("warp: standing on the exit point, waiting for the server")
    return False


def _step_hunt(api, now, step, st):
    return _hunt_here(api, now, st, step.get("idle_s"), step.get("max_s"))


def _step_task(api, now, step, st):
    name = str(step.get("name", "") or "")
    if not name:
        return True
    return _task_here(api, now, st, name, step.get("max_s", 0.0))


def _step_wait(api, now, step, st):
    st.setdefault("t0", now)
    return now - st["t0"] >= float(step.get("s", 0))


_OPS = {"goto": _step_goto, "path": _step_path, "loop": _step_loop,
        "channel": _step_channel, "warp": _step_warp, "hunt": _step_hunt,
        "task": _step_task, "wait": _step_wait}


# ---- run control ----------------------------------------------------------
def _start(api, name):
    global _run
    routes = _routes_all()
    route = routes.get(name)
    if route is None:
        _say("no route %r (known: %s)" % (name, ", ".join(sorted(routes)) or "none"))
        return False
    steps = route.get("steps") or []
    if not steps:
        _say("route %r has no steps" % name)
        return False
    if _run is not None:
        _stop(api, "replaced by %s" % name)
    _run = {"name": name, "route": route, "i": 0, "st": {}, "since": api.now()}
    _env_set(ENV_ROUTE, "1")
    _say("running %r (%d steps%s)" % (name, len(steps),
                                      ", repeat" if route.get("repeat") else ""))
    return True


def _stop(api, why):
    global _run, _hunt_ours, _task_ours
    if _run is None:
        return
    name = _run["name"]
    _run = None
    _env_set(ENV_ROUTE, "")
    if _hunt_ours:
        _env_set(ENV_HUNT_REQ, "stop")
        _hunt_ours = False
    if _task_ours:
        _env_set(ENV_TASK_REQ, "%s stop" % _task_ours)
        _task_ours = ""
    _say("stopped %r - %s" % (name, why))


def _advance(api):
    steps = _run["route"].get("steps") or []
    _run["i"] += 1
    _run["st"] = {}
    if _run["i"] >= len(steps):
        if _run["route"].get("repeat"):
            _run["i"] = 0
            _log("route %r: repeating" % _run["name"])
        else:
            _stop(api, "finished")


def _tick(api, now):
    if _run is None:
        return
    if not api.in_game():
        return                                   # loading: every step holds
    steps = _run["route"].get("steps") or []
    step = steps[_run["i"]]
    op = str(step.get("op", ""))
    fn = _OPS.get(op)
    if fn is None:
        _say("step %d: unknown op %r - skipping" % (_run["i"], op))
        _advance(api)
        return
    if "_entered" not in _run["st"]:
        _run["st"]["_entered"] = now
        if VERBOSE:
            _log("step %d/%d: %s" % (_run["i"] + 1, len(steps), json.dumps(step)))
    r = fn(api, now, step, _run["st"])
    if r is True:
        _advance(api)
    elif r == "fail":
        _stop(api, "step %d (%s) failed: %s" % (_run["i"], op, _run["st"].get("fail")))


# ---- chat commands --------------------------------------------------------
def _site(p):
    return [round(p[0] / 100.0, 1), round(p[1] / 100.0, 1)]


def _command(api, arg):
    global _draft, _saved
    parts = arg.split()
    sub = parts[0].lower() if parts else "status"
    rest = parts[1:]

    if sub == "list":
        routes = _routes_all()
        _say("routes: %s" % (", ".join(sorted(routes)) or "none"))
    elif sub == "run" and rest:
        _start(api, rest[0])
    elif sub == "stop":
        if _run is None:
            _say("nothing running")
        else:
            _stop(api, "by command")
    elif sub == "status":
        if _run is None:
            _say("idle; draft has %d points; hunt=%s chan=%s map=%r"
                 % (len(_draft), _env_get(ENV_HUNT_STATE) or "-",
                    api.channel(), api.map_name()))
        else:
            steps = _run["route"].get("steps") or []
            st = _run["st"]
            extra = ""
            if "i" in st:
                extra = " node %s lap %s phase %s" % (st.get("i"), st.get("lap"), st.get("phase"))
            _say("%r step %d/%d %s%s (%.0fs)"
                 % (_run["name"], _run["i"] + 1, len(steps),
                    steps[_run["i"]].get("op"), extra, api.now() - _run["since"]))
    elif sub == "show" and rest:
        r = _routes_all().get(rest[0])
        if r is None:
            _say("no route %r" % rest[0])
        else:
            for i, s in enumerate(r.get("steps") or []):
                api.chat("  %d: %s" % (i, json.dumps(s)))
    elif sub == "mark":
        if rest and rest[0] == "clear":
            _draft = []
            _say("draft cleared")
        elif rest and rest[0] == "undo":
            if _draft:
                _draft.pop()
            _say("draft has %d points" % len(_draft))
        else:
            p = _pos(api)
            if p is None:
                _say("no position")
            else:
                _draft.append(_site(p))
                _say("marked %s (%d points)" % (_draft[-1], len(_draft)))
    elif sub == "save" and rest:
        if not _draft:
            _say("draft is empty - /route mark first")
            return
        kind = rest[1] if len(rest) > 1 else "loop"
        hunt = "hunt" in rest[2:] or kind == "loop"
        step = {"op": "loop" if kind == "loop" else "path",
                "points": list(_draft)}
        if step["op"] == "loop":
            step["hunt"] = hunt
        _saved[rest[0]] = {"repeat": step["op"] == "loop", "steps": [step]}
        _persist(api)
        _say("saved %r as a %s of %d points%s"
             % (rest[0], step["op"], len(_draft), " (hunt)" if step.get("hunt") else ""))
    elif sub == "delete" and rest:
        if _saved.pop(rest[0], None) is None:
            _say("no saved route %r" % rest[0])
        else:
            _persist(api)
            _say("deleted %r" % rest[0])
    elif sub == "hop" and rest:
        try:
            ch = int(rest[0])
        except Exception:
            _say("usage: /route hop <ch>")
            return
        _env_set(ENV_CHAN, str(ch))
        _say("requested CH%d" % ch)
    else:
        _say("usage: /route list|run <name>|stop|status|show <name>|mark|save <name> loop|path|delete <name>|hop <ch>")


def _install_chat(api):
    n = _mod("m2netm2g") or _mod("net")
    orig = getattr(n, "SendChatPacket", None) if n else None
    if orig is None:
        _log("SendChatPacket missing - chat commands unavailable")
        return

    def guarded(msg, *a, **k):
        try:
            t = str(msg).strip()
        except Exception:
            return orig(msg, *a, **k)
        if t.lower().startswith(CMD) and (len(t) == len(CMD) or t[len(CMD)] == " "):
            try:
                _command(api, t[len(CMD):].strip())
            except Exception as e:
                _say("command failed %s: %s" % (type(e).__name__, e))
            return None                          # never reaches the server
        return orig(msg, *a, **k)

    try:
        setattr(n, "SendChatPacket", guarded)
        _patched.append((n, "SendChatPacket", orig))
    except Exception as e:
        _log("cannot install chat hook (%s)" % type(e).__name__)


# ---- lifecycle ------------------------------------------------------------
def on_load(api):
    global _api, _run, _draft, _saved, _autorun_at, _autorun_done, _hunt_ours, _task_ours
    _api = api
    _run, _draft, _hunt_ours, _task_ours = None, [], False, ""
    _autorun_at, _autorun_done = 0.0, False
    _warned.clear()
    if getattr(api, "VERSION", 0) < 18:
        api.log("routes: needs api v18 (have v%s) - not loading" % getattr(api, "VERSION", "?"))
        return
    _saved = api.store_load(STORE, {}) or {}
    _env_set(ENV_ROUTE, "")
    _install_chat(api)
    api.log("routes: ready - %d route(s) (%s); type /route"
            % (len(_routes_all()), ", ".join(sorted(_routes_all())) or "none"))


def on_update(api, dt):
    global _autorun_at, _autorun_done
    now = api.now()
    # AUTORUN fires ONCE per load. A finished route must stay finished: the
    # first version re-armed on every finish (a -1.0 "done" sentinel is truthy
    # and always in the past) and looped a laps=1 test route for ever.
    if AUTORUN and not _autorun_done and _run is None and api.in_game():
        if not _autorun_at:
            _autorun_at = now + 5.0
        elif now >= _autorun_at:
            _autorun_done = True
            _start(api, AUTORUN)
    try:
        _tick(api, now)
    except Exception as e:
        api.log("routes: tick failed %s: %s" % (type(e).__name__, e))
        _stop(api, "internal error")


def on_unload(api):
    _stop(api, "unloaded")
    _env_set(ENV_ROUTE, "")
    while _patched:
        owner, attr, orig = _patched.pop()
        try:
            setattr(owner, attr, orig)
        except Exception:
            pass
    api.log("routes: unloaded")
