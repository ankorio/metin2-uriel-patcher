# 08 — Glossary

Short definitions, in the sense the other documents use them. Alphabetical.

**Anchor.** Something stable across builds that a resolver starts from: a
string literal, an imported function, a Python method name in a table, an
instruction shape. Addresses are derived *from* anchors, never written down.

**ASLR (address space layout randomisation).** The loader may map an image at
a different base address each run. The rebuild clears the flag so the client
always loads at its preferred base (`0x400000`) and file addresses equal
runtime addresses.

**Calling convention.** Who pushes arguments, in what order, and who cleans
the stack. On 32-bit x86 the ones that matter here are `cdecl` (caller
cleans, `ret`), `stdcall` (callee cleans, `ret imm16`) and `thiscall`
(`this` in `ecx`, callee cleans). The resolver reads the `ret imm16` to check
a native's declared argument width.

**CRT entry.** The C runtime's start function that MSVC links in front of
`main`. It has a recognisable shape (`call __security_init_cookie` then
`jmp __scrt_common_main_seh`), which is how the original entry point is found.

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

**Keystream.** The 4096 bytes XORed over every page of `.text`. Recovered
from ciphertext alone because it is reused.

**Many-time pad.** A one-time pad whose key is reused. Reuse turns the
cipher into a substitution per key position, which statistics break.

**Native.** A function the stub exposes to Python, either implemented in the
stub (`triarch_native.*`) or a game function called through the gateway
declared in `mods/natives.json`.

**OEP (original entry point).** The entry point before the protector moved
it. See *entry point*.

**Offset resolver.** One method of `tools/mkoffsets.py` that turns an anchor
into an address on the build in front of it.

**Page.** 4096 bytes. The unit the protector encrypts and decrypts, and the
unit the keystream tiles over.

**PE (Portable Executable).** The Windows executable format: DOS stub, PE
signature, file header, optional header (with the data directories), section
table, then the sections.

**Profile (`profile.json`).** The unpacker's intermediate record: keystream,
import table (slot → DLL, name), entry point, source hash. Produced by
`derive` (offline) or `harvest` (live); consumed by `rebuild`.

**PyMethodDef table.** The array a C extension module hands CPython: method
name, C function pointer, flags. The game embeds CPython and registers its
own modules this way, which makes the tables an excellent anchor: a Python
name next to a native address.

**RVA (relative virtual address).** An offset from the image base. File
offset is where the bytes sit in the file; VA is RVA plus base. Conversions
go through the section table.

**Section.** A named region of the image (`.text` code, `.rdata` read-only
data, `.data`, `.rsrc`, `.reloc`, plus whatever a protector injects) with its
own permissions.

**Stub (uriel_stub.dll).** The DLL that takes the anti-cheat's place. The
rebuilt exe imports it under the protector's old entry name, so the one call
the game makes into the anti-cheat lands in the stub.

**Trampoline.** The stolen prologue bytes plus a jump back, so a hooked
function can still be called through the hook.

**Typed field.** A struct member exposed to Python by name, address and type
through the ini (`api.field("name")`), rather than a function.

**VEH (vectored exception handler).** A process-wide first-chance exception
handler. Uriel uses one to catch execution faults on the non-executable
`.text` pages and decrypt them on demand.

**Xref (cross-reference).** Every place that references an address or a
string. Finding the code that uses a string is the most common way to find a
function without symbols.
