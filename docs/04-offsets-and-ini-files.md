# 04 - Offsets and the two ini files

How every engine address the stub needs is re-derived per build by
`tools/mkoffsets.py`, and exactly how `uriel_offsets.ini` and
`uriel_natives.ini` come out of it.

## Contents

1. [The rule: never hardcode an address](#1-the-rule-never-hardcode-an-address)
2. [Addresses, three ways: VA, RVA, file offset](#2-addresses-three-ways-va-rva-file-offset)
3. [Anchor techniques, with the code that uses them](#3-anchor-techniques-with-the-code-that-uses-them)
4. [Catalogue: every key `resolve_all()` produces](#4-catalogue-every-key-resolve_all-produces)
5. [`uriel_offsets.ini`](#5-uriel_offsetsini)
6. [`uriel_natives.ini` and `mods/natives.json`](#6-uriel_nativesini-and-modsnativesjson)
7. [Build constants stamped into `mods/config.json`](#7-build-constants-stamped-into-modsconfigjson)
8. [Tutorial: adding a new resolver](#8-tutorial-adding-a-new-resolver)
9. [CLI usage and diffing two builds](#9-cli-usage-and-diffing-two-builds)

---

## 1. The rule: never hardcode an address

The stub (`stub/uriel_stub.cpp`) hooks, calls and reads roughly seventy
locations inside the game executable. Each is a number, and every one differs
on every build of the game. Not only code addresses: the executable is C++, so
when a class gains a member every field after it shifts. The resolver's own
baseline table records the "use skills" byte at `0x501D2` on one build and
`0x501E2` on another; the skill vector at `0x50164` versus `0x50174`.

So the project has one hard invariant: **no address is ever typed in**. Every
value is re-derived, per build, from an *anchor* - something a recompilation
cannot move:

- a literal string the code references (`"SYSERR: "`),
- an import name (`GetCurrentHwProfileA`),
- an instruction *shape* with the volatile parts wildcarded,
- a Python method name in an embedded `PyMethodDef` table,
- a structural relationship ("the only caller of X", "the callee that calls Y").

The resolver lives in `tools/mkoffsets.py`; the patcher imports a byte-identical
copy (`patcher/src/triarch_patcher/vendor/mkoffsets.py`) and runs
`resolve_all()` against the decrypted executable it has just produced. The
stub contains one constant, the preferred image base; its comment says
everything else is "regenerated per build ... never hardcode one here".

Why so strict? A wrong address is a *silent* bug: a hook at the wrong place
never fires or corrupts something unrelated, a struct read at the wrong offset
returns a plausible integer, and nothing throws. So the resolver fails
*closed*: a shape that matches twice returns nothing (`block_movement()`:
"ambiguity here would mean patching an arbitrary function"), and an unresolved
key is *omitted* rather than written as zero - the stub treats an absent key as
"feature off" but `0` as an address.

**Prefer measuring to inferring.** A disassembly shows what *could* execute.
`mkoffsets.py` records several answers that looked right in a listing and were
wrong at runtime: a first `py_glue()` matched a byte shape and returned the
wrong `InitModule`; a shape-match for `CreateAutoBotSettings` found "the first
of many functions with that shape"; an unbounded call scan "silently merged the
NEXT function's calls". Each was caught by running the client with a trace
attached. Trust a value once it has been *observed* doing the job.

## 2. Addresses, three ways: VA, RVA, file offset

- **File offset** - a byte index into the `.exe` on disk. `bytes.find()` works
  in this space.
- **RVA** (relative virtual address) - a byte's position once the file is
  mapped into memory, relative to where the image starts. Section headers,
  import tables and PE data directories speak RVA.
- **VA** (virtual address) - RVA plus the image base. The rebuilt
  `triarch_clean.exe` has ASLR disabled (`patcher/.../pipeline.py`, step
  `rebuild`: "decrypted, imports restored, ASLR off"), and the stub declares
  `kImageBase = 0x00400000`. All numbers in both ini files are VAs against that
  base.

The conversion lives in `tools/namemods.py`, class `Image`. `load_pe()` reads
the PE header (`e_lfanew` at `0x3C`, section count, optional header size, image
base at `+0x34`) and builds a list of `(name, section VA, virtual size, raw
offset, raw size)`. Then:

```python
def off(self, va):                      # VA -> file offset, or None
    for _n, sva, vs, ro, rs in self.secs:
        if sva <= va < sva + max(vs, rs):
            delta = va - sva
            if delta < rs:
                return ro + delta
            return None                 # in the zero-initialised tail
    return None
```

The `None` branch matters: a section's *virtual* size can exceed its size on
disk (uninitialised globals). `Resolver._in_image()` exists because the game's
singleton holders "live past the raw end of `.data`" - valid VAs with no file
offset at all. The reverse mapping for `.text` is `self.tva + (o - self.tro)`.

At runtime the stub's `Abs(va)` computes `GetModuleHandle(NULL) + (va -
kImageBase)`, so the ini stays in VA form and remains right even if the image
were relocated.

## 3. Anchor techniques, with the code that uses them

Every technique below is quoted from a real resolver in `tools/mkoffsets.py`.

### 3.1 String cross-reference

The oldest trick. A C string is stored once in `.rdata`; the code that uses it
pushes its VA as a 32-bit immediate (`68 imm32`). Find the string, find the
pushes, walk back to the function start.

```python
def trace_real(self):
    """__TraceError: the only function that pushes the "SYSERR: " literal."""
    va = self.find_str("SYSERR: ")
    for site in self.pushes_of(va):
        f = self.func_start(site)
        if f:
            return f
```

`find_str` appends the NUL terminator so `"SYSERR: "` cannot match inside a
longer string. `pushes_of` is a C-speed `bytes.find` for `68` + the packed VA
over `.text`. `func_start` walks backwards to a run of two `int3` bytes
(`CC CC`) - MSVC pads between functions with `int3` - then checks the next byte
is a plausible prologue start. Its docstring says why prologue matching alone
is wrong: stack-aligned functions open with `53 8B DC`, not `55 8B EC`.

Two refinements recur. `trace_sink()` counts which *call target* most often
follows pushes of the `PythonApplication.cpp` path, then insists it be a bare
`ret 0` (`C2 00 00`). `auth_recv_phase()` has five functions pushing
`"__AuthState_RecvPhase ERROR!"` and keeps the one that also calls the
function that calls `GetCurrentHwProfileA`. One anchor is a hint; two agreeing
anchors are an identification.

### 3.2 Import-slot cross-reference

An import is a pointer slot in the Import Address Table; code calls through it
with `FF 15 imm32` (`call dword ptr [slot]`) or `FF 25` (`jmp`). `iat()` walks
the import directory by hand and returns `{name: slot VA}`; that dictionary is
emitted directly as the nine `kIat*` keys and drives `fn_calling_import()`:

```python
def fn_calling_import(self, iatmap, name):
    slot = iatmap.get(name)
    c = self.iat_callers(slot)          # every FF15/FF25 that names the slot
    return self.func_start(c[0]) if c else None
```

That is the whole derivation of `kGetPcName` (`GetComputerNameW`) and
`kGetHwProfileId` (`GetCurrentHwProfileA`).

### 3.3 Instruction-shape scanning

The strongest anchor is a sequence of instructions whose *opcodes* are fixed
but whose operands (struct offsets, constants) are read out at match time.
`block_movement()` matches

```
8B 81 ?? ?? ?? ??     mov  eax, [ecx+off32]
85 C0                 test eax, eax
0F 45 C8              cmovne ecx, eax
E9 ..                 jmp  <tail>
```

by scanning for `8B 81`, checking the six bytes after the wildcard, and
demanding the shape occur **exactly once** in `.text`:

```python
if len(hits) != 1:
    return {}
```

Shapes are matched on raw bytes because that is fast on a 68 MB section.
Two places need real instruction boundaries and use capstone: reading a
`ret imm16` (`ret_imm()`), and listing a function's calls (`_fn_calls()`).
The second one was a byte walker until the 2026-08-30 build: it took every
`E8` byte as a call opcode, and a `push` of a string literal whose address
happened to contain `E8` (`68 74 E8 13 05`) produced a phantom call whose
five-byte skip stepped over the real `call Py_BuildValue` right behind it.
Two bindings that had resolved on every earlier build reported zero calls.
A byte pattern is a heuristic; when the anchor is "the calls this function
makes", decode the instructions.

`skip_collision()` anchors on *data* the developers chose: the actor-collision
switch is a magic dword, `0x000A35F5` to enable and `0x000F35F5` to disable.
Searching for `C7 81 ?? ?? ?? ?? F5 35 0A 00` (`mov dword [ecx+off32],
0xA35F5`) yields the function *and* the struct offset in one match.

### 3.4 PyMethodDef tables

The client embeds CPython and registers its own modules (`playerm2g2`,
`miniMap`, `chat`, ...). Each module has a `PyMethodDef[]` array whose entries
are `{const char* name; PyCFunction fn; int flags; const char* doc}` - 16 bytes
in a 32-bit build. The method *name* is a real NUL-terminated string, so it
survives every rebuild. `_py_tables()` harvests every table:

```python
for off in range(0, len(blob) - 16, 4):
    nmp, fn, fl, doc = struct.unpack_from("<IIII", blob, off)
    if not (nmp and fn) or fl not in ok or not (tlo <= fn < thi):
        continue                        # fn must land in .text, flags plausible
    s = self._cstr(nmp)                 # name must be identifier-like
```

and groups consecutive 16-byte entries into tables. `binding(module, method)`
finds a table's registration site - the game pushes the module name as an
immediate right after the table, `68 <table> 68 <name> E8 <InitModule>` -
checks the pushed name equals `module`, and returns the VA for `method`. This
is how `AttackPickedActor`, `PickCloseItem`, `AddWayPoint` and every native are
reached: by name, "the way a human would".

`tools/namemods.py` handles tables referenced from a `PyModuleDef`
(`m_methods` at +32, `m_name` at +20: name pointer = methods pointer - 12) or a
`PyTypeObject` (`tp_methods` +116, `tp_name` +12: -104). It labels a JSON dump
of anonymous tables.

### 3.5 Struct offsets and vtable slots read out of instructions

Once a function is located, its body gives up the layout of the objects it
touches. `attack_bridge()` reads three things out of the inlined
`AttackPickedActor` binding:

```
A1 xx xx xx xx        mov  eax, [imm32]         -> kPlayerInst (the singleton holder)
FF 50 xx              call [eax+imm8]           -> kGetMainInstOff (a vtable slot)
8B CF E8 rel32        mov ecx,edi ; call rel32  -> kOnPressActor
```

`autohunt_api()` takes `kPlayerStateOff` from the byte before the immediate
in `cmp [reg+disp8], 0x89`; `auto_move()` takes the state field and the
walk-state constant together from `81 7E 58 8A 00 00 00` (`cmp dword
[esi+0x58], 0x8A`), "the engine's own test for 'am I auto-moving'". Where a
value could be guessed from a fixed delta, the code reads it from an
instruction instead: "a wrong diagnostic field is worse than no diagnostic
field".

### 3.6 Structural relationships

`callers(target)` keeps every `E8` in `.text` whose displacement lands on
`target`; `_fn_calls(va)` decodes the function with capstone and lists its
direct `call rel32` targets, bounded at the first `ret` followed by padding
(and by the function body's own end, found through the `int3` padding, for
long functions: `AutoHuntLoop` grew past a fixed 0x1200-byte window in
2026-08 and its last callee silently fell outside it). With those two, `autohunt_api()` chains a
whole subsystem off one string:

```
"/auto_hunt end"   -> AutoHuntLoop            (the only function pushing it)
AutoHuntLoop       -> FindAndSetNewTarget     (its callee that calls __OnPressActor)
                   -> CPythonPlayer::Update   (its only caller)
Update             -> __Update_AutoAttack     (the callee opening with two `cmp [r+x],0`)
```

## 4. Catalogue: every key `resolve_all()` produces

"Required" means `step_offsets` in the pipeline raises `Failure` if it is
missing. "Optional (pipeline)" means it is in `OPTIONAL_OFFSETS`: the patch
succeeds and the stub disables the mod host. "Optional (omitted)" means the
resolver returns `{}` on a miss and the key simply does not appear. The last
column is what the stub does with it, taken from `stub/uriel_stub.cpp`.

### Required: anti-cheat replacement and hooks

| Key | Derived from | Stub use |
|---|---|---|
| `kTraceSink` | `trace_sink()`: most-called target after pushes of the `PythonApplication.cpp` path, must be `ret 0` | `PatchTraceSink()` redirects the dead diagnostic sink |
| `kTraceReal` | `trace_real()`: only pusher of `"SYSERR: "` | ...to this live `__TraceError` |
| `kVerifyBufsEqual` | `verify_bufs_equal()`: constant-time compare loop `8D 49 04 33 41 FC 0B`; fallback via the `HashTransformation` string | `HookVerifyBufsEqual()` - 5-byte jmp hook that recognises the sentinel digest |
| `kAuthRecvPhase` | `auth_recv_phase()`: `"__AuthState_RecvPhase ERROR!"` pusher that also calls the hw-profile function | `InstallAuthHooks()` |
| `kAuthProcess` | `auth_process()`: sole caller of `kAuthRecvPhase` | `InstallAuthHooks()` |
| `kGetPcName` | `fn_calling_import("GetComputerNameW")` | `InstallAuthHooks()` |
| `kGetHwProfileId` | `fn_calling_import("GetCurrentHwProfileA")` | `InstallAuthHooks()` |
| `kSendAppend` | `send_append()`: first `jmp`/`call` inside the callback the client registers via the stub's own `FireInTheHole` import | `InstallNetHooks()` - outgoing buffer hook |
| `kRecvBuf` | `recv_buf()`: unique 16-byte prologue + member reads `+0x24/+0x28` | `InstallNetHooks()` - incoming hook |
| `kUrielObjOffset` | `fire_in_the_hole()`: `lea esi,[edi+imm32]` before the import call | loaded as required; no further consumer in the current stub |
| `kIatConnect`, `kIatClosesocket`, `kIatSend`, `kIatRecv`, `kIatWSAGetLastError` | `iat()` | `HookIat()` in `InstallNetHooks()` (`WSAGetLastError` save-only) |
| `kIatWinHttpConnect`, `kIatWinHttpOpenRequest`, `kIatInternetOpenUrlA`, `kIatURLDownloadToFileA` | `iat()` | `HookIat()` in `InstallNetHooks()` |
| `SLOT2_ARG_BYTES` | `slot2_arg_bytes()`: pushes + `sub esp,imm8` before `call edi` in the login gate | `ApplySlot2Ret()` rewrites the stub's own `ret imm16` - not an address |

### Optional (pipeline): the mod host

| Key | Derived from | Stub use |
|---|---|---|
| `kPyRunLine` | `run_line()`: `PythonLauncher.cpp` function pushing `"RunMain Error %s"` and `Py_file_input` (`68 01 01 00 00`) | `PyRunLine()` - every line the mod host executes |
| `kPyLauncherInst` | `launcher_inst()`: `mov ecx,[imm32]` in `RunFile`'s only caller (deref **twice**) | `LauncherThis()` |
| `kPyRunFile` | `run_file()`: launcher function pushing `"file not found! %s"` | not read; intermediate for the two above |
| `kPyRunStringFlags` | `py_run_string_flags()`: `RunLine`'s first call | not read; "recorded so a future change can tell a CPython move from a game-code move" |
| `kPackMgrInst`, `kPackGet` | `pack_mgr()`: `A1 <inst>` ... `8B 08 E8` in `RunFile` | not read; harvested for a future VFS hook |

### Optional (omitted): attack bridge and autohunt (`autohunt_api()`)

| Key | Derived from | Stub use |
|---|---|---|
| `kPlayerInst` | `A1 imm32` in the `AttackPickedActor` binding | `PlayerThis()`; also `[singletons]` in the natives ini |
| `kPlayerSubObj` | constant 4 once `kPlayerInst` resolves (`mov eax,[edi+4]`) | `GetMainInst()` |
| `kGetMainInstOff` | `FF 50 xx` / `FF 90 imm32` vtable call in the same body | `GetMainInst()` vtable slot |
| `kOnPressActor` | `8B CF E8 rel32` in the same body | `AttackTick()` |
| `kAutoHuntLoop` | only pusher of `"/auto_hunt end"` | not read; root of the chain |
| `kFindAndSetNewTarget` | loop callee that calls `kOnPressActor` | `HuntTick()`; native `FindAndSetNewTarget` |
| `kMiniMapInst` | `A1 imm32` in `FindAndSetNewTarget` | hunt range read; `[singletons]` |
| `kAutoHuntRangeOff` | `movss xmm0,[eax+imm32]` there | `HuntRange()` |
| `kAnchorOff` | `add ecx,imm32` or `lea r,[r+imm32]` there | `HuntTick()`; field `hunt_anchor` |
| `kPlayerUpdate` | `func_start` of the loop's first caller | not read; intermediate |
| `kUpdateAutoAttack` | `Update` callee opening with two `83 7E` compares | native `UpdateAutoAttack` |
| `kAutoAtkVidOff` | first `cmp` disp + 4 in that head | `HuntTick()`; fields `auto_attack_vid/_target` |
| `kPlayerStateOff` | byte before imm32 `0x89` in `__OnPressActor` | field `player_state` only |
| `kHuntStoneOff` | `movzx eax, byte [edi+i32]` before the call to `FindAndSetNewTarget` | `HuntTick()`; field `hunt_stones` |
| `kItemInst` | `playerm2g2.PickCloseItem` callee opening `mov eax,[imm32]; mov edi,[eax]` | `ItemObject()` (ground items); also `[singletons]` |
| `kUseAutoSkills` | `_skill_api()`: the `"/user_horse_ride"` pusher with the gate+vector shape | `SkillTick()`; native `UseAutoSkills` |
| `kHuntUseSkillOff` | first `cmp byte [this+i32],0` in that body | `SkillTick()`; field `hunt_use_skill` |
| `kHuntSkillVecOff` | adjacent `mov r,[this+A]` / `mov r,[this+A+4]` pair | `SkillTick()`; field `hunt_skills` |
| `kHuntUseMountOff` | `cmp byte [this+gate+1],0` confirmed present | field `hunt_use_mount` only |
| `kMountedOff` | `cmp byte [eax+d8],0` right after `call [eax+kGetMainInstOff]` | `SkillTick()`; field `mounted` |

### Optional (omitted): collision, pass-through, auto-move

| Key | Derived from | Stub use |
|---|---|---|
| `kEnableSkipCollision`, `kDisableSkipCollision`, `kSkipCollisionOff` | `skip_collision()`: `C7 81 off32 imm32` with the `0xA35F5`/`0xF35F5` sentinels | `SkipCollisionSet()` |
| `kInstActorOff` | `mov ecx,[reg+imm32]` at a caller of Enable | `SkipCollisionSet()` |
| `kActorPassLoVA`, `kActorPassHiVA`, `kActorPassLo`, `kActorPassHi` | `actor_pass()`: two `call getter; cmp eax,imm32` halves joined by `jb +0x12`, both calls to the same `mov eax,[ecx+off]; ret` | `ActorPassSet()` pokes the two immediates; `Lo/Hi` are the originals for restore |
| `kActorPass2LoVA`, `kActorPass2HiVA`, `kActorPass2Lo`, `kActorPass2Hi` | the adjacent second range with the same getter | `ActorPassSet()` band carve-out |
| `kActorRaceOff`, `kActorRaceGetter` | the getter's field offset and VA | not read; diagnostic |
| `kBlockMovement` | `block_movement()`: unique `mov/test/cmovne/jmp` shape | `TerrainPassSet()` |
| `kAutoMoveActiveOff` | `auto_move()`: `mov byte [reg+off],1; call; test al,al; je; mov al,1` | field `auto_move_active` only |
| `kAutoMoveDestOff`, `kAutoMoveEnabledOff` | contiguous `movss` pair and the earlier `mov byte,1` above it | fields `auto_move_dest`, `auto_move_enabled` |
| `kPlayerStateOff2`, `kPlayerWalkState` | `cmp dword [reg+d8], imm32` (`0x80..0xFF`) above the flag | `kPlayerWalkState` -> `config.json` (section 7); `Off2` not consumed |

### Optional (omitted): market capture and minimap

| Key | Derived from | Stub use |
|---|---|---|
| `kOfflineshopRecv` | `offlineshop_cap()`: lone pusher of `"UNKNOWN OFFLINESHOP SUBHEADER"`, nearest `53 8B DC 83 EC ?? ...` prologue above it | `InstallShopCapture()` |
| `kOfflineshopInst` | `offlineshop.GetShopUnlockSlotCount` opening `A1 imm32 ; 8B 00` | shop capture, double-indirect singleton |
| `kMiniMapAddWayPoint` | `minimap_waypoint()`: `6A 06 E8 rel32` unique in the `AddWayPoint` binding | `minimap_mark` native (hand-rolled thunk) |
| `kMiniMapRemoveWayPoint` | `8B 09 E8 rel32` unique in `RemoveWayPoint` | `minimap_unmark` native |

Keys that exist only for `uriel_natives.ini` (not from `resolve_all()`):
`py_glue()` produces the CPython glue, of which the nine `NATIVE_GLUE_KEYS`
(`kPyInitModule`, `kPyImportAddModule`, `kPyModuleAddFunctions`,
`kPyTupleGetUInt`, `kPyTupleGetString`, `kPyTupleGetFloat`, `kPyBuildValue`,
`kPyBuildNone`, `kPyBuildException`) are written; `_native_extras()` produces
`kOpenCharacterMenu`, `kReserveProcessClickActor`, `kSendChatPacket`,
`kCreateAutoBotSettings`, `kNetStreamInst`, `kCharMgrInst`.

## 5. `uriel_offsets.ini`

Written by `step_offsets()` in `patcher/src/triarch_patcher/pipeline.py`. The
format, exactly:

```ini
; generated by the Triarch patcher - regenerate after every game update
[offsets]
kActorPass2Hi=0000EC24
kActorPass2HiVA=00612F5A
...
```

One section, keys sorted, each value `%08X` - eight upper-case hex digits, no
`0x`. Only non-zero keys are written. Struct offsets, range constants and
`SLOT2_ARG_BYTES` use the same form even though they are not addresses; the
stub reads every value with `strtoul(buf, NULL, 16)` and gives it meaning by
key name. (The values shown are placeholders.)

The stub reads the file in `LoadOffsets()`, called from `DllMain` on
`DLL_PROCESS_ATTACH`, via `GetPrivateProfileStringA("offsets", key, ...)`. A
zero in the required list is `FATAL: offset '...' missing/zero` and the stub
runs "inert (no hooks)"; a zero in the optional list only logs a note. It then
exports `TRIARCH_ATTACK_OK`, `TRIARCH_HUNT_OK` and `TRIARCH_SKILL_OK` so mods
can degrade gracefully, and calls `LoadNatives()`.

**The file is read once, at DLL load.** Nothing re-reads it. Regenerating
`uriel_offsets.ini` (or `uriel_natives.ini`) under a running client changes
nothing until that client is restarted; the mod host hot-reloads Python, not
this.

## 6. `uriel_natives.ini` and `mods/natives.json`

### 6.1 What `natives.json` declares

`mods/natives.json` is the single source of truth for the native side of the
mod API. Its top-level keys:

- `singletons` - `{name: {holder, deref, doc}}`; e.g. `player` is
  `**(void***)kPlayerInst`. The `this` of a native names one of these.
- `natives` - a list of callable engine functions. The fields `mkoffsets.py`
  reads are `name`, `abi` (`"x86-msvc-thiscall"`), `this`, `args[].type` and
  `return.type`. Everything else - `stack_bytes`, `confidence`, `requires`,
  `side_effects`, `emits`, `safety`, `capability`, `doc` - is documentation
  and policy recorded for later enforcement. Note that `stack_bytes` is *not*
  trusted: the resolver recomputes it from the argument types.
- `rejected` - natives that were investigated and deliberately not exposed,
  with the reason (`FindVictim` takes a float in `XMM2`, which the trampoline
  cannot pass, so it "FAILS CLOSED").
- `fields` and `instance_fields` - typed reads/writes of struct members:
  `name`, `owner` (`player` or `instance`), `offset` (the *key name* in the
  offsets dictionary, e.g. `kAutoAtkVidOff`), optional `offset_delta`,
  `type`, optional `adapter`, `access` (`r`/`rw`).

The stack widths are fixed by `Resolver.STACK_WIDTH`: every 32-bit type is 4
bytes, `i64`/`u64`/`f64` are 8. `this` travels in `ECX` and is not counted.

### 6.2 How a native is resolved and cross-checked

`Resolver.natives(registry_path)` does three things per entry.

1. **Address.** A fixed `NAMEMAP` maps the registry name to a resolver key
   (`"OnPressActor" -> "kOnPressActor"`), and the address comes from
   `autohunt_api()` plus `_native_extras()`. A name with no key or no value is
   reported as `unresolved` and skipped. So "adding a native needs no stub
   rebuild" is true, but it does need a resolver and a `NAMEMAP` line in
   `tools/mkoffsets.py`.
2. **Declared width.** Sum of `STACK_WIDTH[arg.type]`; an unknown type
   rejects the entry.
3. **The binary's opinion.** `ret_imm(va)` disassembles the function with
   capstone from its start to the first `CC CC` padding and collects the
   immediate of every `ret`. In `thiscall` and `stdcall` the *callee* cleans
   the arguments, so that immediate is ground truth for how many bytes the
   function expects. If it disagrees with the declared width the entry is
   **dropped**, with the message `DECLARED N bytes but the binary cleans M`.
   A wrong width is not an exception at runtime; it is a stack imbalance the
   trampoline cannot detect.

`ret_imm()` also guards against a subtle trap: all `ret`s in one function share
one immediate, so a region whose `ret`s *disagree* is two functions packed
without padding. `OpenCharacterMenu` is exactly that (`ret 4` twice, then
`ret 0xC` from the next function); the leading run wins and a note is
attached. `cdecl` entries carry no immediate and cannot be checked; they are
emitted with `checked=0`.

### 6.3 The glue and why it is identity-anchored

To register a real Python module the stub needs CPython's own entry points.
`py_glue()` finds each from a *named* binding, never a byte fingerprint:
`InitModule` is the call after `push <harvested table>; push <name>` with at
least five votes; the string getter must agree between `SendChatPacket(text)`
(first argument) and `AppendChat(type, text)` (second), so agreement proves
identity rather than position; the float getter is the call
`AutoMoveToPosition(x, y)` makes *twice*. Disagreement drops the key.
If any of the nine `NATIVE_GLUE_KEYS` is missing, `write_natives_ini()` writes
**nothing** and returns the missing list. (The stub reads seven of the nine;
`kPyImportAddModule` and `kPyModuleAddFunctions` are recorded for reference.)

### 6.4 The format written

```ini
; generated by mkoffsets.py --natives - do not edit
; Adding a native: edit natives.json and re-run. The
; stub reads this table, so no rebuild is required.

[pyglue]
kPyInitModule=0084E1A0
...                       ; the nine NATIVE_GLUE_KEYS, in that order

[singletons]
kPlayerInst=052A5844      ; kPlayerInst kCharMgrInst kNetStreamInst kMiniMapInst kItemInst,
...                       ; each only if resolved

; name = owner | offset | type | access
[fields]
auto_attack_vid=player|00000050|u32|r
auto_attack_target=player|00000054|u32|r     ; offset + offset_delta
hunt_skills=player|00050164|vector_u8|r      ; adapter name replaces type

; name = address | convention | stack bytes | this | return | checked
[natives]
FindAndSetNewTarget=0064F170|thiscall|12|player|void|1
```

(`0084E1A0` and `052A5844` are the baseline-build values quoted in the
resolver's own comments; the field lines are illustrative.) A field whose
offset key did not resolve is skipped with `field X: kY unresolved - skipped`
- which is why a new typed field needs *both* files regenerated.

The stub's `LoadNatives()` parses these sections and applies its own rejection
rules per native: zero address, stack not a multiple of 4, more than 16
argument slots, `checked=0` on a non-cdecl entry, an unmarshallable return
type, or a `this` singleton that did not resolve. It then logs
`NATIVE: N native(s), M field(s) loaded from uriel_natives.ini` - the line to
look for after any change.

### 6.5 Why a missing gateway is a silent failure

If `uriel_natives.ini` is absent the stub logs `NATIVE: no uriel_natives.ini -
gateway disabled (this is fine)` and carries on. The client starts, the
anti-cheat replacement works, mods load - and `triarch_native` is never
registered, so ground-item and actor enumeration, every typed field and the
autohunt drive are absent. Dependent mods do nothing, with no crash and no
error. The pipeline docstring calls this "the worst way for a patcher to be
wrong", so `step_natives()` raises `Failure("native gateway incomplete ...
mods would silently do nothing")`, and `step_deploy()` refuses to ship a stub
binary that lacks the string `triarch_native`.

## 7. Build constants stamped into `mods/config.json`

Some numbers a mod needs are facts about the build, not preferences. The
clear case is `WALK_STATE`: `mods/nocollide/main.py`, `autohunt2`, `keeproute`
and `routes` decide "is the player travelling" by `auto_move_active AND
player_state == WALK_STATE`, because a cancelled route leaves
`auto_move_active` set forever and only the state changes. The value (`0x8A` =
138 today) is read out of the engine's `cmp [player+0x58], 0x8A` by
`auto_move()` and emitted as `kPlayerWalkState`.

`pipeline.py` has a `BUILD_CONSTANTS` table mapping `(mod, setting) -> offset
key`; `_stamp_build_constants()` writes the resolved value into
`mods/config.json` on every install, even when the file exists - unlike every
other setting, since `config.json` is otherwise preserved as the file users
edit. The mod host applies config keys to matching module-level constants at
load time, so the mod's `WALK_STATE = 0x8A` is only a fallback. `mods/modhost.py`
lists the same names in `BUILD_KEYS`, which per-character profiles may not
override: a pinned stale state number would survive a game update and silently
break every consumer.

## 8. Tutorial: adding a new resolver

1. **Find it by name first.** Search the strings and the `PyMethodDef` tables
   (`Resolver(path)._py_tables()`). A Python binding is the best start:
   `binding("playerm2g2", "SomeMethod")` gives a function whose body touches
   what you want.
2. **Read the body in a disassembler** and pick the *shape*: the opcodes that
   express the operation, with struct offsets and server-data constants
   (vnums, state numbers) as wildcards. `auto_move()` is a template that reads
   several offsets out of one match.
3. **Write the method** on `Resolver`. Return a dict of keys, `{}` on any miss
   or ambiguity (`len(hits) != 1 -> {}`), never a zero. Add it to
   `resolve_all()` with `vals.update(...)`.
4. **Corroborate with a second anchor** where possible: a call target that
   must be a one-instruction getter (`actor_pass()`), a constant that must
   appear in the body (`run_line()`), two bindings that must agree
   (`py_glue()`).
5. **Resolve on at least two different builds:**

   ```bash
   cd tools && python3 -c "
   from mkoffsets import Resolver
   for exe in ('/path/build_a.exe', '/path/build_b.exe'):
       print(exe, Resolver(exe).your_resolver())"
   ```

   Structural consistency is the test: different addresses, same shape, and
   the constants that should be identical (magic values, vtable slots) are.
   A resolver that works on one build only is not structural.
6. **Check the disassembly at the resolved address** on each build: padding
   before it, a prologue, and the matched instruction is the one you meant.
7. **Confirm at runtime before trusting a store.** A `mov [reg+off], 1` in a
   listing proves the compiler emitted it, not that it executes on the path
   you care about. Hook the function with a runtime tracer (this project uses
   Frida), log the field before and after the action, and only then believe
   the offset. `auto_move()` records exactly this: "Measured live:
   player_state reads 0x8A while a waypoint route walks and 0x85 the moment
   it is cancelled."
8. **Expose it.** For a typed field, add an entry to `mods/natives.json`
   (`name`, `owner`, `offset` = your key, `type`, `access`, `doc`). For a
   callable native, add a `NAMEMAP` line and a `natives` entry; the `ret imm`
   check will tell you if you miscounted the arguments.
9. **Regenerate both ini files against the target build and restart the
   client.** Check `NATIVE: ... field(s) loaded` went up, then read it from a
   mod with `api.field("name")`.
10. **Sync the vendored copy.** `patcher/.../vendor/mkoffsets.py` must stay
    byte-identical to `tools/mkoffsets.py`; a fix in one and not the other is
    the silent-wrong-address bug this whole file exists to prevent.

## 9. CLI usage and diffing two builds

`tools/mkoffsets.py` needs `capstone` (`pip install capstone`) and
`tools/namemods.py` beside it.

```
python3 tools/mkoffsets.py <decrypted.exe> [-o offsets.h] [--check] \
        [--natives mods/natives.json uriel_natives.ini]
```

- With no options it prints every key as `kName = 0x%08X`, then
  `kSlot2ArgBytes`, and exits `1` if *anything* is unresolved. The CLI is
  stricter than the patcher: `OPTIONAL_OFFSETS` is a pipeline concept, so a
  missing `kPackGet` fails the CLI but not a patch.
- `--check` compares against `BASELINE`, the known-good values for the
  executable whose SHA-256 starts `f8e0db12`, printing `OK` or `MISMATCH` per
  key. Run it after changing a resolver: if the old build still gives the old
  answers, you have not broken the shape.
- `-o offsets.h` writes a C header (`static const DWORD kName = 0x...;` plus
  `#define SLOT2_ARG_BYTES`), the pre-ini way of building the stub. It does
  **not** write `uriel_offsets.ini`; only the patcher's `step_offsets()` does.
  To produce it by hand, reproduce those lines:

  ```python
  from mkoffsets import resolve_all
  vals = resolve_all("/path/triarch_clean.exe")
  with open("uriel_offsets.ini", "w") as f:
      f.write("; generated by the Triarch patcher - regenerate after every game update\n[offsets]\n")
      for k in sorted(vals):
          if vals[k]:
              f.write("%s=%08X\n" % (k, vals[k]))
  ```

- `--natives <registry> <out.ini>` runs `write_natives_ini()`, prints the
  glue table and `REJECTED` lines, and exits `1` if the glue is incomplete.

Resolving scans a 68 MB `.text` many times and takes a while; run it once per
build and keep the output.

**Diffing two builds** is the fastest way to see what a game update moved:

```bash
python3 tools/mkoffsets.py old_clean.exe > old.txt
python3 tools/mkoffsets.py new_clean.exe > new.txt
diff old.txt new.txt
```

Every code address will differ; that is expected. Read the non-address keys -
`kSkipCollisionOff`, `kHuntUseSkillOff`, `kAutoAtkVidOff`, `kPlayerWalkState`,
`kActorPassLo/Hi`, `SLOT2_ARG_BYTES`. A changed struct offset means a class
grew and anything assuming the old layout would read garbage; a `NOT FOUND`
means a shape stopped matching and needs a new anchor, not a copied number.
