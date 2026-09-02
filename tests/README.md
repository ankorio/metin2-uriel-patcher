# Offline tests

```
python3 -m pytest tests/
```

Plain `pytest`, standard library only, runs on any OS in well under a minute.
Nothing here needs the game, a Windows box or a live process: the tests build
their own "protected" executables with `tools/make_fixture.py` and run the
real `tools/unuriel.py` (`static_keystream`, `decode_iat`, `attribute_dlls`,
`find_oep`, `derive`, `rebuild`) and `tools/ksattack.py` against them.

## What the fixture is

`tools/make_fixture.py <out.exe>` writes a small PE32 (about 2.4 MB with the
default 600 pages of `.text`) that has the **on-disk layout** documented in
[docs/01](../docs/01-how-uriel-protects-the-client.md) and
[docs/02](../docs/02-unpacking-offline.md):

| part | what it reproduces |
|---|---|
| `.text` | pseudo-x86 with an x86-like byte distribution (about 12% `0x00`, `CC` padding between functions, `55 8B EC` prologues, `C3`/`C2 imm16` returns, `E8 rel32` calls to in-image functions, `68 imm32` pushes, `FF 15` calls through the IAT, vtable calls), XORed with one random 4096-byte key tiled per page; `IMAGE_SCN_MEM_EXECUTE` cleared |
| the CRT entry | `CC`, then `E8 <__security_init_cookie> E9 <__scrt_common_main_seh>`: the callee holds the single `89 0D <cookie>` store and is called exactly once, the jmp target is never called, the entry itself is never a target — the shape `find_oep` keys on |
| `.rdata` | the original IAT (data directory 12, at the start of the section): one NUL-terminated thunk array per DLL in the linker's sorted order, named slots pointing at hint/name entries whose "hint" word is the name **length** and whose bytes are `name[i] ^ key[i]`; ordinal slots as `0x80000000 | n`; a load config directory (data directory 10) whose `SecurityCookie` (+0x3C) points into `.data` |
| `.data`, `.reloc` | the cookie global; a real relocation table covering every absolute `imm32` in `.text` |
| injected section | random three-letter name, RWX, NOP sled with `AddressOfEntryPoint` pointing at it, and the replacement import directory (data directory 1): one descriptor importing `FireInTheHole` from `client_x86.dll` with plain names, `FirstThunk` pointing back at the original `client_x86` slot in `.rdata` |

The import groups are drawn from `tools/imports_db.json`: ADVAPI32, KERNEL32,
USER32 by name; WS2_32 by ordinal only; an OLEAUT32 group holding only `#6`
(the ordinal-table tie that the sorted-descriptor rule breaks); and the
one-import `client_x86.dll` group real builds have. Two deliberate wrinkles:
`CloseHandle` is obfuscated so that a NUL byte appears in the middle of the
name (`key[2]` is forced equal to `'o'`), and `UrielFixtureUnknownApiW` is a
name the table has never seen, so `derive` must attribute it by inheritance.

Next to the exe it writes `<out.exe>.truth.json`: the key (base64), the OEP
RVA, the sha256 of the plaintext `.text`, the slot -> (dll, name) map, the
special addresses (cookie, init_cookie, main_seh, the `FireInTheHole` call
site) and the `.text` byte statistics including `key_vote_margin` - the
smallest gap, over all 4096 offsets, between the winning and runner-up vote
in the histogram attack. Positive means the key recovers exactly; the default
seed gives about 19 votes at 600 pages, and more pages widen it.

Options:

```
python3 tools/make_fixture.py fixture.exe [--pages N] [--seed S] [--plain] [--wrong-name-key]
```

- `--plain` writes the same file with `.text` in the clear (the header still
  says non-executable; only the bytes differ). It is the ground-truth twin
  `tools/ksattack.py <enc> <plain>` scores against.
- `--wrong-name-key` obfuscates the import names with a key that is not the
  `.text` key; `derive` must then stop with "decoded import names are garbage".
- Same seed, same output - the fixture is deterministic, so the truth file and
  the twins always agree.

## What the fixture is not

It is the file format only. It does **not** reproduce the protector's runtime
(the vectored exception handler that decrypts pages on first execution, the
patching of the NOP sled, the hand-filling of the IAT), and the "code" in
`.text` only has to look like x86 *statistically* - it is not a runnable
program and disassembling it will not give you sensible functions. Numbers
that depend on the real client (17 000 pages, 22 groups, 626 imports, 3 OEP
candidates) are not reproduced; the fixture has 600 pages, 6 groups, 38
imports and 1 candidate. Anything that depends on the actual Windows loader
or on `harvest` (live-process mode) is out of scope.

## Regenerating a fixture for the docs' exercises

The exercises in docs/02 refer to `triarch.exe`. Any of them can be done on a
fixture instead:

```
python3 tools/make_fixture.py fixture.exe                 # the protected file
python3 tools/make_fixture.py fixture_plain.exe --plain   # its decrypted twin
python3 tools/ksattack.py fixture.exe fixture_plain.exe   # exercise 1/2: expect 4096/4096
python3 tools/unuriel.py derive  fixture.exe -o profile.json
python3 tools/unuriel.py rebuild fixture.exe profile.json -o fixture_clean.exe --stub-dll uriel_stub
```

`fixture.exe.truth.json` gives you the answers (the key, the OEP, every
slot's DLL and name) to check your own work against. For exercise 2 try
`--pages 200`/`--pages 100` and watch `key_vote_margin` in the truth file go
negative; for exercise 3 the mid-name NUL is in the `CloseHandle` entry at
`nul_entry_rva`; for exercise 4 the `#6`-only group is the OLEAUT32 one.
