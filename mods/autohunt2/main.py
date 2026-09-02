"""autohunt2 - policy on top of the client's own autohunt.

WHAT THIS IS NOW
----------------
Almost nothing, and that is the point. An earlier version of this file
reimplemented target selection, approach, attack sustain and re-arming in
Python. Every one of those already exists in the client, done better - the
selection even runs a 100-sample line-of-sight ray march we cannot reach from
Python at all. Reimplementing them produced a long tail of bugs and no benefit.

The client's whole autohunt reduces to one guarded call on its tick:

    if (player[autoAtkVidOff] == 0)
        FindAndSetNewTarget(player, pMain, bStone, excludeVID);

FindAndSetNewTarget does selection, LOS filtering, targeting, __OnPressActor and
the walk-in; __Update_AutoAttack then sustains the swing every frame until the
target dies and zeroes the VID, which re-opens the guard. Measured live with
Frida: 228 ticks, 9 acquisitions, 8 completed kills, 0 faults.

plus, at the end of the same loop, the skills:

    if (player[huntUseSkillOff])
        UseAutoSkills(pMain, pTarget, time(NULL));

which handles buffs, attack skills, cooldowns, mana and range on its own, from
the skill list the player picked in the autohunt window.

Both of those now run FROM HERE, through api.player.* over the triarch_native
gateway. The TRIARCH_HUNT env-var channel is gone: it existed only because the
stub could not be called into, and it cost a hand-rolled string protocol, a
parser at each end, and a tick of latency between deciding and acting.

and adds the ONE behaviour the client genuinely lacks: noticing that it is
body-blocked - swinging at a target it cannot damage because another mob is in
the way - and naming that target in excludeVID so the client picks a different
victim itself.

ISOLATION
---------
The developers run live analytics on the shipped autohunt, so we must not
appear in it. We never send /auto_hunt and never set the minimap autohunt
STATUS flag - the first condition AutoHuntLoop tests, so the native loop stays
inert. (We DO call CreateAutoBotSettings: it was on this list until it was
actually disassembled, and it turns out to read CPythonConfig and touch no
network stream, minimap or player singleton at all.) /auto_hunt is swallowed
unconditionally
by the chat interceptor, which is also where we start and stop: the button press
becomes our start instead of the server's. Nothing else is ever blocked - see
_LOGGED for why that distinction matters (swallowing /restart_here once broke
the revive button for a whole session).

Writing the anchor and the search radius IS safe - they are separate fields
from the status flag, and the client reads them from wherever they happen to be.

WHY NOT WRITE THE AUTO-ATTACK FIELDS DIRECTLY
---------------------------------------------
__Update_AutoAttack sets a flag called __key__b0t__ when the cached actor
pointer and the stored VID disagree. It is written there and read nowhere else
in the client, i.e. consumed externally. Going through the client's own
functions keeps the pair consistent; hand-writing those fields would not.
"""

CAPABILITIES = ["read", "target", "attack", "ui"]

# ---- configuration --------------------------------------------------------
# Both of these are the autohunt window's own controls, and both default to
# None = "the player owns it, do not touch".
#
# HUNT_STONES  the "stones only" choice. CreateAutoBotSettings writes it into
#              the settings block from the hunt_type config key, and
#              AutoHuntLoop reads that byte at both of its FindAndSetNewTarget
#              call sites. None -> the stub reads the same byte. True/False
#              forces it.
#
# HUNT_RANGE   the range slider. miniMapSetAutoHuntRange stores `slider + 40`
#              in the minimap, and FindAndSetNewTarget uses
#              (that + 40) * 35 as the search radius - so the slider's 100
#              default is 6300 units. The stub used to write this field every
#              tick, which is precisely why moving the slider did nothing.
#              None -> leave the slider alone (the player owns it). A NUMBER is
#              the slider value, applied through miniMap.SetAutoHuntRange, and
#              the resulting radius is (value + 80) * 35 - so 100 gives 6300.
HUNT_STONES = None

# Maximum range by default. The radius is (HUNT_RANGE + 80) * 35, so 300 gives
# 13300 units - roughly twice the shipped slider default of 100 (6300).
#
# This is a POLICY choice, not a discovered ceiling: the client applies no clamp
# on its side. miniMapSetAutoHuntRange is `[minimap+0x168] = f + 40` with no
# bounds check, and FindAndSetNewTarget just multiplies. Set None to hand the
# control back to the in-game slider.
HUNT_RANGE = 300.0
TICK_S = 0.25           # policy tick; the stub drives the client at its own 4 Hz

# Skills. The client's UseAutoSkills does buffs, attack skills, cooldowns and
# mana checks; the only reason it never fired is that nothing was calling it.
USE_SKILLS = True

# Mounted skill use. UseAutoSkills ALREADY implements dismount -> cast -> remount
# by itself: when it wants to cast an attack skill while mounted it emits
# /user_horse_ride to get off (and sets an internal "dismounted to cast" flag at
# player+0x501E0), then on a later call, once the cast is done, emits
# /user_horse_ride again to get back on. BOTH toggles are keyed on the real mount
# state (IsMountingHorse, [main+0x14]), so the command mounts or unmounts
# according to the actual state - exactly the wanted behaviour. So we do NOT
# re-send /user_horse_ride from Python: a second toggle would race the native's
# own and thrash the mount. Integrating the cycle is just KEEPING UseAutoSkills
# called while mounted and letting it drive. True opts into that /user_horse_ride
# traffic (ordinary player traffic, but traffic); False skips casting while
# mounted and never dismounts.
#
# Exposed as a per-character checkbox (ui.json) - "Dismount to cast while
# mounted". A character that cannot attack mounted turns it off in its own
# profile (mods/profiles/<Character>.json: "SKILLS_WHILE_MOUNTED": false),
# leaving the others' dismount/remount cycle untouched.
SKILLS_WHILE_MOUNTED = True

# Remount only for stones. With this on, the dismount -> cast -> remount cycle
# stays mounted ONLY while the objective is a metin stone; against a boss (or
# trash) it stays on foot after dismounting to cast. Implemented by driving the
# hunt_use_mount remount gate per target - 1 when fighting a stone, 0 otherwise.
# Requires SKILLS_WHILE_MOUNTED. Off = the plain behaviour (remount everywhere).
MOUNT_ONLY_ON_STONES = False

BLOCK_AFTER_S = 1.0     # stalled out of reach this long = blocked
BLOCK_MOVE = 30.0       # "not moving" threshold, world units
BLOCK_REACH = 250.0     # beyond this we cannot be hitting the target - measured
APPROACH_DIST = 800.0   # beyond this, a stationary character is not BLOCKED,
                        # it simply is not walking. Measured: a metin acquired
                        # at 4600 units sat at exactly 4600 for 18 ticks while
                        # the detector cried "blocked" 18 times. Excluding the
                        # only target in range achieves nothing; re-issuing the
                        # walk is what it actually needs.
EXCLUDE_S = 6.0         # how long a blocked target stays excluded

# ---- OBJECTIVE PRIORITY -----------------------------------------------------
# Three tiers, strict order. 1 outranks 2 outranks 3.
#
#   1 BOSS_RACES    SEEK and PREEMPT - abandon whatever is being fought
#   2 METIN_RACES   SEEK and PREEMPT - same, but a boss outranks a metin
#   3 TRASH_RACES   PREFERENCE ONLY  - never interrupts, only biases the pick
#
# The difference between tiers 1-2 and tier 3 is the whole design. The first two
# SEEK: they scan the world themselves, because the client can only ever offer
# its own nearest-with-line-of-sight choice and is therefore blind to a rare
# spawn two hundred units away. Tier 3 does not seek and does not interrupt -
# it just decides WHICH target to take when the character is free to choose.
#
# An empty tier list means that tier is OFF, metins included. Listing races
# narrows; it never widens.
#
# Races are matched by RACE NUMBER, which is the mob proto id and is
# language-independent. Names are accepted for convenience and resolved once
# through the client's own lookup - GetNameByVID returns whatever the locale
# pack says, so a name is a convenience, never the identity.
BOSS_RACES = []          # tier 1, e.g. [1093] or ["Siervo Salvaje Fuerte"]
METIN_RACES = []         # tier 2, e.g. [8005] or ["Metin Negro"]
TRASH_RACES = []         # tier 3, preference only. Empty = no preference.

# Seek ANY metin stone the client marks as one - the SAME test entscan uses to
# drop a mark on the atlas (the authoritative IsStone flag, or the STONE instance
# type as a fallback), not just the names in METIN_RACES. On this server a
# stone's live race differs from the race its name resolves to, and stone names
# vary, so a name/race list always misses some; this catches exactly the set
# entscan would. It activates the METIN (tier-2) seek on its own, so METIN_RACES
# may be left empty. Off by default.
METIN_ANY_STONE = False

# ---- clearing the pack that gathers behind you on a stone ------------------
# Breaking a metin is stationary, so pulled mobs collect at your BACK, where the
# frontal swing never reaches, and pile up. This periodically turns to the mob
# most directly BEHIND the stone and hits it (the client faces + swings, and an
# AoE swing catches its neighbours), then goes back to the stone - the same
# 180-and-clear you would do by hand. Only ever fires while actually breaking a
# stone; inert otherwise. On by default.
METIN_SWEEP = True          # clear the pack behind while breaking a stone
METIN_SWEEP_MOUNTED = False # ...but NOT while hunting the stone from horseback: a
                            #   mounted breaker keeps swinging at the stone, and
                            #   peeling off to a rear add mid-cycle thrashes the
                            #   dismount/remount logic. The gate reads mounted OR
                            #   the hunt_use_mount remount gate, so the brief
                            #   on-foot window during a mounted cast cannot start
                            #   a sweep either. A sweep already in flight finishes.
METIN_SWEEP_EVERY_S = 15.0  # how often to peel off and clear the back
METIN_SWEEP_DUR_S = 2.0     # how long to hit the pile before returning to the stone
METIN_SWEEP_RANGE = 250.0   # MELEE distance only. Archers and anything else
                            # standing off are ignored - we only turn for a mob
                            # that is actually on us, and a target this close
                            # needs no walk-in, so the character stays on the
                            # stone. 250 is the client's own melee reach
                            # (BLOCK_REACH); raise it if real adds sit further.

# Only SEEK the tiers that are switched on. Either switch stops trash being
# sought; both may be on at once, which seeks bosses and metins and nothing
# else. Neither on = the ordinary behaviour: bosses, metins and trash.
#
# "Stops SEEKING" is not "refuses to fight". A fight already in progress is left
# alone to finish - the character defends itself, it just does not go looking
# for more. There is deliberately no attempt to acquire whatever hit us: the
# client exposes no "who is attacking me" field, and inventing one from HP
# deltas would be a guess dressed as a feature.
ONLY_BOSSES = False
ONLY_METINS = False

# How close a tier-3 mob has to be to be worth preferring. Deliberately much
# tighter than the hunt radius: preferring a flagged mob 13000 units away would
# march the character across the map, which is a tier-1/2 behaviour, not this.
TRASH_RANGE = 3000.0

FILTER_MEMORY = 12      # remember this many rejected VIDs, so we do not thrash

# ---- how a SEEK tier works (tiers 1 and 2) ---------------------------------
# The client can only ever offer its own nearest-with-line-of-sight choice, so a
# rare spawn two hundred units away is invisible to it until it happens to be
# the nearest thing. A seek tier scans the character manager itself, and when
# something on the list appears it abandons whatever is being fought and goes to
# it. Normal farming resumes afterwards, biased by TRASH_RACES if that is set.
#
# The motivating case: a special monster spawns, everyone on the channel races
# for it, and it dies in seconds. Finishing the current wolf first means losing
# it. So preemption is immediate, not queued.
BOSS_RANGE = 8000.0     # scan this far for a BOSS, world units
# Metins get NO distance cap by default (0 = any distance): a stone is
# stationary and reachable, so if the client has it loaded and it is on the
# METIN tier, go to it however far it is - the pursuit walks the whole way. Set
# a positive number to cap metins like a boss. Only the client's own view/stream
# distance then bounds what is even visible to the scan.
METIN_RANGE = 0.0
BOSS_REACH = 700.0      # ...but only ATTACK from this close. OnPressActor on a
                        # target 7000 units away does not engage: the client
                        # re-acquires something local a tick later, and the next
                        # scan preempts again, dropping a real kill every few
                        # seconds without ever reaching the boss. So beyond this
                        # distance we WALK, and only press once in reach.
BOSS_GIVEUP_S = 0.0     # 0 = never give up: chase until the boss dies or
                        # despawns, however long that takes.
                        #
                        # This used to be 45s (and 25s before that), because the
                        # pursuit could genuinely fail to arrive - mobs body-
                        # blocked the route and the character never travelled, so
                        # an unbounded chase would have meant standing still
                        # forever. `nocollide` removes that failure mode: nothing
                        # can physically stop the walk any more, so a timeout now
                        # only abandons bosses that were still on their way.
                        #
                        # The pursuit already ends the instant the boss is no
                        # longer in the scan - dead, despawned, or out of the
                        # world - which is the condition that actually matters.
                        # See the "gone (dead or despawned)" branch.
                        #
                        # Set a positive value to restore the bound; the whole
                        # timeout path is skipped when this is 0.

# Re-issuing a waypoint CANCELS the one in flight, so calling move_to on every
# scan restarts the pathing forever and the character never actually travels.
# A waypoint is an edge, not a poll: send it once, then only again if the boss
# has genuinely moved away from where we were already heading.
BOSS_REPATH_DIST = 400.0  # boss must drift this far from the sent waypoint
BOSS_REPATH_S = 6.0       # ...or this long passes with no progress at all
BOSS_PROGRESS = 150.0     # movement below this over BOSS_REPATH_S = stalled
# ...or the route was CANCELLED out from under us. Being attacked mid-pursuit
# makes the client engage the attacker, and that engagement kills the walk in
# flight. Dropping the fight on the next scan is not enough on its own: the
# waypoint is already gone, and the code above would sit on a route that no
# longer exists until the 6s stall detector noticed. That is the "stops every
# time a monster attacks" bug - the walk was never re-sent, only eventually
# re-sent. A drop during pursuit now forces an immediate re-issue.
BOSS_REISSUE_MIN_S = 1.0  # ...but never more often than this, or a monster that
                          # keeps re-engaging restarts the path every scan and
                          # the character crawls instead of travelling

# The engine's auto-move state, so a dead route is DETECTED rather than waited
# out. CPythonPlayer + 0x58 reads 0x8A while a waypoint route is being followed
# and something else the instant it stops - measured live, and the constant is
# the one AutoMoveToPosition itself tests (`cmp [player+0x58], 0x8A`).
#
# This exists because a boss walk was seen dying at 13:18:40 and only recovering
# at 13:18:43, six seconds later, via BOSS_REPATH_S. The route had been killed
# without an engagement, so the drop path never invalidated it and the stall
# timer was the only thing left. Asking the engine costs nothing and is instant.
#
# The patcher stamps this per build (kPlayerWalkState); the value here is a
# fallback for a hand-installed mods folder.
WALK_STATE = 0x8A
BOSS_SCAN_S = 0.5       # how often to look; each scan is one native walk plus
                        # a race lookup per visible actor, all client-local
BOSS_RESCAN_S = 3.0     # after committing, leave it alone this long - the
                        # engagement needs time to establish before the next
                        # scan is allowed to change its mind

# ---- keeping distance from other players -----------------------------------
# CONTROL POSITION, NOT TARGET CHOICE.
#
# Three earlier versions picked a MOB to switch to and all three misbehaved,
# because a mob is a terrible thing to plan around: it dies within seconds, so
# the choice is stale almost as soon as it is made and the character zigzags
# between whatever happened to score best on each scan. The log has the full
# post-mortem - a switch to a mob players would come within 248 units of, and a
# 20-second trek to something 5296 units away that was never reached.
#
# The quantity worth controlling is WHERE WE STAND. Stand somewhere far from
# other players and the client's own FindAndSetNewTarget picks local mobs which
# are, by construction, also far from other players. Target selection then needs
# no help from us at all, and keeps its 100-sample line-of-sight ray march that
# is unreachable from Python.
#
# So this is a three-state controller: HUNT -> FINISH -> RELOCATE -> HUNT.
# FINISH is the important one - the current mob is ALWAYS killed first. It has
# already cost a walk-in and probably holds our tap priority, and throwing that
# away was the single most wasteful thing the previous versions did.
AVOID_CONTESTED = True

# ---- what counts as being crowded ----
# TWO triggers, EITHER fires. Neither subsumes the other:
#   PLAYER_NEAR_ME   somebody walked into our spot. Our mob may still be
#                    uncontested, but we are about to be crowded.
#   CONTEST_RADIUS   somebody is on the MOB. They may be nowhere near us - a
#                    ranged class across the clearing - and we still lose the
#                    tap race.
PLAYER_NEAR_ME = 1500.0   # a player this close to US...
CONTEST_RADIUS = 1200.0   # ...or this close to the MOB we are fighting
THREAT_DWELL_S = 1.5      # ...and it has to STAY true this long. Somebody
                          # running past is not a reason to abandon a spot.

# METIN STONES ARE EXEMPT FROM ALL OF IT.
#
# A stone does not move, does not chase and cannot be tap-raced the way a mob
# can - you either break it or you do not. Dropping one part-broken to walk away
# throws away every hit already put into it, and the whole avoidance machinery
# has nothing useful to say about a stationary target. So while the hunt is in
# stone mode: no vetting, no disengaging, no relocating.
AVOID_WHEN_STONES = False

# LEAVE WITH NOTHING ON YOUR TAIL.
#
# Mobs spawn and pull in groups of roughly three to ten, and they FOLLOW. Walking
# off with half a pack still alive drags it to the new spot - and the players we
# were avoiding follow the pack, so the relocation achieves precisely nothing.
#
# So "finish the current kill" is not enough. The finish state waits until
# nothing is left on us, and it keeps HUNTING while it waits rather than
# suppressing acquisition - clearing the pack is the job.
ENGAGED_RADIUS = 1200.0   # a live mob this close counts as still on us
FINISH_TIMEOUT_S = 45.0   # ...but adds can arrive forever, so stop waiting
                          # eventually and leave anyway

# ---- choosing where to go ----
# Candidates are GENERATED, not searched for: a ring sample around us, plus the
# anchor. Pure arithmetic - the only client call in the whole decision is the
# single api.actors() that supplies mob positions.
                          # Twelve directions, every 30 degrees - fixed, because
                          # _directions() builds them by rotation rather than
                          # with math.cos (see the note there).
SPOT_RINGS = [2000.0, 3500.0]   # sampled at these distances from us
SPOT_MOB_RADIUS = 1500.0  # count mobs within this of a candidate
SPOT_MIN_MOBS = 3         # ...and require at least this many. Without it, the
                          # emptiest spot on the map always wins and we relocate
                          # somewhere with nothing to kill.
SAFE_DIST = 2500.0        # a candidate must keep every player at least this far
                          # away, predicted. THIS IS THE FLOOR THAT WAS MISSING:
                          # the old code always took the best candidate even when
                          # the best was a mob players would walk within 248
                          # units of.
SAFE_DIST_MIN = 1500.0    # ...relaxed to this only if nothing else qualifies
MAX_DRIFT = 6000.0        # never relocate further than this from where the hunt
                          # STARTED, so an hour of dodging cannot migrate the
                          # character across the map
TRAVEL_WEIGHT = 0.35      # penalty per unit of travel, so "safe" does not beat
                          # "safe and close"
AWAY_WEIGHT = 1200.0      # how strongly a relocation prefers the direction
                          # pointing directly AWAY from the group. Their chain
                          # unwinds outward from where they stand, so leaving
                          # along that axis is what stops it catching us up.

# ---- exploiting how the other bots choose targets ---------------------------
# Almost all the automation on this server takes the NEAREST mob after each
# kill. That makes their route predictable: from where a player stands, they
# will walk the greedy nearest-first chain through the mobs around them.
#
# So we do not just avoid where they ARE. We simulate the next few links of
# that chain and treat every mob in it as already spoken for. Avoiding their
# current position is reactive and always a step behind; avoiding where their
# own algorithm is about to send them is not.
#
# The cheap half of the same idea, and a useful backstop when they are NOT
# running a bot: a mob closer to them than to us is one they reach first
# whatever they are doing.
#
# Both tests are applied to whatever the client hands us, and a failing mob is
# dropped and excluded so the client picks again - the same vet-and-drop path
# HUNT_RACES uses, which means the client keeps its line-of-sight filtering and
# we never choose a target ourselves.
AWARE_DIST = 3000.0       # only bother while a player is at least this close
CLAIM_STEPS = 3           # links of their nearest-first chain to simulate
CLAIM_BIAS = 1.0          # a mob is theirs when their distance to it is below
                          # ours times this. >1 concedes more, <1 contests more.
CLAIM_GIVEUP = 5          # rejections in a row before we stop arguing and move

# WHERE PLAYERS WILL BE, not where they are.
#
# Scoring on current positions is what produced the reported failure: two
# players walk toward a distant mob, that mob scores brilliantly because right
# now nobody is near it, we commit to it, and they arrive at the same time we
# do. The question that matters is not "who is next to this mob" but "how close
# will anyone GET to it while I am going there and killing it".
#
# So each player's track is projected forward and the score is their CLOSEST
# APPROACH over that window. A mob behind a player's line of travel scores well
# because they are walking away from it; a mob ahead of them scores badly even
# when it is currently miles away. That is the "opposite direction" behaviour,
# and it falls out of the geometry rather than needing a special case.
PREDICT_S = 5.0           # how far ahead to project. Roughly how long it takes
                          # to walk to a local mob and start hitting it.
PREDICT_STEPS = 4         # samples across that window; closest approach can be
                          # mid-window, so endpoints alone would miss it
PLAYER_MAX_SPEED = 900.0  # units/sec. A jump larger than this is a warp or a
                          # respawn, not a walk - extrapolating it would fling
                          # the predicted position across the map and poison
                          # every score for the next few seconds
PLAYER_VEL_SMOOTH = 0.5   # blend with the previous velocity. One sample of a
                          # strafing player points somewhere they are not
                          # actually going.
# ---- bounds on the journey ----
SPOT_ARRIVE = 400.0       # close enough to call it arrived
RELOCATE_TIMEOUT_S = 15.0 # hard cap; re-anchor wherever we got to
SPOT_REPATH_S = 5.0       # no progress for this long = stalled
SPOT_PROGRESS = 150.0     # ...where "progress" means at least this much movement
SPOT_STALLS = 2           # stalls before giving up and re-anchoring here
RELOCATE_COOLDOWN_S = 25.0  # after arriving, hunt for at least this long before
                            # another relocation may start. Without it a busy
                            # channel turns into continuous walking.
RELOCATE_FIGHT_ALONG = False  # True lets it kill things en route. Off by
                              # default: combat cancels the walk, so it re-issues
                              # constantly and may never arrive.
CONTEST_SCAN_S = 0.5      # how often the player list is rebuilt
CONTEST_DEBUG = True      # log every threat, decision and relocation

# Return to the hunt anchor when there is nothing left to kill.
#
# This is not our invention - it is the tail of AutoHuntLoop, the part we never
# replicated because we only drive FindAndSetNewTarget and UseAutoSkills:
#
#     d = GetDistanceNew(playerPos, anchor)
#     if (d <= 300.0f) return;
#     if (AutoPathToDestPosition(anchor.x, anchor.y)) return;
#     NEW_Goto(anchor, 60.0f);                       // pathing failed
#
# Without it the character kills its way outward and then just stands wherever
# the last mob died, waiting for a respawn that happens back at the anchor.
#
# playerm2g2.AutoMoveToPosition (what api.move_to calls) IS
# CPythonPlayer::AutoPathToDestPosition(float,float) - verified, the binding is
# a straight singleton -> call -> Py_BuildNone. So this needs no native work.
# The one thing we cannot reach is the NEW_Goto fallback when A* fails, so a
# failed walk is detected positionally instead: we re-issue while still away.
LEASH_DIST = 300.0      # the client's own threshold
RETURN_EVERY_S = 3.0    # re-issue the walk while still out there
APPROACH_EVERY_S = 3.0  # re-issue an approach at most this often

# How we decide the area is actually clear.
#
# NOT "no target for N seconds" - that is also true for the moment between one
# mob dying and the next being picked, so it walks home while there is still
# plenty to kill. The stub instead counts consecutive FindAndSetNewTarget calls
# that came back empty, which only happens when the client genuinely cannot see
# a victim. At TICK_S=0.25 that is three seconds of searching and finding
# nothing.
#
# Note what "in range" means here: FindVictim searches around the ANCHOR, not
# around the character. So this fires when the anchor circle is empty, which is
# the right moment to go back and let it repopulate.
RETURN_AFTER_FAILS = 12

# ---- channel looping -------------------------------------------------------
# When the hunt runs dry, hop to the next channel instead of standing on an
# empty spawn. Drives the chanswap mod (in-world change, no re-login) through
# the TRIARCH_CHAN_REQUEST env channel - mods run in isolated namespaces and
# cannot call each other directly, so a request travels the same way the loot
# handshake does. Needs the chanswap mod loaded; without it this is a no-op.
#
# Channels wrap 1..5 -> 1, NEVER 6: this server has five, and hopping to a
# channel that does not exist strands the character on a dead select screen.
LOOP_CHANNELS = False   # off by default - opt in
LOOP_IDLE_S = 10.0      # seconds with no objective (nothing engaged, no boss or
                        # metin in play, not mid-relocation) before a hop
LOOP_SETTLE_S = 15.0    # after a hop, hold this long before the idle clock runs
                        # again, so the new channel can load and repopulate
                        # rather than triggering an immediate second hop

# Show the hunt area on the minimap. The client already draws exactly this
# circle - see _show_range - so nothing is plotted by hand.
SHOW_RANGE = True

# Sidestep on a repeated block. OFF: the stall it was written for turned out not
# to be terrain. The real case is a ranged mob with its melee pack in the way,
# where excluding already does the right thing - the character shifts slightly
# and takes the nearest one instead. Kept because an 11-second two-mob ping-pong
# was measured once, and a single excludeVID provably cannot name both.
STALL_SIDESTEP = False
STALL_BLOCKS = 2        # blocks from one spot before we treat it as a stall
STALL_STEP = 900.0      # how far to sidestep, world units
VERBOSE = True
WATCH_CHAT = True

# Start without the button. Off by default - the button press is the normal
# trigger and is what keeps us out of the shipped analytics. Exists because
# injected mouse clicks are ignored by this client, so an unattended test run
# has no other way in.
AUTOSTART = False
AUTOSTART_AFTER = 6.0

# Auto-revive. The character dies while hunting and everything stops until
# somebody clicks; this sends the same command the revive button does.
#
# /restart_here revives ON THE SPOT, /restart_town in the safe zone. Both are
# ordinary player commands - the shipped AutoHuntLoop sends /restart_here
# itself - so neither is autohunt-identifying and neither is blocked by our
# chat guard. That guard swallows ONLY /auto_hunt; an earlier "fail closed"
# version swallowed /restart_here too and silently ate 68 revive attempts in
# one session, which is why _LOGGED exists.
REVIVE = True
REVIVE_CMD = "/restart_here"
REVIVE_AFTER_S = 10.0   # dead this long before we act - death has an animation
REVIVE_EVERY_S = 6.0    # re-send if still dead (the server can refuse briefly)
RESUME_AFTER_S = 5.0    # after reviving, HOLD the hunt this long. Reviving puts
                        # you back on low HP in the middle of what killed you,
                        # so the client's own potion system gets a window before
                        # we go and pick another fight. 5s is enough for the
                        # pots to fire; longer just wastes hunting time.

# Class-level patch of AutoHunting.__StartBtn. OFF by default: whether it takes
# effect depends on whether the autohunt window was constructed before or after
# we load, so leaving it on means two possible code paths and a bug that only
# reproduces on one of them. The chat guard covers both cases. See _install_ui.
UI_DETOUR = False

# The command the shipped autohunt button emits. It is BOTH the one thing that
# must never reach the server (it is what identifies an autohunt session to the
# developers' analytics) AND our trigger - see _install_chat_guard.
_TRIGGER = "/auto_hunt"

# Logged but ALWAYS passed through. These are ordinary player commands that the
# native autohunt happens to use as well - /restart_here is the revive button
# and /user_horse_ride is mounting. Swallowing them breaks the game for the
# player, which is exactly what a previous "fail closed" version of this guard
# did: revive stopped working while the hunt was on.
_LOGGED = ("/restart_here", "/user_horse_ride")

# ---- state ----------------------------------------------------------------
_api = None
_on = False
_anchor = None
_next_tick = 0.0

_exclude = 0            # published to the stub; the whole of our contribution
_exclude_until = 0.0

_eng_vid = 0            # target we have been watching
_eng_since = 0.0
_eng_pos = None

_block_n = 0            # consecutive blocks without reaching anything
_block_pos = None       # where those blocks are happening

_return_next = 0.0      # next allowed walk-home re-issue
_approach_next = 0.0    # next allowed approach re-issue
_obj_at = 0.0           # last time an objective existed, for LOOP_CHANNELS
_hop_after = 0.0        # no channel hop before this (post-hop settle window)
_chan_warned = False    # "cannot read the current channel" logged only once
_loop_idle = False      # in the no-objective loop phase - it owns positioning,
                        # so _home stands down (see _tick)
_returning = False      # walking back to the start point before a hop
_loopback_next = 0.0    # next allowed re-issue of the walk back to start
_sweep_at = 0.0         # next time to clear the pack behind a stone
_sweep_until = 0.0      # hitting the pack until this, then back to the stone
_sweeping = False       # a sweep owns the target; the metin pursuit stands off
_sweep_metin = 0        # the stone to re-press after the sweep
_rejected = []          # VIDs dropped by the body-block detector
_accepted = []          # VIDs engaged, so acceptance is logged once each
_dead_since = 0.0       # when we first saw the character dead
_revive_next = 0.0      # next allowed revive re-send
_resume_at = 0.0        # hold the hunt until this time after a revive

_players = []           # [{vid, pos, vel}] for every OTHER player, rebuilt on
                        # CONTEST_SCAN_S
_players_at = 0.0
_player_prev = {}       # vid -> (pos, ts, vel), so velocity survives the rebuild
_pc_route = None        # which player test worked; reported once
_pc_types = None        # INSTANCE_TYPE_* values meaning "a player"
_state = "hunt"         # hunt | finish | relocate
_threat_since = 0.0     # when a player first got too close, for THREAT_DWELL_S
_finish_vid = 0         # the mob that was on us when the threat was noticed
_finish_since = 0.0     # when the finish state began, for FINISH_TIMEOUT_S
_pack_n = 0             # live mobs on us, refreshed on the CONTEST_SCAN_S clock
_pack_at = 0.0
_spot = None            # (x, y) we are walking to
_spot_at = 0.0          # when the relocation started, for RELOCATE_TIMEOUT_S
_spot_pos = None        # where we were when the walk was last issued
_spot_mark = 0.0        # ...and when, for the stall test
_spot_stalls = 0
_reloc_until = 0.0      # no new relocation before this (RELOCATE_COOLDOWN_S)
_start_anchor = None    # where the hunt began, for the MAX_DRIFT leash
_claim = set()          # VIDs the other players' bots are expected to take next
_claim_at = 0.0
_vetted = 0             # the VID already judged, so each is judged once
_claim_rejects = 0      # rejections in a row, escalates to a relocation

_patched = []
_detour_done = False
_detour_next = 0.0
_detour_note = "not attempted"


def _mod(name):
    import sys
    m = sys.modules.get(name)
    if m is None:
        try:
            __import__(name)
            m = sys.modules.get(name)
        except Exception:
            m = None
    return m


def _d2(a, b):
    dx, dy = a[0] - b[0], a[1] - b[1]
    return dx * dx + dy * dy


def _loot_busy():
    """Is autoloot walking to a drop right now?

    It publishes a DEADLINE in TRIARCH_LOOT_BUSY rather than a flag, so this can
    never latch: if that mod is disabled, reloaded or dies mid-fetch the hold
    lapses within seconds instead of switching hunting off permanently, which is
    a failure nobody would think to look for.

    Standing down is not politeness - AutoMoveToPosition REPLACES the route in
    flight rather than queueing it, so two mods issuing waypoints means neither
    one ever arrives.

    os.environ is the channel api.attack() already uses for TRIARCH_ATTACK."""
    import os
    import time
    try:
        return float(os.environ.get("TRIARCH_LOOT_BUSY", "0")) > time.time()
    except Exception:
        return False


# ---- the drive -------------------------------------------------------------
# v14: the loop lives HERE, in Python, calling the client's own functions
# through api.player.*. The old TRIARCH_HUNT env-var channel is gone - it
# existed only because the stub had no way to be called, and it cost a bespoke
# string protocol, a parser on each side, and a whole tick of latency between
# deciding something and it taking effect.
#
# The rule the client itself uses, unchanged:
#     if (player[autoAtkVidOff] == 0)
#         FindAndSetNewTarget(pMain, bStone, excludeVID)
# The guard is REQUIRED, not an optimisation: driving it unconditionally at
# 4 Hz re-targets every tick, never commits to a kill and eventually faults.


def _native(api):
    return api.natives.module()


def _main_instance(api):
    tn = _native(api)
    if tn is None:
        return 0
    try:
        return tn.main_instance() or 0
    except Exception:
        return 0


def _field(api, name, default=0):
    tn = _native(api)
    if tn is None:
        return default
    try:
        return tn.field(name)
    except Exception:
        return default


def _prefer_trash(api, main):
    """TIER 3. Press a preferred trash mob if one is genuinely close.

    Returns True if a press was issued. Preference, not filter: when nothing on
    the list is nearby this does nothing at all and the client picks as usual,
    so an unlisted mob is never refused - it is simply not preferred.

    TRASH_RANGE is much tighter than the hunt radius on purpose. Preferring a
    flagged mob 13000 units away would march the character across the map, and
    marching to a distant target is a tier-1/2 behaviour, not this one."""
    if not TRASH_RACES:
        return False

    # YIELD WHILE A BODY-BLOCK IS BEING CLEARED.
    #
    # The block detector excludes the blocked VID and drops it so the client
    # re-picks - and the client picks the NEAREST, which is the mob standing in
    # the way. Killing that is what clears the path.
    #
    # Skipping only the excluded VID here is not enough, and that was the bug:
    # _exclude holds ONE vid, so with several preferred mobs bunched behind one
    # blocker we simply pressed the next preferred one, got blocked again, and
    # rotated through them for ever without ever targeting the blocker. A
    # preference must not out-vote getting unstuck, so tier 3 stands aside
    # completely until the exclusion expires.
    if _exclude:
        return False

    try:
        want = _resolve_races(api, TRASH_RACES, "_trash_resolved")
        if not want:
            return False
        near = api.actors(races=want, radius=TRASH_RANGE)
    except Exception as e:
        api.log("autohunt2: trash scan failed %s: %s" % (type(e).__name__, e))
        return False
    if not near:
        return False
    a = near[0]                          # actors() returns nearest first
    try:
        api.player.OnPressActor(main, a["vid"], 1)
    except Exception as e:
        api.log("autohunt2: trash press failed %s: %s" % (type(e).__name__, e))
        return False
    if a["vid"] not in _accepted:
        _accepted.append(a["vid"])
        del _accepted[:-FILTER_MEMORY]
        api.log("autohunt2: preferred %d race=%s %r at %.0f"
                % (a["vid"], a["race"], a["name"], a["dist"] or 0))
    return True


_races_cache = {}


def _resolve_races(api, wanted, key):
    """A race list as a set of race numbers, resolving any names ONCE.

    Shared by every tier, because the lists want identical treatment and
    diverging resolvers is how they would drift apart."""
    hit = _races_cache.get(key)
    if hit is not None:
        return hit
    out = set()
    for r in wanted:
        if isinstance(r, int):
            out.add(r)
            continue
        try:
            data = api.raw.nonplayer.GetMonsterDataByNamePart(str(r))
            for entry in (data or []):
                if isinstance(entry, (list, tuple)) and entry:
                    out.add(int(entry[0]))
                elif isinstance(entry, int):
                    out.add(entry)
        except Exception as e:
            api.log("autohunt2: cannot resolve race name %r (%s)"
                    % (r, type(e).__name__))
    if wanted and not out:
        # Silence here would look exactly like "no boss has spawned yet", so
        # say it: an unresolvable name disables the feature entirely.
        api.log("autohunt2: %s resolved to NOTHING from %r - that list is inert"
                % (key, wanted))
    _races_cache[key] = out
    api.log("autohunt2: %s -> races %s" % (key, sorted(out)))
    return out


_drops_n = 0
_drops_uniq = set()
_drops_said = 0.0


def _count_drop(api, vid, now):
    """Measure the reject/re-acquire rate.

    Rejecting a target is not free: FindAndSetNewTarget runs the client's full
    acquisition path, so a tight acquire->drop->acquire loop is churn, and any
    of it that reaches the wire is exactly the noise we are trying not to make.
    A high count against a low unique count means we are thrashing on the same
    few mobs rather than working through them."""
    global _drops_n, _drops_said
    _drops_n += 1
    _drops_uniq.add(vid)
    if now - _drops_said >= 5.0:
        if _drops_said:
            api.log("autohunt2: filter rejected %d target(s), %d distinct, in %.0fs"
                    % (_drops_n, len(_drops_uniq), now - _drops_said))
        _drops_said = now
        _drops_n = 0
        _drops_uniq.clear()


_boss_next = 0.0
_boss_vid = 0
_boss_hold = 0.0
_boss_since = 0.0
_boss_pursuing = False  # walking to a target: suppress the normal hunt every tick
_boss_tier = None       # which SEEK tier owns the current pursuit ("BOSS"/"METIN")
_boss_goto = None       # waypoint already sent, so we do not resend it
_boss_goto_at = 0.0
_boss_goto_pos = None   # where we were when it was sent, to detect a stall
_was_mounted = None     # last tick's mount state; None until first read. The
                        # False->True edge is the remount re-path trigger.


def _boss_ids(api):
    """BOSS_RACES as race numbers, names resolved once (same path as hunt)."""
    return _resolve_races(api, BOSS_RACES, "_boss_resolved")


def _tier_of(api, actor):
    """Which seek tier an ACTOR belongs to, or None. Highest priority wins.

    Matches by RACE NUMBER and, crucially, by live NAME. Metin stones spawn under
    a race that differs from the one their name resolves to in the client's proto
    table - measured live: 'Metin de Celos' resolves to 8107 but the stone in the
    world is race 8007 - so a race-only match silently never fires for them, and
    the METIN tier looked broken. The live actor already carries its real name,
    so matching that (substring, case-insensitive) is what makes the tier see the
    stone. Boss races match this way too, so the same mismatch cannot bite them.

    Takes the actor dict now, not a bare race, because the name is half the test."""
    if isinstance(actor, dict):
        race = actor.get("race")
        nm = (actor.get("name") or "").lower()
    else:                       # tolerate an old bare-race caller
        race, nm = actor, ""
    for name, races, key, names in _tiers(api):
        if race is not None and race in _resolve_races(api, races, key):
            return name
        if nm and any(s in nm for s in names):
            return name
        # METIN_ANY_STONE: an actor _boss_check tagged as a stone (IsStone / STONE
        # type, entscan's own test) is a metin regardless of its name or race.
        if name == "METIN" and isinstance(actor, dict) and actor.get("stone_metin"):
            return name
    return None


def _is_metin_stone(api, vid):
    """A metin stone, by the SAME test entscan marks with.

    The authoritative IsStone flag first, then the STONE instance type as a
    fallback (`_is_stone(rec)` in entscan is exactly `rec['stone'] or
    type_name == 'STONE'`). Using entscan's own criterion is what makes "anything
    entscan puts a mark on" and "anything the METIN tier seeks" the same set."""
    try:
        if api.raw.pack_chr.IsStone(vid):
            return True
    except Exception:
        pass
    try:
        t = api.raw.pack_chr.GetInstanceType(vid)
        if api.instance_types().get(t) == "STONE":
            return True
    except Exception:
        pass
    return False


def _boss_walk(api, now, boss):
    """Send a waypoint only when it would say something new.

    AutoMoveToPosition replaces the route in flight, so re-sending the same
    destination every scan is not a no-op - it restarts the pathing and the
    character crawls or stops outright. Send once, then only again if the boss
    has drifted from where we were already heading, or if we have visibly
    stopped making progress toward it."""
    global _boss_goto, _boss_goto_at, _boss_goto_pos
    bx, by = boss["pos"]
    here = api.player.position()

    if _boss_goto is not None:
        dx, dy = bx - _boss_goto[0], by - _boss_goto[1]
        drifted = (dx * dx + dy * dy) ** 0.5 >= BOSS_REPATH_DIST
        stalled = False
        # Ask the engine first. If it is no longer following a route, the walk
        # is dead NOW - waiting BOSS_REPATH_S to infer that from a lack of
        # movement throws away seconds on every interruption.
        st = _field(api, "player_state", None)
        if st is not None and int(st) != WALK_STATE:
            stalled = True
        elif now - _boss_goto_at >= BOSS_REPATH_S:
            if here is not None and _boss_goto_pos is not None:
                mx = here[0] - _boss_goto_pos[0]
                my = here[1] - _boss_goto_pos[1]
                stalled = (mx * mx + my * my) ** 0.5 < BOSS_PROGRESS
            else:
                stalled = True
        if not drifted and not stalled:
            return                       # the walk in flight is still correct
        if stalled and not drifted:
            # Rate-limited exactly like the drop path: an interruption that
            # repeats every tick must not restart the route every tick.
            if now - _boss_goto_at < BOSS_REISSUE_MIN_S:
                return
            api.log("autohunt2: boss route dropped (state %s) - re-issuing"
                    % (st if st is not None else "?"))
    elif now - _boss_goto_at < BOSS_REISSUE_MIN_S:
        # _boss_goto was cleared by a drop, meaning the route was cancelled and
        # we want a new one now. Still rate-limit it: a monster that re-engages
        # every scan would otherwise restart the path 2x a second, and a route
        # that is constantly restarted covers no ground at all - the exact
        # failure that made re-sending every scan wrong in the first place.
        return

    try:
        api.move_to(bx, by)
    except Exception as e:
        api.log("autohunt2: boss move_to failed %s: %s" % (type(e).__name__, e))
        return
    _boss_goto = (bx, by)
    _boss_goto_at = now
    _boss_goto_pos = (here[0], here[1]) if here is not None else None


def _tiers(api):
    """The seek tiers that are switched on, highest priority first.

    Priority is positional, so a boss always outranks a metin: the list is
    walked in order and the first tier with a target wins. A tier with an empty
    race list is off, metins included - listing races narrows, never widens.

    ONLY_BOSSES / ONLY_METINS restrict which tiers are SOUGHT. Either one being
    set also stops trash being sought (see _seek_trash)."""
    out = []
    only = ONLY_BOSSES or ONLY_METINS
    # Fourth element: the lowercased NAME entries of the list, for _tier_of's
    # live-name match (the int entries are resolved to races the usual way).
    if BOSS_RACES and (not only or ONLY_BOSSES):
        out.append(("BOSS", BOSS_RACES, "_boss_resolved",
                    [str(r).lower() for r in BOSS_RACES if not isinstance(r, int)]))
    if (METIN_RACES or METIN_ANY_STONE) and (not only or ONLY_METINS):
        out.append(("METIN", METIN_RACES, "_metin_resolved",
                    [str(r).lower() for r in METIN_RACES if not isinstance(r, int)]))
    return out


def _seek_trash():
    """Should ordinary trash be SOUGHT at all?

    False does not mean "refuse to fight" - a fight already in progress is left
    to finish. It means we stop going looking for one."""
    return not (ONLY_BOSSES or ONLY_METINS)


def _boss_check(api, now, main):
    """Preempt for a priority target. Returns True if we took one over.

    Deliberately runs BEFORE the acquisition guard, because the guard returns
    early whenever something is already engaged - which is exactly the case
    this feature exists to interrupt."""
    global _boss_next, _boss_vid, _boss_hold, _boss_since, _boss_goto
    global _boss_pursuing, _boss_tier
    tiers = _tiers(api)
    if not tiers:
        _boss_pursuing = False
        return False
    if _sweeping:
        return True                     # a metin sweep owns the target for now
    if now < _boss_next:
        # Throttled tick - but a pursuit in progress must STILL suppress the
        # normal hunt. _drive runs at 4Hz and this scan at 2Hz, so returning a
        # flat False here let every other tick fall through to
        # FindAndSetNewTarget, which grabbed whatever was nearby and stopped
        # the walk dead. That is the "gets distracted on the way to the boss"
        # bug: not a targeting problem, a duty-cycle one.
        return _boss_pursuing
    _boss_next = now + BOSS_SCAN_S

    cur = _field(api, "auto_attack_vid")
    # Already on the boss we picked: leave it alone.
    if cur and cur == _boss_vid:
        return False
    if now < _boss_hold:
        return False

    # Scan WITHOUT a radius, then apply the range test only to ACQUISITION.
    #
    # Filtering the scan by BOSS_RANGE was wrong, and cost a real boss: one sat
    # at 7912-7997 units against a 8000 limit, so it flickered in and out of the
    # list. Every time it dropped out `found` was empty, the pursuit was
    # abandoned silently, and every time it came back it re-committed and sent a
    # FRESH waypoint - which cancels the route already in flight. The character
    # never travelled and the distance grew (7912 -> 7997) until normal hunting
    # resumed.
    #
    # So: BOSS_RANGE decides what is worth CHASING, and once committed we keep
    # chasing it however far it runs, until it dies or despawns (or
    # BOSS_GIVEUP_S expires, if one is configured - it is 0/off by default).
    # Hysteresis, not a hard edge.
    #
    # Scan UNFILTERED and classify in Python. A race pre-filter cannot see a
    # metin: the stone's live race (8007) differs from the one its name resolves
    # to (8107), so api.actors(races=...) returns it zero times though it is
    # right there in the world. _tier_of matches the live NAME too, so the
    # filtering has to move here, where the actor's real identity is visible.
    try:
        seen = api.actors(radius=0)
    except Exception as e:
        api.log("autohunt2: priority scan failed %s: %s" % (type(e).__name__, e))
        _boss_next = now + 5.0
        return False

    # METIN_ANY_STONE: tag in-range stones so _tier_of classifies them as METIN
    # even when their name/race is not on the list - matching exactly what
    # entscan marks. Bounded to BOSS_RANGE so the IsStone check only runs on
    # actors we might actually pursue.
    if METIN_ANY_STONE and any(t[0] == "METIN" for t in tiers):
        for a in seen:
            d = a["dist"]
            if d is None:
                continue
            if METIN_RANGE > 0.0 and d > METIN_RANGE:
                continue                # capped like a boss only if METIN_RANGE set
            if _is_metin_stone(api, a["vid"]):
                a["stone_metin"] = True

    committed = None
    if _boss_vid:
        for a in seen:
            if a["vid"] == _boss_vid:
                committed = a
                break
    # Only unseen-entirely ends a pursuit. Out-of-range does not.
    if _boss_vid and committed is None:
        api.log("autohunt2: %s %d gone (dead or despawned) - resuming normal hunt"
                % (_boss_tier or "TARGET", _boss_vid))
        _boss_vid, _boss_goto, _boss_pursuing = 0, None, False

    # Keep only actors on a seek tier (by race OR live name), within range. This
    # is where the unfiltered scan is narrowed back down to real targets, so the
    # nearest-first / found[0] logic below still only ever sees tier matches.
    # Per-tier range: metins use METIN_RANGE (0 = any distance), bosses BOSS_RANGE.
    # A known metin should be gone to however far it is; a boss stays capped.
    found = []
    for a in seen:
        if a["dist"] is None:
            continue
        t = _tier_of(api, a)
        if t is None:
            continue
        cap = METIN_RANGE if t == "METIN" else BOSS_RANGE
        if cap <= 0.0 or a["dist"] <= cap:
            found.append(a)
    if committed is not None and committed not in found:
        found = [committed] + found     # keep chasing past the scan radius
    if not found:
        _boss_vid = 0
        _boss_pursuing = False
        return False

    # Stick with the one already chosen while it is still alive, rather than
    # always taking the nearest. Without this, two bosses a few hundred units
    # apart swap places as the character moves and each scan abandons the one
    # it was fighting for the other - measured, and it drops a boss mid-fight.
    # "Focus on them" means committing, so only re-pick once the current one is
    # gone from the scan (dead, despawned, or out of range).
    boss = None
    if _boss_vid:
        for a in found:
            if a["vid"] == _boss_vid:
                boss = a
                break

    # A HIGHER tier always takes over, even mid-pursuit. Walking to a metin when
    # a boss spawns has to become walking to the boss - that is what "priority"
    # means, and it is the same reasoning that makes tier 1/2 preempt a fight.
    # Within one tier, nearest wins (actors() returns nearest first).
    order = [t[0] for t in tiers]
    best = None
    for a in found:
        t = _tier_of(api, a)
        if t is None:
            continue
        if best is None or order.index(t) < order.index(best[0]):
            best = (t, a)
    if best is not None and (boss is None or
                             order.index(best[0]) < order.index(_boss_tier or best[0])):
        if boss is not None and best[1]["vid"] != boss["vid"]:
            api.log("autohunt2: %s outranks %s - switching" % (best[0], _boss_tier))
        boss = best[1]
    if boss is None:
        boss = found[0] if best is None else best[1]
    _boss_tier = _tier_of(api, boss) or "BOSS"

    # Commit bookkeeping happens HERE, once, for every path out of this
    # function. Doing it only in the walk branch meant a boss engaged straight
    # away never refreshed _boss_since, so the pursuit timer was already
    # expired if it later stepped out of reach - "not reached in 25s" fired one
    # tick after a successful ENGAGING line.
    if boss["vid"] != _boss_vid:
        _boss_vid = boss["vid"]
        _boss_since = now
        _boss_goto = None               # new target, new route
    d = boss["dist"]
    if d is None:
        return False

    # Out of reach: walk, do NOT press. Pressing from here is what produced the
    # preempt/drop/preempt loop - the target does not take, the client picks a
    # local mob, and the next scan throws that kill away too.
    if d > BOSS_REACH:
        if _boss_since == now:          # just committed above
            api.log("autohunt2: %s %d race=%s %r at %.0f - closing in"
                    % (_boss_tier, boss["vid"], boss["race"], boss["name"], d))
        elif BOSS_GIVEUP_S and now - _boss_since > BOSS_GIVEUP_S:
            api.log("autohunt2: %s %d not reached in %.0fs - giving up"
                    % (_boss_tier, _boss_vid, BOSS_GIVEUP_S))
            _boss_vid, _boss_hold, _boss_pursuing = 0, now + BOSS_GIVEUP_S, False
            return False
        if cur:
            # Dropping stops the sustain loop, but the engagement has ALREADY
            # cancelled the walk in flight - so the route we think we are on no
            # longer exists. Forget it, which makes _boss_walk send a fresh one
            # immediately instead of waiting for the stall detector.
            if _drop(api):
                _boss_goto = None
        _boss_pursuing = True           # hold the normal hunt off until we
                                        # arrive, on EVERY tick not just scans
        _boss_walk(api, now, boss)
        return True

    # In reach. If the client is already swinging at this exact boss there is
    # nothing to do - re-pressing every scan is pure churn, and it was showing
    # up as a re-ENGAGING line every 3s on a boss already being killed.
    if cur == boss["vid"]:
        _boss_vid = boss["vid"]
        _boss_since = now               # fighting IS progress; do not time out
        _boss_pursuing = False          # arrived; __Update_AutoAttack has it now
        return False
    if cur:
        _drop(api)                      # abandon the current fight, now
    try:
        # The third argument is 1, not 0. FindAndSetNewTarget @0x006505A0 calls
        # __OnPressActor(pMain, vid, 1) - passing 0 targets without committing
        # to the attack, so the client re-acquired a local mob a tick later and
        # the boss was never actually fought.
        #
        # It follows that call with 0x00655300, which is NOT part of the commit:
        # that one dispatches UI callbacks (SetShopTargetBoard and friends), so
        # it is deliberately not called here.
        api.player.OnPressActor(main, boss["vid"], 1)
    except Exception as e:
        api.log("autohunt2: boss OnPressActor failed %s: %s"
                % (type(e).__name__, e))
        _boss_pursuing = False
        return False
    _boss_vid = boss["vid"]
    _boss_hold = now + BOSS_RESCAN_S
    # Landing a hit is progress, so restart the pursuit clock. Without this the
    # timer kept running from the moment we first SAW the boss: one was chased
    # 7467 -> 598 -> 43 units, engaged twice, and then declared "not reached in
    # 25s" three seconds later, because 25s had elapsed since acquisition even
    # though we were standing on top of it. BOSS_GIVEUP_S is meant to bound
    # "cannot get there", not "fight is taking a while".
    _boss_since = now
    _boss_pursuing = False              # arrived - stop suppressing the hunt
    api.log("autohunt2: %s %d race=%s %r at %.0f - ENGAGING%s"
            % (_boss_tier, boss["vid"], boss["race"], boss["name"], d,
               " (dropped %d)" % cur if cur and cur != boss["vid"] else ""))
    return True


def _race_of(api, vid):
    try:
        return api.raw.nonplayer.GetRaceNumByVID(vid)
    except Exception:
        return None


def _name_of(api, vid):
    try:
        return api.raw.pack_chr.GetNameByVID(vid) or ""
    except Exception:
        return ""


def _drop(api):
    """End the engagement so the guard re-opens. Returns the dropped VID."""
    try:
        return int(api.natives.module().drop_engagement() or 0)
    except Exception as e:
        api.log("autohunt2: drop_engagement failed %s: %s" % (type(e).__name__, e))
        return 0


# ---- who else is here ------------------------------------------------------
def _pc_type_values(api):
    """INSTANCE_TYPE_* values that mean "a player".

    Read from pack_chr's own constants through api.instance_types() and NEVER
    hardcoded: the stock Metin2 ordering does not hold on this build - a player
    was measured as type 6 while type 0 is an ordinary monster."""
    global _pc_types
    if _pc_types is None:
        vals = set()
        try:
            for v, nm in api.instance_types().items():
                if nm and ("PLAYER" in nm or nm == "PC"):
                    vals.add(v)
        except Exception:
            pass
        _pc_types = vals
    return _pc_types


def _is_pc(api, vid):
    """Another character rather than a monster?

    Two routes, because neither binding is guaranteed on this build, and which
    one answered is logged ONCE. A silent fallback would make a wrong player
    list impossible to notice - and a wrong player list means this feature
    quietly does nothing, which looks exactly like "no one else is around"."""
    global _pc_route
    try:
        r = api.raw.chrmgr.IsPC(vid)
        if r is not None:
            if _pc_route is None:
                _pc_route = "chrmgr.IsPC"
                api.log("autohunt2: player test = chrmgr.IsPC")
            return bool(r)
    except Exception:
        pass
    vals = _pc_type_values(api)
    if vals:
        try:
            t = api.raw.pack_chr.GetInstanceType(vid)
            if _pc_route is None:
                _pc_route = "GetInstanceType"
                api.log("autohunt2: player test = pack_chr.GetInstanceType in %s"
                        % sorted(vals))
            return t in vals
        except Exception:
            pass
    if _pc_route is None:
        _pc_route = "NONE"
        api.log("autohunt2: !! no working player test - chrmgr.IsPC and "
                "GetInstanceType both unavailable, so AVOID_CONTESTED is INERT")
    return False


def _refresh_players(api, now):
    """Positions of every OTHER player the client has loaded.

    Walks the character manager through the native gateway rather than
    api.actors(). actors() resolves a race with nonplayer.GetRaceNumByVID and
    SKIPS any vid that raises - which is what a player vid does on a mob-proto
    accessor. Hunting for players with a mob-shaped enumeration would therefore
    find none of them, every time, silently."""
    global _players, _players_at
    if now - _players_at < CONTEST_SCAN_S:
        return _players
    _players_at = now
    tn = _native(api)
    if tn is None:
        _players = []
        return _players
    me = api.player.vid()
    try:
        n = tn.actors()
    except Exception as e:
        api.log("autohunt2: actor walk failed %s: %s - AVOID_CONTESTED is off "
                "for this tick" % (type(e).__name__, e))
        _players = []
        return _players
    out = []
    seen = set()
    for i in range(n):
        try:
            vid = tn.actor_at(i)
        except Exception:
            break
        if not vid or vid == me:
            continue
        if not _is_pc(api, vid):
            continue
        # api.entity().position() already handles both GetPixelPosition shapes
        # and the (-100,-100,-100) "not placed in the world" sentinel.
        p = api.entity(vid).position()
        if p is None:
            continue
        pos = (p[0], p[1])
        seen.add(vid)
        vel = _player_velocity(vid, pos, now)
        out.append({"vid": vid, "pos": pos, "vel": vel})
    # Forget players who have gone out of range, or their stale last position
    # would be differenced against a fresh one when they come back and produce
    # one enormous bogus velocity.
    for vid in list(_player_prev):
        if vid not in seen:
            del _player_prev[vid]
    _players = out
    return _players


def _player_velocity(vid, pos, now):
    """(vx, vy) units/sec for one player, smoothed, or (0, 0) when unknown.

    Two guards, both learned from what extrapolation does when it goes wrong:
    a sample faster than PLAYER_MAX_SPEED is a warp rather than a walk and is
    discarded outright, and the result is blended with the previous velocity so
    a single sideways step does not swing the predicted track."""
    prev = _player_prev.get(vid)
    vel = (0.0, 0.0)
    if prev is not None:
        ppos, pts, pvel = prev
        dt = now - pts
        if dt > 0.05:
            vx = (pos[0] - ppos[0]) / dt
            vy = (pos[1] - ppos[1]) / dt
            if (vx * vx + vy * vy) ** 0.5 > PLAYER_MAX_SPEED:
                vx = vy = 0.0           # warp, respawn or channel change
            k = PLAYER_VEL_SMOOTH
            vel = (pvel[0] * k + vx * (1.0 - k),
                   pvel[1] * k + vy * (1.0 - k))
        else:
            vel = pvel                  # too soon to measure; keep what we had
    _player_prev[vid] = (pos, now, vel)
    return vel


def _min_player_dist(pos):
    """Distance from `pos` to the NEAREST other player, or None if alone.

    None means "no players loaded", which is NOT the same as "very far" and must
    not be folded into a number - a channel with nobody on it should switch
    nothing, and a 1e9 sentinel would read as the best possible score."""
    if not _players or pos is None:
        return None
    best = None
    for q in _players:
        dx, dy = q["pos"][0] - pos[0], q["pos"][1] - pos[1]
        d = (dx * dx + dy * dy) ** 0.5
        if best is None or d < best:
            best = d
    return best


def _closest_approach(pos):
    """The nearest any player is PREDICTED to get to `pos` over PREDICT_S.

    This is the scoring metric, and it is the whole fix for "it picked a mob
    that was far away but they were walking straight at it". Distance now is
    only the t=0 sample; a player heading for the mob collapses the score at
    t>0, and a player walking away raises it.

    Sampled rather than solved: the closest approach of two moving points has a
    closed form, but the sampled version costs four multiplies per player per
    mob, cannot divide by zero when a player is stationary, and degrades to
    "current distance" exactly when velocity is unknown."""
    if not _players or pos is None:
        return None
    best = None
    steps = max(1, int(PREDICT_STEPS))
    for pl in _players:
        px, py = pl["pos"]
        vx, vy = pl["vel"]
        for i in range(steps + 1):
            t = PREDICT_S * i / float(steps)
            dx = (px + vx * t) - pos[0]
            dy = (py + vy * t) - pos[1]
            d = (dx * dx + dy * dy) ** 0.5
            if best is None or d < best:
                best = d
    return best


def _attackable(api, vid):
    """A mob we may actually engage?

    api.actors() reports whatever the character manager holds, which includes
    NPCs, corpses still playing their death animation, and metin stones.
    Pressing one of those is wasted at best and opens a shop window at worst.
    Ordered cheapest-first, each test guarded on its own so a build missing one
    binding loses that check rather than the whole feature."""
    try:
        if api.raw.pack_chr.IsDead(vid):
            return False
    except Exception:
        pass
    try:
        if api.raw.pack_chr.IsNPC(vid):
            return False
    except Exception:
        pass
    try:
        if api.raw.pack_chr.IsStone(vid):
            return False        # stones are the autohunt window's own choice
    except Exception:
        pass
    return not _is_pc(api, vid)


_COS30, _SIN30 = 0.8660254037844387, 0.5


def _directions():
    """Twelve unit vectors, one every 30 degrees.

    Built by repeated rotation rather than with math.cos, because `math` is not
    reliably importable in this client - the hybrid importer falls through to a
    zipimport that is broken in the rebuilt exe (see the note at the top of
    modhost.py). A feature that dies on an import is worse than one that does
    its own trigonometry."""
    out, x, y = [], 1.0, 0.0
    for _ in range(12):
        out.append((x, y))
        x, y = x * _COS30 - y * _SIN30, x * _SIN30 + y * _COS30
    return out


def _claimed_vids(api, now):
    """VIDs the other players are about to take, by simulating their algorithm.

    From where each player stands: the nearest mob, then the nearest to THAT,
    and so on for CLAIM_STEPS links - the greedy nearest-first walk their bots
    run. A mob already claimed by an earlier player is skipped, so two of them
    standing together claim six mobs between them rather than the same three.

    Rebuilt on the CONTEST_SCAN_S clock and cached, because it is O(steps x
    players x mobs) - about 700 distance comparisons on a busy screen, which is
    cheap once every half second and wasteful four times a second."""
    global _claim, _claim_at
    if now - _claim_at < CONTEST_SCAN_S:
        return _claim
    _claim_at = now
    _claim = set()
    if not _players or CLAIM_STEPS <= 0:
        return _claim
    try:
        acts = api.actors(races=None,
                          radius=0)
    except Exception:
        return _claim                   # no list, no claims - fail open
    pool = [(a["vid"], a["pos"]) for a in acts if a["pos"] is not None]
    for pl in _players:
        p = pl["pos"]
        for _ in range(int(CLAIM_STEPS)):
            best, bd = None, None
            for vid, mp in pool:
                if vid in _claim:
                    continue
                d = (mp[0] - p[0]) ** 2 + (mp[1] - p[1]) ** 2
                if bd is None or d < bd:
                    bd, best = d, (vid, mp)
            if best is None:
                break
            _claim.add(best[0])
            p = best[1]                 # they walk to it, then look again there
    return _claim


def _theirs(api, vid, here, now):
    """Why this mob is effectively already someone else's, or None.

    Two independent reasons, either sufficient:
      * it is in the predicted nearest-first chain of somebody's bot
      * it is simply closer to them than to us, so they get there first
        whatever they happen to be running
    """
    if not _players:
        return None
    if vid in _claimed_vids(api, now):
        return "next in their path"
    mp = api.entity(vid).position()
    if mp is None:
        return None                     # cannot tell - do not fight the client
    dme = ((mp[0] - here[0]) ** 2 + (mp[1] - here[1]) ** 2) ** 0.5
    dthem = _min_player_dist(mp)
    if dthem is not None and dthem < dme * CLAIM_BIAS:
        return "closer to them (%.0f vs our %.0f)" % (dthem, dme)
    return None


def _mob_positions(api):
    """(vid, (x, y)) of every attackable mob near enough to matter.

    The ONLY client work in the whole decision - one api.actors() call plus an
    _attackable test each. Everything downstream is arithmetic over this list,
    which is what makes scoring two dozen candidate spots cheap.

    Bounded by the furthest ring plus the counting radius: a mob further out
    than that cannot be within SPOT_MOB_RADIUS of any candidate, so fetching it
    would be pure cost."""
    reach = (max(SPOT_RINGS) if SPOT_RINGS else 0.0) + SPOT_MOB_RADIUS
    try:
        cands = api.actors(races=None,
                           radius=reach)
    except Exception as e:
        api.log("autohunt2: spot scan failed %s: %s" % (type(e).__name__, e))
        return []
    out = []
    for a in cands:
        if a["pos"] is not None and _attackable(api, a["vid"]):
            out.append((a["vid"], a["pos"]))
    return out


def _pick_spot(api, now, here):
    """Where to stand. Returns (x, y, why) or None meaning "stay put".

    THE ALGORITHM.

    Candidate standing positions are GENERATED - two rings of twelve around us,
    plus the anchor - then filtered by three hard constraints and ranked:

        safety >= SAFE_DIST        nobody gets this close, PREDICTED over
                                   PREDICT_S, so a spot people are walking
                                   toward scores as badly as one they are on
        mobs   >= SPOT_MIN_MOBS    something to actually kill when we arrive
        drift  <= MAX_DRIFT        measured from where the hunt STARTED, so
                                   repeated dodging cannot migrate us off the map

        maximise  safety - TRAVEL_WEIGHT * travel

    Returning None is a REAL ANSWER and the whole fix for what this replaces.
    The old code always took the best candidate however bad it was, and was
    logged switching to a mob players would come within 248 units of. Moving to
    somewhere equally contested costs the travel on top of the contention, so
    when nothing qualifies the right move is to keep hunting where we are.

    Relaxation is stepped and NAMED in the return value. A silent relaxation
    would be indistinguishable from that same bug."""
    # Mobs already in somebody's chain are not ours to count. Relocating toward
    # a cluster they are about to eat is the same mistake as staying put.
    claimed = _claimed_vids(api, now)
    mobs = [p for vid, p in _mob_positions(api) if vid not in claimed]
    anchor = _start_anchor or (here[0], here[1])

    # The direction pointing away from where the group will be. Their chain
    # unwinds outward from where they stand, so this is the axis that keeps us
    # ahead of it rather than merely distant from it right now.
    away = None
    if _players:
        n_p = float(len(_players))
        cx = sum(p["pos"][0] + p["vel"][0] * PREDICT_S for p in _players) / n_p
        cy = sum(p["pos"][1] + p["vel"][1] * PREDICT_S for p in _players) / n_p
        ax, ay = here[0] - cx, here[1] - cy
        an = (ax * ax + ay * ay) ** 0.5
        if an > 1.0:
            away = (ax / an, ay / an)

    spots = []
    for ring in SPOT_RINGS:
        for dx, dy in _directions():
            spots.append((here[0] + dx * ring, here[1] + dy * ring))
    spots.append(anchor)

    r2 = SPOT_MOB_RADIUS * SPOT_MOB_RADIUS
    best = best_score = None
    note = ""
    for need_mobs, need_safe, label in (
            (SPOT_MIN_MOBS, SAFE_DIST, ""),
            (0, SAFE_DIST, " [no mobs there]"),
            (0, SAFE_DIST_MIN, " [RELAXED safety]")):
        for s in spots:
            if _d2(s, anchor) ** 0.5 > MAX_DRIFT:
                continue
            safety = _closest_approach(s)
            if safety is None or safety < need_safe:
                continue
            n = 0
            for m in mobs:
                if (m[0] - s[0]) ** 2 + (m[1] - s[1]) ** 2 <= r2:
                    n += 1
            if n < need_mobs:
                continue
            # +AWAY_WEIGHT straight away from them, -AWAY_WEIGHT straight at
            # them, scaled smoothly in between by the dot product.
            bonus = 0.0
            if away is not None:
                vx, vy = s[0] - here[0], s[1] - here[1]
                vn = (vx * vx + vy * vy) ** 0.5
                if vn > 1.0:
                    bonus = AWAY_WEIGHT * ((vx / vn) * away[0]
                                           + (vy / vn) * away[1])
            score = safety - TRAVEL_WEIGHT * (_d2(s, here) ** 0.5) + bonus
            if best_score is None or score > best_score:
                best, best_score, note = (s, safety, n), score, label
        if best is not None:
            break

    if best is None:
        return None
    s, safety, n = best
    return (s[0], s[1], "safety %.0f, %d mob(s), %.0f away, from %d candidates%s"
            % (safety, n, _d2(s, here) ** 0.5, len(spots), note))


def _begin_relocate(api, now, here):
    """Commit to a spot and start walking. False means we are staying."""
    global _state, _spot, _spot_at, _spot_pos, _spot_mark, _spot_stalls
    global _threat_since, _reloc_until

    pick = _pick_spot(api, now, here)
    if pick is None:
        _state, _threat_since = "hunt", 0.0
        _reloc_until = now + RELOCATE_COOLDOWN_S
        api.log("autohunt2: crowded, but no spot within %.0f is any better - "
                "staying put and hunting on" % MAX_DRIFT)
        return False
    sx, sy, why = pick
    if not api.move_to(sx, sy):
        _state, _threat_since = "hunt", 0.0
        _reloc_until = now + RELOCATE_COOLDOWN_S
        api.log("autohunt2: cannot relocate - move_to unavailable")
        return False
    _state = "relocate"
    _spot = (sx, sy)
    _spot_at, _spot_mark = now, now
    _spot_pos, _spot_stalls = (here[0], here[1]), 0
    _threat_since = 0.0
    api.log("autohunt2: relocating to (%.0f,%.0f) - %s" % (sx, sy, why))
    return True


def _relocate_tick(api, now, here):
    """Walk to the spot. Always returns True - we own the tick while moving."""
    global _state, _spot, _spot_pos, _spot_mark, _spot_stalls
    global _reloc_until, _threat_since

    def arrive(why):
        global _state, _spot, _reloc_until, _threat_since
        # Re-anchor, or FindVictim keeps searching the spot we just left and
        # _home spends the next few minutes dragging us back to it.
        _set_anchor(api, (here[0], here[1]))
        _state, _spot = "hunt", None
        _threat_since = 0.0
        _reloc_until = now + RELOCATE_COOLDOWN_S
        api.log("autohunt2: %s - re-anchored at (%.0f,%.0f), hunting again"
                % (why, here[0], here[1]))
        return True

    # Travelling with purpose: combat cancels the route, so anything the client
    # grabs on the way has to go or we never arrive.
    if not RELOCATE_FIGHT_ALONG and _field(api, "auto_attack_vid"):
        _drop(api)

    if _spot is None:                   # defensive: cannot happen via _begin
        return arrive("lost the destination")

    if _d2(here, _spot) ** 0.5 <= SPOT_ARRIVE:
        return arrive("arrived")
    if now - _spot_at > RELOCATE_TIMEOUT_S:
        return arrive("gave up after %.0fs" % RELOCATE_TIMEOUT_S)

    if now - _spot_mark >= SPOT_REPATH_S:
        moved = _d2(here, _spot_pos) ** 0.5 if _spot_pos else 0.0
        _spot_pos, _spot_mark = (here[0], here[1]), now
        if moved < SPOT_PROGRESS:
            _spot_stalls += 1
            if _spot_stalls >= SPOT_STALLS:
                return arrive("stalled %d times on the way" % _spot_stalls)
            api.log("autohunt2: relocation stalled (%.0f units in %.0fs) - "
                    "re-issuing the walk" % (moved, SPOT_REPATH_S))
            api.move_to(_spot[0], _spot[1])
    return True


def _hunting_stones(api):
    """Is this a metin run? Same resolution order the drive uses.

    HUNT_STONES None means "the autohunt window owns the choice", so the byte is
    read back rather than assumed - a hardcoded answer here would silently
    diverge from what FindAndSetNewTarget is actually being told to look for."""
    if HUNT_STONES is not None:
        return bool(HUNT_STONES)
    return bool(_field(api, "hunt_stones"))


def _mobs_on_us(api, now, here):
    """How many live mobs are within ENGAGED_RADIUS.

    A PROXY for "still fighting", and deliberately a crude one: there is no
    aggro accessor on this build, and no accessor returns a mob's target either.
    Proximity is what is actually measurable, and for a pack that has pulled
    onto us it is close enough - they are on top of us by definition.

    Cached on the CONTEST_SCAN_S clock: it costs an actor walk plus three
    binding calls per nearby mob, which is fine twice a second and wasteful at
    the 4 Hz policy tick."""
    global _pack_n, _pack_at
    if now - _pack_at < CONTEST_SCAN_S:
        return _pack_n
    _pack_at = now
    try:
        acts = api.actors(radius=ENGAGED_RADIUS)
    except Exception:
        return _pack_n                  # cannot tell: keep the last answer
    n = 0
    for a in acts:
        if a["dist"] is not None and _attackable(api, a["vid"]):
            n += 1
    _pack_n = n
    return n


def _vet_target(api, now, vid):
    """Reject a mob that is already somebody else's. True if we dropped it.

    Dropped AND excluded, because excluding alone never re-opens the
    acquisition guard - FindAndSetNewTarget is only called while
    auto_attack_vid is zero, so the client would sit on the rejected mob for
    ever. The pair is what makes it pick again, and it picks the next-nearest,
    which after a few rejections is the one AWAY from them.

    After CLAIM_GIVEUP rejections in a row, arguing has failed - everything
    within reach belongs to somebody - so we stop rejecting and move instead."""
    global _exclude, _exclude_until, _claim_rejects

    if not AVOID_WHEN_STONES and _hunting_stones(api):
        return False                    # a stone is never given up, ever
    here = api.player.position()
    if here is None:
        return False
    near = _min_player_dist(here)
    if near is None or near > AWARE_DIST:
        _claim_rejects = 0
        return False                    # nobody near enough to argue with

    why = _theirs(api, vid, here, now)
    if not why:
        _claim_rejects = 0              # a clean pick: the area is workable
        return False

    _claim_rejects += 1
    _exclude, _exclude_until = vid, now + EXCLUDE_S
    _drop(api)
    if CONTEST_DEBUG:
        api.log("autohunt2: %d %r is %s - leaving it (%d/%d)"
                % (vid, _name_of(api, vid), why, _claim_rejects, CLAIM_GIVEUP))

    if _claim_rejects >= CLAIM_GIVEUP:
        _claim_rejects = 0
        api.log("autohunt2: %d in a row were theirs - moving instead of arguing"
                % CLAIM_GIVEUP)
        _begin_relocate(api, now, here)
    return True


def _reposition(api, now, main):
    """HUNT -> FINISH -> RELOCATE -> HUNT. True when it owns the tick.

    Runs before the acquisition guard for the same reason the boss scan does:
    the guard returns early whenever something is engaged, and an engagement is
    exactly the state this has to reason about.

    FINISH never drops anything. Suppressing acquisition is enough on its own to
    stop after the current kill, because the native AutoHuntLoop is inert - we
    never set the minimap status flag it gates on - so the ONLY way a target is
    ever acquired is our own FindAndSetNewTarget call further down _drive.
    Returning True stops that call happening."""
    global _state, _threat_since, _finish_vid, _finish_since

    if not AVOID_CONTESTED:
        return False
    here = api.player.position()
    if here is None:
        return False

    # Metin runs opt out entirely - including out of a relocation already under
    # way, in case the mode was switched mid-hunt.
    if not AVOID_WHEN_STONES and _hunting_stones(api):
        if _state != "hunt":
            api.log("autohunt2: stone mode - abandoning the move, stones are "
                    "never given up")
            _state, _threat_since, _finish_vid = "hunt", 0.0, 0
        return False

    # Contention avoidance is a TRASH-farming behaviour: it moves you to an
    # uncontested spot so the client's local targeting picks fresh mobs. When you
    # are only seeking bosses/metins it is worse than useless - _pick_spot finds
    # no mobs to count and relocates to empty spots, and it will walk you clean
    # off a stone you are breaking (measured live: relocating 8436 units away,
    # to a 0-mob spot, while ENGAGING a metin at 114). So stand down entirely in
    # seek-only mode, and never leave a committed stone whatever the mode. The
    # metin sweep below is what handles the pack instead of relocating.
    if not _seek_trash() or (_boss_vid and _is_metin_stone(api, _boss_vid)):
        if _state != "hunt":
            _state, _threat_since, _finish_vid = "hunt", 0.0, 0
        return False

    _refresh_players(api, now)

    if _state == "relocate":
        return _relocate_tick(api, now, here)

    if _state == "finish":
        # Wait for the WHOLE PACK, not just the one mob. They pull in groups and
        # they follow: walking off with three still alive drags them to the new
        # spot, and the players we are avoiding follow the pack straight to it.
        #
        # Note this returns False rather than True - we keep HUNTING while we
        # wait. Suppressing acquisition here would leave us standing among live
        # mobs waiting for them to die of nothing.
        left = _mobs_on_us(api, now, here)
        if left and now - _finish_since < FINISH_TIMEOUT_S:
            return False
        api.log("autohunt2: pack cleared (%s) - leaving now"
                % ("%d still up after %.0fs" % (left, FINISH_TIMEOUT_S)
                   if left else "nothing left on us"))
        _finish_vid = 0
        return _begin_relocate(api, now, here)

    cur = _field(api, "auto_attack_vid")

    # ---- hunting: is anybody crowding us? ----
    if cur and cur == _boss_vid:
        return False                    # a boss outranks this entirely
    if now < _reloc_until:
        return False                    # just relocated; actually hunt for a bit

    near_me = _min_player_dist(here)
    near_mob = _min_player_dist(api.entity(cur).position()) if cur else None
    crowded = ((near_me is not None and near_me <= PLAYER_NEAR_ME)
               or (near_mob is not None and near_mob <= CONTEST_RADIUS))

    if not crowded:
        _threat_since = 0.0
        return False
    if not _threat_since:
        _threat_since = now
        return False
    if now - _threat_since < THREAT_DWELL_S:
        return False                    # somebody running past is not a threat

    left = _mobs_on_us(api, now, here)
    if cur or left:
        # Anything still on us has to die before we move, or it comes with us.
        _state, _finish_vid, _finish_since = "finish", cur, now
        api.log("autohunt2: crowded (player %s from me, %s from the mob) - "
                "clearing %d mob(s) on us before moving"
                % ("%.0f" % near_me if near_me is not None else "n/a",
                   "%.0f" % near_mob if near_mob is not None else "n/a",
                   left))
        return False
    return _begin_relocate(api, now, here)


def _rear_add(api, metin_vid):
    """The attackable mob most directly BEHIND the stone, in range, or None.

    "Behind" is relative to the stone: the stone is what we face, so the rear is
    the opposite bearing. The add whose direction from us has the most negative
    dot-product with the stone's is the one dead behind - exactly the pile a 180
    would turn into. Positions only; no rotation field needed. Never a stone or a
    player."""
    here = api.player.position()
    if here is None:
        return None
    mp = api.entity(metin_vid).position()
    if mp is None:
        return None
    fx, fy = mp[0] - here[0], mp[1] - here[1]
    fn = (fx * fx + fy * fy) ** 0.5
    if fn < 1.0:
        return None
    fx, fy = fx / fn, fy / fn
    try:
        acts = api.actors(radius=METIN_SWEEP_RANGE)
    except Exception:
        return None
    best, best_dot = None, 1.0
    for a in acts:
        vid = a["vid"]
        if vid == metin_vid or a["pos"] is None:
            continue
        if _is_metin_stone(api, vid) or _is_pc(api, vid):
            continue                    # never sweep another stone or a player
        ax, ay = a["pos"][0] - here[0], a["pos"][1] - here[1]
        an = (ax * ax + ay * ay) ** 0.5
        if an < 1.0:
            continue
        dot = (ax / an) * fx + (ay / an) * fy   # -1 dead behind, +1 toward stone
        if dot < best_dot:
            best_dot, best = dot, a
    return best if (best is not None and best_dot < 0.0) else None


def _metin_sweep(api, now, main):
    """Every METIN_SWEEP_EVERY_S while breaking a stone, turn to the pack behind
    and hit it, then go back to the stone - the 180-and-clear, automated.

    Sets _sweeping while it owns the target, which _boss_check honours so the
    metin pursuit does not re-grab the stone mid-swing. Runs BEFORE _boss_check
    in _drive for exactly that reason."""
    global _sweep_at, _sweep_until, _sweeping, _sweep_metin
    if not METIN_SWEEP:
        return
    cur = _field(api, "auto_attack_vid")

    # ---- in a sweep: hold the add, then return to the stone ----
    if _sweeping:
        if now < _sweep_until:
            return                      # let the swing land; _boss_check is held
        _sweeping = False
        _sweep_at = now + METIN_SWEEP_EVERY_S
        if _sweep_metin:
            try:
                api.player.OnPressActor(main, _sweep_metin, 1)
            except Exception:
                pass
        _sweep_metin = 0
        return

    # ---- mounted metin hunting: never START a sweep (one in flight finishes,
    # so its stone re-press above always runs) ----
    if not METIN_SWEEP_MOUNTED and (_field(api, "mounted")
                                    or _field(api, "hunt_use_mount")):
        _sweep_at = now + METIN_SWEEP_EVERY_S
        return

    # ---- idle unless actually breaking a stone ----
    if not (_boss_vid and cur == _boss_vid and _is_metin_stone(api, _boss_vid)):
        _sweep_at = now + METIN_SWEEP_EVERY_S   # first sweep 15s after we engage
        return
    if now < _sweep_at:
        return

    add = _rear_add(api, _boss_vid)
    if add is None:
        _sweep_at = now + METIN_SWEEP_EVERY_S   # nothing behind; look again later
        return
    _sweep_metin = _boss_vid
    try:
        api.player.OnPressActor(main, add["vid"], 1)
        _sweeping = True
        _sweep_until = now + METIN_SWEEP_DUR_S
        api.log("autohunt2: metin sweep - turning to add %d %r behind (%.0f) for %.0fs"
                % (add["vid"], add["name"], add["dist"] or 0, METIN_SWEEP_DUR_S))
    except Exception as e:
        api.log("autohunt2: metin sweep press failed %s: %s" % (type(e).__name__, e))
        _sweep_at = now + METIN_SWEEP_EVERY_S


def _remount_repath(api, now):
    """Re-issue the pursuit waypoint the moment we are back on the horse.

    The dismount->cast->remount cycle (UseAutoSkills buffing mid-walk) CANCELS
    the route in flight, leaving the character standing with a far-away target -
    which is exactly the unstick mod's stuck signature, so it sidesteps, the
    pursuit re-paths, the next cast cycle repeats it, and the two loop forever.
    The remount is an EDGE, not a poll, so re-sending the same destination here
    skips _boss_walk's stall detection and its rate limit: one move_to per
    remount, same coordinates, and the walk resumes before unstick's 2.5s
    stall window closes."""
    global _boss_goto_at, _boss_goto_pos
    if not (_boss_pursuing and _boss_goto is not None):
        return
    try:
        api.move_to(_boss_goto[0], _boss_goto[1])
    except Exception as e:
        api.log("autohunt2: remount re-path failed %s: %s" % (type(e).__name__, e))
        return
    here = api.player.position()
    _boss_goto_at = now                  # fresh route: restart the stall clock
    _boss_goto_pos = (here[0], here[1]) if here is not None else None
    api.log("autohunt2: remounted mid-pursuit - re-issued waypoint (%.0f, %.0f)"
            % (_boss_goto[0], _boss_goto[1]))


def _drive(api, now):
    """One tick of the client's own autohunt, driven from here."""
    global _fails, _vetted, _was_mounted
    main = _main_instance(api)
    if not main:
        return

    # Mount edge first: back ON the horse after a cast cycle -> re-issue the
    # pursuit waypoint the remount just cancelled (see _remount_repath).
    mounted = _field(api, "mounted")
    if mounted and _was_mounted is False:
        _remount_repath(api, now)
    _was_mounted = bool(mounted)

    # Skills first, as AutoHuntLoop does: buffs are maintained whether or not
    # there is a target, and the attack branch checks for NULL itself.
    if USE_SKILLS and _field(api, "hunt_use_skill"):
        if mounted and not SKILLS_WHILE_MOUNTED:
            pass          # opted out: skip casting entirely while mounted, so
                          # UseAutoSkills is never called and never dismounts
        else:
            # Mounted or not, we keep calling UseAutoSkills: when mounted it
            # dismounts itself to cast an attack skill and remounts when done,
            # via its own /user_horse_ride toggles (see SKILLS_WHILE_MOUNTED) -
            # we must not send that command ourselves.
            #
            # While walking to a boss, hand it a NULL target. Buffs are
            # maintained regardless of target and the attack branch checks for
            # NULL itself, so this keeps self-sustain running while refusing to
            # swing at whatever hit us on the way - which was re-engaging us and
            # cancelling the route a tick after the drop. (A NULL target also
            # means the attack branch never fires, so it will not dismount mid
            # walk-in - buffs only until we arrive.)
            tgt = 0 if _boss_pursuing else _field(api, "auto_attack_target")
            # Per-target remount policy: MOUNT_ONLY_ON_STONES keeps us mounted
            # (remount after each cast) only while the objective is a metin
            # stone, and on foot against a boss or trash. Drive hunt_use_mount to
            # match BEFORE the native reads it at its remount decision. The
            # objective vid (_boss_vid, stable through a pursuit) classifies more
            # reliably than the momentary attack target, which flips during a
            # metin sweep. Without the option the gate stays as _build_settings
            # set it.
            if SKILLS_WHILE_MOUNTED and MOUNT_ONLY_ON_STONES:
                obj = _boss_vid or tgt
                _set_mount_gate(api, bool(obj) and _is_metin_stone(api, obj))
            try:
                api.player.UseAutoSkills(main, tgt, int(now))
            except Exception as e:
                api.log("autohunt2: UseAutoSkills failed %s: %s"
                        % (type(e).__name__, e))

    # autoloot is walking to a drop. Stand down until it is done - but AFTER the
    # skills block above, so buffs are still maintained while it collects.
    if _loot_busy():
        return

    # While breaking a stone, periodically turn and clear the pack that has
    # gathered behind us. Runs before _boss_check so its _sweeping flag is set
    # when _boss_check reads it, and so the pursuit does not re-grab the stone
    # mid-swing.
    _metin_sweep(api, now, main)

    # Priority targets come first: the guard below returns early whenever
    # something is engaged, which is precisely the state we want to interrupt.
    if _boss_check(api, now, main):
        return

    # Crowded by another player - on us or on our mob? Finish the current kill,
    # then move somewhere emptier. Before the acquisition guard for the same
    # reason as the boss scan: the guard returns early whenever engaged.
    if _reposition(api, now, main):
        return

    # The acquisition guard. Something is already engaged -> leave it alone.
    #
    # TIER 3 NEVER GETS HERE, and that is deliberate. A preferred trash mob does
    # not interrupt: it only decides what to take when the character is free to
    # choose. Preempting for trash would mean abandoning a half-killed mob every
    # time a flagged one wandered past, which is thrash, not priority.
    vid = _field(api, "auto_attack_vid")
    if vid:
        # A boss we deliberately committed to is never vetted or dropped: it is
        # usually not what the contention filter would keep, so without this the
        # two features would fight one tick after a preempt.
        if vid == _boss_vid:
            return
        # Already someone else's? Judged ONCE per acquired VID; the drop/exclude
        # makes the client re-pick, and after CLAIM_GIVEUP in a row it relocates.
        if AVOID_CONTESTED and vid != _vetted:
            _vetted = vid
            if _vet_target(api, now, vid):
                return
        return

    # Nothing engaged. Should we even look?
    if not _seek_trash():
        # ONLY_BOSSES / ONLY_METINS. Not "refuse to fight" - a fight already in
        # progress returned above. This is "stop going looking for one".
        return

    stones = HUNT_STONES
    if stones is None:
        stones = bool(_field(api, "hunt_stones"))
    global _fails

    # TIER 3: prefer a flagged mob that is genuinely close, by pressing it
    # directly rather than by fighting the client's choice. The alternative -
    # letting the client pick and then dropping it until it offers something on
    # the list - burns a full selection pass per rejection and was measurably
    # thrashy. Pressing what we want is one call.
    picked = _prefer_trash(api, main)
    if picked:
        if _field(api, "auto_attack_vid"):
            _fails = 0
            return
        # The press did not take (out of range, lost line of sight, just died).
        # Fall through to the client's own selection rather than stall.

    try:
        api.player.FindAndSetNewTarget(main, 1 if stones else 0, _exclude)
    except Exception as e:
        api.log("autohunt2: FindAndSetNewTarget failed %s: %s"
                % (type(e).__name__, e))
        return
    if _field(api, "auto_attack_vid"):
        _fails = 0
    else:
        _fails += 1


def _set_range(api):
    """Force the search radius, when HUNT_RANGE is not None.

    Uses the client's OWN setter - miniMap.SetAutoHuntRange(f) stores f + 40 in
    the minimap, and FindAndSetNewTarget computes (that + 40) * 35. Writing the
    field directly would need a minimap-owned field type; the binding already
    exists and does the bookkeeping (__SetPosition) too.

    Needed because the radius comes from the autohunt window's slider, and on a
    client whose window has never been opened that value is unset - which is a
    tiny search area, not a broken one: mobs a few hundred units away were still
    found while a metin stone further out was not.
    """
    if HUNT_RANGE is None:
        return
    try:
        api.raw.miniMap.SetAutoHuntRange(float(HUNT_RANGE))
        api.log("autohunt2: range forced to %.0f -> radius %.0f units"
                % (HUNT_RANGE, (HUNT_RANGE + 80.0) * 35.0))
    except Exception as e:
        api.log("autohunt2: cannot set the range (%s)" % type(e).__name__)


def _set_anchor(api, pos):
    tn = _native(api)
    if tn is None:
        return False
    try:
        tn.set_field("hunt_anchor", float(pos[0]), float(pos[1]))
        return True
    except Exception as e:
        api.log("autohunt2: cannot set the hunt anchor (%s)" % type(e).__name__)
        return False


def _set_mount_gate(api, on):
    """Write the hunt_use_mount remount gate (player+0x501E3).

    1 lets the native remount after a mounted cast; 0 leaves it on foot. Silent
    if the field is not armed (an old ini) - _build_settings logs that case once.
    """
    tn = _native(api)
    if tn is None:
        return
    try:
        tn.set_field("hunt_use_mount", 1 if on else 0)
    except Exception:
        pass


def _stub_ready(api):
    """The native gateway, not the old env-var handshake."""
    return api.natives.module() is not None and \
        "FindAndSetNewTarget" in api.natives.by_name


def _stub_skills_ready(api):
    return "UseAutoSkills" in api.natives.by_name


def _gateway_report(api):
    """Say what the gateway ACTUALLY answers, once, at start.

    _stub_ready proves only that triarch_native loaded and that natives.json
    names FindAndSetNewTarget - not that the stub implements the pieces the
    drive reads every tick. When one is missing, every accessor here swallows
    the error and returns its default, so _drive returns at `if not main` on
    every tick and the hunt runs, logs nothing and does nothing.

    That is the worst failure shape there is, and it is not hypothetical on this
    build: triarch_native turned out to have no skip_collision at all. Three
    read-only calls are cheap insurance against debugging silence."""
    tn = _native(api)
    if tn is None:
        api.log("autohunt2: gateway MISSING - triarch_native is not loaded, so "
                "the drive cannot run at all")
        return
    bits = []
    for name, call in (("main_instance", lambda: tn.main_instance()),
                       ("field(auto_attack_vid)",
                        lambda: tn.field("auto_attack_vid")),
                       ("actors", lambda: tn.actors())):
        try:
            bits.append("%s=%r" % (name, call()))
        except Exception as e:
            bits.append("%s !! %s" % (name, type(e).__name__))
    api.log("autohunt2: gateway  %s" % "   ".join(bits))


def _build_settings(api):
    """Populate the client's own autohunt settings block from its config.

    UseAutoSkills is gated on a byte in that block (hunt_use_skill) and reads
    the skill list from it, so if nothing has built it the skill drive is
    correctly armed and still does nothing - which is exactly the symptom.

    The shipped Start button calls this before sending /auto_hunt, so by the
    time our chat guard fires it has usually already run. Calling it again is
    both harmless and the only way to be sure, because "usually" depends on UI
    ordering we do not control.

    Safe under the isolation rule, checked rather than assumed: the function
    behind this binding (x86 0x0064F600, 2813 bytes) reads CPythonConfig and
    writes the settings struct. It references no network stream, no minimap
    singleton and no player singleton, and calls SendChatPacket zero times.
    """
    p = _mod("playerm2g2")
    fn = getattr(p, "CreateAutoBotSettings", None) if p else None
    if fn is None:
        api.log("autohunt2: CreateAutoBotSettings not exposed - relying on the "
                "autohunt window having built the settings itself")
        return False
    try:
        fn()
    except Exception as e:
        api.log("autohunt2: CreateAutoBotSettings failed %s: %s"
                % (type(e).__name__, e))
        return False

    # Remount gate. CreateAutoBotSettings just wrote hunt_use_mount (player+
    # 0x501E3) from the client's own config, which is normally clear - and with
    # it clear UseAutoSkills dismounts to cast an attack skill and then NEVER
    # gets back on. Force it to our policy so the native completes its own
    # dismount -> cast -> remount cycle: on when we cast mounted, off otherwise.
    tn = _native(api)
    if tn is not None:
        try:
            # Start on foot when the remount is stone-conditional: the per-tick
            # policy in _drive raises the gate once a stone is actually engaged.
            init_on = SKILLS_WHILE_MOUNTED and not MOUNT_ONLY_ON_STONES
            tn.set_field("hunt_use_mount", 1 if init_on else 0)
        except Exception as e:
            api.log("autohunt2: hunt_use_mount field unavailable (%s) - mounted "
                    "casts will dismount but not remount; regenerate the ini"
                    % type(e).__name__)
    return True


# Consecutive acquisitions that came back empty. Counted HERE now: the stub
# used to count them and publish TRIARCH_HUNT_STAT, but with the drive in
# Python the stub's hunt tick is inert and nothing would update it. Counting at
# the call site is also simply more direct - we make the call, we see the result.
_fails = 0


def _home(api, now, vid, pos, fails):
    """Walk back to the anchor once the area is actually clear.

    Distance from the anchor is NOT a reason to go back. The character is
    supposed to hunt outward - it only returns when the client has run out of
    things to find, which is what `fails` measures. Anything closer to "no
    target right now" would drag it home between kills.

    Only while unengaged: with a target the client is already walking to it and
    a second destination would fight its walk-in.

    The search centre never moves (the stub writes the anchor into FindVictim),
    so this is purely about the CHARACTER drifting to the edge and standing
    there while the respawns happen back in the middle.
    """
    global _return_next

    if vid or _anchor is None:
        return
    if fails < RETURN_AFTER_FAILS or now < _return_next:
        return

    d2 = _d2(pos, _anchor)
    if d2 <= LEASH_DIST * LEASH_DIST:
        return                               # home already

    _return_next = now + RETURN_EVERY_S
    ok = api.move_to(_anchor[0], _anchor[1])
    api.log("autohunt2: area clear (%d empty searches), %.0f from anchor - "
            "walking back%s"
            % (fails, d2 ** 0.5, "" if ok else " FAILED (no AutoMoveToPosition)"))


def _show_range(api, on):
    """The client's own autohunt range circle, on the minimap, at our anchor.

    Nothing is drawn by hand. CPythonMiniMap::Render already renders a scaled
    circle image for exactly this, gated on one bool and sized from the range
    slider's own field - so it tracks the slider live with no work from us:

        if (!minimap[0x170]) skip;                  // the visibility bool
        scale = min(mapScale * 0.5 * minimap[0x168] * k, 1.2);
        draw the circle image at minimap[0x174..0x178];

    The centre comes from SetAutoHuntStatus, which sets it to the player's
    position at the moment it is called - and, crucially, does so REGARDLESS of
    its argument:

        [this+0x16c] = new bool(b);                 // AutoHuntLoop's gate
        if (pMain) [this+0x174] = pMain.position;   // unconditional

    So SetAutoHuntStatus(False) pins the circle to where we are standing now -
    the anchor - while writing a zero into the very flag we need to stay zero.
    It reinforces the isolation rather than threatening it, which is why the
    guard lets False through.

    Caveat: Render clamps the scale at 1.2, so at large ranges the drawn circle
    stops growing before the real search radius does.
    """
    mm = _mod("miniMap")
    if mm is None:
        return False
    if on:
        try:
            mm.SetAutoHuntStatus(False)      # centre it here, gate stays clear
        except Exception:
            pass
    try:
        mm.SetAutoHuntRangeStatus(bool(on))
        return True
    except Exception:
        return False


def _revive(api, now):
    """Revive when dead, then hold the hunt while HP comes back.

    Returns True whenever the hunt should stay paused - dead, or recovering.

    Deliberately independent of _on: dying with the hunt running and staying
    dead is the worst outcome, and reviving is what the player would do anyway.
    """
    global _dead_since, _revive_next, _resume_at
    if not REVIVE:
        return False
    vid = api.player.vid()
    if not vid:
        return False
    try:
        dead = bool(api.raw.pack_chr.IsDead(vid))
    except Exception:
        return False

    if dead:
        if not _dead_since:
            _dead_since = now
            _resume_at = 0.0
            api.log("autohunt2: character is dead - reviving in %.0fs"
                    % REVIVE_AFTER_S)
        if now - _dead_since >= REVIVE_AFTER_S and now >= _revive_next:
            _revive_next = now + REVIVE_EVERY_S
            try:
                api.raw.m2netm2g.SendChatPacket(REVIVE_CMD)
                api.log("autohunt2: sent %s" % REVIVE_CMD)
            except Exception as e:
                api.log("autohunt2: revive failed %s: %s" % (type(e).__name__, e))
        return True

    if _dead_since:
        _dead_since = 0.0
        _resume_at = now + RESUME_AFTER_S
        api.log("autohunt2: revived - holding the hunt %.0fs for potions"
                % RESUME_AFTER_S)
    if _resume_at:
        if now < _resume_at:
            return True
        _resume_at = 0.0
        api.log("autohunt2: recovered - hunting again")
    return False


def _sidestep(api, pos, tpos):
    """Walk clear of a spot where nothing is reachable.

    Measured stall: the character stood at one point for 11 seconds alternating
    between two mobs ~590 units away, excluding each in turn. At that distance
    nothing is standing in the way - the path itself is blocked, so no choice of
    target helps and a single excludeVID cannot name two mobs anyway.

    Sidestepping perpendicular to the target rather than retreating: the mobs
    are the reason we are here, so we want to keep them in range and only change
    the approach line. The side alternates with _block_n so a failed step tries
    the other way rather than repeating itself.

    Uses the client's own AutoMoveToPosition - the same auto-move the shipped
    autohunt uses, so it emits nothing the player would not normally emit.
    """
    dx, dy = tpos[0] - pos[0], tpos[1] - pos[1]
    n = (dx * dx + dy * dy) ** 0.5
    if n < 1.0:
        return
    s = 1.0 if (_block_n % 2) else -1.0
    tx = pos[0] + (-dy / n) * STALL_STEP * s
    ty = pos[1] + (dx / n) * STALL_STEP * s
    ok = api.move_to(tx, ty)
    api.log("autohunt2: stalled here - sidestepping to (%.0f,%.0f)%s"
            % (tx, ty, "" if ok else " FAILED (no AutoMoveToPosition)"))


# ---- the one thing the client does not do ---------------------------------
def _tick(api, now):
    """Body-block detection.

    The client will happily stand still forever with a target it cannot reach,
    because another mob's collision sphere holds it off. Its own escape hatch
    never fires: measured, a blocked character sits at ~435-443 units, just
    inside the client's threshold.

    THREE conditions, not two. Position alone cannot tell "stuck" from
    "fighting" - a character meleeing a mob is also standing still, and there is
    no damage feedback to break the tie (no accessor on this build returns a
    target's HP; all four shapes were tried live). The discriminator is RANGE:

        held the same target, and
        did not move, and
        the target is further than we could possibly be hitting it

    Measured on a live hunt: normal melee sits at d=130..170, a real block at
    d=435..443. Nothing in between, so BLOCK_REACH=250 separates them with
    room on both sides, and it is comfortably above the 200.0f the client's own
    UseAutoSkills uses as "close enough to act on this target".

    Adding the range test is what makes the 1s timer safe. Without it, 2.5s was
    already producing false positives - one measured block fired at d=131, i.e.
    the character was in range and hitting the thing when we pulled it off.

    Naming the target in excludeVID makes the client pick again with its own
    line-of-sight test. It selects the nearest reachable enemy, which is the
    blocker by construction: the interposer is closer than the target it is
    blocking.
    """
    global _eng_vid, _eng_since, _eng_pos, _exclude, _exclude_until
    global _block_n, _block_pos, _approach_next

    if _exclude and now >= _exclude_until:
        _exclude = 0

    vid = api.target.vid() or 0
    pos = api.player.position()
    if not pos:
        return

    # Same stand-down as _drive: without it the body-block detector would read a
    # character deliberately walking away (a fetch, or a relocation) as a stall.
    if _loot_busy():
        return

    # The `or (_state != "hunt")` suppresses the walk home whenever the
    # repositioner is mid-move: _home bails when a target is held, and two live
    # waypoints would cancel each other (AutoMoveToPosition replaces the route).
    _home(api, now, vid or (_state != "hunt") or _loop_idle, pos, _fails)

    if vid != _eng_vid:
        _eng_vid, _eng_since, _eng_pos = vid, now, pos
        return
    if not vid:
        return

    if _eng_pos is None or _d2(pos, _eng_pos) > BLOCK_MOVE * BLOCK_MOVE:
        _eng_since, _eng_pos = now, pos      # still moving: not blocked
        return

    tpos = api.target.position()
    if not tpos:
        return                               # cannot judge range: do not guess
    d2 = _d2(pos, tpos)
    if d2 <= BLOCK_REACH * BLOCK_REACH:
        _eng_since = now                     # in reach and stationary: fighting
        _block_n, _block_pos = 0, None       # we got somewhere: not stalled
        return

    if now - _eng_since < BLOCK_AFTER_S:
        return

    # Far and stationary is a PATHING failure, not an interposer. The client's
    # own walk-in inside FindAndSetNewTarget did not take - so ask for the walk
    # again rather than blacklisting the target, which at long range is usually
    # the only thing in the search area anyway.
    if d2 > APPROACH_DIST * APPROACH_DIST:
        _eng_since = now
        if now >= _approach_next:
            _approach_next = now + APPROACH_EVERY_S
            ok = api.move_to(tpos[0], tpos[1])
            api.log("autohunt2: target %d is %.0f away and we are not moving - "
                    "re-issuing the walk%s"
                    % (vid, d2 ** 0.5, "" if ok else " FAILED"))
        return

    if _exclude == vid:
        _eng_since = now                     # already excluded; give it time
        return

    # Count blocks that happen from the SAME spot. Moving between them means the
    # exclusion is working and we are just meeting mobs one after another.
    if _block_pos is None or _d2(pos, _block_pos) > STALL_STEP * STALL_STEP / 4.0:
        _block_n, _block_pos = 1, pos
    else:
        _block_n += 1

    _exclude = vid
    _exclude_until = now + EXCLUDE_S
    _eng_since = now
    _drop(api)          # re-open the guard; excluding alone never re-acquires
    api.log("autohunt2: blocked on vid=%d at d=%.0f (#%d here) - excluding %.0fs"
            % (vid, d2 ** 0.5, _block_n, EXCLUDE_S))

    if STALL_SIDESTEP and _block_n >= STALL_BLOCKS:
        _sidestep(api, pos, tpos)
        _block_n, _block_pos = 0, None


# ---- start / stop ---------------------------------------------------------
def start(api):
    global _on, _anchor, _exclude, _eng_vid, _eng_since, _eng_pos
    global _block_n, _block_pos, _return_next, _fails
    global _state, _threat_since, _finish_vid, _spot, _spot_stalls
    global _reloc_until, _start_anchor, _claim_at, _vetted, _claim_rejects
    global _finish_since, _pack_n, _pack_at, _obj_at, _hop_after, _chan_warned
    global _loop_idle, _returning, _loopback_next
    global _sweep_at, _sweep_until, _sweeping, _sweep_metin
    if _on:
        return
    if not _stub_ready(api):
        api.log("autohunt2: REFUSING - uriel_stub reports the native autohunt "
                "drive is not armed (regenerate uriel_offsets.ini)")
        api.chat("autohunt2: native drive unavailable")
        return
    _gateway_report(api)
    pos = api.player.position()
    if not pos:
        api.log("autohunt2: cannot start - no player position")
        return
    _on = True
    _anchor = (pos[0], pos[1])
    _exclude = 0
    _eng_vid, _eng_since, _eng_pos = 0, 0.0, None
    _block_n, _block_pos = 0, None
    _return_next = 0.0
    _fails = 0
    # The leash is measured from HERE - not from wherever a relocation last
    # left us, or the limit would move with the character and bound nothing.
    _start_anchor = (pos[0], pos[1])
    _state, _threat_since, _finish_vid = "hunt", 0.0, 0
    _finish_since, _pack_n, _pack_at = 0.0, 0, 0.0
    _spot, _spot_stalls, _reloc_until = None, 0, 0.0
    _claim.clear()
    _claim_at, _vetted, _claim_rejects = 0.0, 0, 0
    _obj_at, _hop_after, _chan_warned = 0.0, 0.0, False
    _loop_idle, _returning, _loopback_next = False, False, 0.0
    _sweep_at, _sweep_until, _sweeping, _sweep_metin = 0.0, 0.0, False, 0
    # Before the anchor is written: this rebuilds the settings block that the
    # anchor lives in, so the other order would discard it.
    if USE_SKILLS:
        _build_settings(api)
    # Must happen here, standing on the anchor: the circle's centre is taken
    # from the player's position at the moment SetAutoHuntStatus is called.
    if SHOW_RANGE and not _show_range(api, True):
        api.log("autohunt2: minimap range circle unavailable")
    _set_anchor(api, _anchor)
    _set_range(api)
    api.log("autohunt2: START anchor=(%.0f,%.0f) range=%s stones=%s"
            % (_anchor[0], _anchor[1],
               "slider" if HUNT_RANGE is None
               else "%.0f -> %.0f units" % (HUNT_RANGE, (HUNT_RANGE + 80.0) * 35.0),
               "window" if HUNT_STONES is None else HUNT_STONES))
    if USE_SKILLS and not _stub_skills_ready(api):
        api.log("autohunt2: skills requested but the stub reports no skill "
                "drive - regenerate uriel_offsets.ini and rebuild")
    api.chat("autohunt2 on")


def stop(api):
    global _on, _exclude, _boss_vid, _boss_pursuing, _boss_goto, _boss_tier
    global _state, _threat_since, _finish_vid, _spot, _reloc_until
    global _finish_since, _pack_n, _pack_at
    if not _on:
        return
    _on = False
    _exclude = 0
    _boss_vid, _boss_pursuing, _boss_goto = 0, False, None
    _boss_tier = None
    _finish_since, _pack_n, _pack_at = 0.0, 0, 0.0
    # Leave no relocation half-finished: a stopped hunt that still thinks it is
    # walking somewhere would suppress _home and drop targets on the next start.
    _state, _threat_since, _finish_vid = "hunt", 0.0, 0
    _spot, _reloc_until = None, 0.0
    if SHOW_RANGE:
        _show_range(api, False)
    api.log("autohunt2: STOP")
    api.chat("autohunt2 off")


def toggle(api):
    stop(api) if _on else start(api)


# ---- isolation ------------------------------------------------------------
def _patch(api, owner, attr, fn, label):
    try:
        orig = getattr(owner, attr)
    except Exception:
        return False
    try:
        setattr(owner, attr, fn)
    except Exception as e:
        api.log("autohunt2: cannot patch %s (%s)" % (label, type(e).__name__))
        return False
    _patched.append((owner, attr, orig))
    return True


def _install_chat_guard(api):
    """The trigger AND the isolation barrier, in one place.

    THIS IS WHAT STARTS US. _install_ui patches AutoHunting.__StartBtn on the
    class, which sounds like the obvious hook and is not: the client binds its
    buttons with SetEvent(ui.__mem_func__(self.__StartBtn)) when the window is
    CONSTRUCTED, which captures the bound method. If that window already exists
    when we patch - and after login it always does - the button keeps calling
    the original and our detour is dead code that still reports success. That
    is exactly what happened in the 15:28 session: 'detour = uiAutoHunt:StartBtn'
    was logged, then the very next button press emitted /auto_hunt start.

    The packet is the one point every route converges on, so trigger there.
    Pressing Start emits '/auto_hunt start <name> <n>' before anything else
    happens; we swallow it (it never reaches the server, which is the isolation
    requirement anyway) and start ourselves instead. Same for 'end'.

    This sits at module level on m2netm2g, so it also catches Python callers we
    have not thought of. It does NOT catch the native AutoHuntLoop, which calls
    SendChatPacket from C++ - but that loop is gated on the minimap status flag
    we never set, so it is inert by construction."""
    if not WATCH_CHAT:
        return True
    n = _mod("m2netm2g")
    orig = getattr(n, "SendChatPacket", None) if n else None
    if orig is None:
        return False

    def guarded(msg, *a, **k):
        try:
            t = str(msg)
        except Exception:
            return orig(msg, *a, **k)

        if t.startswith(_TRIGGER):
            arg = t[len(_TRIGGER):].strip()
            if arg.startswith("start"):
                api.log("autohunt2: intercepted %r - taking over" % t)
                start(api)
            elif arg.startswith("end"):
                api.log("autohunt2: intercepted %r - stopping" % t)
                stop(api)
            else:
                api.log("autohunt2: SWALLOWED unrecognised %r" % t)
            return None                      # never reaches the server

        for w in _LOGGED:
            if t.startswith(w):
                api.log("autohunt2: chat %r (passed through)" % t)
                break
        return orig(msg, *a, **k)

    return _patch(api, n, "SendChatPacket", guarded, "m2netm2g.SendChatPacket")


def _install_minimap_guard(api):
    """Never let anything set the minimap autohunt status flag - it is the first
    condition AutoHuntLoop tests, so setting it would wake the native loop and
    have it drive the character alongside us."""
    mm = _mod("miniMap")
    orig = getattr(mm, "SetAutoHuntStatus", None) if mm else None
    if orig is None:
        return
    def guarded(*a, **k):
        if a and a[0]:
            api.log("autohunt2: BLOCKED miniMap.SetAutoHuntStatus%r" % (a,))
            return None
        return orig(*a, **k)
    _patch(api, mm, "SetAutoHuntStatus", guarded, "miniMap.SetAutoHuntStatus")


def _install_ui(api):
    """Secondary route: take over the game's autohunt button at class level.

    Only reaches presses on windows constructed AFTER this runs - the client
    captures bound methods in SetEvent at construction time, so an already-open
    autohunt window keeps the original handler. The chat guard is the route that
    always works; this one just saves a round trip when it happens to attach.

    REPLACE, NEVER WRAP: calling the original would emit /auto_hunt start and
    set the minimap flag."""
    import sys
    global _detour_note
    if not UI_DETOUR:
        _detour_note = "disabled (chat guard is the trigger)"
        return True
    done = []
    for modname in ("uiAutoHunt", "uiAutoHuntNew"):
        m = sys.modules.get(modname)
        cls = getattr(m, "AutoHunting", None) if m else None
        if cls is None:
            continue
        for attr, fn in (("_AutoHunting__StartBtn", lambda *a, **k: start(_api)),
                         ("OnClickStartBtn", lambda *a, **k: start(_api)),
                         ("_AutoHunting__StopBtn", lambda *a, **k: stop(_api))):
            if hasattr(cls, attr) and _patch(api, cls, attr, fn,
                                             "%s.%s" % (modname, attr)):
                done.append("%s:%s" % (modname, attr.split("__")[-1]))
    hud = sys.modules.get("uiHud")
    hc = getattr(hud, "HudControls", None) if hud else None
    if hc is not None and hasattr(hc, "_OnAutoHuntToggle"):
        if _patch(api, hc, "_OnAutoHuntToggle", lambda *a, **k: toggle(_api),
                  "uiHud.HudControls._OnAutoHuntToggle"):
            done.append("hud:toggle")
    if not done:
        return False
    _detour_note = ", ".join(done)
    api.log("autohunt2: secondary UI detour attached (%s)" % _detour_note)
    return True


# ---- lifecycle ------------------------------------------------------------
def on_load(api):
    global _api, _on, _detour_done, _detour_next, _exclude
    global _boss_next, _boss_vid, _boss_hold, _boss_since, _boss_pursuing
    global _boss_goto, _boss_goto_at, _boss_goto_pos
    global _pc_types, _pc_route, _state, _threat_since, _finish_vid
    global _spot, _reloc_until, _vetted, _claim_rejects
    global _obj_at, _hop_after, _chan_warned
    global _loop_idle, _returning, _loopback_next
    global _sweep_at, _sweep_until, _sweeping, _sweep_metin
    global _was_mounted
    _api = api
    _on = False
    _was_mounted = None
    _exclude = 0
    _detour_done, _detour_next = False, 0.0
    # Names resolve once and are cached, so the cache MUST die with the config
    # that produced it - otherwise editing BOSS_RACES appears to do nothing
    # until the client is restarted, which is a maddening thing to debug.
    _races_cache.clear()
    del _rejected[:]
    del _accepted[:]
    _boss_next, _boss_vid, _boss_hold, _boss_since = 0.0, 0, 0.0, 0.0
    _boss_pursuing = False
    _boss_goto, _boss_goto_at, _boss_goto_pos = None, 0.0, None
    # Player-detection caches die with a reload so a build change re-probes them.
    _pc_types, _pc_route = None, None
    del _players[:]
    _player_prev.clear()
    _claim.clear()
    _state, _threat_since, _finish_vid = "hunt", 0.0, 0
    _spot, _reloc_until, _vetted, _claim_rejects = None, 0.0, 0, 0
    _obj_at, _hop_after, _chan_warned = 0.0, 0.0, False
    _loop_idle, _returning, _loopback_next = False, False, 0.0
    _sweep_at, _sweep_until, _sweeping, _sweep_metin = 0.0, 0.0, False, 0
    trigger = _install_chat_guard(api)
    _install_minimap_guard(api)
    api.log("autohunt2: loaded (api v%s) native_drive=%s skills=%s trigger=%s"
            % (api.VERSION, _stub_ready(api),
               _stub_skills_ready(api) if USE_SKILLS else "off",
               "armed" if trigger else "FAILED - the Start button will do nothing"))
    if trigger:
        api.log("autohunt2: press the game's autohunt Start button to begin")


def on_unload(api):
    if _on:
        stop(api)
    if SHOW_RANGE:
        _show_range(api, False)              # also covers a reload while stopped
    while _patched:
        owner, attr, orig = _patched.pop()
        try:
            setattr(owner, attr, orig)
        except Exception:
            pass
    api.log("autohunt2: unloaded, handlers restored")


_autostart_at = 0.0


# ---- channel looping -------------------------------------------------------
def _loop_channels(api, now):
    """Hop to the next channel when the hunt has nothing to do.

    "An objective" = an engaged target, a boss/metin in play, or a contention
    move in progress. When none of those has been true for LOOP_IDLE_S, ask the
    chanswap mod to change channel.

    We cannot call chanswap directly - mods run in isolated namespaces, not
    sys.modules - so the request rides os.environ, exactly as the loot handshake
    does. chanswap watches TRIARCH_CHAN_REQUEST, honours it once and clears it;
    if chanswap is not loaded nothing consumes it and this is a no-op.

    Channels wrap 1..5 -> 1, never 6.
    """
    global _obj_at, _hop_after, _chan_warned, _loop_idle, _returning
    global _loopback_next
    if not LOOP_CHANNELS:
        _loop_idle = False
        return
    # A running route owns positioning AND channel choice (see the routes mod):
    # it walks us between anchors and hops channels itself, so our own hop
    # would fight it. TRIARCH_ROUTE_ACTIVE is set for the route's lifetime.
    if _route_active():
        _obj_at, _loop_idle, _returning = now, False, False
        return
    # Mid-load - a swap in flight, or any zoning - has no world to judge, so pin
    # the idle clock and never hop again before the last hop has landed.
    if not api.in_game():
        _obj_at, _loop_idle, _returning = now, False, False
        return
    if now < _hop_after:
        _obj_at, _loop_idle, _returning = now, False, False
        return

    busy = (bool(_field(api, "auto_attack_vid")) or _boss_vid or _boss_pursuing
            or _state != "hunt")
    if busy:
        _obj_at, _loop_idle, _returning = now, False, False
        return
    _loop_idle = True

    # Step 1: before hopping, walk back to where the hunt STARTED and hold the
    # idle clock until we arrive. Farming then resumes from the same known spot
    # on every channel instead of from wherever a relocation or a chase left us.
    # _home is suppressed while this runs (see _tick) so it cannot pull us to the
    # possibly-relocated _anchor instead of the original start.
    here = api.player.position()
    if _start_anchor is not None and here is not None \
            and _d2(here, _start_anchor) > LEASH_DIST * LEASH_DIST:
        if not _returning:
            _returning = True
            _loopback_next = 0.0
            api.log("autohunt2: no objectives - returning to the start point "
                    "(%.0f away) before hopping" % (_d2(here, _start_anchor) ** 0.5))
        if now >= _loopback_next:
            _loopback_next = now + RETURN_EVERY_S
            api.move_to(_start_anchor[0], _start_anchor[1])
        _obj_at = now                        # do not count the wait until home
        return
    _returning = False

    # Step 2: standing on the start point - now run the idle wait, and only hop
    # if nothing worth hunting turns up here within LOOP_IDLE_S.
    if _obj_at == 0.0:
        _obj_at = now
        return
    if now - _obj_at < LOOP_IDLE_S:
        return

    cur = api.channel()
    if not cur or not (1 <= int(cur) <= 5):
        if not _chan_warned:
            _chan_warned = True
            api.log("autohunt2: LOOP_CHANNELS on but the current channel is "
                    "unreadable (%r) - not hopping" % cur)
        _obj_at = now
        return
    _chan_warned = False
    nxt = (int(cur) % 5) + 1                 # 1->2->3->4->5->1, never 6
    import os
    try:
        os.environ["TRIARCH_CHAN_REQUEST"] = str(nxt)
    except Exception as e:
        api.log("autohunt2: could not post channel request %s: %s"
                % (type(e).__name__, e))
        return
    api.log("autohunt2: no objectives for %.0fs - requesting hop CH%d -> CH%d"
            % (now - _obj_at, int(cur), nxt))
    # Hold the idle clock off until the swap has had time to land and the new
    # channel to repopulate, so we do not machine-gun requests at chanswap.
    _obj_at = now
    _hop_after = now + LOOP_SETTLE_S


# ---- route hand-off -------------------------------------------------------
# The routes mod starts and stops us at each stop of a route and waits for the
# area to run dry. Same os.environ channel as the loot handshake and chanswap
# (mods are namespace-isolated; os is shared): a request key is consumed once,
# the state key is republished every tick. Protocol documented in
# Mods/modloader/routes/main.py.
_ENV_ROUTE_ACTIVE = "TRIARCH_ROUTE_ACTIVE"
_ENV_HUNT_REQ = "TRIARCH_HUNT_REQUEST"
_ENV_HUNT_STATE = "TRIARCH_HUNT_STATE"


def _route_active():
    import os
    try:
        return bool(os.environ.get(_ENV_ROUTE_ACTIVE, ""))
    except Exception:
        return False


def _route_poll(api):
    """Honour a start/stop request from the routes mod, exactly once."""
    import os
    try:
        raw = os.environ.get(_ENV_HUNT_REQ, "")
    except Exception:
        return
    if not raw:
        return
    try:
        os.environ[_ENV_HUNT_REQ] = ""
    except Exception:
        pass
    if raw == "start":
        api.log("autohunt2: route requested start")
        start(api)
    elif raw == "stop":
        api.log("autohunt2: route requested stop")
        stop(api)


def _route_publish(api):
    """off | busy | idle | clear, republished every tick.

    "clear" is the same test _home uses to walk back: the client's own
    FindVictim came back empty RETURN_AFTER_FAILS times in a row while nothing
    was engaged. "idle" is unengaged but the counter has not tripped - ONLY_*
    profiles never search for trash, so they can only ever report idle."""
    import os
    if not _on:
        s = "off"
    else:
        busy = (bool(_field(api, "auto_attack_vid")) or _boss_vid
                or _boss_pursuing or _state != "hunt" or _loot_busy()
                or _is_dead())
        s = "busy" if busy else ("clear" if _fails >= RETURN_AFTER_FAILS else "idle")
    try:
        os.environ[_ENV_HUNT_STATE] = s
    except Exception:
        pass


def _is_dead():
    return bool(_dead_since)


def on_update(api, dt):
    global _next_tick, _detour_done, _detour_next, _autostart_at
    now = api.now()
    _route_poll(api)

    if AUTOSTART and not _on:
        if not _autostart_at:
            _autostart_at = now + AUTOSTART_AFTER
        elif now >= _autostart_at and api.in_game():
            _autostart_at = now + 30.0      # one attempt per 30s, not per tick
            api.log("autohunt2: AUTOSTART (no button press - test mode)")
            start(api)

    # The UI detour is a bonus, never a precondition. An earlier version
    # returned here until it attached, which meant a failed detour silently
    # disabled the policy tick as well. The chat guard is what actually starts
    # us and it is installed at load.
    if not _detour_done and now >= _detour_next:
        _detour_next = now + 2.0
        if api.in_game() and _install_ui(api):
            _detour_done = True

    if not _on:
        _route_publish(api)
        return
    if now < _next_tick:
        return
    _next_tick = now + TICK_S
    try:
        if _revive(api, now):
            _route_publish(api)
            return              # dead: nothing else is meaningful
        _drive(api, now)
        _tick(api, now)
        _loop_channels(api, now)
        _route_publish(api)
    except Exception as e:
        api.log("autohunt2: tick failed %s: %s" % (type(e).__name__, e))
