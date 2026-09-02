"""mobdiag - prove the actor walk, and find race numbers by name.

Read-only: it enumerates and reports, and never targets or attacks anything.

Two jobs. First, confirm the character-manager walk returns real actors - the
same "does 0 mean empty or mean broken" question the ground-item walk got
wrong, so an unresolved singleton raises here instead of reporting an empty
world. Second, and more useful day to day: print the race histogram, which is
the practical way to discover the race NUMBER for a mob you only know by name.
"""
CAPABILITIES = ["read"]
EVERY = 4.0
NAME_LIKE = ""          # if set, list every matching actor individually

_next = 0.0


def on_load(api):
    global _next
    _next = 0.0
    if api.VERSION < 16:
        api.log("mobdiag: needs api v16 (have v%s) - actors() is missing"
                % api.VERSION)
        return
    api.log("mobdiag: armed (api v%s) name_like=%r" % (api.VERSION, NAME_LIKE))


def on_update(api, dt):
    global _next
    if api.VERSION < 16 or not api.in_game():
        return
    now = api.now()
    if now < _next:
        return
    _next = now + EVERY
    try:
        acts = api.actors()
    except Exception as e:
        api.log("mobdiag: actors() failed %s: %s" % (type(e).__name__, e))
        _next = now + 15.0
        return

    hist = {}
    for a in acts:
        key = (a["race"], a["name"])
        hist[key] = hist.get(key, 0) + 1
    top = sorted(hist.items(), key=lambda kv: -kv[1])[:10]
    api.log("mobdiag: %d actor(s) %s"
            % (len(acts),
               ", ".join("r%s %r x%d" % (k[0], k[1], v) for k, v in top)))

    if NAME_LIKE:
        for a in acts:
            if NAME_LIKE.lower() in (a["name"] or "").lower():
                api.log("mobdiag:   MATCH vid=%s race=%s %r d=%s"
                        % (a["vid"], a["race"], a["name"], a["dist"]))
