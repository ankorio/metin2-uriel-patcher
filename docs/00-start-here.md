# 00 — Start here

This repository is a complete, working example of taking a protected Windows
game client apart and putting a modding framework inside it. It is written to
be read in order. Each document assumes familiarity with the ones before it and explains every
new concept the first time it appears.

## What you need to know already

- **One programming language well.** The tools are written in Python, the stub in C++ and
  the mods are Python as well. If you have written Java, Kotlin or C#, you will be able to read
  all of it.
- **What a function call is at the machine level.** Arguments go somewhere
  (the stack, on 32-bit x86), a `call` instruction jumps to somewhere else in the code and a `ret` comes back.
  You do not need to be able to write assembly; you need to be able to read and understand
  a dozen instructions with a reference open. The byte patterns the documents
  quote (`E8 rel32`, `FF 15 imm32`, `8B 81 disp32`, ...) are collected in one
  table at the end of the [glossary](08-glossary.md#x86-byte-patterns-used-in-this-repo).
- **Hexadecimal numeric system.** Addresses and offsets are written in hex throughout.

You do **not** need prior experience with PE files, debuggers, disassemblers,
or anti-cheat systems. That is what the case study teaches.

## What you need, and what runs where

You need your own copy of the protected client — `triarch.exe` and the
`client_x86.dll` beside it, from a game installation. The repository does not
ship them. Due to the nature of the project it ships with no prebuilt binaries for any of the tooling either: there is no (and never will be) a
release download. The `stub/uriel_stub.dll` and
`patcher/dist/TriarchPatcher.exe` are built from source on Windows as the
README's _Building_ section describes. With a client and no Windows machine
you can still run the first half of the chain:

| Runs on any OS (pure Python)                                         | Windows only                                                             |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `tools/ksattack.py` — the keystream attack                           | `stub/build_stub.bat` and `patcher/build.bat` — MSVC and PyInstaller     |
| `tools/unuriel.py derive` and `rebuild` — the whole unpacker         | `uriel_stub.dll` — it is loaded by the game                              |
| `tools/attack_figures.py` — the figures in document 02               | `TriarchPatcher.exe`, and `tools/unuriel.py harvest` (the live fallback) |
| `tools/mkoffsets.py` on a rebuilt `triarch_clean.exe` — every offset | the game itself, and everything in documents 05–07 that watches it run   |
| `patcher/sync.py --check` — the staging self-test                    |                                                                          |

Document 09 describes the disposable Windows VM the right-hand column
assumes; read it before the first launch of anything.

## Reading order

| document                                                                | read it when                                                | time      |
| ----------------------------------------------------------------------- | ----------------------------------------------------------- | --------- |
| [00b how a Metin2 client works](00b-how-a-metin2-client-works.md)       | first — what is inside the process the protector wraps      | 20 min    |
| [01 how Uriel protects the client](01-how-uriel-protects-the-client.md) | next — it sets up the problem                               | 30 min    |
| [02 unpacking offline](02-unpacking-offline.md)                         | you want to see a protector defeated with a histogram       | 45 min    |
| [03 the patcher pipeline](03-patcher-pipeline.md)                       | you want to run or modify the tool                          | 20 min    |
| [04 offsets and ini files](04-offsets-and-ini-files.md)                 | you need an address in the binary and refuse to hardcode it | 60 min    |
| [05 the stub DLL](05-stub-dll.md)                                       | you want to know what runs inside the game process          | 60 min    |
| [06 the mod framework](06-mod-framework.md)                             | you want to write a mod                                     | 40 min    |
| [07 mods catalogue](07-mods-catalogue.md)                               | you want to see what has been built with it                 | reference |
| [08 glossary](08-glossary.md)                                           | whenever a word is unfamiliar                               | reference |

If you only have an hour: read 00b, 01 and 02. They contain the whole idea.

Each tutorial page has **See it yourself** boxes: a free tool, the exact clicks, and a description of what you should be looking at. The screenshots for those boxes are still being added; until they are, each box carries a text placeholder that says what the capture shows, so you know what to look for on your own screen. Document 01 opens with the list of tools to install (PE-bear, Detect It Easy, a hex editor, Python, System Informer, x32dbg, Ghidra); have them ready before you start.

## The idea in one paragraph

The protector encrypts the game's code with a single 4096-byte key reused on
every page. Reusing a key is the one thing a stream cipher must never do: with
seventeen thousand pages of x86 code, the most common byte at every position
_is_ the key, this is a very well known vulnerability in cryptography. The same
key is used to encrypt the import names as well, which the protector leaves on disk.
The entry point has a recognisable shape. So the whole executable can be restored from
the file alone, in a few seconds, without ever running it. Once it is restored,
every address the modding layer needs is re-derived from stable anchors (strings, imports, instruction shapes) so that
nothing breaks when the game updates, and a small DLL takes the anti-cheat's
place inside the process effectively hijacking it and hosts Python mods.

## How to use the code while reading

Every document points at the exact function it is describing. Keep the file
open beside the text. The tools are small enough to read whole:

| file                                      | lines        | what it is                                                                              |
| ----------------------------------------- | ------------ | --------------------------------------------------------------------------------------- |
| `tools/ksattack.py`                       | ~90          | the keystream attack alone, as an experiment                                            |
| `tools/attack_figures.py`                 | ~180         | draws the five figures in document 02 from a protected exe (needs numpy and matplotlib) |
| `tools/unuriel.py`                        | ~830         | the unpacker: derive, rebuild, and the live fallback                                    |
| `tools/mkoffsets.py`                      | ~1800        | every offset resolver, one method each                                                  |
| `patcher/src/triarch_patcher/pipeline.py` | ~560         | the seven steps                                                                         |
| `stub/uriel_stub.cpp`                     | ~3500        | the DLL: hooks, natives, Python bootstrap                                               |
| `mods/modhost.py`, `mods/api.py`          | ~550 + ~1300 | the mod host and the API mods call                                                      |
