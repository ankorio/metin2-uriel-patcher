# m2-uriel-patcher

**TL;DR** — Uriel Anti-Cheat encrypts a Metin2 client's code with one
4096-byte XOR key reused on every page, leaves the original import table on
disk with the names XORed by that same key, and moves the entry point into a
NOP sled. All three undo themselves from the file alone: the key falls out of
a byte histogram in three seconds, the imports decode with it, and the entry
point has the shape of every MSVC start-up. This repository is the complete
tool chain built on that (an offline unpacker, an offset resolver that never
hardcodes an address, a DLL that takes the anti-cheat's place and hosts Python
mods, and the mods) plus nine documents that teach it from zero as a
reverse-engineering case study. Verified on eight client builds and one live
boot. **Every weakness here has been disclosed and this tool is not
maintained**: see the next paragraph.

> **Disclosure and status.** Everything this project relies on was reported
> to the developers of the Triarch Online server and to the developers of
> the Uriel anti-cheat before publication. It is published as learning
> material, with their knowledge, once they had what they needed to fix it.
> **This tool will not be maintained for later versions of either the client
> or the anti-cheat.** If a newer build breaks it, that is the expected
> outcome, not a bug to report: the documents in `docs/` describe how the
> analysis was done, and that is the part meant to outlive the code.

An offline unpacker, a re-derivable offset resolver, a replacement DLL and a
Python mod framework for a Metin2-based Windows client protected by
**Uriel Anti-Cheat** — published as a reverse-engineering case study.

The whole chain runs without ever launching the game:

```
triarch.exe  (protected)
   │  tools/unuriel.py derive      keystream + imports + entry point, from the file alone
   │  tools/unuriel.py rebuild     -> triarch_clean.exe  (decrypted, imports restored, ASLR off)
   │  tools/mkoffsets.py           -> uriel_offsets.ini, uriel_natives.ini (every address re-derived)
   │  stub/uriel_stub.dll          replaces the anti-cheat inside the process, hosts Python mods
   └─ mods/                        the mod host, the API and the mods themselves
```

`patcher/` wraps all of that into a single drop-in executable that does the
seven steps in order and writes a log you can read.

## Who this is for

- **Learners.** If you can program but have never opened a PE file, start at
  [`docs/00-start-here.md`](docs/00-start-here.md). Every document explains
  its concepts from first principles and ends with exercises.
- **Modders.** If you only want to write a mod, read
  [`docs/06-mod-framework.md`](docs/06-mod-framework.md) and copy one of the
  small mods in `mods/`.
- **Tool builders.** If you are fighting a similar protector, the unpacker in
  `tools/unuriel.py` and the resolver conventions in `tools/mkoffsets.py` are
  written to be reused.

## Quick start (users)

1. Build `patcher/dist/TriarchPatcher.exe` (see *Building*). There is no
   prebuilt binary and no release download: the repository ships source only.
   It does not ship the game client either — you need your own installation
   with `triarch.exe` and `client_x86.dll` in it.
2. Copy it into the game folder, next to `triarch.exe`.
3. Run it. A log window shows the seven steps; the run takes a few minutes,
   most of it spent resolving offsets in a 68 MB code section.
4. Launch `triarch_clean.exe --game`.

Run it again after every game update. Nothing is hardcoded: the keystream,
the import table, the entry point and every hooked address are re-derived from
the new executable.

Keep these in the game folder:

| file | role | if missing |
|---|---|---|
| `triarch_clean.exe` | the patched client | nothing to run |
| `uriel_stub.dll` | replaces the anti-cheat DLL | the client will not start |
| `uriel_offsets.ini` | addresses for this build | the stub refuses to arm |
| `uriel_natives.ini` | the `triarch_native` gateway | client runs, **mods silently do nothing** |
| `mods\` | mod host + mods | no mods |

`mods\config.json` is yours: a re-patch never overwrites it.

## Repository map

```
tools/      mkoffsets.py   structural offset resolver -> the two .ini files
            unuriel.py     the unpacker: derive (offline), rebuild, harvest (live fallback)
            ksattack.py    the keystream attack as a stand-alone experiment
            attack_figures.py  draws the five figures in docs/02 from a protected exe
            imports_db.json  import name -> DLL table used by derive
            namemods.py    helpers for the embedded-Python method tables
stub/       uriel_stub.cpp / .def / build_stub.bat   the replacement DLL (MSVC)
mods/       modhost.py, api.py, uikit.py, natives.json, config.json, <mod>/main.py ...
patcher/    the one-exe wrapper: pipeline.py, ui.py, winproc.py, sync.py, build.bat
docs/       the case study and the framework specifications (reading order below)
```

`patcher/src/triarch_patcher/vendor/` and `.../resources/` are **generated**
by `patcher/sync.py` from `tools/`, `mods/` and `stub/`. They are ignored by
git and must never be edited by hand; a fix made in one copy and not the other
is exactly the silent-wrong-address bug this project exists to avoid.

## Documentation

| # | document | what you learn |
|---|---|---|
| 00 | [start here](docs/00-start-here.md) | reading order, prerequisites, how to set up a safe lab |
| 00b | [how a Metin2 client works](docs/00b-how-a-metin2-client-works.md) | the engine, the embedded Python, the binding tables, the frame loop, entities, the protocol: the picture every later document assumes |
| 01 | [how Uriel protects the client](docs/01-how-uriel-protects-the-client.md) | the four transformations, and how each was discovered |
| 02 | [unpacking offline](docs/02-unpacking-offline.md) | the many-time-pad keystream attack, decoding the import table, finding the entry point, rebuilding the PE |
| 03 | [the patcher pipeline](docs/03-patcher-pipeline.md) | the seven steps as a specification, failure modes, building the exe |
| 04 | [offsets and ini files](docs/04-offsets-and-ini-files.md) | never hardcode an address: anchors, resolvers, the ini formats, adding a resolver |
| 05 | [the stub DLL](docs/05-stub-dll.md) | how the DLL is loaded, every hook, every native exposed to Python, how mods are bootstrapped |
| 06 | [the mod framework](docs/06-mod-framework.md) | modhost lifecycle, the API surface, config and profiles, writing a mod |
| 07 | [mods catalogue](docs/07-mods-catalogue.md) | every shipped mod: purpose, config keys, what it teaches |
| 08 | [glossary](docs/08-glossary.md) | the vocabulary, briefly |

## Requirements

Python 3.11+ and `capstone` (`pip install capstone`) for `tools/mkoffsets.py`
and for running the patcher from source; `tools/unuriel.py` and
`tools/ksattack.py` need nothing beyond the standard library. `numpy` and
`matplotlib` for `tools/attack_figures.py` only. PyInstaller and the Visual
Studio 2017 Build Tools (x86 toolchain) for building the DLL and freezing the
exe. `pytest` for `tests/`.

## Building

Everything is built on Windows, in a VM you can throw away.

```
pip install pyinstaller capstone
cd patcher
build.bat stub        # compile stub/uriel_stub.cpp with MSVC 2017 Build Tools, then freeze
build.bat             # freeze only (the DLL must already exist)
```

`build.bat` runs `sync.py` first, so the frozen exe always carries the
current `tools/`, `mods/` and the DLL you just built, plus the DLL's sha256 so
the patcher can prove it deployed the right bytes.

To run the patcher from source instead: `cd patcher`, `python sync.py`,
`set PYTHONPATH=src` (`export PYTHONPATH=src` on a Unix shell), then
`python -m triarch_patcher <game folder> --console` (add `--live` for the
original launch-and-harvest method, see docs/03).

## Verification status

The offline path was run on eight distinct client builds (PE timestamps
2026-07-27 to 2026-08-30, differently named injected sections, different
keystreams). Each derived a keystream with zero weak offsets, decoded the same
22 import groups / 626 imports, found the entry point, and passed every
resolver. For the two builds where a clean exe from the live method existed,
the offline rebuild was byte-identical in `.text`, entry point and every data
section. A client patched by the frozen exe built from this tree booted with the
stub attached, the eight gateway natives declared in `mods/natives.json`
armed (the 22 stub functions on `triarch_native` are a separate count — see
docs/05), and the mods loaded, and reached the login screen; the logs are
quoted in docs/03.

## Rules this project keeps

- **No crafted or malformed packets to a live server.** The framework does not
  expose a raw send, on purpose.
- **No terrain pass-through.** The client reports its own position several
  times a second; walking through walls is visible server-side. Actor
  pass-through (walking through mobs) is client-only and is what `nocollide`
  does.
- **No credentials in the tree.** `mods/config.json` ships with `autologin`
  disabled (`{"enabled": false}`), and the mod stores no credentials.
- **Nothing hardcoded.** If a number is an address, it is derived, or it is a
  bug. The one table of literal addresses, `BASELINE` in
  `tools/mkoffsets.py`, is a self-test fixture: known-good values from one
  build that `--check` compares fresh resolutions against. It is never used to
  produce output.

## License

MIT — see [LICENSE](LICENSE).
