# 07 — Mods catalogue

One section per mod in `mods/`. Each entry is derived from that mod's
`main.py` and `ui.json`; the config keys are the module-level UPPERCASE
constants the host overrides (see [06 — The mod framework](06-mod-framework.md),
section 2.3), with the defaults the source file declares. Where
`mods/config.json` or `patcher/config.example.json` ship a different value,
that is noted.

"Needs natives" means the mod depends on the `triarch_native` gateway the
stub registers from `uriel_natives.ini`. Without it, those mods either log a
warning and idle, or raise into the host's fault counter — never crash the
client.

## Table of contents

- [Summary table](#summary-table)
- [A note on default enablement](#a-note-on-default-enablement)
- Gameplay mods
  - [autohunt2](#autohunt2)
  - [autoloot](#autoloot)
  - [fishing](#fishing)
  - [follow](#follow)
  - [routes](#routes)
  - [goto](#goto)
  - [chanswap](#chanswap)
  - [keeproute](#keeproute)
  - [unstick](#unstick)
  - [nocollide](#nocollide)
  - [entscan](#entscan)
  - [bossscanner](#bossscanner)
  - [autologin](#autologin)
  - [shoppos](#shoppos)
  - [autotrade](#autotrade)
  - [autodonatexp](#autodonatexp)
  - [ribfarmer](#ribfarmer)
  - [orcfarmer](#orcfarmer)
  - [automonkey](#automonkey)
- Infrastructure
  - [modui](#modui)
- Diagnostics and examples
  - [posinfo](#posinfo)
  - [lootdiag](#lootdiag)
  - [mobdiag](#mobdiag)
  - [apidiag](#apidiag)
  - [nativeprobe](#nativeprobe)
  - [probe_ui](#probe_ui)
  - [uispy](#uispy)
- [Cross-mod protocols](#cross-mod-protocols)

---

## Summary table

| Mod            | Category             | Default enabled (`mods/config.json`)              | Needs natives                                                            | `ui.json` |
| -------------- | -------------------- | ------------------------------------------------- | ------------------------------------------------------------------------ | --------- |
| `autohunt2`    | combat automation    | **yes**                                           | yes (drive; degrades without)                                            | yes       |
| `autoloot`     | pickup               | **yes**                                           | for `WANTED` filtering, fetch and kill trigger; plain mode works without | yes       |
| `modui`        | manager window       | **yes**                                           | no                                                                       | yes       |
| `autologin`    | session              | no                                                | no                                                                       | no        |
| `chanswap`     | session              | no                                                | no                                                                       | no        |
| `entscan`      | minimap              | no                                                | optional (`minimap_mark` for mob dots)                                   | yes       |
| `fishing`      | automation           | no                                                | yes (`fishing_poll`)                                                     | no        |
| `follow`       | movement             | no                                                | no                                                                       | yes       |
| `goto`         | movement             | no                                                | no                                                                       | no        |
| `keeproute`    | movement             | no                                                | yes (fields)                                                             | yes       |
| `lootdiag`     | diagnostic           | no                                                | yes                                                                      | no        |
| `mobdiag`      | diagnostic           | no                                                | yes                                                                      | no        |
| `nocollide`    | movement             | no                                                | yes (`actor_pass`, fields)                                               | yes       |
| `posinfo`      | example / diagnostic | no                                                | no                                                                       | yes       |
| `routes`       | travel scripting     | no                                                | yes (fields)                                                             | yes       |
| `shoppos`      | data export          | no (inert until `URL` set)                        | yes (`offline_shops`, `http_post`)                                       | no        |
| `unstick`      | movement             | no                                                | no                                                                       | yes       |
| `apidiag`      | diagnostic           | no (ships an explicit `"enabled": false` section) | no                                                                       | no        |
| `nativeprobe`  | diagnostic           | no (ships an explicit `"enabled": false` section) | yes                                                                      | no        |
| `probe_ui`     | diagnostic           | no (ships an explicit `"enabled": false` section) | no                                                                       | no        |
| `uispy`        | diagnostic           | no (ships an explicit `"enabled": false` section) | no                                                                       | no        |
| `bossscanner`  | minimap              | no                                                | optional (`mark_mob` preferred; falls back to `atlas_mark`)              | no        |
| `autotrade`    | trade                | no                                                | no (drives the client's `exchange` module + `SendExchangeAcceptPacket`)  | yes       |
| `autodonatexp` | guild                | no (also gated by `ARMED` module flag)            | no (drives `SendGuildOfferPacket`)                                       | no        |
| `ribfarmer`    | farm preset          | no                                                | inherits `autohunt2` / `autoloot` needs                                  | yes       |
| `orcfarmer`    | farm preset          | no                                                | inherits `autohunt2` / `autoloot` needs                                  | yes       |
| `automonkey`   | dungeon loop         | no (also gated by `ONLY_CHARACTER`)               | uses `drop_engagement` from the stub; degrades otherwise                 | no        |

## A note on default enablement

`modhost.is_enabled()` returns `True` when a mod has **no section** in the
effective config, so a folder you drop into `mods/` loads on the next tick
whether or not you meant it to. This tree ships explicit `"enabled": false`
sections for the four diagnostic probes (`apidiag`, `nativeprobe`,
`probe_ui`, `uispy`) for exactly that reason: `nativeprobe` ships
`RUN_ACQUIRE = True` and its final stage calls `FindAndSetNewTarget`, which
can acquire a target and walk to it. A mod that disabled terrain collision
used to live here too; it was removed because terrain pass-through is
visible to the server (the client reports its own position several times a
second), and the framework's rule is that nothing server-visible ships.
When you add a mod, give it a section with `"enabled": false` first and turn
it on per character in a profile.

Two of the mods added in this pass carry a SECOND safety catch on top of
`enabled`, because the mod can act irreversibly on a live character:

- `autodonatexp` refuses to send anything until `ARMED = True`. It spends
  the character's own experience irreversibly, so being loaded is
  deliberately NOT enough.
- `automonkey` refuses to run unless `ONLY_CHARACTER` matches the logged-in
  character. It moves the character and starts a hunt; picking up the wrong
  profile is both surprising and hard to notice, so the guard is opt-in.

`ribfarmer` and `orcfarmer` are mutually exclusive with each other and with
a directly-loaded `autohunt2` or `autoloot` — each checks the effective
config at load, names the mod in the way, and stands down without disabling
anyone.

---

## autohunt2

**Purpose.** Policy on top of the client's own auto-hunt: objective
priorities (boss > metin stone > preferred trash), body-block detection,
keeping distance from other players, channel looping, auto-revive and a
route hand-off — while the client's native code does the actual selection,
approach and attack sustain.

**How it works.** The docstring explains the design decision: an earlier
version reimplemented targeting and attacking in Python, and every one of
those already existed in the client, done better (the victim search runs a
line-of-sight ray march unreachable from Python). The client's whole
auto-hunt reduces to a guarded `FindAndSetNewTarget(player, main, bStone,
excludeVID)` when no target is engaged, plus `UseAutoSkills(main, target,
now)` for skills. `autohunt2` calls both through `api.player.*` (natives from
`natives.json`) on its own `TICK_S = 0.25` policy tick, reads
`auto_attack_vid` through `api.field`, and adds what the client lacks:

- **Body-block detection**: swinging at a target it cannot reach for
  `BLOCK_AFTER_S` names that VID as `excludeVID` so the client picks another.
- **Seek tiers**: `BOSS_RACES` and `METIN_RACES` are scanned with
  `api.actors()` every `BOSS_SCAN_S`; a hit pre-empts the current fight. A
  boss is walked to with `api.move_to` and only engaged within `BOSS_REACH`,
  because pressing a target 7000 units away does not engage.
- **Contested-spot avoidance**: it predicts other players' positions
  (velocity-smoothed) and simulates their nearest-first target chain, then
  relocates to a ring-sampled candidate spot that keeps `SAFE_DIST` from
  everyone and still has `SPOT_MIN_MOBS` around it. Metin stones are exempt.
- **Metin rear sweep**: periodically turns to the mob most directly behind a
  stone to clear the pack that gathers there.
- **Isolation**: it never sends `/auto_hunt` and blocks
  `miniMap.SetAutoHuntStatus`, the flag the native loop tests first, so the
  client's own loop stays inert. The `/auto_hunt start|end` chat command that
  the shipped Start button emits is intercepted at `m2netm2g.SendChatPacket`
  and becomes this mod's start/stop. `/restart_here` and `/user_horse_ride`
  are logged and passed through (swallowing them once broke the revive
  button).
- **Coordination**: honours `TRIARCH_LOOT_BUSY` (a deadline published by
  `autoloot`), publishes `TRIARCH_HUNT_STATE` and consumes
  `TRIARCH_HUNT_REQUEST` for `routes`, and posts `TRIARCH_CHAN_REQUEST` for
  `chanswap` when `LOOP_CHANNELS` finds the area empty.

**Config keys** (the important ones; the file declares 95 module constants
and comments each):

| Key                                                                                                                                                                                                     | Type                | Default                                                                                           | Meaning                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `AUTOSTART`, `AUTOSTART_AFTER`                                                                                                                                                                          | bool, s             | `False`, `6.0`                                                                                    | Start without the button (test mode; the button is the normal trigger).                                                          |
| `WALK_STATE`                                                                                                                                                                                            | int                 | `0x8A`                                                                                            | Build constant stamped by the patcher; never pin in a profile.                                                                   |
| `HUNT_STONES`                                                                                                                                                                                           | bool/None           | `None`                                                                                            | `None` = the window owns the "stones only" choice.                                                                               |
| `HUNT_RANGE`                                                                                                                                                                                            | float/None          | `300.0`                                                                                           | Search radius slider value; radius = `(v + 80) * 35` units. `None` = leave the slider alone.                                     |
| `USE_SKILLS`                                                                                                                                                                                            | bool                | `True`                                                                                            | Drive `UseAutoSkills`.                                                                                                           |
| `SKILLS_WHILE_MOUNTED`                                                                                                                                                                                  | bool                | `True`                                                                                            | Let the client dismount → cast → remount. Uncheck on characters that cannot attack mounted.                                      |
| `MOUNT_ONLY_ON_STONES`                                                                                                                                                                                  | bool                | `False`                                                                                           | Remount only while the objective is a stone.                                                                                     |
| `BOSS_RACES`, `METIN_RACES`, `TRASH_RACES`                                                                                                                                                              | list of int or name | `[]`                                                                                              | The three tiers. Names resolve through `nonplayer.GetMonsterDataByNamePart`, cached per load.                                    |
| `METIN_ANY_STONE`                                                                                                                                                                                       | bool                | `False`                                                                                           | Seek anything the client flags `IsStone`, not just listed races.                                                                 |
| `TRASH_RANGE`                                                                                                                                                                                           | units               | `3000.0`                                                                                          | How close a tier-3 mob must be to be preferred.                                                                                  |
| `ONLY_BOSSES`, `ONLY_METINS`                                                                                                                                                                            | bool                | `False`                                                                                           | Stop seeking trash (a fight in progress still finishes).                                                                         |
| `METIN_SWEEP`, `METIN_SWEEP_EVERY_S`, `METIN_SWEEP_DUR_S`, `METIN_SWEEP_RANGE`, `METIN_SWEEP_MOUNTED`                                                                                                   |                     | `True`, `15.0`, `2.0`, `250.0`, `False`                                                           | The rear sweep.                                                                                                                  |
| `BOSS_RANGE`, `METIN_RANGE`, `BOSS_REACH`, `BOSS_GIVEUP_S`                                                                                                                                              | units, s            | `8000.0`, `0.0` (no cap), `700.0`, `0.0` (never)                                                  | Seek-tier distances.                                                                                                             |
| `BLOCK_AFTER_S`, `BLOCK_MOVE`, `BLOCK_REACH`, `APPROACH_DIST`, `EXCLUDE_S`                                                                                                                              |                     | `1.0`, `30.0`, `250.0`, `800.0`, `6.0`                                                            | Body-block detector.                                                                                                             |
| `AVOID_CONTESTED`, `AVOID_WHEN_STONES`, `PLAYER_NEAR_ME`, `CONTEST_RADIUS`, `THREAT_DWELL_S`, `SAFE_DIST`, `SAFE_DIST_MIN`, `MAX_DRIFT`, `RELOCATE_COOLDOWN_S`, `RELOCATE_FIGHT_ALONG`, `CONTEST_DEBUG` |                     | `True`, `False`, `1500.0`, `1200.0`, `1.5`, `2500.0`, `1500.0`, `6000.0`, `25.0`, `False`, `True` | Player avoidance and relocation.                                                                                                 |
| `LOOP_CHANNELS`, `LOOP_IDLE_S`, `LOOP_SETTLE_S`                                                                                                                                                         |                     | `False`, `10.0`, `15.0`                                                                           | Hop channels when the area is empty (needs `chanswap`).                                                                          |
| `SHOW_RANGE`                                                                                                                                                                                            | bool                | `True`                                                                                            | Show the client's own hunt-area circle on the minimap.                                                                           |
| `REVIVE`, `REVIVE_CMD`, `REVIVE_AFTER_S`, `REVIVE_EVERY_S`, `RESUME_AFTER_S`                                                                                                                            |                     | `True`, `"/restart_here"`, `10.0`, `6.0`, `5.0`                                                   | Auto-revive with the same command the revive button sends.                                                                       |
| `WATCH_CHAT`                                                                                                                                                                                            | bool                | `True`                                                                                            | Install the `/auto_hunt` interceptor (the start trigger).                                                                        |
| `UI_DETOUR`                                                                                                                                                                                             | bool                | `False`                                                                                           | Also patch the autohunt window's Start button at class level; off because whether it takes effect depends on construction order. |
| `VERBOSE`                                                                                                                                                                                               | bool                | `True`                                                                                            |                                                                                                                                  |

`mods/config.json` ships `WALK_STATE: 138`, empty race lists and
`TRASH_RANGE: 3000.0`; `patcher/config.example.json` shows a fuller example
with `BOSS_RACES: [1093]` and `METIN_RACES: [8005]`.

**UI page.** Value rows for the three tiers, `TRASH_RANGE` slider,
`ONLY_BOSSES` / `ONLY_METINS`, `USE_SKILLS`, `SKILLS_WHILE_MOUNTED`,
`MOUNT_ONLY_ON_STONES`, `METIN_SWEEP_MOUNTED`, and Revert. Race lists are
display-only in the UI (no list editor widget exists); edit them in the file.

**Profile notes.** Race lists and `ONLY_*` are the natural per-character
settings. `WALK_STATE` is always taken from the shared file.

**Limitations.** Needs the native drive; without it the mod loads, logs
`native_drive=False` and the Start button reports "native drive
unavailable". Channel looping needs `chanswap` loaded. The relocation logic
is heuristic and documented as such in the comments.

**What it teaches.** Driving the engine's own functions through a typed
native table instead of re-implementing them; reading engine state through
fields; intercepting a chat command to convert a UI button into a mod
trigger; the environment-variable protocol between mods.

---

## autoloot

**Purpose.** Pick up drops that belong to you, without the keypress.

**How it works.** `api.pick_up_items()` and `api.pick_up_money()` call the
client's own batch pickup, which keeps only drops within ~1000 units whose
ownership string matches your name — so this can never take someone else's
loot. Three modes:

1. `WANTED` empty (default): a batch sweep triggered on the **kill edge**
   (`auto_attack_vid` going non-zero → zero, read via `api.field`), with
   `KILL_SWEEPS` spaced sweeps, plus a slow jittered idle sweep. The
   docstring explains why: the old 0.5 s metronome was the shape of a macro.
2. `WANTED` filled: `api.ground_items()` enumerates the floor, entries are
   matched by vnum (int) or localised display-name substring (string; short
   fragments must match a whole word), and one `api.pick_up(id)` goes out per
   match. With `FETCH_WANTED`, a listed drop outside pickup range up to
   `FETCH_RANGE` away is walked to: the mod publishes a `TRIARCH_LOOT_BUSY`
   deadline so `autohunt2` stands down, calls the stub's `drop_engagement()`
   so the engine's own attack walk stops pulling the other way, and gives
   the walk a distance-scaled deadline.
3. `LEGACY_MODE` (or the older name `SPAM_PICKUP`): the original fixed-period
   loop, kept verbatim as a fallback, now gated on drops being present.

**Config keys.**

| Key                                                                                                                                                                                              | Type            | Default                                                                                  | Meaning                                                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `TRIGGER_ON_KILL`, `KILL_DELAY`, `KILL_SWEEPS`, `KILL_SPACING`                                                                                                                                   | bool, s, int, s | `True`, `0.6`, `2`, `1.2`                                                                | Kill-edge sweeps.                                                       |
| `IDLE_INTERVAL`, `IDLE_JITTER`                                                                                                                                                                   | s               | `8.0`, `3.0`                                                                             | Fallback sweep, period randomised ± jitter.                             |
| `LEGACY_MODE`, `LEGACY_INTERVAL`, `SPAM_PICKUP`, `SPAM_INTERVAL`, `INTERVAL`                                                                                                                     |                 | `False`, `0.5`, `False`, `0.5`, `0.5`                                                    | Legacy loop and its back-compat names.                                  |
| `TAKE_ITEMS`, `TAKE_MONEY`                                                                                                                                                                       | bool            | `True`                                                                                   |                                                                         |
| `WANTED`                                                                                                                                                                                         | list            | `[]`                                                                                     | Allow-list of vnums / name fragments. Empty = take everything.          |
| `NAME_MIN_LEN`                                                                                                                                                                                   | int             | `4`                                                                                      | Fragments shorter than this match whole words only.                     |
| `IGNORED_LOGGED`                                                                                                                                                                                 | bool            | `True`                                                                                   | Log once per vnum what is being left behind.                            |
| `SET_CLIENT_FLAG`                                                                                                                                                                                | bool            | `True`                                                                                   | Turn on the client's own AUTO_PICK option so the options window agrees. |
| `FETCH_WANTED`, `FETCH_RANGE`, `FETCH_ARRIVE`, `FETCH_TIMEOUT_S`, `FETCH_SPEED`, `FETCH_REPATH_S`, `FETCH_PROGRESS`, `FETCH_COOLDOWN_S`, `FETCH_RETRY_S`, `FETCH_HOLD_S`, `FETCH_WHILE_FIGHTING` |                 | `True`, `3000.0`, `700.0`, `10.0`, `250.0`, `3.0`, `150.0`, `1.5`, `45.0`, `3.0`, `True` | Walking to out-of-range listed drops.                                   |
| `VERBOSE`                                                                                                                                                                                        | bool            | `False`                                                                                  | Log every sweep.                                                        |

**UI page.** `TAKE_ITEMS`, `TAKE_MONEY`, `LEGACY_MODE` + `LEGACY_INTERVAL`,
`TRIGGER_ON_KILL`, `IDLE_INTERVAL`, `SET_CLIENT_FLAG`, `VERBOSE`, Revert.

**Profile notes.** `WANTED` differs per character in practice (the example
config shows `["Sword", 27610]`; names follow the client's active locale pack,
the examples use the English one). Name matching is localised; prefer vnums.

**Limitations.** Needs api v13 for pickup, v15 for `WANTED` (falls back to
blind pickup with a log line). The kill trigger and fetching need the native
field layer; without it `_engaged()` reads 0 and only the idle sweep fires.
Imports `random` at module level, which the boot dump lists as resident on
this client.

**What it teaches.** Using the client's own filtered pickup rather than
reimplementing ownership; turning a metronome into an event-triggered action
with jitter; publishing a **deadline** rather than a flag to hold another mod.

---

## fishing

**Purpose.** Automate the rod-fishing loop: cast, wait for the bite, wait the
species-specific delay, reel, use bait and the fish-info consumable, open
caught fish, recast. Armed and disarmed with F5.

**How it works.** The bite is native-only — no Python callback fires — so the
stub watches the fishing packets and exposes counters through
`api.fishing_poll()`; `bite` is monotonic, so polling cannot miss one. Cast
and reel both go through `ClickSkillSlot` on the fishing skill's slot (the
client's own skill-press path; the slot is resolved from `FISH_SKILL_ID` at
runtime). The catch is not "reel on the bite": the mod waits a per-species
delay from `FISH_BASE` (adjusted by `ROD_ADJUST` and `PIPELINE_LAG`), which
it can only know when the fish-info consumable is active. A `STUDY_LOG`
writes a CSV of species/delay/result so the delays can be tuned. The mod wraps
the game window's `OnKeyDown` to catch F5 and restores it on unload only if
its wrapper is still the installed one (so `modui`, loaded later, keeps its
own wrapper).

**Config keys** (46 constants; the main ones):

| Key                                                                                | Default                                | Meaning                                                                                                 |
| ---------------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `ARM_KEY`                                                                          | `63` (DIK_F5)                          | Toggle key; read from `app` at runtime, this is the fallback.                                           |
| `CAST_METHOD`, `REEL_METHOD`                                                       | `"skill"`                              | `"space"` selects the legacy `api.key_event` path, which did not reach the game on the measured client. |
| `FISH_SKILL_ID`, `FISH_SKILL_SLOT`, `SKILL_SCAN_MAX`                               | `123`, `-1`, `160`                     | Skill resolution.                                                                                       |
| `HOLD_S`                                                                           | `0.2`                                  | Legacy key hold.                                                                                        |
| `USE_BOLA`, `BOLA_VNUM`, `BOLA_AFFECT`, `BOLA_REFRESH`, `BOLA_CHECK_S`             | `True`, `27610`, `208`, `120.0`, `5.0` | Keep the fish-info consumable up (affect 208, measured 20 min).                                         |
| `USE_FISH`, `FISH_ITEM_TYPE`, `USEFISH_MAX`, `USEFISH_CHECK_S`                     | `True`, `12`, `4`, `2.0`               | Open caught fish, rate-capped.                                                                          |
| `USE_BAIT`, `BAIT_VNUMS`, `BAIT_SETTLE`, `MIN_BAIT_GAP`                            | `True`, `[27802, 27801]`, `0.5`, `5.0` | Bait the rod before each round; pause if none held.                                                     |
| `MIN_BITE_DELAY`, `CAST_CONFIRM_S`, `FISH_TIMEOUT`, `REEL_TIMEOUT`, `RECAST_DELAY` | `1.5`, `3.0`, `55.0`, `10.0`, `3.5`    | Loop timing.                                                                                            |
| `ROD_ADJUST`, `PIPELINE_LAG`, `DEFAULT_DELAY`, `ITEM_DELAY`, `FISH_BASE`           | `0.225`, `0.30`, `2.5`, `2.15`, table  | Reel-delay model.                                                                                       |
| `SKIP_JUNK`, `JUNK_GRACE`                                                          | `True`, `0.6`                          | Junk bites are uncatchable; reel at once to resolve the line.                                           |
| `FAIL_LIMIT`, `FAIL_BACKOFF`, `NOREEL_AFTER_S`, `NOREEL_LIMIT`                     | `4`, `20.0`, `2.0`, `3`                | Dead-cast and no-reel detection with backoff.                                                           |
| `STUDY_LOG`, `STUDY_SWEEP`, `LOCKED_VNUMS`, `SWEEP_OFF`                            | `True`, `True`, set, list              | Timing study.                                                                                           |
| `AUTOARM`, `AUTOARM_AFTER`                                                         | `False`, `5.0`                         | Arm without F5, once per load. Profile-only by convention.                                              |
| `DEBUG_KEYS`                                                                       | `False`                                | Log every key code.                                                                                     |

**UI page.** None; configure in the file or a profile.

**Profile notes.** `AUTOARM` is documented as per-character profile only; the
shared config keeps it off.

**Limitations.** Needs a stub with `fishing_poll`; warns once and idles
without it. Item vnums and the fish table are server-specific data captured
from one server's item set.

**What it teaches.** When the Python layer cannot observe an event, the stub
observes the packet and exposes a monotonic counter; how to wrap and safely
unwrap a shared key handler; building a measurement log into a mod so tuning
is data-driven.

---

## follow

**Purpose.** Keep a second character near your main and re-buff it, across
channels.

**How it works.** Every client running the mod publishes its own record
(name, channel, map, position, buff timers, timestamp) through `api.link`
every `PUBLISH_S`. A client whose character is not `MAIN` reads the main's
record and walks toward it with `api.move_to`, with a hysteresis band
(`CAST_DIST` … `FOLLOW_DIST`), a re-path rule that only re-aims when the goal
has moved enough (`REPATH_MOVE` or `REPATH_FRAC` of the remaining distance,
because every move restarts pathing), and a stuck watchdog. It keeps the main
targeted (`api.set_target`, with the clear → select → set fallback for a
stale selection, rate-limited by `RETARGET_S`) and presses `BUFF_SLOTS` in
turn when the main's published timers say a buff is missing or below
`BUFF_REFRESH_AT` — the caster cannot read another character's affects, so
the timers come over the link. A map mismatch pauses following; a channel
mismatch keeps shadowing the position (coordinates are identical across
channels) and, with `FOLLOW_CHANNEL`, requests a swap via
`TRIARCH_CHAN_REQUEST` once the main has held a channel for
`CHAN_FOLLOW_DELAY`.

**Config keys.**

| Key                                                                        | Default                                        | Meaning                                                                                                                                                |
| -------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `MAIN`                                                                     | `""`                                           | **Required.** The main character's name. Deliberately not auto-detected (two clients would chase each other) and deliberately not baked into the file. |
| `CAST_DIST`, `FOLLOW_DIST`                                                 | `600.0`, `900.0`                               | Hold inside the first; start walking outside the second.                                                                                               |
| `REPATH_MOVE`, `REPATH_FRAC`, `ARRIVE_DIST`, `STUCK_S`                     | `300.0`, `0.25`, `250.0`, `4.0`                | Re-path rules.                                                                                                                                         |
| `PUBLISH_S`                                                                | `0.5`                                          | Link publish rate.                                                                                                                                     |
| `FOLLOW_CHANNEL`, `CHAN_SETTLE_S`, `CHAN_FOLLOW_DELAY`                     | `False`, `15.0`, `30.0`                        | Auto channel-join via `chanswap`.                                                                                                                      |
| `TARGET_MAIN`, `RETARGET_S`                                                | `True`, `3.0`                                  | Keep the main selected.                                                                                                                                |
| `BUFF_SLOTS`, `BUFF_AFFECTS`, `BUFF_REFRESH_AT`, `BUFF_PERIOD`, `BUFF_GAP` | `[0,1,2]`, `[94,95,96]`, `15.0`, `55.0`, `2.5` | Hotbar slots to press, affect indices to watch, thresholds.                                                                                            |

**UI page.** `TARGET_MAIN`, `MAIN` edit box, `CAST_DIST`, `FOLLOW_DIST`,
`BUFF_REFRESH_AT`, read-only `BUFF_SLOTS` / `BUFF_AFFECTS`, Revert.

**Profile notes.** The same file runs in both clients; `MAIN` in the shared
config makes the main publish-only and every other character follow. Set
`BUFF_SLOTS: []` in a non-caster's profile for follow-only.

**Limitations.** File-based link with a 6 s staleness window; channels 1–5
for the auto-join; the buff logic assumes the main runs the mod too.

**What it teaches.** Cross-process coordination through atomic file writes;
hysteresis and edge-triggered movement; why "VIDs are per client" forces
name-based resolution.

---

## routes

**Purpose.** Scripted travel that coordinates the other mods: walk paths,
closed hunting loops, channel hops and map exits, handing control to
`autohunt2` (or any worker) at each stop.

**How it works.** A route is data — the config key `ROUTES` or
`_patcher\routes.json` written by the in-game recorder — a list of steps with
an `op`: `goto`, `path`, `loop`, `channel`, `warp`, `hunt`, `task`, `wait`.
Each step function returns `True` when done, `"fail"` to abort, or nothing to
keep waiting. Walking re-issues `api.move_to` when away and not travelling
(the two-field test `auto_move_active` and `player_state == WALK_STATE`, via
`api.field`), sidesteps perpendicular after `STUCK_S` of no progress, and
gives up after `MAX_STUCK_TRIES`. Hunting stops set `TRIARCH_HUNT_REQUEST =
"start"` and wait for `TRIARCH_HUNT_STATE` to report `clear` for
`HUNT_IDLE_S` (or `idle` for `HUNT_IDLE_NOCLEAR_S`); channel steps post
`TRIARCH_CHAN_REQUEST`; `TRIARCH_ROUTE_ACTIVE = "1"` tells `autohunt2` not to
hop channels on its own. Commands are typed in chat and swallowed at
`SendChatPacket`:

```
/route list | run <name> | stop | status | show <name>
/route mark | mark undo | mark clear
/route save <name> loop|path [hunt]
/route delete <name> | hop <ch>
```

**Config keys.**

| Key                                                                                                   | Default                                               | Meaning                                                                    |
| ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------- |
| `WALK_STATE`                                                                                          | `138`                                                 | Build constant, stamped by the patcher.                                    |
| `ARRIVE_DIST`, `REISSUE_S`, `STUCK_S`, `MIN_PROGRESS`, `SIDESTEP`, `MAX_STUCK_TRIES`, `LEG_TIMEOUT_S` | `250.0`, `3.0`, `4.0`, `60.0`, `1100.0`, `4`, `180.0` | Walking.                                                                   |
| `CHANNEL_TIMEOUT_S`, `WARP_TIMEOUT_S`, `SETTLE_S`                                                     | `60.0`, `90.0`, `3.0`                                 | Channel/map transitions.                                                   |
| `HUNT_IDLE_S`, `HUNT_IDLE_NOCLEAR_S`, `HUNT_MAX_S`, `HUNT_START_TIMEOUT_S`, `TASK_START_TIMEOUT_S`    | `6.0`, `20.0`, `0.0`, `8.0`, `8.0`                    | Hunting/task stops.                                                        |
| `AUTORUN`                                                                                             | `""`                                                  | Route to start once per load, 5 s after entering the world. Use a profile. |
| `ROUTES`                                                                                              | `{}`                                                  | Routes in config; `routes.json` entries override by name.                  |
| `CMD`, `SCALE_BELOW`, `STORE`, `VERBOSE`                                                              | `"/route"`, `20000.0`, `"routes.json"`, `True`        |                                                                            |

**UI page.** `AUTORUN` (read-only), `ARRIVE_DIST`, `STUCK_S`, `SIDESTEP`,
`HUNT_IDLE_S`, `HUNT_MAX_S`, `VERBOSE`, Revert.

**Profile notes.** `AUTORUN` is the per-character key.

**Limitations.** Needs api v18. Coordinates auto-scale like `goto` (values
under 20000 are ×100). The `task` op describes a worker protocol that no
shipped mod implements yet.

**What it teaches.** A small step interpreter over JSON; a request/state
protocol between isolated mods with timeouts so an absent worker cannot hang
the route.

---

## goto

**Purpose.** A `/goto x,y` chat command that walks the character to a point.

**How it works.** Wraps `m2netm2g.SendChatPacket`; a message starting with
`CMD` is parsed and swallowed (`return None`), everything else is passed to
the original. `api.move_to` does the walking. Inputs under `SCALE_BELOW` are
treated as map-display coordinates and multiplied by 100; larger values are
raw world units. The wrapper is restored in `on_unload`.

**Config keys.** `CMD = "/goto"`, `SCALE_BELOW = 20000.0`.

**UI page.** None. **Profile notes.** None needed.

**Limitations.** No pathfinding beyond the client's straight-line auto-move.

**What it teaches.** The minimal chat-command pattern used by `routes` and
`autohunt2`, in a hundred lines.

---

## chanswap

**Purpose.** Change channel programmatically, in-world, without clicks and
without natives.

**How it works.** Two methods. `METHOD = "direct"` reproduces exactly what
the channel window's connect button does, recovered from a static decode of
the compiled handler: `SetServerInfo("<name>, CH<n>")` then
`SendChatPacket("/kanal <n>")`; the server answers with a warp and the client
reconnects on its own. `METHOD = "ui"` drives the live, registered
`ChannelChanger` window (show, refresh, select the list row, commit), the
recipe that was measured to work before the direct one was found. Other mods
request a change by writing `TRIARCH_CHAN_REQUEST`, which this mod consumes
once; it also registers `sys.modules["triarch_chan"]` with `change_to(ch)`,
`current()` and `state()` for direct calls.

**Config keys.** `METHOD = "direct"`, `MIN_CH, MAX_CH = 1, 5`, `SETTLE = 2.0`
(between UI stages), `LAND_TIMEOUT = 40.0`.

**UI page.** None.

**Limitations.** Channels 1–5; the UI method only reaches the first list
page. Only works in the game phase.

**What it teaches.** Preferring the client's own high-level handlers over
raw network calls; reading a compiled handler to find the two calls that
matter; exporting a tiny interface through `sys.modules`.

---

## keeproute

**Purpose.** Resume a walk that combat interrupted — and nothing else.

**How it works.** The measured problem: being attacked while auto-walking
cancels the route and the client never resumes it. The hard part is telling
"combat took my walk" from "I cancelled it on purpose". The discriminator is
read off the engine at the moment the route dies: if `auto_attack_vid` is
non-zero then, combat did it and the destination (`auto_move_dest`) is
re-issued with `api.move_to`, at most every `RESUME_EVERY` and at most
`RESUME_LIMIT` times; otherwise the player did it and the mod stands down.
Travelling is the two-field test (`auto_move_active` and `player_state ==
WALK_STATE`).

**Config keys.** `WALK_STATE = 0x8A`, `RESUME_EVERY = 1.5`, `RESUME_LIMIT =
12`, `ARRIVE_DIST = 300.0`, `VERBOSE = True`.

**UI page.** `RESUME_EVERY`, `RESUME_LIMIT`, `VERBOSE`, Revert.

**Limitations.** Needs api v18 and the field layer. A deliberate cancel
_while_ fighting is ambiguous and resolves as "resume" — hence the limit.

**What it teaches.** Making a decision once, from engine state, at the moment
it is decidable; never inventing a destination.

---

## unstick

**Purpose.** Break the auto-move deadlock when the character wedges on
geometry or a mob.

**How it works.** The docstring explains that the client's auto-move is not a
pathfinder — it steps straight at the destination and lets collision stop it
— so a concave corner is a trap. The mod samples position every
`CHECK_EVERY`; if there is a target further than `NEAR_DIST` and no
`MIN_PROGRESS` for `STUCK_S`, it walks `SIDESTEP` units perpendicular to the
blocked heading (alternating sides), waits `SETTLE_S`, and lets whatever was
driving the walk re-issue it. After `MAX_TRIES` it backs off for
`COOLDOWN_S`. Collision stays on; movement is `api.move_to`.

**Config keys.** `CHECK_EVERY = 0.5`, `STUCK_S = 2.5`, `MIN_PROGRESS = 60.0`,
`NEAR_DIST = 400.0`, `SIDESTEP = 1100.0`, `SETTLE_S = 2.5`, `MAX_TRIES = 3`,
`COOLDOWN_S = 15.0`, `VERBOSE = True`.

**UI page.** `STUCK_S`, `SIDESTEP`, `MAX_TRIES` sliders, `NEAR_DIST`
read-only, `VERBOSE`, Revert.

**Limitations.** The arming condition needs a target; standing still with
nothing selected is never "stuck".

**What it teaches.** Pure read-only detection plus one client-sanctioned
action; a comment in the code records a subtle bug (re-anchoring at "now"
burned two tries per stall) and its fix.

---

## nocollide

**Purpose.** Walk through monsters while travelling to a waypoint. Terrain
still stops you. Metin stones always keep their collision.

**How it works.** Monsters body-block by _displacement_: the engine pushes
you back out of each nearby actor every frame. The engine's actor test is a
race whitelist with two exempt ranges; `api.actor_pass(on, (lo, hi))` widens
them, leaving the `SOLID_RACES` band solid — the mechanism mounts already
use. By default (`ONLY_ON_WAYPOINT`) it is on only while a route is being
followed, which is the two-field test on `auto_move_active` and
`player_state` read via `api.field`. `_travelling()` returns `None` when a
field cannot be read, and the caller **holds the previous state** and warns
once rather than guessing. Both switches are cleared on unload and on
leaving the world. `NO_TERRAIN` is a separate, loudly-warned switch on
`api.terrain_pass`. The mod also checks that any targeted stone's race falls
inside `SOLID_RACES` and complains if not.

**Config keys.**

| Key                | Default                                                 | Meaning                                                                                           |
| ------------------ | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `NO_TERRAIN`       | `False`                                                 | Walls too. Not safe; see the framework chapter, section 6.                                        |
| `SOLID_RACES`      | `[8000, 8999]`                                          | Race band kept solid (stones sit at 8005 on the measured server). `[]` makes stones passable too. |
| `ONLY_ON_WAYPOINT` | `True`                                                  | Pass through only while walking to a waypoint.                                                    |
| `WALK_STATE`       | `0x8A`                                                  | Build constant.                                                                                   |
| `VERBOSE`          | `True`                                                  |                                                                                                   |
| `DEBUG`, `DEBUG_S` | `True` in source (`False` in `mods/config.json`), `3.0` | Log raw engine state every `DEBUG_S`.                                                             |

**UI page.** `ONLY_ON_WAYPOINT`, `SOLID_RACES` read-only, `VERBOSE`, `DEBUG`,
a red warning block, and the `NO_TERRAIN` checkbox last.

**Profile notes.** `enabled` per character is the usual override.

**Limitations.** Needs api v18, a stub with band-aware `actor_pass`, and the
field layer; without the fields it holds state and warns to restart the
client (the `.ini` is read once at DLL load).

**What it teaches.** The difference between a _stop_ and a _displacement_;
separating two risks into two switches; never treating "cannot read" as
"false"; restoring global state on every exit path.

---

## entscan

**Purpose.** Mark every loaded metin stone on the atlas, and put a red target
mark on monsters whose name matches a list.

**How it works.** There is no enumeration binding for this in Python, so the
sweep asks `api.has_instance(vid)` across a VID window around your own VID
(`RADIUS_LO` below, `RADIUS_HI` above, because VIDs are issued ascending and
never reused), `CHUNK` ids per pump so a 550k window costs about four seconds
with no frame hitch. Hits are `api.describe`d; stones get `api.atlas_mark`
(type-6 waypoint), named mobs get `api.mark_mob` (type-13 target mark that
the engine moves with the mob). The cheap half runs every pump: one
`has_instance` per marked entity, so a destroyed stone comes off the map
within a second. A map or channel change drops everything and re-sweeps.

**Config keys.** `SWEEP_EVERY = 5.0`, `CHUNK = 100000`, `RADIUS_LO = 500000`,
`RADIUS_HI = 50000`, `MAX_MARKS = 60`, `MARK_BASE = 991000`,
`TARGET_NAMES = "Snake Archer, Scorpion Archer"` (comma string or JSON
list of case-insensitive substrings of the localised name; empty = stones
only), `MAX_TARGET_MARKS = 40`, `TARGET_MARK_BASE = 992000`.

**UI page.** `SWEEP_EVERY`, `CHUNK`, `MAX_MARKS`, `TARGET_NAMES` edit box,
`MAX_TARGET_MARKS`, window sizes read-only, Revert.

**Limitations.** Mob dots need the stub's `minimap_mark` native (warns once,
stones still marked). Names are localised.

**What it teaches.** Splitting expensive discovery from cheap watching;
spreading a long loop across pumps; disjoint id ranges so two features cannot
clobber each other's marks.

---

## bossscanner

**Purpose.** Ping every monster the client would draw with an ORANGE name —
bosses, kings and the whole "special" class — without a hand-maintained name
list. Complements `entscan`, which matches by localised name.

**How it works.** Special is decided from **two sources**, unioned at load
from the running client:

- **Rank** — `nonplayer.GetGradeByVID` returning `4` (BOSS) or `5` (KING). The
  docstring names a trap: metin stones and NPCs also read `5`, so the rank
  test is applied only when `pack_chr.GetInstanceType(vid)` matches
  `INSTANCE_TYPE_ENEMY`.
- **The client's own boss list** — the `WikiConfig` module exposes
  `CUSTOM_BOSS_CATEG_VNUMS_*` per-region categories plus `bossList`. Unioned
  and cached; on a server patch that adds a special, a reload picks it up.

A separate `BOSS_SPAWN_VNUMS` list holds vnums parsed from a static cut of
the server's own `boss.txt` files, and `EXTRA_VNUMS = [681]` covers
Thunderclap — orange in game and absent from every list available at the
time. Ordinary marking is `api.mark_mob` (type-13, moves with the actor);
the fallback when the stub does not export it is `api.atlas_mark`, re-placed
each pass to track a moving mob. Ownership of a mob is checked every
`PRUNE_EVERY_S`; a killed one comes off the map within about half a second.

**Config keys** (main ones):

| Key                                                   | Default             | Meaning                                                 |
| ----------------------------------------------------- | ------------------- | ------------------------------------------------------- |
| `BOSS_RANKS`                                          | `[4, 5]`            | Ranks that count as special on their own.               |
| `USE_WIKI_LIST`, `WIKI_MODULES`, `WIKI_PREFIXES`      | `True`, ...         | The client-side vnum list, unioned at load.             |
| `BOSS_SPAWN_VNUMS`                                    | 29 vnums            | Static copy of the server's own boss spawns.            |
| `EXTRA_VNUMS`, `IGNORE_VNUMS`, `IGNORE_NAMES`         | `[681]`, `[]`, `[]` | Manual overrides.                                       |
| `USE_IMMUNE`                                          | `False`             | Falsified — kept only for the EXPLAIN diagnostic.       |
| `SCAN_EVERY_S`, `PRUNE_EVERY_S`                       | `2.0`, `0.5`        | Discovery vs upkeep cadence.                            |
| `MAX_MARKS`, `MARK_BASE`                              | `40`, `994000`      | Cap and id range (disjoint from `entscan`, `autoloot`). |
| `ANNOUNCE`, `ANNOUNCE_ONCE`                           | `True`, `True`      | Chat popup on first sight.                              |
| `EXPLAIN_NAMES`, `PROBE`, `HUNT_COLOUR`, `HUNT_VNUMS` | `[]` / `False`      | One-shot probes; verbose diagnostics.                   |
| `VERBOSE`                                             | `True`              |                                                         |

**UI page.** None.

**Limitations.** Type-13 mob marks need the stub's `minimap_mark` native; on
builds without it, atlas marks are used and re-placed on every prune tick.
Rank 3 (`S_KNIGHT`) is deliberately NOT special (Black Orc is one, and its
name is red). `BOSS_SPAWN_VNUMS` is a static copy of the server-side file
the docstring quotes.

**What it teaches.** Combining two authoritative sources at load rather than
maintaining a hand-typed list; the difference between "correlates" and "is"
(rank, AI flag, immunity, boss list — each one wrong in isolation); reading
the client's own tables to keep a mod robust across server patches.

---

## autologin

**Purpose.** Get from launch to in-game unattended by pressing the client's
own buttons.

**How it works.** When an account has logged in before, the landing screen is
a "tap to play" gate with saved credentials already loaded, so the tap _is_
the login. The mod waits `TAP_AFTER`, calls `__OnTapToPlay` on phase window
1, then polls phase window 2 until its character list has arrived (pressing
earlier raises inside the client's own handler) and calls
`StartGameButton()`. It stops when `IsGamePhase()` or after `MAX_STEPS`.
Injected mouse clicks are documented as ignored by this client.

**Config keys.** `TAP_AFTER = 5.0`, `SETTLE = 3.0`, `MAX_STEPS = 40`,
`VERBOSE = True`. (`ENABLED = True` in the source has no effect.)

**UI page.** None.

**Limitations.** Assumes saved credentials; there is no credential handling
in the mod and the shipped config keeps it disabled. `GetPhaseWindow` is
documented as an unstable handle (it can be `None`, a window, or an int), so
every access checks the object.

**What it teaches.** Driving UI by calling handlers rather than injecting
input; the value of `uispy` for finding those handler names.

---

## shoppos

**Purpose.** Progressive mapper of offline-shop locations: scans the loaded
shop entities and uploads new or moved ones to an HTTP endpoint you run
yourself.

**How it works.** Every `SCAN_PERIOD` it calls the stub's `offline_shops()` /
`offline_shop_at(i)` (seller, x, y), builds a signature `map|channel|x|y`,
and diffs against `_patcher\shoppos_seen.json`. Fresh entries (new seller,
moved, or older than `REFRESH_H`) are batched into one JSON `api.http_post`
with `Authorization: Bearer <UPLOAD_TOKEN>`. A 2xx marks them seen. A quiet
scan never posts.

**Config keys.** `URL = ""` (empty = disabled), `UPLOAD_TOKEN = ""`,
`SCAN_PERIOD = 4.0`, `REFRESH_H = 6.0`, `MAX_BATCH = 200`, `MAX_SHOPS =
5000`, `STATE_NAME = "shoppos_seen.json"`.

**UI page.** None.

**Profile notes.** `UPLOAD_TOKEN` is redacted in `mods.log` by the host.

**Limitations.** `http_post` blocks the frame for the request. Uses `with
open(...)` for its state file, which the framework documents as unsupported
by this client's `open()` — expect the state load/save to fail silently on
such a build (both are wrapped in `try`). The receiving endpoint's contract
(idempotent, stamps map/channel/loc by seller) is described only in the
docstring.

**What it teaches.** Change-only upload with a local dedup file; keeping
credentials out of the source and the log.

---

## autotrade

**Purpose.** Accept incoming trades on a receiving character, once the
other side's offer has settled.

**How it works.** Reads the whole exchange state through the client's own
`exchange` module (`isTrading()`, `GetItemVnumFrom*`, `GetElkFrom*`,
`GetAcceptFrom*`, `GetNameFromTarget`) and only fires
`m2netm2g.SendExchangeAcceptPacket` when the OTHER SIDE's offer has been
unchanged for `STABLE_S`. Metin2 clears both accept flags whenever either
side edits the offer, so an accept on an empty window is simply re-sent
once they put something in — the retry budget resets on every signature
change. The safety property that matters is `GIVE_NOTHING`: while ANY item
or yang is on our side of the window, no accept is sent. That turns the
character into a strictly RECEIVING one, and the docstring frames it plainly
("auto-accept any trade with items on our side is a standing invitation to
be emptied").

**Config keys.** `GIVE_NOTHING = True`, `STABLE_S = 0.0`, `RECHECK_S = 0.0`
(every pump), `MAX_TRIES = 3`, `RETRY_S = 2.0`, `SLOTS = 12`,
`DRY_RUN = False`, `VERBOSE = True`.

**UI page.** `GIVE_NOTHING`, `STABLE_S` slider, `DRY_RUN`, `VERBOSE`, Revert.

**Limitations.** Assumes the client's grid size at `SLOTS = 12`; a build
that indexes differently exits the loop early on the first error.
`SendExchangeAcceptPacket` is looked up on `m2netm2g` — the mod logs and
does nothing when it is absent.

**What it teaches.** Reading the client's own trade module rather than
guessing at the wire packets; a boolean whose ONLY job is a real safety
property; letting the SIGNATURE of an offer drive the retry budget so
zero-latency accept is safe.

---

## autodonatexp

**Purpose.** Donate the character's EXP to the guild, on a timer, unattended.

**How it works.** The player-visible chain — `Alt+G → Donate → number box →
OK` — cannot be reproduced by keystrokes on a background client (this client
uses `RegisterRawInputDevices`, which only delivers to the foreground
window), so the mod calls the end of the chain directly:
`m2netm2g.SendGuildOfferPacket(exp)`. The clamp that
`uipickmoney.PickMoneyDialogExp` applies BEFORE that packet has to be
re-applied here, against `playerm2g2.GetEXP()`, or the server refuses the
whole offer (measured — 999999999 with 2,905,003 EXP moved nothing). A
verification pass reads guild EXP, own EXP and yang around each send and
logs all three deltas, so a donation that did nothing says so.

The docstring records HOW the OnOffer chain was identified against two other
"donate" features on the server (guild bank; donation certificates) — the
decisive evidence is that `OnOffer`'s argument is named `exp` where
`OnDeposit`'s is `money`, and the code bodies are stripped of everything
else. Measured cost: **100 player EXP → 1 guild EXP; yang is not touched**.

**Safety.** Nothing is sent until `ARMED = True`. `ONLY_CHARACTER` restricts
the mod to a named character. `REQUIRE_GUILD` drops sends made when
`guild.GetGuildID()` reports zero (also true during the login/select phase).

**Config keys.**

| Key                                 | Default                 | Meaning                                                              |
| ----------------------------------- | ----------------------- | -------------------------------------------------------------------- |
| `ARMED`                             | `False`                 | **Master switch.** Nothing is sent while this is false.              |
| `ONLY_CHARACTER`                    | `""`                    | Restrict to one character; `""` disables the check.                  |
| `AMOUNT`, `MAX_PER_DONATION`        | `999999999`, `0`        | Number typed into the box (clamped to `GetEXP()`); optional ceiling. |
| `EVERY_MIN`, `FIRST_AFTER_MIN`      | `60.0`, `2.0`           | Cadence; first send delayed for the guild data to arrive.            |
| `TEST_AMOUNT`, `VERIFY_AFTER_S`     | `0`, `6.0`              | One-shot experiment amount; verification delay.                      |
| `REQUIRE_GUILD`, `PROBE`, `VERBOSE` | `True`, `False`, `True` | Safety, one-shot introspection, log every skip.                      |

**UI page.** None.

**Profile notes.** `ARMED` and `ONLY_CHARACTER` are the per-character keys.

**Limitations.** Spends the character's own experience irreversibly; the
docstring warns that a still-levelling character is the wrong home for this
and that nothing here can tell "capped" from "still growing".
`SendGuildOfferPacket` and the balance readers are looked up at send time;
absent bindings are logged and the send skipped.

**What it teaches.** Bypassing a UI whose keystrokes cannot reach a
background window by calling the packet at the end of its chain; using
`co_varnames` as evidence when `co_names` is stripped; VERIFYING an
irreversible action by reading balances before and after.

---

## ribfarmer

**Purpose.** One switch for a Red Iron Blade farm: `autohunt2` and
`autoloot` pointed at the blade-dropping mobs, with the blade filters and a
radar for drops.

**How it works.** Not a fourth hunting engine — it `exec`s the SAME
`autohunt2` and `autoloot` code that ships in this folder into private
namespaces, then applies its overrides. That is the isolation modhost
provides, used twice, so hosting is one file rather than a divergent copy
of either engine. The either/or rule: it REFUSES to run when `autohunt2`,
`autoloot` or `orcfarmer` is enabled for this character,
names which one, and stands down without switching anyone off itself.

The blade radar (`BLADE_RADAR`, `ANNOUNCE_DROPS`) marks every listed drop
on the atlas; `FETCH_UNOWNED` walks to blades dropped for OTHER players and
`CAMP_S = 45` stands on them until the ownership lapses.
`HUNT_ANYTHING_WHEN_NONE` PRIORITISES `RIB_RACES` rather than restricting
to them — the docstring explains why (a hard filter starves itself of
spawns). An aggro tracker publishes `TRIARCH_FIGHT_BUSY` and `TRIARCH_AGGRO`
so the collector defers to the fight, and `LOOT_WHILE_IDLE = False` stands
the collector down when the hunt is not running.

The hosted engines are re-`exec`'d when their files change on disk
(`ENGINE_POLL_S`), so editing `autoloot/main.py` behaves identically
whether it is loaded directly or by this mod.

**Config keys** (main ones):

| Key                                                     | Default                                | Meaning                                              |
| ------------------------------------------------------- | -------------------------------------- | ---------------------------------------------------- |
| `RIB_RACES`                                             | `[702, 703]`                           | Dark Arahan, Esoteric Arahan Fighter.                |
| `RIB_VNUMS`, `RIB_NAMES`                                | `3210..3219`, `"red iron blade+0..+9"` | Every refinement level.                              |
| `IGNORE_PLAYERS`                                        | `[]`                                   | Own alts, so avoidance does not count them.          |
| `SEEK_RANGE`, `FETCH_RANGE`                             | `12000.0`, `12000.0`                   | Walk radius for wanted mobs / listed drops.          |
| `HUNT_ANYTHING_WHEN_NONE`, `BLANK_S`                    | `True`, `8.0`                          | Priority vs restriction; blank-time trigger.         |
| `BLADE_RADAR`, `RADAR_RANGE`                            | `True`, `0.0`                          | Announce and pin every drop, no distance limit.      |
| `TAKE_OTHERS`, `CAMP_S`                                 | `True`, `45.0`                         | Camp on unowned drops until they turn.               |
| `EXTRA_NAMES`, `EXTRA_VNUMS`, `PASSING_ONLY`            | small lists                            | Extras taken in passing; explicit "in passing only". |
| `FETCH_EVERYTHING`, `TAKE_MONEY`, `LEARN_DROPS`         | `True`, `False`, `True`                | Fetching scope; yang; drop-source stats.             |
| `FIGHT_FIRST`, `AGGRO_*`, `FIGHT_HOLD_S`, `FIGHT_MAX_S` | many, see file                         | Aggro tracker and its ceilings.                      |
| `LOOT_WHILE_IDLE`                                       | `False`                                | Collector stops with the hunt.                       |
| `LABEL`, `HUNT`, `LOOT`, `VERBOSE`                      | `"ribfarmer"`, `{}`, `{}`, `True`      | Chat label; free-form engine overrides.              |

**UI page.** `RIB_RACES`, `RIB_VNUMS`, `HUNT_ANYTHING_WHEN_NONE`, `BLANK_S`,
`BLADE_RADAR`, `TAKE_OTHERS`, `CAMP_S`, `TAKE_MONEY`, `LEARN_DROPS`,
`SEEK_RANGE`, `IGNORE_PLAYERS`, `VERBOSE`, Revert. Start/stop is the game's
own autohunt button.

**Profile notes.** `IGNORE_PLAYERS` is per-character (your own alts).

**Limitations.** Two hunting engines on one client is a fight, hence the
either/or rule. Hosted-engine changes only propagate on the disk-poll tick.

**What it teaches.** Reusing a mod as a library by `exec`ing it into a
private namespace; publishing DEADLINES to arbitrate FIGHT > LOOT > HUNT;
priority-not-restriction as a target rule.

---

## orcfarmer

**Purpose.** One switch for an Orc-drop farm: same hosting shape as
`ribfarmer`, hunting by NAME (`"orc"`) rather than by race number.

**How it works.** `ribfarmer` with a substring hunt: `HUNT_RACES = ["orc"]`
is resolved by `autohunt2` through `nonplayer.GetMonsterDataByNamePart`, so
one entry covers Black Orc, Black Orc Giant, Bold Black Orc, Elite Orc
Fighter, Elite Orc Sorcerer and any other Orc the server adds. The
docstring names the trap: this server has "Orchid Sword" items whose name
contains "orc", but items cannot appear in a monster lookup — read the
`_hunt_resolved` log line to confirm what the string matched to.

Every listed drop gets the full treatment (`PASSING_ONLY = []`): announced,
pinned to the atlas, walked to, and camped on. Same either/or rule against
`autohunt2`, `autoloot`, `ribfarmer`.

**Config keys.** `ORC_RACES = ["orc"]`,
`ORC_VNUMS = [30006, 30076]` (Orc Tooth, Orc Amulet+ — measured on the
ground), `ORC_NAMES = ["orc tooth", "orc amulet", "orc molar"]`,
`IGNORE_PLAYERS = []`, `SEEK_RANGE = 12000.0`, `TAKE_MONEY = False`,
`LEARN_DROPS = True`, `HUNT_ANYTHING_WHEN_NONE = True`, `BLANK_S = 8.0`,
`BLADE_RADAR = True`, `TAKE_OTHERS = True`, `CAMP_S = 45.0`,
`FETCH_EVERYTHING = True`, `PASSING_ONLY = []`, `EXTRA_NAMES = []`,
`EXTRA_VNUMS = []`, `LABEL = "orcfarmer"`, `HUNT = {}`, `LOOT = {}`,
plus the same aggro-tracker block as `ribfarmer`.

**UI page.** `ORC_RACES`, `ORC_NAMES`, `ORC_VNUMS`, `BLADE_RADAR`,
`TAKE_OTHERS`, `CAMP_S`, `HUNT_ANYTHING_WHEN_NONE`, `BLANK_S`,
`TAKE_MONEY`, `LEARN_DROPS`, `SEEK_RANGE`, `IGNORE_PLAYERS`, `VERBOSE`,
Revert.

**Profile notes.** `IGNORE_PLAYERS` per character.

**Limitations.** As with the other farm presets, two hunting engines on one
client is a fight. Name-based hunting is only as good as
`GetMonsterDataByNamePart`'s substring behaviour — verified on measured
mobs to return one 2-tuple rather than a list.

**What it teaches.** When to hunt by NAME rather than by race number, and
how to check what the resolve produced.

---

## automonkey

**Purpose.** Run the monkey dungeon loop for ever, unattended: walk from
Pyungmoo to Bakra to the dungeon, use the skill on arrival, start the
game's OWN autohunt, and repeat when the dungeon spits the character out.

**How it works.** The map IS the state. `ROUTE` maps a map name (as
`api.map_name()` returns it) to a world-coordinate destination; each tick
compares the current map, walks to the target, and on arrival runs
per-map actions (`USE_SKILL` for hunt maps; `START_HUNT` calls
`CreateAutoBotSettings()` + `SetAutoHuntStatus` and/or the `/auto_hunt
start` chat command — the docstring records which combination is
CONFIRMED WORKING and warns against layering both toggles). No sequence,
no "began at step 3" state to get stuck in.

Walking has three regimes, chosen per map: `"auto"` (issue once,
watchdog resumes), `"repath"` (re-issue the destination whenever progress
stalls — measured: 1m36s vs a 3m34s spread on the dungeon leg, because
`__AutoPathSegment` wedges on a segment it computed and re-issuing
replaces it with a fresh one), and `"steer"` (hold camera-relative
`SetMultiDirKeyState` and re-aim every tick — kept as a fallback).
`IGNORE_MOBS_MAPS` uses the stub's `drop_engagement()` to refuse every
fight while walking, so nothing turns the character to swing and the
route survives. The transformation applied on the boot back to Pyungmoo
is dropped in two steps (click affect icon → answer the confirmation
dialog with `acceptButton.CallEvent`), with the dialog picked from a
ranked candidate list (`CANCEL_PREFER` / `CANCEL_NEVER`) so a whisper
window that happens to be open is never mistaken for the confirm.

**Conflicts.** `autohunt2` and `ribfarmer` both intercept `/auto_hunt` and
block `SetAutoHuntStatus`, so a mod that starts the client's OWN autohunt
cannot coexist with them on the same character. `_check_hunt_conflict`
warns and continues; disable them (or set `START_HUNT = False`).

**Config keys** (main ones):

| Key                                                                       | Default                            | Meaning                                                   |
| ------------------------------------------------------------------------- | ---------------------------------- | --------------------------------------------------------- |
| `ONLY_CHARACTER`                                                          | `""`                               | Restrict to a farming character; `""` disables the check. |
| `ROUTE`                                                                   | 3 entries                          | Map name → world destination `[x, y]`.                    |
| `HUNT_MAPS`, `TRANSFORM_MAPS`, `IGNORE_MOBS_MAPS`, `REPATH_MAPS`          | 1–2 entries each                   | Per-map behaviour toggles.                                |
| `SKILL_KEY`, `SKILL_SLOT`, `USE_SKILL`                                    | `2`, `None`, `True`                | Hotbar press on entering a hunt map.                      |
| `START_HUNT`, `HUNT_VIA_SETTINGS`, `HUNT_VIA_MINIMAP`, `HUNT_VIA_CHAT`    | `True`, `True`, `False`, `True`    | The confirmed working combination.                        |
| `STOP_HUNT_ON_LEAVE`                                                      | `True`                             | `/auto_hunt end` when we leave a hunt map.                |
| `ARRIVE`, `SETTLE_S`, `STALL_S`, `RESUME_EVERY_S`, `MAX_RESUMES`          | `400.0`, `3.0`, `1.5`, `2.0`, `25` | Arrival radius; timing tolerances.                        |
| `IGNORE_RESUME_EVERY_S`, `IGNORE_MAX_RESUMES`                             | `1.0`, `200`                       | Ignore-map resume budget.                                 |
| `COMBAT_MEMORY_S`, `WALK_STATE`, `WALK_STATES`                            | `6.0`, `0x8A`, `[0x8A, 0x88]`      | Discriminator for "combat killed the walk".               |
| `CANCEL_TRANSFORM`, `TRANSFORM_AFFECT`, `CANCEL_ANSWER_S`, `CANCEL_TRIES` | `True`, `220`, `0.4`, `3`          | Transform cancel (measured NEW_AFFECT_POLYMORPH).         |
| `CANCEL_PREFER`, `CANCEL_NEVER`                                           | lists                              | Ranked dialog candidates; deny-list for chat/whisper etc. |
| `MOVE_METHOD`, `REPATH_*`, `STEER_*`                                      | `"auto"`, many                     | The three walking regimes.                                |
| `PROBE_TRANSFORM`, `DIAG`, `FORCE_*`, `VERBOSE`                           | mixed                              | One-shot probes; diagnostics; verbosity.                  |

**UI page.** None — this is a per-character preset, configured in a profile.

**Profile notes.** `ONLY_CHARACTER` is the safety catch. `ROUTE`, the map
lists and the hunt toggles are per-server data.

**Limitations.** Needs `api.natives.module().drop_engagement()` to refuse
fights while walking — degrades otherwise. Needs `gc` at import time to
locate the transformation dialog and its acceptButton; the docstring
records that `gc` is a builtin and imports even where `zipimport` is
broken. Route coordinates are world units (in-game units × 100, verified).

**What it teaches.** Building a loop out of a MAP-KEYED RULE rather than a
sequence; re-issuing over polling as the recovery pattern for a
segment-based auto-mover; ranked candidates for a "which window is the
question?" problem where the FIRST match was the earlier bug.

---

## modui

**Purpose.** The mod manager window: a rail of installed mods on the left,
the selected mod's settings page on the right, one Save button.

**How it works.** `_mod_names()` lists the mod folders; each rail row is a
`TextLine` plus a pick-always `ui.Window` hitbox (a bare window has no
visual, and children of one do not render). Opening a tab reads that mod's
`ui.json`, prepends the injected `Enable <mod>` checkbox, computes effective
values (shared config with the profile merged over, then unsaved edits) and
calls `api.ui.render`. Edits go to `_pending`; the title shows `Mods *`.
**Save** merges them into `api.config_path()` with one atomic
write; `reset` drops the open tab's edits. F10 toggles the window by wrapping
`OnKeyDown` on the game phase window. The open/closed state is runtime-only
because both clients share one config file.

**Config keys.** `OPEN_ON_START = True` in source (`False` in
`mods/config.json`), `TOGGLE_KEY = 68`, `WIDTH = 520`, `HEIGHT = 430`, plus
layout constants.

**UI page.** `OPEN_ON_START` checkbox and the toggle key as read-only.

**Limitations.** If the game window has no Python `OnKeyDown`, F10 cannot be
bound and the window only appears with `OPEN_ON_START`. List-valued settings
are display-only.

**What it teaches.** The constraints of the client's UI toolkit (artwork,
pickability, alignment, widget lifetime); the "accumulate, then one write"
pattern so hot reload does not fire per slider tick.

---

## posinfo

**Purpose.** Example / diagnostic: log your position and your target's every
`INTERVAL` seconds. The smallest useful mod; it proves the whole chain
(stub → launcher → modhost → api → bindings) with reads only.

**How it works.** `on_update` throttles with `api.now()`, reads
`api.player.position()` and `api.target.*`, and logs only when the line
changes. With `PROBE = True`, `on_load` dumps which client modules exist and
tries every plausible spelling of the position calls — the tool that
established, on this build, that `GetPixelPosition(vid)` is the working form
and `GetActorPixelPosition` returns garbage.

**Config keys.** `INTERVAL = 2.0`, `PROBE = False`.

**UI page.** `INTERVAL` slider.

**What it teaches.** Start here. It is the template for every rule in the
framework chapter: constants, deadlines, `in_game`, and measuring rather than
assuming.

---

## lootdiag

**Purpose.** Diagnostic: prove the ground-item walk returns real drops.

**How it works.** Every `EVERY` seconds it calls the stub's `ground_items()`
and prints up to 16 entries with id, vnum, position and both trailing strings
(owner and display name), plus your own name — so the reader can see which
column is ownership. Speaks only when the answer changes, or every
`QUIET_REPEAT` at most. Read-only: never picks anything up.

**Config keys.** `EVERY = 1.0`, `QUIET_REPEAT = 30.0`. `ENABLED = True` is
vestigial.

**Marked as:** example / probe. Disabled in `mods/config.json`.

**What it teaches.** How to prove a native walk is real ("does 0 mean empty
or broken?") and how to log only changes.

---

## mobdiag

**Purpose.** Diagnostic: prove the actor walk and discover race numbers by
name.

**How it works.** Every `EVERY` seconds, `api.actors()` and a race/name
histogram of the top ten; with `NAME_LIKE` set, every matching actor is
listed with VID, race and distance. Read-only.

**Config keys.** `EVERY = 4.0`, `NAME_LIKE = ""`. `ENABLED = True` is
vestigial.

**Marked as:** example / probe. Disabled in `mods/config.json`.

**What it teaches.** The practical way to fill `BOSS_RACES` / `METIN_RACES`
for `autohunt2`.

---

## apidiag

**Purpose.** One-shot: what does this `Api` instance actually have?

**How it works.** On the first pump it logs `api.VERSION`, whether
`api.natives` / `api.raw` exist, the type of `api.player`, whether
`triarch_native` is in `sys.modules`, and how many natives are registered.
Then it does nothing.

**Config keys.** None effective (`ENABLED = True` is vestigial).

**Marked as:** example / probe. Ships with an explicit `"enabled": false`
section in `mods/config.json`; enable it in a profile when you want the
report. Harmless. A `sys.modules['triarch_native'] = None` line within the
first second after boot is the registration race, not a broken gateway: the
mod host's first tick runs some 70–80 ms before the stub registers the module
(document 05, section 1.6). The probe waits for the module, or a couple of
seconds, before reporting, so a `None` that stands is real.

**What it teaches.** Twenty lines that answer "is the gateway up?" before you
debug anything else.

---

## nativeprobe

**Purpose.** Prove the `triarch_native` gateway works, safely, in escalating
stages.

**How it works.** Stage 0: is the module importable, and do the error paths
(unknown name, missing arguments) raise cleanly rather than crash. Stage 1:
`CreateAutoBotSettings` — zero arguments, so a stack bug cannot be the cause,
and audited as touching no network. Stage 2: `main_instance()` returns a
pointer. Stage 3: `OpenCharacterMenu(0)`, one argument, UI-only. Stage 4
(`RUN_ACQUIRE`): `FindAndSetNewTarget(main, 0, 0)` — three arguments
including a real pointer. Runs once; waits for the game phase.

**Config keys.** `RUN_CALL = True`, `RUN_ACQUIRE = True`. `ENABLED = True` is
vestigial.

**Marked as:** probe. Ships with an explicit `"enabled": false` section in
`mods/config.json` — and stage 4 has a real combat effect (it may acquire a
target and walk to it). Enable it only in the profile of a character you do
not care about, or set `RUN_ACQUIRE: false` first. A `FAIL - 'triarch_native'
is not importable` within the first second after boot is the registration race
described under `apidiag`, not a missing `uriel_natives.ini`; the probe waits
for the module (or a couple of seconds) before judging, so a `FAIL` that
persists is real — then check `uriel_stub.log` for the `NATIVE:` lines.

**What it teaches.** How to test an unsafe layer in order of blast radius.

---

## probe_ui

**Purpose.** Answer "can the game's own autohunt button be detoured into a
mod?" on the live client.

**How it works.** After `DELAY_S`, it reports which autohunt UI modules are
loaded, lists members of the `AutoHunting` classes whose names look relevant,
reaches the live game window and its `interface` object, and tests whether
each is patchable with a `setattr`/`delattr` round-trip on a dummy attribute
(undone immediately). It clicks nothing and sends nothing.

**Config keys.** `DELAY_S = 3.0`, `DUMMY`, `CHUNK = 5`.

**Marked as:** probe. Ships with an explicit `"enabled": false` section in
`mods/config.json`; harmless when enabled.

**What it teaches.** Compiled UI classes may refuse attribute assignment;
test before designing around a patch.

---

## uispy

**Purpose.** Record what the UI actually does when a human clicks, instead of
guessing handler names from `dir()`.

**How it works.** Wraps `ui.Button.CallEvent` / `OnMouseLeftButtonUp` /
`Click` and `ui.Window.OnMouseLeftButtonUp`; each wrapper logs the widget's
class and name, the bound callback unwrapped to `Class.method`, and its
arguments, then calls straight through. Unhooks on unload. The docstring lists
the guesses it replaced (a handler that was right but called too early, a
phase window that turned out to be an int).

**Config keys.** None effective (`ENABLED = True` is vestigial).

**Marked as:** probe. Ships with an explicit `"enabled": false` section in
`mods/config.json`; read-only in effect but chatty when enabled.

**What it teaches.** Observe, then call: the technique that produced
`autologin` and `chanswap`.

---

## Cross-mod protocols

Mods run in isolated namespaces and cannot import one another, so they talk
through the process environment (`os.environ`), which every mod shares.
Every **request** key is consumed exactly once by its reader; **state** keys
are republished every tick; a **hold** is a deadline so a dead writer cannot
freeze the reader.

| Key                                          | Writer → Reader                                | Value                                                                     |
| -------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------- |
| `TRIARCH_ATTACK`                             | `api.attack()` → stub                          | target VID; the stub acts on change                                       |
| `TRIARCH_ATTACK_OK`                          | stub → `api.attack_available()`                | `"1"` when the bridge is armed                                            |
| `TRIARCH_MODS`                               | bootstrap → stub                               | status string, written to `uriel_stub.log`                                |
| `TRIARCH_LOOT_BUSY`                          | `autoloot` → `autohunt2`                       | epoch deadline; hunting stands down until it passes                       |
| `TRIARCH_CHAN_REQUEST`                       | `autohunt2`, `follow`, `routes` → `chanswap`   | channel number, consumed once                                             |
| `TRIARCH_ROUTE_ACTIVE`                       | `routes` → `autohunt2`                         | `"1"` while a route runs                                                  |
| `TRIARCH_HUNT_REQUEST`                       | `routes` → `autohunt2`                         | `"start"` / `"stop"`, consumed once                                       |
| `TRIARCH_HUNT_STATE`                         | `autohunt2` → `routes`                         | `"off"` / `"busy"` / `"idle"` / `"clear"`, every tick                     |
| `TRIARCH_TASK_REQUEST`, `TRIARCH_TASK_STATE` | `routes` ↔ a worker mod                        | `"<name> start                                                            | stop"`/`"<name>:off | busy | done"` (no shipped worker yet) |
| `TRIARCH_FIGHT_BUSY`                         | `ribfarmer` / `orcfarmer` → hosted `autoloot`  | epoch deadline; the collector will not START a fetch while a mob is on us |
| `TRIARCH_AGGRO`                              | `ribfarmer` / `orcfarmer` → hosted `autohunt2` | `"<deadline> <vid,vid,...>"`; who has aggroed us right now                |

The one exception is `chanswap`, which also registers
`sys.modules["triarch_chan"]` so a mod can call `change_to(ch)` directly.
