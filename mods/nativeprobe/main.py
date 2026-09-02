"""nativeprobe - prove the triarch_native gateway works, safely, once.

This is the first thing to run after the gateway is deployed. It escalates
deliberately: registration, then error paths, then ONE real native call chosen
because it is the least dangerous thing in the table.

WHY CreateAutoBotSettings IS THE TEST CALL
    * thiscall through the player singleton, so it exercises the singleton
      deref, ECX setup, the call and the ESP restore - the whole trampoline.
    * zero arguments, so a stack-push bug cannot be what breaks it. If this
      fails, the fault is in the machinery, not in argument marshalling.
    * audited over its true bounds (2813 bytes): reads CPythonConfig, writes
      the autohunt settings block, calls SendChatPacket zero times, touches no
      network/minimap/player singleton. It is also what the shipped Start
      button calls, so the client does this to itself routinely.

Everything is logged and nothing is retried: a fault disarms the name inside
the stub, and hammering it would only produce a second identical fault.
"""

import time

CAPABILITIES = ["read"]

WAIT_S = 2.0            # the stub registers triarch_native ~70 ms after the first tick
RUN_CALL = True         # False = registration + error paths only, no native call
RUN_ACQUIRE = True      # stage 4: FindAndSetNewTarget. Real combat effect -
                        # it may acquire a target and walk to it, exactly as
                        # autohunt2 does. Set False to stop after stage 3.

_done = False
_t0 = 0.0


def _native():
    import sys
    m = sys.modules.get("triarch_native")
    if m is None:
        try:
            __import__("triarch_native")
            m = sys.modules.get("triarch_native")
        except Exception:
            m = None
    return m


def _probe(api):
    api.log("nativeprobe: ==== gateway probe ====")

    m = _native()
    if m is None:
        api.log("nativeprobe: FAIL - 'triarch_native' is not importable %.1fs "
                "after load. In the first second after boot this is the "
                "registration race (the stub registers the module just after the "
                "host's first tick), not a broken gateway; any later, check "
                "uriel_stub.log for the NATIVE: lines."
                % (time.time() - _t0))
        return
    api.log("nativeprobe: module imported, call=%r" % getattr(m, "call", None))

    # --- error paths first. These must fail CLEANLY, not crash. ---
    try:
        m.call("ThisNativeDoesNotExist")
        api.log("nativeprobe: WARN - unknown name did not raise")
    except Exception as e:
        api.log("nativeprobe: unknown name rejected: %s" % e)

    try:
        m.call("FindAndSetNewTarget")        # needs 3 dwords, given none
        api.log("nativeprobe: WARN - missing arguments did not raise")
    except Exception as e:
        api.log("nativeprobe: missing args rejected: %s" % e)

    if not RUN_CALL:
        api.log("nativeprobe: RUN_CALL off - stopping before the real call")
        return

    if not api.in_game():
        api.log("nativeprobe: not in game yet - the player singleton would be "
                "null; will retry")
        return False

    # --- stage 1: zero arguments. If this breaks, the machinery is wrong. ---
    try:
        r = m.call("CreateAutoBotSettings")
        api.log("nativeprobe: PASS 0-arg - CreateAutoBotSettings returned %r" % (r,))
    except Exception as e:
        api.log("nativeprobe: FAIL 0-arg - %s: %s" % (type(e).__name__, e))
        return True

    # --- stage 2: a returned value, and the pointer Python cannot make ---
    try:
        main = m.main_instance()
        api.log("nativeprobe: PASS return - main_instance() = 0x%08X" % (main or 0))
        if not main:
            api.log("nativeprobe: main instance is 0 - stopping before the "
                    "pointer-taking calls")
            return True
    except Exception as e:
        api.log("nativeprobe: FAIL return - %s: %s" % (type(e).__name__, e))
        return True

    # --- stage 3: one argument. Exercises the push path minimally. ---
    # OpenCharacterMenu(0): UI only, and 0 is not a live VID so it has nothing
    # to open. Chosen because a wrong push here cannot do anything worse.
    try:
        m.call("OpenCharacterMenu", 0)
        api.log("nativeprobe: PASS 1-arg - OpenCharacterMenu(0) survived")
    except Exception as e:
        api.log("nativeprobe: FAIL 1-arg - %s: %s" % (type(e).__name__, e))
        return True

    # --- stage 4: three arguments, one of them a real pointer ---
    # This is the call the whole autohunt path is built on. It may or may not
    # find a victim (the search is centred on the hunt anchor, which nothing has
    # written this session) - either outcome proves the argument marshalling.
    if RUN_ACQUIRE:
        try:
            before = api.target.vid() or 0
            m.call("FindAndSetNewTarget", main, 0, 0)
            after = api.target.vid() or 0
            api.log("nativeprobe: PASS 3-arg - FindAndSetNewTarget(0x%08X,0,0) "
                    "survived; target %d -> %d" % (main, before, after))
        except Exception as e:
            api.log("nativeprobe: FAIL 3-arg - %s: %s" % (type(e).__name__, e))
    return True


def on_load(api):
    global _done, _t0
    _done = False
    _t0 = time.time()
    api.log("nativeprobe: loaded, waiting for the game phase")


def on_update(api, dt):
    global _done
    if _done:
        return
    # The first ticks run before the stub has registered triarch_native, so
    # probing there was a false FAIL every boot. Wait for it, up to WAIT_S.
    if _native() is None and time.time() - _t0 < WAIT_S:
        return
    if _probe(api) is False:
        return              # not in game yet, try again next tick
    _done = True
