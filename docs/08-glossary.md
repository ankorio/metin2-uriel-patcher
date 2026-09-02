# 08 — Glossary

Short definitions, in the sense the other documents use them. Alphabetical.

**Anchor.** Something stable across builds that a resolver starts from: a
string literal, an imported function, a Python method name in a table, an
instruction shape. Addresses are derived *from* anchors, never written down.

**ASLR (address space layout randomisation).** The loader may map an image at
a different base address each run. The rebuild clears the flag so the client
always loads at its preferred base (`0x400000`) and file addresses equal
runtime addresses.

**Atlas.** The full-map window the client opens over the minimap. The
`minimap_mark` stub function places a mark on both; the stock binding
`miniMap.AddWayPoint` reaches the atlas only.

**Calling convention.** Who pushes arguments, in what order, and who cleans
the stack. On 32-bit x86 the ones that matter here are `cdecl` (caller
cleans, `ret`), `stdcall` (callee cleans, `ret imm16`) and `thiscall`
(`this` in `ecx`, callee cleans). The resolver reads the `ret imm16` to check
a native's declared argument width.

**Capstone.** The disassembler library `tools/mkoffsets.py` uses where a
byte pattern is not enough: reading a `ret imm16` and listing a function's
calls need real instruction boundaries. Bundled into the patcher with
`--collect-all capstone` because it ships a native library.

**CRT entry.** The C runtime's start function that MSVC links in front of
`main`. It has a recognisable shape (`call __security_init_cookie` then
`jmp __scrt_common_main_seh`), which is how the original entry point is found.

**Cython.** A compiler from Python source to C extension modules. The
client's own UI and game logic are Python modules compiled with it, which is
why they have no readable source and why some of their classes refuse
attribute assignment (what `probe_ui` tests).

**Data directory.** A table in the PE optional header pointing at the import
directory, the IAT, relocations, the load config and so on. Uriel repoints the
import directory but leaves the IAT directory alone.

**Detour / inline hook.** Overwrite the first bytes of a function with a jump
to your own code, keep the stolen bytes in a trampoline so the original can
still be called.

**Entry point (AddressOfEntryPoint).** The RVA the loader jumps to after
mapping the image. Uriel points it into its injected section; the rebuild
points it back at the CRT entry.

**Forwarder.** An export that redirects to another DLL's export
(`KERNEL32!HeapAlloc` → `ntdll!RtlAllocateHeap`). A live harvest sees the
final address and may name the import after the wrong DLL; the on-disk table
has the original name.

**Gateway native.** See *Native*.

**Hint/name entry.** In a normal import table, a 16-bit hint followed by the
NUL-terminated function name. Uriel keeps the entries but stores the name
*length* in the hint word and XORs the name bytes.

**Hot reload.** The mod host watches `mods/` and re-imports a mod when its
file changes. `modhost.py` itself is exempt because it is the watcher.

**IAT (import address table).** The array of function pointers the loader
fills in at start-up; code calls imports through it (`call [slot]`). One
thunk array per DLL, each NUL-terminated.

**Import descriptor.** One 20-byte record per imported DLL: name, the
original-first-thunk array (names), the first-thunk array (the IAT slots).
The part Uriel actually removes.

**Int3 padding.** The `0xCC` (`int3`) bytes MSVC places between functions.
`func_start()` walks back to a `CC CC` run to find where a function begins,
`find_oep()` requires one before the entry, and their sheer frequency in
code is one reason the many-time pad breaks.

**Keystream.** The 4096 bytes XORed over every page of `.text`. Recovered
from ciphertext alone because it is reused.

**Many-time pad.** A one-time pad whose key is reused. Reuse turns the
cipher into a substitution per key position, which statistics break.

**Metin stone.** A large stationary monster object (race 8005 on the
reference server) that spawns mobs while it is attacked. The `hunt_stones`
field tells the engine's hunt loop whether to target them; `nocollide` keeps
them solid through the `actor_pass` race band.

**Naked function.** A `__declspec(naked)` C++ function for which the
compiler emits no prologue or epilogue; the body is inline assembly that
manages the stack itself. The stub's vtable slots and hook thunks are naked
because each must clean exactly the bytes the game pushed (`ret N`) and
preserve exactly the registers the game expects.

**Native.** Used in two senses, which the documents keep apart with two
names. A **stub function** is one of the 22 entries the DLL registers on the
`triarch_native` module (`call`, `field`, `actors`, `http_post`, ...),
implemented in C++ in `stub/uriel_stub.cpp`. A **gateway native** is an
engine function declared in `mods/natives.json` — eight on the shipped
registry — and reached through the `call` stub function, with its address and
stack width resolved per build into `uriel_natives.ini`. A log line such as
`8 native(s) armed` counts gateway natives.

**NOP sled.** A run of `0x90` (`nop`) bytes; execution that lands anywhere
in it slides to the end. Uriel fills its injected section with one and the
protector's DLL patches a jump into it at runtime; on disk it is all `90`.

**OEP (original entry point).** The entry point before the protector moved
it. See *entry point*.

**Offset resolver.** One method of `tools/mkoffsets.py` that turns an anchor
into an address on the build in front of it.

**Page.** 4096 bytes. The unit the protector encrypts and decrypts, and the
unit the keystream tiles over.

**PE (Portable Executable).** The Windows executable format: DOS stub, PE
signature, file header, optional header (with the data directories), section
table, then the sections.

**Phase / phase window.** The client's top-level state machine — login,
character select, loading, game — as a number; phase 5 is the game.
`m2netm2g.GetPhaseWindow(5)` returns the game window object. Some bindings
demand it as an argument (`SetTarget(vid, window)`) as an anti-bot gate, and
mods hook keys by wrapping its `OnKeyDown`. `api.in_game()` polls it.

**Profile (`profile.json`).** The unpacker's intermediate record: keystream,
import table (slot → DLL, name), entry point, source hash. Produced by
`derive` (offline) or `harvest` (live); consumed by `rebuild`.

**PyInstaller.** Freezes a Python program and its dependencies into one
executable. `patcher/build.bat` uses it to produce `TriarchPatcher.exe`, with
`--noupx` so the bundled stub DLL's bytes are left untouched.

**PyMethodDef table.** The array a C extension module hands CPython: method
name, C function pointer, flags. The game embeds CPython and registers its
own modules this way, which makes the tables an excellent anchor: a Python
name next to a native address.

**Race.** The numeric type of an actor: a mob species, a player class, a
mount, a metin stone. What the engine's `GetRace` getter returns and what
`IsBlockObject` exempts in ranges — the ranges `actor_pass` rewrites.
`api.actors(races=...)` filters on it.

**RVA (relative virtual address).** An offset from the image base. File
offset is where the bytes sit in the file; VA is RVA plus base. Conversions
go through the section table.

**Section.** A named region of the image (`.text` code, `.rdata` read-only
data, `.data`, `.rsrc`, `.reloc`, plus whatever a protector injects) with its
own permissions.

**Singleton / holder.** The engine keeps one instance of each manager class
(`CPythonPlayer`, `CPythonCharacterManager`, `CPythonItem`, ...) behind a
global pointer, the *holder*. The ini names holder addresses (`kPlayerInst`,
`kItemInst`); the object is `**(holder)`, looked up per call and never cached,
because it does not exist on the login and character-select screens.

**Stub function.** See *Native*.

**Stub (uriel_stub.dll).** The DLL that takes the anti-cheat's place. The
rebuilt exe imports it under the protector's old entry name, so the one call
the game makes into the anti-cheat lands in the stub.

**Tail hook.** A hook that observes a function without changing it: a naked
thunk saves every register and flag, calls a counter, restores them and jumps
into the trampoline. It touches neither the arguments nor the return address,
so it works for any calling convention. The four auth-tracing hooks are tail
hooks.

**Trampoline.** The stolen prologue bytes plus a jump back, so a hooked
function can still be called through the hook.

**Typed field.** A struct member exposed to Python by name, address and type
through the ini (`api.field("name")`), rather than a function.

**VEH (vectored exception handler).** A process-wide first-chance exception
handler. Uriel uses one to catch execution faults on the non-executable
`.text` pages and decrypt them on demand.

**VID (virtual id).** The per-session number the server assigns to every
character the client can see — players, mobs, NPCs. Bindings and stub
functions identify actors by it (`SetTarget(vid)`, `actor_at(i)` returns a
VID, the auto-attack target is a VID). It changes on respawn and reconnect.

**Vnum (virtual number).** The id of an item or mob *type* in the server's
data tables — an item vnum, a mob vnum. Stable across sessions and servers
where names are localised, which is why configs prefer vnums to names.

**Xref (cross-reference).** Every place that references an address or a
string. Finding the code that uses a string is the most common way to find a
function without symbols.

## x86 byte patterns used in this repo

The documents quote raw opcode bytes when an anchor or a hook is defined by
them. These are the ones that occur, with what each means in this project.
`rel32` is a signed 32-bit displacement from the end of the instruction,
`imm32` an absolute 32-bit value, `disp32` a 32-bit displacement added to a
register.

| Bytes | Instruction | Where it appears here |
|---|---|---|
| `E8 rel32` | `call rel32` — call, target = next instruction + rel32 | the CRT entry's first instruction; `callers()` and `_fn_calls()`; the OEP finder's pass over `.text` |
| `E9 rel32` | `jmp rel32` | the CRT entry's second instruction; the five-byte detour the stub writes over a hooked prologue |
| `FF 15 imm32` | `call dword ptr [imm32]` — call through a pointer slot | every call through the IAT; `iat_callers()`; the protector call sites `rebuild` rewrites |
| `FF 25 imm32` | `jmp dword ptr [imm32]` | the other IAT form (tail calls into an import) |
| `68 imm32` | `push imm32` | pushing a string literal's address — the string cross-reference anchor (`pushes_of`) |
| `A1 imm32` | `mov eax, [imm32]` | reading a singleton holder (`kPlayerInst`, `kMiniMapInst`, `kItemInst`) |
| `8B 81 disp32` | `mov eax, [ecx+disp32]` | reading a member off `this`; the `block_movement()` shape — the disp32 *is* the struct offset |
| `C7 81 disp32 imm32` | `mov dword ptr [ecx+disp32], imm32` | writing a constant into a member: the `skip_collision()` sentinels `0xA35F5` / `0xF35F5` |
| `C2 imm16` | `ret imm16` — return and pop imm16 bytes | the callee-cleans conventions; `ret_imm()`'s cross-check of a native's stack width; `SLOT2_ARG_BYTES` |
| `C3` | `ret` | a `cdecl` return; `terrain_pass` writes one over `BlockMovement`'s first byte |
| `CC` | `int3` | function padding; `func_start()` and `find_oep()` rely on it |
| `55 8B EC` | `push ebp; mov ebp, esp` | the ordinary MSVC prologue (figure 5 in document 02; `kNetPrologue` starts with it) |
| `53 8B DC` | `push ebx; mov ebx, esp` | the stack-aligned prologue; the auth functions and `RecvOfflineshopPacket` open with it |
| `89 0D imm32` | `mov [imm32], ecx` | the single store to `__security_cookie` that identifies `__security_init_cookie` |
| `90` | `nop` | the NOP sled in the injected section |
