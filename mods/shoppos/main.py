"""shoppos - progressive offline-shop map/channel mapper (always-on).

Runs every session. Rather than re-posting the whole visible set on a timer, it
SCANS the loaded offline-shop entity vector every SCAN_PERIOD seconds and uploads
only shops it has NOT already recorded - a new seller, or a shop that has moved.
A local dedup file (`_patcher\\shoppos_seen.json`) remembers what has been sent,
so re-running across sessions never re-posts unchanged shops. As you play and
roam, shops stream into the client and get mapped near-real-time; the ones that
were never in range simply get picked up the moment they load.

Join is by seller (one character == one offline shop, so the name is unique). The
receiver is an HTTP endpoint you run yourself; it is expected to stamp each shop
it knows about with {map, channel, loc} and to be idempotent, so incremental
partial posts are safe. HTTP POST blocks the caller,
but a quiet scan (nothing new) never posts, so the steady state costs nothing.

Config (mods/config.json -> "shoppos"); shipped defaults EMPTY so it is inert
until configured:
  URL           endpoint, e.g. "https://example.invalid/api/shop-locations"
  UPLOAD_TOKEN  API key -> "Authorization: Bearer <token>"
  SCAN_PERIOD   seconds between scans (default 4 - "realtime-ish")
  REFRESH_H     re-post an unchanged shop after this many hours; a safety net so
                a shop the receiving side did not yet know about when it was
                first posted still lands its location eventually (default 6)
  MAX_BATCH     cap shops per single POST (default 200)
"""
CAPABILITIES = ["read", "market"]

import json
import os

URL = ""                 # empty => disabled
UPLOAD_TOKEN = ""        # empty => no auth header
SCAN_PERIOD = 4.0
REFRESH_H = 6.0
MAX_BATCH = 200
MAX_SHOPS = 5000
STATE_NAME = "shoppos_seen.json"

_next = 0.0
_seen = {}               # seller -> {"sig": "map|ch|x|y", "ts": epoch}
_loaded = False


def _state_path(api):
    return api.data_path(STATE_NAME)


def _load_state(api):
    global _seen, _loaded
    _loaded = True
    try:
        with open(_state_path(api), "r") as f:
            d = json.load(f)
        _seen = d if isinstance(d, dict) else {}
    except Exception:
        _seen = {}
    api.log("shoppos: %d shop locations already mapped" % len(_seen))


def _save_state(api):
    """Atomic write (temp + replace) so a crash mid-save can't corrupt the file."""
    path = _state_path(api)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_seen, f)
        os.replace(tmp, path)
    except Exception as e:
        api.log("shoppos: state save ERR %r" % (e,))


def _sig(mp, ch, x, y):
    return "%s|%s|%d|%d" % (mp, ch, int(round(x)), int(round(y)))


def on_load(api):
    global _next, _loaded
    _next = 0.0
    _loaded = False
    api.log("shoppos: loaded (url set: %s)" % bool(URL))


def on_update(api, dt):
    global _next
    if not URL or not api.in_game():
        return
    now = api.now()
    if now < _next:
        return
    _next = now + SCAN_PERIOD

    if not _loaded:
        _load_state(api)

    tn = api.natives.module()
    if tn is None or not hasattr(tn, "offline_shops"):
        return
    try:
        n = int(tn.offline_shops() or 0)
    except Exception as e:
        api.log("shoppos: offline_shops() ERR %r" % (e,))
        return
    if n <= 0:
        return

    mp = api.map_name() or ""
    ch = api.channel() or 0
    refresh = REFRESH_H * 3600.0

    # Diff the loaded set against what we've already uploaded. A shop is fresh if
    # it's a new seller, its map/channel/position changed, or its record is older
    # than the refresh window (safety re-post).
    fresh = []
    for i in range(min(n, MAX_SHOPS)):
        try:
            r = tn.offline_shop_at(i)
        except Exception:
            continue
        if not r or not r[0]:
            continue
        seller, x, y = r[0], r[1], r[2]
        sig = _sig(mp, ch, x, y)
        prev = _seen.get(seller)
        if prev and prev.get("sig") == sig and (now - prev.get("ts", 0)) < refresh:
            continue
        fresh.append((seller, x, y, sig))
        if len(fresh) >= MAX_BATCH:
            break

    if not fresh:
        return

    shops = [[s, x, y] for (s, x, y, _s) in fresh]
    body = json.dumps({"map": mp, "channel": ch, "ts": int(now), "shops": shops})
    auth = ("Bearer " + UPLOAD_TOKEN) if UPLOAD_TOKEN else None
    try:
        st = api.http_post(URL, body, auth=auth, content_type="application/json")
    except Exception as e:
        api.log("shoppos: http_post ERR %r" % (e,))
        return

    if isinstance(st, int) and 200 <= st < 300:
        for (s, x, y, sig) in fresh:
            _seen[s] = {"sig": sig, "ts": now}
        _save_state(api)
        api.log("shoppos: +%d mapped (%s ch%s | %d loaded, %d total known) -> HTTP %s"
                % (len(fresh), mp, ch, n, len(_seen), st))
    else:
        api.log("shoppos: POST failed HTTP %s (%d shops still pending)" % (st, len(fresh)))
