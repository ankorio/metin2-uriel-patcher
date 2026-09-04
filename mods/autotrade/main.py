"""autotrade - accept incoming trades on a receiving character.

WHAT IT DOES
------------
While a trade window is open, reads BOTH sides through the client's own
`exchange` module and sends SendExchangeAcceptPacket once the offer has settled.

WHY IT READS BEFORE IT ACCEPTS
------------------------------
Firing SendExchangeAcceptPacket blind would be a packet sent with no exchange in
progress - useless at best, and exactly the shape of traffic that makes a client
stand out at worst. The client exposes the whole exchange state
(exchange.isTrading, GetItemVnumFrom*, GetElkFrom*, GetAcceptFrom*), so every
decision here is made from what the client itself already knows.

THE SAFETY PROPERTY THAT MATTERS
--------------------------------
GIVE_NOTHING (on by default) refuses to accept while ANYTHING is on our side of
the window - any item, any yang. That turns this into a strictly RECEIVING
character: someone can hand it goods and it will accept, and it can never give
anything away, however the other party arranges the trade.

That is not a nicety. "Auto-accept any trade" with items on our side is a
standing invitation to be emptied by anyone who can open a trade window, and the
mod cannot tell a gift from a theft. Turning GIVE_NOTHING off makes this
character accept whatever it is holding out - only do that deliberately.

STABILITY, NOT SPEED
--------------------
The offer must be UNCHANGED for STABLE_S before we accept. Metin2 clears both
accept flags whenever either side edits the offer, so accepting the instant a
window opens just means accepting again after every edit - noisy, and it hands
the timing to the other party. Waiting for the offer to settle is both quieter
and harder to play games with.
"""

import sys

CAPABILITIES = ["read", "trade"]

# Refuse to accept while anything of ours is in the window. See the note above -
# this is what makes the character receive-only.
GIVE_NOTHING = True

STABLE_S = 0.0          # how long the other side's offer must hold before we
                        # accept. 0 = accept the moment the window is seen.
                        #
                        # Non-zero is the cautious setting and costs latency;
                        # zero is safe here because the retry budget RESETS on
                        # every change of offer (see the signature check in
                        # on_update). Metin2 clears both accept flags whenever
                        # either side edits the offer, so an instant accept on
                        # an empty window is simply re-sent once they put
                        # something in - it does not consume the budget.
RECHECK_S = 0.0         # 0 = look every pump (~10 Hz), which is as fast as this
                        # mod can see anything at all
MAX_TRIES = 3           # accepts sent per trade before giving up (the server may
                        # simply refuse; re-sending forever would be spam)
RETRY_S = 2.0           # between those attempts
SLOTS = 12              # exchange grid size; the loop stops early on any error
DRY_RUN = False         # True = decide and log, send nothing
VERBOSE = True

_next = 0.0
_sig = None             # signature of the other side's current offer
_since = 0.0            # when that signature was first seen
_tries = 0
_sent_at = 0.0
_active = False         # were we in a trade on the previous tick
_refused = False        # logged the give-nothing refusal for this trade


def _ex():
    return sys.modules.get("exchange")


def _net():
    return sys.modules.get("m2netm2g")


def _trading():
    e = _ex()
    if e is None:
        return False
    try:
        return bool(e.isTrading())
    except Exception:
        return False


def _side(e, which):
    """(list of (vnum, count), yang) for 'Self' or 'Target'.

    Bounded and individually guarded: the grid size is a guess at 12, and a
    build that indexes differently should cost this loop, not the mod."""
    items = []
    for i in range(SLOTS):
        try:
            vnum = e and getattr(e, "GetItemVnumFrom" + which)(i)
        except Exception:
            break
        if not vnum:
            continue
        try:
            cnt = int(getattr(e, "GetItemCountFrom" + which)(i))
        except Exception:
            cnt = 1
        items.append((int(vnum), cnt))
    try:
        elk = int(getattr(e, "GetElkFrom" + which)())
    except Exception:
        elk = 0
    return items, elk


def _partner(e):
    try:
        return str(e.GetNameFromTarget() or "?")
    except Exception:
        return "?"


def _already_accepted(e):
    try:
        return bool(e.GetAcceptFromSelf())
    except Exception:
        return False


def _accept(api):
    n = _net()
    fn = getattr(n, "SendExchangeAcceptPacket", None) if n else None
    if not callable(fn):
        api.log("autotrade: SendExchangeAcceptPacket is not available - "
                "cannot accept")
        return False
    try:
        fn()
        return True
    except Exception as e:
        api.log("autotrade: accept failed %s: %s" % (type(e).__name__, e))
        return False


def _reset():
    global _sig, _since, _tries, _sent_at, _refused
    _sig, _since, _tries, _sent_at, _refused = None, 0.0, 0, 0.0, False


def on_load(api):
    global _next
    _next = 0.0
    _reset()
    if _ex() is None:
        api.log("autotrade: the 'exchange' module is not loaded - this client "
                "cannot report trade state, so nothing will be accepted")
        return
    api.log("autotrade: ready%s - accepts after the offer holds %.1fs%s"
            % (" (DRY RUN, sends nothing)" if DRY_RUN else "", STABLE_S,
               "; REFUSES to accept while anything of ours is in the window"
               if GIVE_NOTHING else
               "; WILL GIVE AWAY whatever is on our side - GIVE_NOTHING is off"))


def on_unload(api):
    api.log("autotrade: unloaded")


def on_update(api, dt):
    global _next, _sig, _since, _tries, _sent_at, _active, _refused

    now = api.now()
    if now < _next:
        return
    _next = now + RECHECK_S

    if not _trading():
        if _active:
            _active = False
            if VERBOSE:
                api.log("autotrade: trade window closed")
            _reset()
        return

    e = _ex()
    if not _active:
        _active = True
        _reset()
        if VERBOSE:
            api.log("autotrade: trade opened with %r" % _partner(e))

    mine, my_elk = _side(e, "Self")
    theirs, their_elk = _side(e, "Target")

    # The safety gate. Checked every tick, not once: the other party can add to
    # our side at any point by handing items back, and an accept that was safe a
    # second ago may not be now.
    if GIVE_NOTHING and (mine or my_elk):
        if not _refused:
            _refused = True
            api.log("autotrade: NOT accepting - our side holds %d item(s) and "
                    "%d yang. GIVE_NOTHING keeps this character receive-only; "
                    "clear our side or turn it off deliberately."
                    % (len(mine), my_elk))
        return
    _refused = False

    if _already_accepted(e):
        return                      # nothing to do; waiting on them

    sig = (tuple(theirs), their_elk)
    if sig != _sig:
        # A new or edited offer. Fresh retry budget, fresh stability clock -
        # but do NOT return here. Returning cost a whole tick before the first
        # accept could even be considered, which is most of the delay the
        # cautious version had; with STABLE_S at 0 the check below passes
        # immediately and we accept on this very tick.
        _sig, _since, _tries, _sent_at = sig, now, 0, 0.0
        if VERBOSE:
            api.log("autotrade: %r offers %s%s%s"
                    % (_partner(e),
                       ", ".join("%dx%d" % (c, v) for v, c in theirs) or "nothing",
                       " + %d yang" % their_elk if their_elk else "",
                       "" if not STABLE_S
                       else " - waiting %.1fs for it to settle" % STABLE_S))

    if STABLE_S and now - _since < STABLE_S:
        return
    if _tries >= MAX_TRIES:
        return
    if _sent_at and now - _sent_at < RETRY_S:
        return

    _tries += 1
    _sent_at = now
    if DRY_RUN:
        api.log("autotrade: DRY RUN - would accept %r's offer (%d item(s)%s)"
                % (_partner(e), len(theirs),
                   ", %d yang" % their_elk if their_elk else ""))
        return
    if _accept(api):
        api.log("autotrade: accepted %r's offer (%d item(s)%s) [%d/%d]"
                % (_partner(e), len(theirs),
                   ", %d yang" % their_elk if their_elk else "",
                   _tries, MAX_TRIES))
