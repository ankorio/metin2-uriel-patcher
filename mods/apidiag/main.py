"""apidiag - one-shot: what does this Api instance actually have?"""
CAPABILITIES = ["read"]
ENABLED = True
_done = False


def on_update(api, dt):
    global _done
    if _done:
        return
    _done = True
    import sys
    api.log("apidiag: VERSION=%r has_natives=%r has_raw=%r has_playerNs=%r"
            % (getattr(api, "VERSION", None), hasattr(api, "natives"),
               hasattr(api, "raw"), type(getattr(api, "player", None)).__name__))
    tn = sys.modules.get("triarch_native")
    api.log("apidiag: sys.modules['triarch_native'] = %r" % (tn,))
    if hasattr(api, "natives"):
        api.log("apidiag: natives.module()=%r n=%d"
                % (api.natives.module(), len(api.natives.by_name)))
