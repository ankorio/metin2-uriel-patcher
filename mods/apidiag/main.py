"""apidiag - one-shot: what does this Api instance actually have?"""
CAPABILITIES = ["read"]
WAIT_S = 2.0    # the stub registers triarch_native ~70 ms after the host's first tick
_done = False
_t0 = 0.0


def on_load(api):
    global _done, _t0
    import time
    _done = False
    _t0 = time.time()


def on_update(api, dt):
    global _done
    if _done:
        return
    import sys
    import time
    # Judging on the first tick raced the stub's registration and printed a
    # false FAIL every boot; wait for the module, or WAIT_S, then report once.
    nat = getattr(api, "natives", None)
    if (nat is None or nat.module() is None) and time.time() - _t0 < WAIT_S:
        return
    _done = True
    api.log("apidiag: VERSION=%r has_natives=%r has_raw=%r has_playerNs=%r"
            % (getattr(api, "VERSION", None), hasattr(api, "natives"),
               hasattr(api, "raw"), type(getattr(api, "player", None)).__name__))
    tn = sys.modules.get("triarch_native")
    api.log("apidiag: sys.modules['triarch_native'] = %r" % (tn,))
    if hasattr(api, "natives"):
        api.log("apidiag: natives.module()=%r n=%d"
                % (api.natives.module(), len(api.natives.by_name)))
