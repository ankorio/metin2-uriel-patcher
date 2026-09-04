"""autodonatexp - donate the character's EXP to the guild, on a timer.

WHAT THE PLAYER DOES
--------------------
    Alt+G                open the guild window
    click "Donate"       opens a number-entry box
    type 999999999       the amount
    click OK             the guild gains EXP

WHAT THIS DOES INSTEAD
----------------------
Not the keystrokes. This client reads the keyboard through
RegisterRawInputDevices/GetRawInputData, and Windows delivers WM_INPUT only to
the FOREGROUND window - so a synthesised Alt+G reaches a background client not
at all, and the one behaviour that matters ("it keeps donating while I do
something else") is exactly the one that would not work.

The rule is the one the rest of this folder already follows: call the function
the client itself runs AFTER it has decoded the input. A button press is not the
thing; the thing is what the button does.

    button -> uipickmoney.PickMoneyDialogExp -> GuildWindow.OnOffer(exp)
                                             -> m2netm2g.SendGuildOfferPacket(exp)

...and this mod is the last line of that chain, once per DONATE_EVERY_MIN.

HOW THAT CHAIN WAS ESTABLISHED, BECAUSE IT WAS NOT OBVIOUS
----------------------------------------------------------
Three different things on this server wear the word "donate", and picking the
wrong one spends real yang on the wrong feature. What the probe found:

  guild bank        localeInfo.GUILD_DEPOSIT = 'Deposit'
                    GuildWindow.OnDeposit(self, MONEY) -> SendGuildDepositMoneyPacket
                    Yang parked in the guild's bank. Withdrawable. NOT EXP.

  donation certs    GUILD_DONATE_FAIL_NOT_ENOUGH_{MIDDLE,HIGH}_ITEM,
                    GUILD_DONATE_COUNT 'Current Donations: %d/%d', tabs for
                    Small/Medium/Large, rewards in Medals of Honor. An item
                    feature with a counter; it does not take a nine-digit number.
                    app.ENABLE_GUILD_DONATE_ATTENDANCE = 0.

  THIS ONE          localeInfo.GUILDWINDOW_BUTTON_DONATE = 'Donate'
                    uiScriptLocale.GUILD_DONATE_TITLE    = 'Donate EXP'
                    GuildWindow.OnOffer(self, EXP)   <- the argument is named exp
                    uipickmoney.PickMoneyDialogExp   <- its own number box
                    m2netm2g.SendGuildOfferPacket    <- present on this build
                    "Offer" is the stock name for the yang -> guild-EXP donation;
                    this server relabelled the button 'Donate'.

The argument NAME is the decisive evidence: OnOffer takes `exp` where OnDeposit
takes `money`, and they are the only two money buttons on the window. It had to
be argument names because Uriel strips code objects - co_names came back EMPTY
for every method on the class, so the bodies cannot be read, only the signatures.

WHY THE PACKET AND NOT THE WINDOW
---------------------------------
OnOffer is a method, and a method needs an INSTANCE - the live GuildWindow, not
the class. There is no way to get one here: it is not held at module level (the
probe swept every resident module and found none), it hangs off the interface
object which hangs off the game window, and `gc` is NOT resident on this build,
so the general "walk every object" route is closed too.

That leaves the packet, which is fine, because the packet is what OnOffer sends.
Sending it directly also means the window never has to be open, which is what
makes this work on a client that is minimised.

WHAT IT COSTS - MEASURED, NOT ASSUMED
-------------------------------------
It spends the CHARACTER'S OWN EXPERIENCE. Not yang. Measured live on a spare
character by sending a deliberately small amount and watching all three balances:

    03:13:18  sent 10000. before: guild=1615924 myexp=2905003 yang=558518
    03:13:24  CONFIRMED +100 guild EXP (1615924 -> 1616024)
              Cost: myexp -6966, yang +0

Yang is the clean reading - it was +0, and this character picks none up, so
nothing else could have moved it. The -6966 is the net of the 10000 donated
against roughly 3000 gained from hunting during those six seconds. So:

    100 player EXP  ->  1 guild EXP,  and yang is never touched.

WHY THE AMOUNT HAS TO BE CLAMPED HERE
-------------------------------------
The first live send was the literal 999999999 and it was REFUSED - guild EXP did
not move at all. The character had 2,905,003 EXP, so the server rejects an
over-balance offer outright rather than clamping it down.

The player never meets that, because PickMoneyDialogExp caps the box at what
they actually have BEFORE OnOffer is called. Typing a billion into that box does
not mean a billion, it means "all of it" - and the clamp lives in the dialog
this mod bypasses. So the clamp has to be re-applied here, against
playerm2g2.GetEXP(), or AMOUNT is just a number the server throws away.

VERIFICATION IS BUILT IN
------------------------
This is a black box: the server's refusal comes back as a chat line we cannot
read. So the mod measures instead - guild EXP, own EXP and yang, sampled before
the packet and again VERIFY_AFTER_S later, with all three deltas logged. A
donation that did nothing says so, with the numbers, rather than looking exactly
like one that worked.
"""

import sys

CAPABILITIES = ["read", "ui", "control"]

# ---- what it does -----------------------------------------------------------
# The number the player types into the box, meaning "as much as allowed". It is
# clamped to playerm2g2.GetEXP() before sending - see the note above on why that
# clamp has to happen here rather than being left to the server.
AMOUNT = 999999999

# A ceiling on top of that clamp. 0 = none, i.e. AMOUNT means the character's
# whole experience pool, every EVERY_MIN.
#
# Worth a thought before leaving it at 0: this spends EXPERIENCE, so a character
# that is still levelling donates its own progress. That is the right answer for
# a parked lure or a capped character and the wrong one for anything you are
# still growing, and nothing here can tell those apart - so it is left as the
# operator's call rather than guessed at.
MAX_PER_DONATION = 0

EVERY_MIN = 60.0            # how often, once armed
FIRST_AFTER_MIN = 2.0       # ...but not the instant we log in. The guild module
                            # reports id=0/name='' during the login and select
                            # phase, and a donation sent then is sent by nobody.

# A ONE-SHOT EXPERIMENT, and the reason it exists.
#
# The first live send was 999999999 and was refused with no EXP change. Both of
# the character's balances were far below it - EXP 2,767,756 and yang 558,518 on
# the measured character - so the server is almost certainly refusing outright
# rather than clamping. The player never sees that because PickMoneyDialogExp
# caps the box at what they have BEFORE OnOffer is called; typing a billion
# means "all of it", and the clamp lives in the dialog we are bypassing.
#
# Which balance it spends is still a guess, and guessing is what this constant
# exists to avoid: OnOffer's argument is named `exp` and the window is titled
# 'Donate EXP', which points at the character's experience - but 'Donate' also
# sits beside a yang-priced certificate feature on this server.
#
# So send a small amount and watch all three numbers. Whichever one moves is the
# unit, and the ratio between it and the guild's gain is the rate. 10000 is
# trivial against either balance and cannot delevel anything.
#
# 0 = off, use AMOUNT. Left in the file because the next server patch can move
# any of this, and re-running the experiment is how it gets found again.
TEST_AMOUNT = 0

VERIFY_AFTER_S = 6.0        # how long to wait before reading the EXP back. Long
                            # enough for the round trip and the guild refresh
                            # packet that follows it.

# ---- the safety catches -----------------------------------------------------
# ARMED is the one that matters. This mod spends yang, irreversibly, and a mod
# that spends the character's experience the moment it is dropped into the
# folder is not a mod, it is an accident waiting for a filename. Nothing is sent
# until this is True.
ARMED = False

# "" runs anywhere. A name restricts it to that character - the same guard
# automonkey uses, and for the same reason: picking up the wrong profile is both
# surprising and, here, expensive.
ONLY_CHARACTER = ""

# Refuse to send when the character is not in a guild. There is no legitimate
# reason to send a guild packet without a guild, and guild.GetGuildID() reads 0
# both for "no guild" and for "not in the world yet" - so this doubles as the
# readiness test that FIRST_AFTER_MIN only approximates.
REQUIRE_GUILD = True

PROBE = False               # dump the guild surface on load and change nothing.
                            # How the chain above was found; left in because the
                            # next server patch may move it.
VERBOSE = True

_probed = False
_next_at = 0.0              # when the next donation is due
_verify_at = 0.0            # when to read the EXP back, 0 = nothing pending
_before = None              # (summary, current, needed) sampled before sending
_sent = 0


# ---- reading the guild ------------------------------------------------------
def _guild(api):
    return sys.modules.get("guild")


def _net(api):
    return sys.modules.get("m2netm2g")


def _exp(api):
    """(summary, current, needed) or None. The number that proves it worked.

    GetGuildExperienceSummary() is the running total and is what actually moves
    on a donation; GetGuildExperience() returns (current, needed-for-next-level)
    and is carried alongside because it is what the window shows, so a log line
    holding both can be checked against the screen."""
    g = _guild(api)
    if g is None:
        return None
    try:
        summary = int(g.GetGuildExperienceSummary())
    except Exception:
        return None
    cur, need = 0, 0
    try:
        pair = g.GetGuildExperience()
        if pair and len(pair) >= 2:
            cur, need = int(pair[0]), int(pair[1])
    except Exception:
        pass
    return (summary, cur, need)


def _wallet(api):
    """(player exp, yang) or (None, None). The two balances a donation could
    come out of - sampled around the send so the delta names the unit."""
    pm = sys.modules.get("playerm2g2")
    if pm is None:
        return None, None
    def get(fn):
        try:
            return int(getattr(pm, fn)())
        except Exception:
            return None
    return get("GetEXP"), get("GetElk")


def _level(api):
    """The character's level, or None.

    Carried purely so a delevel would be VISIBLE. Donating experience is the one
    effect of this mod that cannot be undone and whose limits are unknown - the
    server accepted 10000 without complaint, but nothing tells us what it does
    when handed the whole pool. If it drops a level, the log says so on the line
    that reports the donation, rather than being noticed a week later.

    This build exposes LEVEL as an index into GetStatus rather than a getter, so
    both spellings are tried and neither is assumed."""
    pm = sys.modules.get("playerm2g2")
    if pm is None:
        return None
    fn = getattr(pm, "GetStatus", None)
    idx = getattr(pm, "LEVEL", None)
    if fn is not None and idx is not None:
        try:
            return int(fn(idx))
        except Exception:
            pass
    fn = getattr(pm, "GetLevel", None)
    if fn is not None:
        try:
            return int(fn())
        except Exception:
            pass
    return None


def _guild_name(api):
    g = _guild(api)
    if g is None:
        return 0, ""
    try:
        return int(g.GetGuildID()), str(g.GetGuildName() or "")
    except Exception:
        return 0, ""


# ---- probing ----------------------------------------------------------------
def _code(api, label, fn):
    """What a Python function calls, without having its source.

    Kept because it is how this mod was written and it is how it will be fixed:
    the client's scripts ship compiled inside an encrypted pack, so a code object
    is the only description of a handler available. Note that on THIS build
    co_names is stripped and only co_varnames survives - the argument names are
    the evidence, which is why they are what the docstring cites."""
    code = getattr(fn, "__code__", None)
    if code is None:
        api.log("  %-38s (not a Python function: %s)" % (label, type(fn).__name__))
        return
    try:
        args = list(code.co_varnames)[:code.co_argcount]
        api.log("  %-38s (%s)  uses: %s"
                % (label, ", ".join(args), ", ".join(code.co_names) or "<stripped>"))
    except Exception as e:
        api.log("  %-38s !! %s: %s" % (label, type(e).__name__, e))


def _probe(api):
    """Dump the donation surface. Strictly read-only - sends nothing."""
    api.log("---- autodonatexp probe ----")
    gid, gname = _guild_name(api)
    api.log("  guild: id=%d name=%r exp=%r" % (gid, gname, _exp(api)))

    for mn in ("localeInfo", "uiScriptLocale"):
        m = sys.modules.get(mn)
        if m is None:
            continue
        for a in sorted(dir(m)):
            low = a.lower()
            if "donate" in low or "deposit" in low or "offer" in low:
                try:
                    api.log("  %s.%-36s = %r" % (mn, a, getattr(m, a)))
                except Exception:
                    pass

    ug = sys.modules.get("uiguild")
    cls = getattr(ug, "GuildWindow", None) if ug else None
    if cls is None:
        api.log("  uiguild.GuildWindow MISSING")
    else:
        for n in sorted(n for n in dir(cls) if not n.startswith("__")):
            low = n.lower()
            if "offer" in low or "deposit" in low or "donat" in low:
                _code(api, "GuildWindow.%s" % n, getattr(cls, n, None))

    # WHAT WE ARE SPENDING. The first live send was refused with no EXP change,
    # and OnOffer's argument is named `exp`, not `money` - so the number in that
    # box may be the player's OWN experience rather than yang. These are the two
    # balances that decide it: whichever one 999999999 exceeds is the one the
    # server is refusing on.
    pm = sys.modules.get("playerm2g2")
    if pm is not None:
        cands = sorted(set(
            [n for n in dir(pm) if "elk" in n.lower()]
            + [n for n in dir(pm) if "exp" in n.lower()]
            + [n for n in dir(pm) if "money" in n.lower()]
            + [n for n in dir(pm) if "gold" in n.lower()]
            + [n for n in dir(pm) if "level" in n.lower()]))
        api.log("  playerm2g2 balance-ish: %s" % ", ".join(cands))
        for n in cands:
            fn = getattr(pm, n, None)
            if not callable(fn):
                api.log("  playerm2g2.%-28s = %r" % (n, fn))
                continue
            try:
                api.log("  playerm2g2.%-28s -> %r" % (n + "()", fn()))
            except Exception as e:
                api.log("  playerm2g2.%-28s !! %s" % (n + "()", type(e).__name__))

    # The dialog the button opens. Its own methods say what unit it deals in and
    # whether it caps the number before OnOffer ever sees it.
    upm = sys.modules.get("uipickmoney")
    for cn in ("PickMoneyDialog", "PickMoneyDialogExp"):
        cls2 = getattr(upm, cn, None) if upm else None
        if cls2 is None:
            continue
        meth = sorted(n for n in dir(cls2) if not n.startswith("_"))
        api.log("  uipickmoney.%s: %s" % (cn, ", ".join(meth[:24])))
        for n in meth:
            low = n.lower()
            if "max" in low or "accept" in low or "unit" in low or "set" in low:
                _code(api, "  %s.%s" % (cn, n), getattr(cls2, n, None))

    net = _net(api)
    for fn in ("SendGuildOfferPacket", "SendGuildDepositMoneyPacket",
               "SendGuildWithdrawMoneyPacket"):
        api.log("  m2netm2g.%-32s %s"
                % (fn, "yes" if (net and hasattr(net, fn)) else "NO"))
    api.log("  gc module: %s"
            % ("resident" if sys.modules.get("gc") else "MISSING - no instance walk"))
    api.log("---- end probe ----")


# ---- donating ---------------------------------------------------------------
def _ready(api):
    """"" when it is safe to send, or the reason it is not."""
    if not ARMED:
        return "not ARMED"
    if ONLY_CHARACTER:
        try:
            who = api.player.name() or ""
        except Exception:
            who = ""
        if who != ONLY_CHARACTER:
            return "this is %r, not %r" % (who, ONLY_CHARACTER)
    net = _net(api)
    if net is None or not hasattr(net, "SendGuildOfferPacket"):
        return "m2netm2g.SendGuildOfferPacket is missing on this build"
    if REQUIRE_GUILD:
        gid, gname = _guild_name(api)
        if not gid:
            return "not in a guild (or the guild data has not arrived yet)"
    return ""


def _donate(api, now):
    """Send one donation and arm the verification read."""
    global _verify_at, _before, _sent

    why = _ready(api)
    if why:
        if VERBOSE:
            api.log("autodonatexp: skipping this round - %s" % why)
        return False

    gid, gname = _guild_name(api)
    wallet = _wallet(api)
    amount = int(TEST_AMOUNT or AMOUNT)
    if not TEST_AMOUNT:
        # THE CLAMP. Without it the server refuses the whole offer - see the
        # docstring. TEST_AMOUNT deliberately skips it: an experiment has to
        # send exactly what it says it sends.
        if wallet[0] is None:
            api.log("autodonatexp: cannot read the character's EXP - refusing "
                    "to send an unclamped %d, which the server would reject "
                    "outright" % amount)
            return False
        amount = min(amount, int(wallet[0]))
        if MAX_PER_DONATION > 0:
            amount = min(amount, int(MAX_PER_DONATION))
    if amount <= 0:
        if VERBOSE:
            api.log("autodonatexp: nothing to donate (own EXP %s)" % wallet[0])
        return False
    _before = (_exp(api), wallet, _level(api))
    try:
        _net(api).SendGuildOfferPacket(amount)
    except Exception as e:
        api.log("autodonatexp: SendGuildOfferPacket(%d) raised %s: %s"
                % (amount, type(e).__name__, e))
        _before, _verify_at = None, 0.0
        return False

    _sent += 1
    _verify_at = now + VERIFY_AFTER_S
    api.log("autodonatexp: sent %d%s to %r (guild %d) - send #%d. before: "
            "guild=%s myexp=%s yang=%s level=%s"
            % (amount,
               " (TEST_AMOUNT)" if TEST_AMOUNT
               else " (clamped from %d)" % AMOUNT if amount < AMOUNT else "",
               gname, gid, _sent,
               "?" if _before[0] is None else _before[0][0],
               _before[1][0], _before[1][1], _before[2]))
    return True


def _verify(api):
    """Read the guild EXP back and say plainly whether anything happened."""
    global _verify_at, _before
    after, wallet, level = _exp(api), _wallet(api), _level(api)
    before, _before, _verify_at = _before, None, 0.0
    if before is None or before[0] is None or after is None:
        api.log("autodonatexp: cannot verify - guild EXP unreadable "
                "(before=%r after=%r)" % (before, after))
        return
    # All three deltas, always. Which balance moved is the whole question, and a
    # log line that only reported the guild's gain would answer it for exactly
    # nobody reading it later.
    # before is (guild_exp_tuple, wallet_tuple) - so the guild total is
    # before[0][0], not before[0]. It was before[0] once, and that shipped:
    #   03:06:21 verify failed TypeError: unsupported operand type(s)
    #            for -: 'int' and 'tuple'
    delta = after[0] - before[0][0]
    d_exp = None if (wallet[0] is None or before[1][0] is None) else wallet[0] - before[1][0]
    d_yang = None if (wallet[1] is None or before[1][1] is None) else wallet[1] - before[1][1]
    if delta > 0:
        api.log("autodonatexp: CONFIRMED +%d guild EXP (total %d -> %d, level "
                "progress %d/%d). Cost: myexp %s, yang %s, level %s"
                % (delta, before[0][0], after[0], after[1], after[2],
                   "?" if d_exp is None else "%+d" % d_exp,
                   "?" if d_yang is None else "%+d" % d_yang,
                   "?" if (level is None or before[2] is None)
                   else "%d (was %d)" % (level, before[2]) if level != before[2]
                   else "%d unchanged" % level))
    else:
        # Loud, and with both numbers, because "it ran and nothing happened" is
        # the failure that otherwise looks exactly like success in a log.
        api.log("autodonatexp: NO CHANGE after %.0fs - guild EXP still %d "
                "(myexp %s, yang %s). Refused - and it is no longer the "
                "clamp, which is applied before the send. Most likely a daily "
                "cap, or a level or membership-age requirement."
                % (VERIFY_AFTER_S, after[0],
                   "?" if d_exp is None else "%+d" % d_exp,
                   "?" if d_yang is None else "%+d" % d_yang))


# ---- lifecycle --------------------------------------------------------------
def on_load(api):
    global _probed, _next_at, _verify_at, _before, _sent
    _probed, _next_at, _verify_at, _before, _sent = False, 0.0, 0.0, None, 0
    api.log("autodonatexp: loaded - %s, %d every %.0f min, first in %.0f min%s"
            % ("ARMED" if ARMED else "NOT ARMED", int(TEST_AMOUNT or AMOUNT),
               EVERY_MIN, FIRST_AFTER_MIN,
               "" if not ONLY_CHARACTER else ", only on %r" % ONLY_CHARACTER))
    if TEST_AMOUNT:
        api.log("autodonatexp: TEST_AMOUNT=%d overrides AMOUNT=%d - this is the "
                "one-shot experiment that identifies which balance a donation "
                "comes out of. Set it to 0 when the answer is in the log."
                % (TEST_AMOUNT, AMOUNT))
    if not ARMED:
        api.log("autodonatexp: nothing will be sent. This mod spends the "
                "character's own EXPERIENCE irreversibly, so it stays "
                "inert until ARMED is set in "
                "mods\\config.json or this character's profile.")


def on_unload(api):
    api.log("autodonatexp: unloaded after %d donation(s)" % _sent)


def on_update(api, dt):
    """Must never raise - five faults and modhost disables the mod."""
    global _probed, _next_at

    if not api.in_game():
        return
    now = api.now()

    if PROBE and not _probed:
        # Once per load and only once in the world: the guild module reports
        # id=0/name='' during login and select, and a probe that ran there would
        # report "no guild" on a character that has one.
        _probed = True
        try:
            _probe(api)
        except Exception as e:
            api.log("autodonatexp: probe failed %s: %s" % (type(e).__name__, e))

    # The verification read comes first: it is due at a specific moment and must
    # not be pushed behind the next donation's schedule.
    if _verify_at and now >= _verify_at:
        try:
            _verify(api)
        except Exception as e:
            api.log("autodonatexp: verify failed %s: %s" % (type(e).__name__, e))

    if not _next_at:
        # Set on the first tick IN THE WORLD rather than at load, so the delay
        # is measured from being able to donate rather than from the mod being
        # read off disk - which on a reload would be immediately.
        _next_at = now + FIRST_AFTER_MIN * 60.0
        if VERBOSE and ARMED:
            api.log("autodonatexp: first donation in %.0f min" % FIRST_AFTER_MIN)
        return

    if now < _next_at:
        return
    # Rescheduled BEFORE the attempt, and on the same clock whether it succeeded
    # or not. A failed send that retried immediately would hammer a server that
    # has already said no - the refusals here are per-day, so a retry loop would
    # run for hours.
    _next_at = now + EVERY_MIN * 60.0
    try:
        _donate(api, now)
    except Exception as e:
        api.log("autodonatexp: donate failed %s: %s" % (type(e).__name__, e))
