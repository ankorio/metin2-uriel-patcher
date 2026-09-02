# 00 — Start here

This repository is a complete, working example of taking a protected Windows
game client apart and putting a modding framework inside it. It is written to
be read in order. Each document assumes the ones before it and explains every
new concept the first time it appears.

## What you need to know already

- **One programming language well.** The tools are Python, the stub is C++,
  the mods are Python. If you have written Java, Kotlin or C#, you will read
  all of it.
- **What a function call is at the machine level.** Arguments go somewhere
  (the stack, on 32-bit x86), a `call` instruction jumps, a `ret` comes back.
  You do not need to be able to write assembly; you need to be able to read
  a dozen instructions with a reference open.
- **Hexadecimal.** Addresses and offsets are written in hex throughout.

You do **not** need prior experience with PE files, debuggers, disassemblers,
or anti-cheat systems. That is what the case study teaches.

## Reading order

| document | read it when | time |
|---|---|---|
| [01 how Uriel protects the client](01-how-uriel-protects-the-client.md) | first — it sets up the problem | 30 min |
| [02 unpacking offline](02-unpacking-offline.md) | you want to see a protector defeated with a histogram | 45 min |
| [03 the patcher pipeline](03-patcher-pipeline.md) | you want to run or modify the tool | 20 min |
| [04 offsets and ini files](04-offsets-and-ini-files.md) | you need an address in the binary and refuse to hardcode it | 60 min |
| [05 the stub DLL](05-stub-dll.md) | you want to know what runs inside the game process | 60 min |
| [06 the mod framework](06-mod-framework.md) | you want to write a mod | 40 min |
| [07 mods catalogue](07-mods-catalogue.md) | you want to see what has been built with it | reference |
| [08 glossary](08-glossary.md) | whenever a word is unfamiliar | reference |
| [09 lab setup](09-lab-setup.md) | before you run anything on a machine you care about | 20 min |

If you only have an hour: read 01 and 02. They contain the whole idea.

Each tutorial page has **See it yourself** boxes: a free tool, the exact clicks, and a screenshot of what you should be looking at. Document 01 opens with the list of tools to install (PE-bear, Detect It Easy, a hex editor, Python, System Informer, x32dbg, Ghidra); have them ready before you start.

## The idea in one paragraph

The protector encrypts the game's code with a single 4096-byte key reused on
every page. Reusing a key is the one thing a stream cipher must never do: with
seventeen thousand pages of x86 code, the most common byte at every position
*is* the key. The same key obfuscates the import names, which the protector
left in place on disk. The entry point has a recognisable shape. So the whole
executable can be restored from the file alone, in a few seconds, without ever
running it. Once it is restored, every address the modding layer needs is
re-derived from stable anchors (strings, imports, instruction shapes) so that
nothing breaks when the game updates, and a small DLL takes the anti-cheat's
place inside the process and hosts Python mods.

## How to use the code while reading

Every document points at the exact function it is describing. Keep the file
open beside the text. The tools are small enough to read whole:

| file | lines | what it is |
|---|---|---|
| `tools/ksattack.py` | ~80 | the keystream attack alone, as an experiment |
| `tools/unuriel.py` | ~750 | the unpacker: derive, rebuild, and the live fallback |
| `tools/mkoffsets.py` | ~1800 | every offset resolver, one method each |
| `patcher/src/triarch_patcher/pipeline.py` | ~560 | the seven steps |
| `stub/uriel_stub.cpp` | ~3500 | the DLL: hooks, natives, Python bootstrap |
| `mods/modhost.py`, `mods/api.py` | ~550 + ~1300 | the mod host and the API mods call |

## A note on honesty

The documents record what was measured, and where an earlier belief turned
out wrong they say so (the README of the patcher once explained at length why
the game had to run to be unpacked; it did not). Reverse engineering is mostly
the discipline of not trusting your own first reading of a disassembly.
When you extend this project, keep that habit: a `mov` you found is not proof
it executes until you have watched it execute.
