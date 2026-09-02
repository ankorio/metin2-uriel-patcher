# 06 — The mod framework

This chapter is two things at once: a **specification** of the Python mod
framework that ships in `mods/`, and a **tutorial** that walks you through
writing, deploying and debugging a mod of your own. Everything here is taken
from the code in `mods/modhost.py`, `mods/api.py`, `mods/uikit.py`,
`mods/natives.json`, `mods/config.json` and the mods themselves. Where the
code records a measurement or a lesson, this document repeats it; where the
code is silent, so is this document.

## Table of contents

1. [The runtime model](#1-the-runtime-model)
   - [Who runs what](#11-who-runs-what)
   - [The tick](#12-the-tick)
   - [Threads, and what is safe to call when](#13-threads-and-what-is-safe-to-call-when)
   - [Constraints the embedded interpreter imposes](#14-constraints-the-embedded-interpreter-imposes)
2. [The modhost lifecycle](#2-the-modhost-lifecycle)
   - [Discovery](#21-discovery)
   - [Enablement: being loaded is being enabled](#22-enablement-being-loaded-is-being-enabled)
   - [Configuration injection](#23-configuration-injection)
   - [Per-character profiles](#24-per-character-profiles)
   - [Build constants](#25-build-constants)
   - [Hot reload](#26-hot-reload)
   - [Fault isolation](#27-fault-isolation)
   - [Logging and secret redaction](#28-logging-and-secret-redaction)
   - [The UTF-8 BOM pitfall](#29-the-utf-8-bom-pitfall)
3. [The mod contract](#3-the-mod-contract)
4. [The `api` surface](#4-the-api-surface)
   - [Output and persistence](#41-output-and-persistence)
   - [Session state](#42-session-state)
   - [Entities: `api.player`, `api.target`, `api.entity`](#43-entities)
   - [World queries](#44-world-queries)
   - [Targeting, skills and affects](#45-targeting-skills-and-affects)
   - [Movement and attack](#46-movement-and-attack)
   - [Ground items and pickup](#47-ground-items-and-pickup)
   - [Minimap and atlas marks](#48-minimap-and-atlas-marks)
   - [Collision switches](#49-collision-switches)
   - [Typed fields: `api.field`](#410-typed-fields-apifield)
   - [Stub natives: `api.natives`, `fishing_poll`, `key_event`, `http_post`](#411-stub-natives)
   - [Cross-client link](#412-cross-client-link)
   - [Namespaces and `api.raw`: what `_Ns` resolves](#413-namespaces-and-apiraw-what-_ns-resolves)
5. [uikit and `ui.json`](#5-uikit-and-uijson)
6. [Safety rules baked into the framework](#6-safety-rules-baked-into-the-framework)
7. [Tutorial: a "hello" mod from scratch](#7-tutorial-a-hello-mod-from-scratch)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. The runtime model

### 1.1 Who runs what

The game client is already a Python application: a CPython interpreter is
statically linked into the executable and the client's own UI and game logic
are Python modules (compiled with Cython in this build). The framework does not
add a scripting engine. It adds a **second source of scripts** to an
interpreter that is already there.

The layers, bottom to top:

| Layer | File | Job |
|---|---|---|
| 1 | `stub/uriel_stub.cpp` (the DLL the patcher installs) | Find the client's own `CPythonLauncher::RunLine(const char*)` and use it to execute one bootstrap string, once. Then call `_triarch_pump()` on a timer. |
| 2 | `mods/modhost.py` | Discovery, configuration, lifecycle, fault isolation, hot reload. |
| 3 | `mods/api.py` | The façade every mod is written against. Wraps the client's renamed native modules and the stub's `triarch_native` gateway. |
| 3b | `mods/uikit.py` | A declarative renderer for settings pages, handed to mods as `api.ui`. |
| 4 | `mods/<name>/main.py` | A mod. |

The bootstrap string (`kBootstrapFmt` in `uriel_stub.cpp`) reads
`mods/modhost.py`, `exec`s it into a fresh globals dictionary with `MODS_DIR`
and `LOG_DIR` pre-set, stores the resulting `pump` as
`builtins._triarch_pump`, and calls `boot()`. It reports its outcome through
the environment variable `TRIARCH_MODS`, which the stub copies into
`uriel_stub.log` — necessary because the bootstrap swallows every exception
(an escaping one would pop the client's modal traceback dialog on each
retry), so without that channel a silent failure would look like success.

Paths, relative to the game executable:

| Path | Contents |
|---|---|
| `<exe>\mods\` | `modhost.py`, `api.py`, `uikit.py`, `config.json`, `natives.json`, one folder per mod, `profiles\` |
| `<exe>\_patcher\` | runtime data: `mods.log`, `uriel_stub.log`, `link\`, anything a mod saves with `api.store_save` |
| `<exe>\_patcher\mods.off` | kill switch: if this file exists at start-up the stub never bootstraps the mod host |

### 1.2 The tick

The stub is called by the client once per rendered frame from the main thread,
with the interpreter live. On every `kPumpEvery`-th such call it runs the
Python string `_triarch_pump()`. In the shipped stub `kPumpEvery` is `1`, i.e.
the pump runs **every frame**; the comment beside it records that the earlier
value of 6 gave roughly 10 Hz at 60 fps and about 1.4 Hz on a slow, GPU-less
machine. The important consequence for you as a mod author:

- The pump rate is **frame-bound**. It is not a fixed timer. Never assume a
  particular rate; use `api.now()` and your own deadlines.
- `RunLine` compiles its string on every call. The pump body is Python and is
  free to do more work, but nothing inside it should be per-frame heavy.

`modhost.pump()` does exactly two things: once a second it polls for file
changes (section 2.6), and on every call it invokes `on_update(api, dt)` on
every enabled mod, where `dt` is the wall-clock seconds since the previous
pump (`0.0` on the first).

Before the launcher exists the stub polls for it every 30 ticks. Five failed
bootstraps, or 30 consecutive failed pumps, and the stub sets `g_modsDead` and
stops touching Python for the rest of the session. That is the stub-side
backstop; the Python side has its own (section 2.7).

### 1.3 Threads, and what is safe to call when

`boot()`, `pump()`, and therefore every mod hook, run **on the client's main
thread, inside the frame**, with the interpreter exactly as the game left it.
Three consequences:

1. **Every client binding is callable from a hook.** No cross-thread
   marshalling exists or is needed.
2. **Anything that blocks, blocks the frame.** `api.http_post` is a
   synchronous WinHTTP call — call it once per finished scan, not per pump.
   A long loop has the same effect; `entscan` chunks its VID sweep across
   pumps for exactly this reason.
3. **Phase matters more than threads.** A binding that is safe in the game
   phase may return `None`, raise, or silently no-op during login or
   character select. Convention: `on_update` begins with
   `if not api.in_game(): return` unless the mod means to act earlier
   (`autologin` does).

There are no worker threads, timers or event queues. "Do this in three
seconds" is `_next = api.now() + 3.0` and a comparison on the next pump.

### 1.4 Constraints the embedded interpreter imposes

Each of these is recorded in `modhost.py` and was learned from a live failure.

- **Imports are restricted.** The client replaces `__import__` with its own
  pack-aware importer whose stdlib fallback does not work in the rebuilt
  executable, so only modules **already resident** in `sys.modules` can be
  imported. `os`, `sys`, `time`, `json` are; `traceback` is not, which is why
  `modhost.fmt_exc()` formats exceptions by hand. On boot the host logs a
  resident/ABSENT line for `os`, `sys`, `time`, `math`, `json`, `struct`,
  `traceback`, `re`, `collections` and `random`; check it before importing
  anything else.
- **`builtins.open` is not CPython's `open`.** It is a Cython function
  returning a `system.pack_file`: real paths work, but keyword arguments
  (`encoding=`, `errors=`) raise `TypeError` and `with open(...)` fails
  (no context-manager protocol). Use `modhost.read_bytes()`,
  `api.store_save()`, or explicit `try/finally: f.close()`. One shipped mod,
  `shoppos`, uses `with open(...)`; treat that as a known fragility.
- **Bindings are thin C wrappers.** A wrong argument type can be an access
  violation, not a `TypeError` — one reason the façade exists.

---

## 2. The modhost lifecycle

### 2.1 Discovery

`_mod_paths()` lists `MODS_DIR`, sorted alphabetically, skips any entry whose
name begins with `.` or `_`, and keeps every directory that contains a
`main.py`. That is the whole discovery rule. A mod is a folder with a
`main.py`; nothing has to be registered anywhere.

Alphabetical order is load order, and it is observable: `fishing` and
`modui` both wrap the game window's `OnKeyDown`, and `fishing` (loaded first)
has explicit logic so that unloading it does not tear `modui`'s wrapper off
the chain.

### 2.2 Enablement: being loaded is being enabled

This is the single most misunderstood rule in the framework, so it gets its
own section.

`is_enabled(name)` reads `config["<name>"]["enabled"]`, defaulting to `True`
when the section or key is missing. If it is false, the mod is **not loaded at
all** — `boot()` logs `mod 'x' disabled by config` and moves on. If it is
true, the mod is loaded and `on_load` runs.

When `_load_mod` copies config values into the mod's globals it **skips the
`enabled` key** (`if k == "enabled": continue`). Therefore:

- A module-level `ENABLED = ...` constant in a mod is **never** set from
  config. It sits at whatever default the source file gives it, forever.
- Testing `if not ENABLED: return` in `on_update` is a bug. Several shipped
  mods still carry a vestigial `ENABLED = True` (`autologin`, `apidiag`,
  `lootdiag`, `mobdiag`, `nativeprobe`, `uispy`, `autohunt2`); it has no
  effect.
- The "off switch" for a mod is `on_unload`. When the user unticks a mod in
  the manager window, `config.json` is rewritten, the host sees the mtime
  change, calls `on_unload` on every mod, and reloads only the enabled ones.
  Whatever your mod changed in the world (a patched function, an engine flag,
  a mark on the minimap) must be undone in `on_unload`, because nothing else
  will do it.

`nocollide/main.py` records the failure this produced the first time round:
the mod loaded, the checkbox was ticked, and nothing was ever applied.

### 2.3 Configuration injection

`_load_mod` `exec`s `main.py` into a fresh dictionary `g` with `__name__`
set to `mod_<name>`, `__file__` to the path, and `__mod_dir__` to the folder.
Then, for every key in the mod's config section other than `enabled`:

```
if k in g:  g[k] = v      # override the module-level constant
else:       log("mod 'x': config key 'K' matches nothing - ignored")
```

So configuration is **by name match against module globals**. Declare a
default as an ordinary UPPERCASE constant, and any key of the same name in
`config.json` (or a profile) replaces it before `on_load` runs. There is no
schema, no type coercion, and no plumbing in the mod. A misspelt key is
logged, not silently accepted — that line is the first thing to look for when
"my setting does nothing".

The applied overrides are logged once per load, with secrets redacted
(section 2.8).

### 2.4 Per-character profiles

Several clients can run from one game folder, so `mods/config.json` is
shared. A change saved from the UI on one character would rewrite it for every
other character. Profiles fix that.

`mods/profiles/<Character>.json` is **merged over** `config.json`, per mod
section, by `load_config()`:

```
merged = deep-ish copy of base
for mod, sect in profile.items():
    if both are dicts: merged[mod].update(sect)
    else:              merged[mod] = sect
```

A profile therefore only needs to contain what differs. A mod added to
`config.json` later still gets its defaults on every character. The character
name is reduced to `[A-Za-z0-9_-]` before it becomes a file name — a server
supplied name must not be able to point at a path — and a profile applies
**only when the file exists**; nothing is created automatically.

Two timing details from `_poll_reload`:

- The character name is not known when mods first load; it arrives seconds
  later, once in the world. The poll loop re-evaluates `profile_path()` every
  second and treats a change in *which* profile applies as a config change, so
  the profile kicks in shortly after login and `mods.log` shows
  `profile: <name>.json`.
- The UI writes to `api.config_path()`, which resolves to the profile when one
  exists and to the shared file otherwise — the same resolution the loader
  uses, so what you see is what gets saved.

### 2.5 Build constants

`BUILD_KEYS = ("WALK_STATE",)` in `modhost.py` names settings that a profile
is **not allowed to override**. After the merge, the host copies these keys
back from the base `config.json` into every section that has them. The reason
is in `mods/profiles/README.txt` and repeated in the code: `WALK_STATE` is
the engine's auto-move state value, resolved from the binary by the offset
tool and stamped into `config.json` by the patcher on every install. It is a
fact about the game build, not a preference. A profile pinning it would
survive a game update and feed a stale constant to `nocollide`, `keeproute`,
`routes` and `autohunt2`. Mods carry a fallback default (`0x8A`) that the
stamped value overrides.

### 2.6 Hot reload

Once a second (`RELOAD_POLL = 1.0`) `_poll_reload` checks modification times
in this order and acts on the first change:

1. **Config** — `config.json` mtime, which profile applies, or that profile's
   mtime → `load_config()`, `on_unload` every mod, reload the enabled ones.
2. **`uikit.py`** → reload uikit, rebuild the api, reload every mod.
3. **`api.py`** → rebuild the `Api` object and reload every mod (mods hold no
   reference between calls, but reloading keeps the two cases identical).
4. A mod folder **removed from disk** → `on_unload` it.
5. A mod's **`main.py`** changed → `on_unload`, re-`exec`, `on_load`. A
   present, enabled, not-yet-loaded mod is loaded.

The edit loop is therefore: save, wait at most a second, read `mods.log`.

**`modhost.py` itself does not hot-reload.** It is the code doing the
watching; the stub `exec`s it once and keeps its `pump`. A change needs a
client restart. The same is true of `uriel_offsets.ini` and
`uriel_natives.ini`, which the stub reads once at DLL load — regenerating
them under a running client changes nothing, as `nocollide`'s own warning
text says.

`ui.json` is not watched by the host; `modui` re-reads it whenever a page is
opened.

### 2.7 Fault isolation

Two rules at the top of `modhost.py`: `pump()` and `boot()` must not raise
(anything escaping reaches `RunLine`, which logs a traceback every pump), and
a broken mod disables itself without taking the client or other mods down.

Every hook is invoked through `_call()`, which catches exceptions, logs a
hand-formatted traceback, and counts **consecutive** faults; a success resets
the count. At `MAX_FAULTS = 5` the mod stops being pumped and the log says
`mod 'x' disabled after 5 consecutive faults`. Editing the file reloads it
and restarts the count. A mod that raises **at load** is recorded as a
disabled placeholder with the file's mtime, so it is retried only when the
file changes, not once per second forever.

### 2.8 Logging and secret redaction

`modhost.log()` appends to `<exe>\_patcher\mods.log` in the form

```
HH:MM:SS [<pid>] message
HH:MM:SS [<pid>/<Character>] message      once the character is known
```

Every line is tagged because several clients share one log. The tag is
re-checked at the 1 Hz poll, so relogging onto another character retags
rather than lying. `log()` never raises; it tries plain 2-argument `open`
first and falls back to binary mode, for the `open()` reasons in section 1.4.

When config overrides are applied, the host logs them — but any key whose
upper-cased name contains `PW`, `PASS`, `SECRET` or `TOKEN` is shown as
`<redacted>`. `mods.log` gets copied around; credentials must not travel with
it. `shoppos.UPLOAD_TOKEN` is the shipped example.

### 2.9 The UTF-8 BOM pitfall

`_read_json_file` reads the bytes, decodes them as UTF-8 with replacement,
and hands the string to `json.loads`. A file saved "UTF-8 with BOM" by some
Windows editors begins with the byte-order mark, which decodes to `U+FEFF`.
`json.loads` does not accept that character before the opening brace, so the
parse fails. Follow the code from there:

1. `load_config()` gets `None` back, logs `config.json is invalid - ignoring
   it` with the traceback, and sets `_cfg = {}` and `_cfg_mtime = 0.0`.
2. With an empty config every mod is enabled with defaults (the design is
   "a typo in config must never take the mod host down").
3. On the next poll, `_mtime(CONFIG_PATH)` is a real number and `_cfg_mtime`
   is `0.0`, so they differ → `config changed - reloading mods` → step 1
   again. **Every second.**

The symptom is a `mods.log` that grows by a reload cycle per second, and mods
whose `on_load` runs continuously. The fix is to save `config.json` without a
BOM. The same applies to profile files.

---

## 3. The mod contract

A mod is `mods/<name>/main.py`, optionally with `mods/<name>/ui.json`. The
host looks up these module-level names:

| Name | Called | Notes |
|---|---|---|
| `on_load(api)` | after the file is exec'd and config applied | Reset your module state here: a hot reload re-execs the file, but globals you assign in `on_load` are what you can trust. Check `api.VERSION` and bail out with a log line if too old. |
| `on_update(api, dt)` | every pump, while enabled | `dt` = seconds since the previous pump. Return early unless `api.in_game()`. |
| `on_unload(api)` | on reload, on disable, on config change, on api/uikit reload, when the folder is deleted | Restore **everything** you changed: patched functions, engine flags, marks, environment keys. |
| `CAPABILITIES` | read at load, logged | A list of strings such as `["read", "move"]`. Recorded and logged (`caps=[...]`), **not enforced**. It documents intent. |

There are no other hooks. In particular there is no `on_key`, `on_chat` or
`on_phase`. Mods that need those wrap the client's own Python objects and
restore them on unload:

- Keys: wrap `OnKeyDown` on the phase-5 (game) window from
  `m2netm2g.GetPhaseWindow(5)`, as `modui` and `fishing` do. Note that some
  keys are polled natively and never reach Python at all — that is what
  `api.key_event` exists for.
- Chat commands: wrap `m2netm2g.SendChatPacket` at module level, handle your
  command, and return `None` so it never reaches the server. `goto`, `routes`
  and `autohunt2` all do this and chain correctly (each passes text it does
  not own to the original).
- Phase: there are no events; poll `api.in_game()`.

Rules that the shipped mods converged on after getting them wrong:

1. **No `ENABLED` flag.** See section 2.2.
2. **Typed fields are read with `api.field("name")`, never
   `api.player.<field>`.** See section 4.10.
3. **Restore on unload *and* on leaving the world.** A global change outlives
   the mod otherwise. `nocollide` clears both switches when `in_game()` turns
   false, not just in `on_unload`.
4. **Never treat "cannot read" as "false".** Hold the previous state and warn
   once. Guessing false looks exactly like the mod being switched off.
5. **Only `os`, `sys`, `time`, `json` (and whatever the boot dump lists as
   resident) may be imported.**
6. **No `with open`, no `encoding=`.** Use `api.store_save` / `api.store_load`
   for JSON next to the log, or explicit `try/finally`.
7. **Time your own work.** Keep a `_next` deadline in module state; do not do
   anything expensive every pump.
8. **Inter-mod communication goes through `os.environ`.** Mods run in
   isolated namespaces and cannot import each other. The convention (used by
   `autoloot` ↔ `autohunt2`, `follow`/`autohunt2`/`routes` → `chanswap`,
   `routes` ↔ `autohunt2`) is: a **request** key is consumed exactly once by
   its reader; a **state** key is republished every tick; a **hold** is a
   deadline, not a flag, so a crashed writer cannot freeze the reader
   forever. `chanswap` also registers a plain object as
   `sys.modules["triarch_chan"]` for direct calls, which is the one exception.

A minimal skeleton:

```python
"""<name> - one line on what it does. Then WHY this shape, with measurements."""

CAPABILITIES = ["read"]

SOME_SETTING = True          # every config key is a module global like this
INTERVAL = 2.0

_next = 0.0                  # module state, reset in on_load


def on_load(api):
    global _next
    _next = 0.0
    if api.VERSION < 18:
        api.log("<name>: needs api v18 (have v%s) - not loading" % api.VERSION)
        return
    api.log("<name>: ready")


def on_update(api, dt):
    global _next
    if not api.in_game():
        return
    now = api.now()
    if now < _next:
        return
    _next = now + INTERVAL
    ...


def on_unload(api):
    ...                      # restore anything global you changed
```

---

## 4. The `api` surface

`API_VERSION` is `23`; `api.VERSION` exposes it. The file's docstring states
the boundary: mods never touch the client's native modules directly, because
the modules are already renamed as an anti-cheat measure (stock
`player`/`net`/`chr` are `playerm2g2`/`m2netm2g`/`pack_chr`) and can be
renamed again, because the bindings are thin C wrappers, and because the
façade is a deliberate capability boundary. Packet sending on `m2netm2g` is
**not** wrapped; movement goes through the client's own pathing.

Every helper below returns a safe value (`False`, `None`, `0`, `[]`) when the
underlying binding is missing — **except** those that touch the stub's
natives, which raise, because "silently did nothing" and "off" must never
look the same.

### 4.1 Output and persistence

| Call | Semantics |
|---|---|
| `api.log(msg)` | Append a tagged line to `mods.log`. Never raises. |
| `api.chat(msg) -> bool` | Write a line into the in-game chat window (`chat.AppendChat`, info type). Local rendering only; nothing is transmitted. `False` before the chat window exists. |
| `api.store_save(name, obj) -> bool` | Write `obj` as JSON to `_patcher\<name>`, atomically (temp file + `os.replace`). |
| `api.store_load(name, default=None)` | Read it back, or `default`. |
| `api.data_path(name) -> str` | The full path under `_patcher\`. |
| `api.config_path() -> str` | The config file a mod should **write** for this character: the profile if one exists, else `mods/config.json`. Never creates anything. |

### 4.2 Session state

| Call | Semantics |
|---|---|
| `api.now() -> float` | `time.time()`. |
| `api.in_game() -> bool` | The player exists and has a position. |
| `api.channel() -> int` | `app.GetChannel()`, `0` if unknown. |
| `api.map_name() -> str` | `background.GetCurrentMapName()`, `""` if unknown. World coordinates are global across the map atlas, so a position on another map is a real place somewhere else — `follow` gates on this. |
| `api.map_base() -> (x, y) | None` | Origin of the current map in atlas units, derived by feeding `(0, 0)` to `GlobalPositionToLocalPosition` and negating. |

### 4.3 Entities

`api.player` and `api.target` are `_Entity` objects wrapping "the main
character's VID" and "the current target VID" respectively. `api.entity(vid)`
wraps any VID (captured, not re-resolved, so it goes stale when the instance
despawns).

| Call | Semantics |
|---|---|
| `.vid() -> int` | `0` when there is none. |
| `.exists() -> bool` | `vid() != 0`. |
| `.name() -> str` | `pack_chr.GetNameByVID`. The client reports the literal string `"None"` for a character not yet in the world; the host does not adopt that as an identity. |
| `.position() -> (x, y, z) | None` | `pack_chr.GetPixelPosition(vid)`, falling back to `SelectInstance` + bare call on `TypeError`. The sentinel `(-100, -100, -100)` ("instance exists but is not placed") is reported as `None`. Do not use `GetActorPixelPosition`: it returns uninitialised memory. |

`api.player` is additionally a namespace (section 4.13), so
`api.player.position()` (curated) and `api.player.GetTargetVID()` (raw
binding) both work.

### 4.4 World queries

| Call | Semantics |
|---|---|
| `api.vid_of(name) -> int` | VID of a named character **in this client**, or `0`. VIDs are per-client instance ids; a VID from another client means nothing here. `GetVIDByName` returns `-1` for "not here"; the wrapper turns that into `0`. |
| `api.has_instance(vid) -> bool` | `pack_chr.HasInstance`. |
| `api.instance_types() -> {int: str}` | The client's own `INSTANCE_TYPE_*` constants, read at call time rather than hardcoded (on this build a player reports type 6 and type 0 is an ordinary monster). |
| `api.describe(vid) -> dict | None` | `vid, name, type, type_name, race, level, guild, dead, stone, npc, enemy, pc, pos`. Each field individually guarded. `None` unless the instance is registered. |
| `api.actors(races=None, radius=None, alive_only=True) -> [dict]` | Every character the client holds, nearest first: `vid, race, name, pos, dist`. Walks the character manager through the stub (there is no binding for this). `races` is a set of race numbers applied before the expensive per-VID calls. Returns `[]` when the native layer is absent. |

### 4.5 Targeting, skills and affects

| Call | Semantics |
|---|---|
| `api.set_target(vid) -> bool` | `playerm2g2.SetTarget(vid, GetPhaseWindow(5))`. The two-argument form is required: the binding compares its second argument against the game-phase window and silently does nothing on mismatch — an anti-bot gate that breaks every stock script. |
| `api.clear_target() -> bool` | `ClearTarget()`. |
| `api.select(vid) -> bool` | `pack_chr.Select` — the world selection, distinct from the UI target. `follow` uses clear → select → set to displace a stale selection. |
| `api.use_quickslot(index) -> bool` | `RequestUseLocalQuickSlot(index)` — exactly what pressing hotbar key `index+1` does. No skill ids, no packets from the mod. |
| `api.quickslot(index) -> (type, value) | None` | `(0, 0)` means empty; type 2 is a skill. |
| `api.affect_seconds(idx) -> int` | `GetAffectData(idx, 0)`: seconds remaining on affect `idx`, `0` if absent. Measured by watching slots count down. |
| `api.active_affects(indices) -> {str(idx): seconds}` | The subset of `indices` currently active. |
| `api.affects()` | The raw affect list, shape unpinned; for experiments. |
| `api.auto_pickup(on=None) -> bool | None` | Read or set the client's own AUTO_PICK option. Documented as **not** gating `PickCloseItemVector`; exposed so the options window agrees with reality. |

### 4.6 Movement and attack

| Call | Semantics |
|---|---|
| `api.move_to(x, y) -> bool` | `playerm2g2.AutoMoveToPosition(x, y)` — the same auto-move the shipped auto-hunt and quest navigation use. **Not a pathfinder**: it steps straight at the destination and lets collision stop it (`unstick`'s docstring explains the four calls it reduces to). Re-issuing a move restarts pathing, so treat it as an edge, not a poll. |
| `api.attack(vid) -> bool` | Publishes the VID in the `TRIARCH_ATTACK` environment variable; the stub calls the client's own `__OnPressActor` on the next frame, and only when the value changes. `0` stops. Exists because no Python binding attacks a chosen target on this build. |
| `api.attack_available() -> bool` | `TRIARCH_ATTACK_OK == "1"`: the stub resolved the offsets for the bridge. |

### 4.7 Ground items and pickup

| Call | Semantics |
|---|---|
| `api.PICKUP_RADIUS` | `1000.0`. The client's own batch pickup keeps an item only within ~1000 units **and** if its ownership string matches our name. |
| `api.pick_up_items() -> bool` | `PickCloseItemVector`: the client's batch pickup. One packet per surviving item, so call it on a timer. Cannot take another player's drop; the filter is native. |
| `api.pick_up_money() -> bool` | `PickCloseMoney`, same idea for dropped currency. |
| `api.ground_items(radius=None, mine_only=True) -> [dict]` | Every drop the client knows: `id, vnum, name, owner, pos, dist`, nearest first. Walks `CPythonItem`'s map through the stub (the bindings that look like they do this are inlined and uncallable). Defaults reproduce the client's own filter; `radius=0` and `mine_only=False` show everything. `id` is *which* drop, `vnum` is *what kind*. |
| `api.pick_up(item_id) -> bool` | `SendItemPickUpPacket(id)`: take one specific drop. Usable only because `ground_items()` supplies valid ids. |

### 4.8 Minimap and atlas marks

| Call | Semantics |
|---|---|
| `api.atlas_mark(mark_id, x, y, label="") -> bool` | Place or move a pulsing waypoint (type 6) on the atlas. Coordinates are the ones `position()` reports, passed straight through — they are already map-local; converting them again is what made earlier marks vanish. Removes first, because `AddWayPoint` silently ignores an id that already exists. |
| `api.atlas_unmark(mark_id) -> bool` | Remove it. |
| `api.mark_mob(mark_id, vid, label="") -> bool` | The type-13 **target** mark, on minimap and atlas, attached to a VID so the engine moves it every frame. Needs the stub's `minimap_mark` native; returns `False` (never raises) when absent. |
| `api.unmark_mob(mark_id) -> bool` | Remove it. |

### 4.9 Collision switches

| Call | Semantics |
|---|---|
| `api.actor_pass(on, exclude=None)` | Walk through other actors; terrain unaffected. Monsters body-block by *displacement* (the engine pushes you back out every frame), and the test is a race whitelist with two exempt ranges; this widens them. `exclude=(lo, hi)` keeps one race band solid (metin stones). **Raises** if the native is missing, and raises if the stub predates band support and would have made everything passable. |
| `api.terrain_pass(on)` | Walk through **walls**. Deliberately a separate switch so nothing can turn it on as a side effect of wanting to pass a monster. The client self-reports x/y about 2.5 times a second, so this is visible to the server. See section 6. |

### 4.10 Typed fields: `api.field`

`mods/natives.json` declares typed **fields** — named offsets into engine
singletons that the stub resolves at start-up:

| Field | Type | Access | Meaning |
|---|---|---|---|
| `auto_attack_vid` | u32 | r | Current auto-attack target, `0` when unengaged. |
| `auto_attack_target` | u32 | r | The cached actor pointer beside it. |
| `hunt_use_skill` | bool8 | rw | The autohunt window's "use skills" box. |
| `hunt_use_mount` | bool8 | rw | Its "use mount" box (the remount gate). |
| `hunt_stones` | bool8 | rw | Its "stones only" choice. |
| `player_state` | u32 | r | Player state enum; `0x8A` while auto-moving. |
| `hunt_anchor` | vec2f | rw | The point the victim search is centred on. |
| `hunt_skills` | vector\<u8\> | r | Configured skill slots, via an adapter. |
| `auto_move_active` | bool8 | r | A route was accepted. Not cleared on cancellation — only `player_state` moves. |
| `auto_move_enabled` | bool8 | r | Set alongside it. |
| `auto_move_dest` | vec2f | r | Where the current route is heading. |
| `mounted` (instance field) | bool8 | r | `IsMountingHorse()`, inlined as a byte. |

`api.field(name)` calls `triarch_native.field(name)` and **raises** when the
gateway is absent. It does not return a default: a caller that cannot tell
"unavailable" from `0` will read a missing field as a legitimate false and act
on it, which is how `nocollide` once held collision on while reporting itself
ready. Writable fields are set with `triarch_native.set_field` (used by
`autohunt2`); the api has no curated wrapper for that yet.

**Rule:** fields are read with `api.field("name")` and **never**
`api.player.<name>`. The reason is in section 4.13: `_Ns` resolves curated
helpers, natives and bindings, and a field is none of those, so
`api.player.auto_move_active` raises `AttributeError` however correctly the
field is declared.

### 4.11 Stub natives

`api.natives` is a `_Natives` object loaded from `natives.json`:

| Member | Semantics |
|---|---|
| `api.natives.by_name`, `.by_area` | The registry. |
| `api.natives.module()` | The `triarch_native` module the stub registers a second or two into the run, or `None`. Misses are not cached, precisely because it appears late. |
| `api.natives.available() -> bool` | |
| `api.natives.call(name, args)` | Marshal `args` per the declared types (64-bit types take two dword slots) and call the stub's trampoline. Arity errors raise `TypeError` whether or not the stub is present. |

The natives declared in the shipped registry are all in area `player`:
`FindAndSetNewTarget(main, b_stone, exclude)`, `OnPressActor(main, vid,
wait)`, `UseAutoSkills(main, target, now_i64)`, `OpenCharacterMenu(vid)`,
`UpdateAutoAttack()`, `ReserveProcessClickActor()`, `CreateAutoBotSettings()`.
`SendChatPacket` is registered with area `null` on purpose so that it is
**not** reachable as `api.<area>.SendChatPacket`. `FindVictim` is in the
`rejected` list: it takes a float in an XMM register, which the trampoline
cannot express, so the entry fails closed rather than corrupting the stack.

The module also carries functions the api wraps directly (`actors`,
`actor_at`, `ground_items`, `ground_item_*`, `main_instance`,
`drop_engagement`, `skip_collision`, `actor_pass`, `terrain_pass`,
`minimap_mark`/`unmark`, `offline_shops`/`offline_shop_at`, `http_post`,
`fishing_poll`, `key_event`, `field`, `set_field`). Prefer the api wrapper;
reach `api.natives.module()` only when there is none.

| Call | Semantics |
|---|---|
| `api.http_post(url, body, auth=None, content_type="text/csv") -> int` | Synchronous WinHTTP POST through the stub. Returns the HTTP status, or a negative sentinel (`-1` bad URL … `-8` no status header) when no request could be made. `auth` is the full `Authorization` header value. **Blocks the frame.** Raises if the native is absent. |
| `api.fishing_poll() -> (seq, sub, bite, our_vid, fish_vnum, cast_seq)` | Counters the stub keeps from the fishing packets for our own character. `bite` is monotonic, so a 4 Hz poll cannot miss one. Raises if absent. |
| `api.key_event(vk, scan, down) -> int` | Inject a keyboard event by scancode via `SendInput`, for keys the engine polls natively and never hands to Python. Hold across a tick for a real press. Raises if absent. |

### 4.12 Cross-client link

`api.link` is a `Link`: filesystem IPC between clients on the same machine,
keyed by **character name** (both clients run from one folder).

| Call | Semantics |
|---|---|
| `api.link.publish(name, data) -> bool` | Write `_patcher\link\<name>.json` atomically (temp + `os.replace`). |
| `api.link.read(name) -> dict | None` | The latest record, or `None` if missing or older than `Link.STALE_S` (6 s) by its `ts` field. |

Sockets are not reliably importable here, so a file is the pragmatic channel.
`follow` publishes name, channel, map, position and buff timers this way.

### 4.13 Namespaces and `api.raw`: what `_Ns` resolves

`api.py` v14 added semantic namespaces over both backends:

| Namespace | Client modules behind it |
|---|---|
| `api.player` | `playerm2g2`, `skill`, `petskill` (curated layer: the `_Entity`) |
| `api.world` | `pack_chr`, `chrmgr`, `nonplayer` |
| `api.shop` | `shop`, `itemshop`, `m2netm2g` |
| `api.market` | `offlineshop`, `shopSearch` |
| `api.storage` | `safebox`, `cube`, `switchbot` |
| `api.social` | `guild`, `messenger`, `whispermgr`, `chat` |
| `api.session` | `app`, `m2netm2g`, `ServerStateChecker` |
| `api.items` | `item` |
| `api.map` | `background`, `fly` |
| `api.ui` | declared over `wndMgr`, `grp`, `miniMap`, `render_manager` — **but see below** |

Attribute lookup on a namespace (`_Ns.__getattr__`) resolves, in order:

1. **Curated `snake_case` helpers** — ours, stable across patches
   (`api.player.position()`).
2. **Natives** from `natives.json` whose `area` matches (`api.player.
   FindAndSetNewTarget(...)`). If a native's name also exists as a binding in
   the area, the lookup **refuses** unless `_COLLISION_OVERRIDES` names a
   winner; a silent repoint of existing callers is treated as the worst
   possible outcome.
3. **Bindings** — the first module in the area that has the `CamelCase` name.
   If two modules export it, the lookup raises unless `_COLLISION_OVERRIDES`
   decides (e.g. `("shop", "GetItemPrice") -> "shop"`).

A miss raises `AttributeError` with a "did you mean" list built from
substring matches; `api.<area>.names()` lists everything reachable. The case
of the name tells you which backend you are on: `snake_case` is ours,
`CamelCase` is the client's.

**Fields are not in this chain.** That is the whole reason for the
`api.field()` rule.

**`api.ui` is not the `ui` namespace in practice.** `modhost._load_api`
assigns `_api.ui = _uikit` after construction, replacing the namespace with
the uikit module (or `None` when `uikit.py` is missing). Reach `miniMap`,
`wndMgr` and friends through `api.raw` instead.

`api.raw.<module>.<Name>` is the escape hatch: any client module by its real
name, no policy, no collision handling. `nocollide` uses
`api.raw.pack_chr.IsStone(vid)`; `autohunt2` uses
`api.raw.m2netm2g.SendChatPacket` for its revive command.

---

## 5. uikit and `ui.json`

`mods/uikit.py` is a renderer, not a framework: it builds widgets from a JSON
spec, reports edits through a callback, and never reads or writes files.
`modui` (the manager window, section 7 of the catalogue) is the only caller in
the shipped set and owns persistence.

```
rendered = api.ui.render(spec, host, values, on_change, log, width=300, top=6, zone="body")
...
rendered.destroy()
```

- `spec` is the parsed `ui.json`; `values` is `{key: current value}`;
  `on_change(key, value)` fires on every edit; buttons fire
  `on_change("__action__", action)`.
- Rows carrying `"zone"` other than the requested one are skipped, so one spec
  can feed two regions.
- Unknown row types are logged and skipped, so a spec written for a newer
  uikit still renders what it can.
- `Rendered.widgets` holds every widget for as long as the page is up
  (widgets are collected the moment nothing references them). `destroy()`
  hides and destroys them in reverse order.

A `ui.json` looks like this (`mods/unstick/ui.json`):

```json
{
  "title": "Unstick",
  "hint": "Sidesteps when auto-move wedges on a corner or a mob. Collision stays on.",
  "rows": [
    {"type": "slider", "key": "STUCK_S", "label": "Stall before acting",
                       "min": 0.5, "max": 10, "step": 0.5, "suffix": "s"},
    {"type": "slider", "key": "SIDESTEP", "label": "Sidestep distance",
                       "min": 300, "max": 3000, "step": 100},
    {"type": "separator"},
    {"type": "value",  "key": "NEAR_DIST", "label": "Arrived within"},
    {"type": "checkbox", "key": "VERBOSE", "label": "Log every sidestep"},
    {"type": "button", "label": "Revert", "action": "reset", "x": 10}
  ]
}
```

Row types (`_BUILDERS` in `uikit.py`):

| `type` | Keys | Renders as | Emits |
|---|---|---|---|
| `label` | `text`, optional `color` (0xAARRGGBB int) | left-aligned text | — |
| `separator` | — | a `ui.Line`, or a dashed text fallback | — |
| `value` | `key`, `label` | label + read-only current value | — |
| `checkbox` | `key`, `label` | check image + label, full-row hitbox | `(key, bool)` |
| `edit` | `key`, `label`, `width` (120), `max_length` (32), `numeric` | text input over a background image | `(key, str)` on Return/Escape |
| `slider` | `key`, `label`, `min`, `max`, `step`, `suffix` | slider + live readout | `(key, number)` rounded to `step`; integral values become `int` |
| `button` | `label`, `action`, `x`, `width` | art-backed button, or text + hitbox fallback | `("__action__", action)` |

`modui` understands the actions `save` and `reset`; anything else is logged
as unknown. The `Enable <mod>` checkbox is injected by `modui` for every mod
— **never declare it in `ui.json`**. A mod with no `ui.json` still gets a
valid page containing just that checkbox.

`modui` lists the mod folders as tabs; opening one reads its `ui.json`,
prepends the enable row, computes the effective values (shared config with
the profile merged over, plus unsaved edits) and calls `render`. Edits
accumulate until **Save** writes `api.config_path()` once, so the host
reloads once rather than per slider tick. F10 (`TOGGLE_KEY = 68`) toggles the
window by wrapping `OnKeyDown` on the game phase window. The catalogue entry
for `modui` has the rest.

If you ever build UI by hand, the three constraints at the top of `uikit.py`
apply: a widget with no artwork has zero size and cannot be picked (a bare
`ui.Button` ignores clicks; use art, or a `ui.Window` with `SetPickAlways()`
and a mouse-down event); art must come in matched pairs at sensible sizes;
and `TextLine` is centre-aligned unless you set left alignment.

---

## 6. Safety rules baked into the framework

This client talks to a live server with other players on it. The framework's
boundaries are chosen so that a mod written against the api cannot easily
become something else. The rules, and where they are enforced:

| Rule | Where | Why |
|---|---|---|
| **No crafted, duplicated or malformed packets.** | `api.py` wraps no `m2netm2g` send functions; `SendChatPacket` has `area: null` in `natives.json` so it is unreachable through the namespaces. | Every action a mod takes goes through the client's own input paths (`AutoMoveToPosition`, `RequestUseLocalQuickSlot`, `PickCloseItemVector`, `__OnPressActor`), so what reaches the wire is byte-identical to a human doing it. |
| **No terrain pass-through by default.** | `terrain_pass` is a separate switch; `nocollide` ships `NO_TERRAIN: false`, logs it in red in the UI, and always logs a change regardless of `VERBOSE`. | The client self-reports its x/y about 2.5 times a second. Walking through a monster is invisible to the server; standing where the map forbids is not, and an in-client legality dialog has already been observed. |
| **Ownership is enforced natively.** | `pick_up_items` and the default `ground_items(mine_only=True)`. | The client keeps a drop only if its ownership string matches our own name; a mod cannot widen that, so it cannot take someone else's loot. |
| **No metronomes.** | `autoloot` triggers on the kill edge and jitters its idle sweep; `follow` rate-limits `SetTarget`; `keeproute` and `entscan` bound their re-issue and sweep rates; `http_post` is documented as off-hot-path. | A perfectly periodic burst of identical packets is the signature of a macro and is noise the server does not need. |
| **Stay out of the shipped autohunt's telemetry.** | `autohunt2` swallows `/auto_hunt` and blocks `miniMap.SetAutoHuntStatus`, and drives the client's own functions instead. | The server-side analytics key on that command and flag. |
| **Fail loud on missing natives.** | `field`, `actor_pass`, `terrain_pass`, `http_post`, `fishing_poll`, `key_event` raise. | "Silently did nothing" and "off" must never be confused for a switch that changes what the character can walk through. |
| **A broken mod cannot take the client down.** | `_call` fault counting, `pump`/`boot` never raise, stub-side backstop, `mods.off`. | See section 2.7. |
| **Secrets never reach the log; nothing is shipped enabled with credentials.** | `_load_mod` redaction; `shoppos` ships `URL: ""` and is inert until configured. | `mods.log` gets copied around. |

`CAPABILITIES` is recorded, not enforced. The façade is a convention with
teeth in the api's shape, not a sandbox: a mod can reach `sys.modules`
directly. The rules above are what the shipped mods obey; a mod that ignores
them is outside what this project supports.

---

## 7. Tutorial: a "hello" mod from scratch

We will write a mod that logs a greeting on load, says hello in the chat
window every few seconds while in the world, and can be tuned from the F10
window and per character.

### Step 1 — the folder

Create `<exe>\mods\hello\` (in the repository: `mods/hello/`). The name must
not start with `.` or `_`.

### Step 2 — `main.py`

```python
"""hello - the smallest possible mod: logs on load, greets on a timer.

Exists to show the contract end to end: config injection, the pump, the log,
chat output, and a clean unload.
"""

CAPABILITIES = ["read"]

GREETING = "hello from the mod host"   # overridable from config.json / profile
INTERVAL = 5.0                          # seconds between greetings
SAY_IN_CHAT = True                      # False = log only

_next = 0.0


def on_load(api):
    global _next
    _next = 0.0
    api.log("hello: loaded (api v%s) greeting=%r every %.0fs"
            % (api.VERSION, GREETING, INTERVAL))


def on_update(api, dt):
    global _next
    if not api.in_game():
        return
    now = api.now()
    if now < _next:
        return
    _next = now + INTERVAL
    line = "%s - I am %s at %s" % (GREETING, api.player.name(),
                                   tuple(int(v) for v in api.player.position()))
    api.log("hello: " + line)
    if SAY_IN_CHAT:
        api.chat(line)


def on_unload(api):
    api.log("hello: unloaded")
```

Points to notice: no imports at all; three UPPERCASE constants that config can
override; a `_next` deadline instead of counting pumps; an `in_game()` guard;
nothing global to restore, so `on_unload` only logs.

### Step 3 — the config entry

A folder with no section in `config.json` is **enabled by default**, so the
mod would load as soon as the file is saved. Be explicit anyway. Add to
`mods/config.json`:

```json
  "hello": {
    "enabled": true,
    "INTERVAL": 10.0
  }
```

`INTERVAL` matches a constant, so it overrides. If you had typed `INTERVAl`,
`mods.log` would say `mod 'hello': config key 'INTERVAl' matches nothing -
ignored` — that line is your spell-checker. Save the file **without a BOM**.

### Step 4 — `ui.json`

```json
{
  "title": "Hello",
  "hint": "Greets you on a timer. A demonstration mod.",
  "rows": [
    {"type": "edit",     "key": "GREETING", "label": "Greeting", "max_length": 40},
    {"type": "slider",   "key": "INTERVAL", "label": "Every",
                         "min": 2, "max": 60, "step": 1, "suffix": "s"},
    {"type": "checkbox", "key": "SAY_IN_CHAT", "label": "Also say it in chat"},
    {"type": "button",   "label": "Revert", "action": "reset", "x": 10}
  ]
}
```

Do not add an `enabled` checkbox; `modui` injects it. Press F10 in game, click
`hello` in the rail, change the slider, press **Guardar** (Save). The
reference client is Spanish-localised, so its buttons and its mob and item
names are Spanish strings; that is also why names in the example configs read
`"Lobo"` or `"Espada"`, and why the mods prefer vnums to names. One write
goes to `config.json` (or your profile), the host reloads, and `on_load` logs
the new interval.

### Step 5 — a per-character override

Suppose one character should be quieter. Create
`mods/profiles/<ThatCharacter>.json`:

```json
{ "hello": { "SAY_IN_CHAT": false, "INTERVAL": 30 } }
```

Only the differences. Within a few seconds of that character entering the
world, `mods.log` shows `profile: <ThatCharacter>.json` followed by
`config changed - reloading mods`, and from then on the F10 window on that
client saves to the profile rather than the shared file. Delete the file to
go back to sharing. (`WALK_STATE` would be ignored if you put it here — see
section 2.5.)

### Step 6 — deploy

There is no deploy step beyond copying the folder into `<exe>\mods\`. If the
client is running, the host notices `main.py` within a second:

```
12:03:41 [4120/Alice] loaded mod 'hello' caps=['read']
12:03:41 [4120/Alice] mod 'hello' config: {'INTERVAL': 10.0}
12:03:41 [4120/Alice] hello: loaded (api v23) greeting='hello from the mod host' every 10s
12:03:51 [4120/Alice] hello: hello from the mod host - I am Alice at (38462, 44033, 17724)
```

Edit `main.py` again (change the greeting default, say) and save: you will
see `mod 'hello' changed - reloading`, then `hello: unloaded`, then the new
`hello: loaded` line. That is the whole edit loop.

### Step 7 — read the log

`<exe>\_patcher\mods.log`. On Windows, `Get-Content -Wait` in PowerShell, or
any tail-style viewer, works. The first lines of a session tell you what you
are running on:

```
=== mod host up  api=v23  dir=C:\...\mods
    module os           resident
    module traceback    ABSENT
api: 8 native(s) registered from natives.json
api: v23 namespaces: items, map, market, player, session, shop, social, storage, ui, world
uikit v1 loaded
```

If those lines are missing, the mod host never came up; look at
`uriel_stub.log` for the `MODS: bootstrap ran ... status:` line and its
`TRIARCH_MODS` payload.

### Step 8 — break it on purpose

The troubleshooting table in section 8 lists the log line each failure
produces. With `hello` loaded and the client running, cause four of them
deliberately and find each line before reading the table's answer. Undo each
one before the next.

1. **Save `config.json` with a UTF-8 BOM** (most Windows editors offer it as
   "UTF-8 with BOM"). Watch `mods.log` for the next few seconds. Which line
   repeats, how often, and what does the line just before the first
   repetition say? Section 2.9 explains why the host cannot simply ignore it.
2. **Misspell a key.** Change `"INTERVAL"` to `"INTERVAl"` in the `hello`
   section and save (without the BOM). One line names the key; the mod keeps
   the previous interval. Why is that the right behaviour rather than an
   error?
3. **`import traceback`** at the top of `main.py`. The mod fails to load —
   find the line and the traceback beneath it. Section 1.4 says which modules
   *are* importable; pick one from the resident list and confirm that one
   loads.
4. **Rename `on_update` to `on_tick`.** Nothing fails: the mod loads, logs its
   greeting, and never greets again, because the host looks up three names
   and does not warn about a fourth (section 3). This is the quietest failure
   in the framework, and the one to remember when a mod "loads but does
   nothing".

Then break the *stub* side, once, with the client stopped: rename
`uriel_natives.ini` and start the client. `hello` needs no natives and keeps
working; `apidiag` (enable it in a profile) reports the module as `None`, and
`uriel_stub.log` says `NATIVE: no uriel_natives.ini - gateway disabled (this
is fine)` — the silent failure document 03 is built around. Put the file back.

---

## 8. Troubleshooting

| Symptom | Where to look | Likely cause |
|---|---|---|
| Nothing in `mods.log` at all | `_patcher\uriel_stub.log` | `MODS: mods.off present` (kill switch), `bootstrap failed 5 times`, or `status: FAILED ...` from the Python bootstrap. If the status says `kwargs-FAIL`, `open()` rejects keywords — expected; if it says `write-FAIL`, the log path is not writable. |
| `mod host up` but my mod is not listed | `mods.log` around boot | Folder name starts with `.`/`_`, no `main.py`, or `mod 'x' disabled by config`. |
| `mod 'x' failed to load` + traceback | the traceback | Usually an import of a non-resident module, or a syntax error. Fix the file; the host retries only when the mtime changes. |
| `mod 'x' disabled after 5 consecutive faults` | the five preceding tracebacks | An exception in `on_update`. Fix, save, and the reload restarts the count. |
| `config key 'K' matches nothing - ignored` | your `main.py` | Misspelt key, or no module-level constant of that name. |
| Setting changed in F10 but behaviour unchanged | `mods.log` | Save not pressed; or the mod copies the constant elsewhere at load; or a profile overrides it (look for the `profile:` line). |
| `config changed - reloading mods` every second | `mods.log` | `config.json` or a profile is invalid — usually a UTF-8 BOM (section 2.9). The preceding `is invalid` line has the parse error. |
| My `ENABLED = False` mod runs anyway | section 2.2 | `enabled` is consumed by the host, never injected. Set it in `config.json`; `on_unload` is the off switch. |
| `AttributeError: api.player has no 'auto_move_active'` | section 4.10 | Fields are read with `api.field("...")`. |
| `field(...) needs triarch_native - check uriel_stub.log` | `NATIVE:` lines in `uriel_stub.log` | Gateway not registered (no `uriel_natives.ini`, or resolution failed) — or you called too early; it registers a second or two into the run, so retry rather than caching the failure. |
| `... is not in this client's field table` | `uriel_stub.log` | The `.ini` was regenerated under a running client. It is read once at DLL load: **restart the client**. |
| `this stub has no <native>` / `actor_pass ignored the exclude range` | stub version | The DLL predates that native or band support. Rebuild, redeploy, restart. |
| `SetTarget` "succeeds" but the target does not change | section 4.5 | The phase-window second argument is required (`api.set_target` does it). A stale selection needs `clear_target` → `select` → `set_target`. |
| `position()` is `None` / `api.chat` returns `False` in the world | phase | Between maps, the `(-100,-100,-100)` sentinel, or the chat window does not exist yet. Return early and retry next pump. |
| `api.ui is None` / `uikit: <row> failed` | boot lines / the row spec | `uikit.py` missing or failed to load (traceback in the log); or a widget class this build lacks — the row degrades, not fatal. |
| The manager window never appears | `modui:` lines | `GameWindow has no OnKeyDown - key handling is native` means F10 cannot be bound; set `OPEN_ON_START: true`. |
| Editing `modhost.py` changes nothing | section 2.6 | It does not hot-reload. Restart the client. |
| `triarch_native` is `None` inside `on_load`, fine a moment later | The host's first tick can run before the stub registers the module (82 ms apart on a measured boot). | Touch natives from `on_update`, not `on_load`; or reload the mod once. |
