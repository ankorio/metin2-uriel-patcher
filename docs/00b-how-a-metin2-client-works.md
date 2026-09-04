# 00b — How a Metin2 client works

Read this before the step-by-step documents. Everything the toolkit does
(the unpacker, the offset resolver, the stub, the mod host) targets one
specific component which is the same for every game in the Metin2 family.

## Contents

1. [Where these clients come from](#where-these-clients-come-from)
2. [The process, from the launcher to the game](#the-process-from-the-launcher-to-the-game)
3. [Inside the executable: C++ engine plus embedded Python](#inside-the-executable-c-engine-plus-embedded-python)
4. [The binding tables: where C++ meets Python](#the-binding-tables-where-c-meets-python)
5. [One thread, one frame loop](#one-thread-one-frame-loop)
6. [Entities: VIDs, actors, the player, the ground](#entities-vids-actors-the-player-the-ground)
7. [The network: phases, packets, encryption](#the-network-phases-packets-encryption)
8. [Assets: pack files and scripts](#assets-pack-files-and-scripts)
9. [Where the toolkit plugs in](#where-the-toolkit-plugs-in)

## Where these clients come from

Metin2 is a 2004 Korean MMORPG whose client and server source became public
many years ago. Almost every private server, and modernised client
built them, descend from that code. The class names survive
(`CPythonApplication`, `CPythonNetworkStream`, `CInstanceBase`,
`CActorInstance`, `EterPack`), and so does the architecture: a C++ engine
that embeds a Python interpreter and hands it the game logic and the whole
user interface.

The client this repository studies is one such descendant, that has been heavily updated:
the renderer is bgfx rather than the original Direct3D 8, the embedded
interpreter is CPython 3.14 rather than Python 2.2, the Python scripts are
compiled with Cython instead of shipped as source, the network layer uses
XTEA and protocol buffers, and a commercial anti-cheat wraps the executable.
None of that changes the architecture below. They only change how hard each part is to
see and reverse engineer, which is what the rest of the documents are about.

## The process, from the launcher to the game

```
TriarchLauncher.exe          a small updater with an embedded web view
      │  checks the manifest, patches pack files, then spawns
      ▼
triarch.exe --game           the client proper (32-bit Windows, ~97 MB)
      │  the Windows loader maps its imports first:
      ├─ client_x86.dll      the anti-cheat ("Uriel"), the ONLY import listed on disk
      │                      it decrypts the code and rebuilds the real import table,
      │                      then jumps to the game's real entry point
      └─ KERNEL32, USER32, WS2_32, ...   the 22 real DLLs, bound at runtime by the anti-cheat
```

The important consequence for reverse engineering: `triarch.exe` as it sits
on disk is not a runnable program. Its code section is encrypted, its import
table names one DLL, and its entry point lands in a block of NOPs. Document
01 describes those four changes; Document 02 reverts them without execution.

## Inside the executable: C++ engine plus embedded Python

Two worlds share one process:

| world          | written in                                           | responsible for                                                                                                                                                                                                                                         |
| -------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **engine**     | C++                                                  | the window, the frame loop, rendering (bgfx), the virtual file system over pack files, networking, the entity model (actors, items, effects), pathfinding and collision, sound (Miles), animation (Granny), trees (SpeedTree), crash reporting (Sentry) |
| **game layer** | Python, running inside the engine's embedded CPython | login and character-select screens, every window of the in-game UI, inventory and shop logic, skill bars, chat, quest dialogs, the glue that turns a button click into a packet                                                                         |

The engine creates the interpreter at start-up, registers its own modules
into it, and then imports a root script that builds the UI. From that point
the two call each other constantly: Python calls `net.SendAttackPacket(...)`
and `player.GetItemCount(...)`; C++ calls back into Python for every UI
event and every frame.

The Python side is not shipped as `.py` files. Some 516 modules are
compiled with Cython into native code and registered as built-in modules,
so `import uiinventory` resolves to C++ code that Cython generated from the
original script. The names of every function, class and variable are preserved in
that code, which is why string searches remain so effective on this
client. A fallback loader can still import plain `.py`/`.pyc` files from the
pack root, and that loader is vulnerability the mod host uses (document
05).

## The binding tables: where C++ meets Python

Every module the engine exposes to Python is declared the way any C
extension declares itself: a table of entries, each holding a **method
name** (a C string), a **function pointer**, and flags. CPython calls this
array a `PyMethodDef` table. The client has dozens of them, one per
module: `app`, `player`, `net`, `chat`, `wndMgr`, `item`, `skill`,
`background`, `miniMap`, `chr`, `systemSetting`, and so on, a few thousand
entries in total.

For this project those tables are the single most valuable structure in the
binary:

- They pair a **readable name** with a **native address**, on a build that
  otherwise has no symbols. `net.SendChatPacket` next to a pointer is a
  symbol by another name.
- They are **data in `.rdata`**, which the protector does not encrypt, so
  they are readable even in the protected file.
- The functions they point at are small wrappers with a fixed shape (parse
  the Python arguments, call the real engine method, build a return value),
  which makes the engine method one hop away.

Document 04 shows how the offset resolver walks these tables to find
functions and struct offsets without ever saving any of the addresses, and
Document 05 shows the stub registering a table of its own
(`triarch_native`) to hand mods new functions.

## One thread, one frame loop

The engine is single-threaded in the way that matters. One function, called
once per frame from the message loop, does everything in a fixed order:

```
per frame:
   pump the network socket           receive packets, dispatch each by header
   update the world                  move actors, advance animations, expire effects
   run the Python side               UI update callbacks, timers, the mod host's tick
   render                            bgfx draws the scene and the UI
```

Two things follow. First, a hook on any per-frame function gives you a
reliable heartbeat on the main thread, and the stub uses exactly one such
place (a virtual-table slot the anti-cheat is called through every frame) to
run the mod host. Second, everything a mod does runs on that thread, in
between two frames: there is no locking to think about, but a mod that
takes 200 ms takes 200 ms out of a frame. Document 06 builds its rules on
this.

## Entities: VIDs, actors, the player, the ground

The server describes the world to the client as a set of **entities**, each
identified by a **VID** (a 32-bit "virtual id" the server assigns). The
client keeps them in a manager keyed by VID:

- **Actors** (`CInstanceBase`, and the animated `CActorInstance` under it):
  players, monsters, NPCs, guards, pets, and _metin stones_ (the
  destructible rocks the game is named after). Each carries a race number
  (which model), a type (player, monster, NPC, stone...), a position, a
  state such as "walking" or "attacking", and per-actor flags such as
  collision.
- **The player** is one actor singled out into a singleton (`CPythonPlayer`)
  that also knows inventory, skills, target, auto-move destination and the
  rest of what only _you_ have.
- **Ground items** are a separate manager: item vnum (the item type number),
  position, owner, and the VID the server gave the drop.
- **Maps** are tiles with a height field and an attribute layer (which cells
  block movement; this is what "terrain collision" means). Each map runs on
  several **channels**: identical copies on different game-server
  processes, chosen at login and switchable in game.

Almost every mod in Document 07 is a loop over one of these managers:
"nearest monster of race X", "any ground item worth picking up", "am I
still walking".

## The network: phases, packets, encryption

The client speaks a plain TCP protocol to two kinds of server:

1. **Auth server**: takes the login, returns a key for the game server.
2. **Game server** (one per channel): everything else, from character
   select to the last packet before logout.

The conversation moves through **phases** (login, select, loading, game),
and each phase accepts a different set of packets. A packet is a one-byte
**header** followed by a body whose length the header implies (fixed) or a
size field states (variable). The source names them `HEADER_CG_*` for
client-to-game and `HEADER_GC_*` for game-to-client, and those names are
still in the binary as strings. Every packet on the wire is encrypted with a
session key (XTEA in this fork) and carries a sequence byte, so a replayed
or invented packet is detectable.

The stub hooks the receive and send paths (Document 05) to observe traffic,
and our framework deliberately exposes no way to build a packet by hand.
The server side logs what it sees, and a malformed packet from a real
account is a fingerprint. Nothing in this repository sends anything the
unmodified client would not send.

## Assets: pack files and scripts

Models, textures, maps, sounds, and the compiled scripts live in **pack
files** under `pack/`, read through the engine's virtual file system. The
lineage's format is EterPack (an index file plus a data file, with per-file
compression and optional encryption); this client uses a reworked variant
with per-file keys. Text the UI shows lives in locale packs, one per language; the
login screen offers a dozen. These documents and the example configs use the
English pack. Mods that match by name see whatever locale the client is set
to, which is why configs prefer vnums to names.

The unpacker never touches any of this: the packs are consumed by the
running client, and the stub and mods work on the running client.

## Where the toolkit plugs in

| client part                                     | what it is                                   | what this project does with it                                                  | document |
| ----------------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------- | -------- |
| `client_x86.dll` import                         | the anti-cheat, loaded before the game       | replaced by `uriel_stub.dll`, which answers the one call the game makes into it | 02, 05   |
| `.text` encryption                              | one 4096-byte key tiled over ~17,000 pages   | recovered from ciphertext statistics                                            | 02       |
| on-disk import table                            | left in place, names XORed with the same key | decoded and rebuilt into a normal import directory                              | 02       |
| entry point                                     | redirected into a NOP sled                   | found again by the CRT start-up shape                                           | 02       |
| `PyMethodDef` tables                            | name/function pairs for every Python module  | the anchors for the offset resolver; the model for the stub's own module        | 04, 05   |
| per-frame slot the anti-cheat is called through | the heartbeat                                | where the stub runs the mod host                                                | 05       |
| embedded CPython + fallback loader              | runs the game's UI                           | runs `modhost.py` and the mods                                                  | 05, 06   |
| entity managers, player singleton               | the world as the server describes it         | the `api` namespaces mods read                                                  | 06       |
| send/receive paths                              | the protocol                                 | observed, never forged                                                          | 05       |

If one sentence has to carry this document: **the client is a C++ engine
that embeds Python and publishes its own API to it through tables in a data
section; the protector encrypts the code but not those tables, and the
toolkit lives in the gap.**
