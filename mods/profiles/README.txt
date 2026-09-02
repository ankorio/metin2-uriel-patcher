Per-character settings.
=======================

Several clients run from this one mods folder, so mods/config.json is SHARED:
changing a setting in the F10 UI on one character used to rewrite it for every
other character too. A profile fixes that.

  Drop a file here named exactly after the character:

      profiles/Alice.json
      profiles/Bob.json

  That character then uses it, and the F10 UI writes there instead of the
  shared file. Delete it and the character goes back to sharing.

The profile is MERGED OVER config.json, not swapped for it, so it only needs to
contain what DIFFERS:

      { "autohunt2": { "BOSS_RACES": ["Arquero Bestial"] },
        "nocollide": { "enabled": false } }

Everything not mentioned comes from config.json. That is deliberate: a mod added
to config.json later still gets sensible defaults on every character, instead of
disappearing from all of them at once.

Two things a profile cannot do:

  * WALK_STATE and anything else the patcher stamps per build is always taken
    from config.json. Those are facts about the game build, not preferences, and
    a profile pinning one would survive a game update and quietly feed a stale
    value to the mod that depends on it.

  * The file name is matched after stripping anything that is not a letter,
    digit, dash or underscore. Character names come from the server, and a name
    is not allowed to point at a path.

The profile is picked up a few seconds after login, once the client knows which
character it is - you will see a "profile: <name>.json" line in mods.log.
