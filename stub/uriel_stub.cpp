// uriel_stub.cpp - instrumented do-nothing replacement for Uriel (client_x86.dll).
//
// Slot names recovered from the client's own [UDIAG] diagnostics (TRIARCH-WIKI 3):
//   slot 0 = Initialize(0x15ab, 0x6a)   UserInterface.cpp:421       ret 8
//   slot 1 = anticheat.Tick(&a,&b,&c)   PythonApplication.cpp:948   ret 12, per frame
//   slot 3 / slot 7 = inbound challenge sinks (opcodes 170 / 178)   ret 4
//
// Two jobs beyond logging:
//   1. slot 1 must *construct* a destructible object at arg1. The client's EH
//      state variable goes 0 -> 1 across the call, so it believes one object
//      became live; leaving it uninitialised makes the scope-exit destructor run
//      on stack garbage -> FAST_FAIL_INVALID_ARG (BEX c0000409 subcode 5).
//   2. re-enable the client's [UDIAG] trace at runtime. The 667 diagnostic call
//      sites are intact but their sink at 0x00583380 is a bare `ret 0`;
//      __TraceError at 0x006d9110 is live and takes the identical
//      (const char* file, int line, const char* fmt, ...) cdecl signature.
//      Doing this from DllMain keeps triarch_clean.exe untouched on disk and
//      makes the change revertible by rebuilding the stub.

#include <windows.h>
#include <stdio.h>
#include <share.h>   // _SH_DENYWR for the shared-read netlog
#include <stdarg.h>
#include <string.h>
#include <time.h>
#include <intrin.h>
#include <winhttp.h>          // http_post native (generic HTTPS POST for mods)
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "user32.lib")   // SendInput for the key_event native (polled keys)
#pragma intrinsic(_ReturnAddress)

// ---- build-time switches ---------------------------------------------------
#define ENABLE_UDIAG_PATCH 1   // redirect the dead trace sink into __TraceError
#define ENABLE_SLOT1_CTOR  1   // construct an empty std::string at slot 1 arg1
#define DUMP_SLOT1_BUFFERS 1   // hexdump slot 1's parameters
#define ENABLE_VBE_HOOK    1   // reimplement CryptoPP::VerifyBufsEqual, log callers
#define ENABLE_NET_LOG     1
#define ENABLE_AUTH_TRACE  1
#define ENABLE_MODS        1   // bootstrap <exe>\mods\modhost.py into the interpreter
#define ENABLE_SHOP_CAPTURE 1  // MARKET-SCAN: dump offline-shop item prices per packet

// ---- image layout ----------------------------------------------------------
// Every address below is regenerated per build by tools/mkoffsets.py; never
// hardcode one here.  Run `mkoffsets.py <decrypted.exe> --check -o offsets.h`
// after each game update and rebuild.
static const DWORD kImageBase = 0x00400000;

// Offsets are loaded at runtime from uriel_offsets.ini next to the exe, written
// by the patcher for this specific build. That keeps this DLL build-independent
// so it can ship prebuilt inside the patcher instead of needing MSVC on site.
struct Offsets {
    DWORD traceSink, traceReal, verifyBufsEqual, sendAppend, recvBuf;
    DWORD authRecvPhase, authProcess, getPcName, getHwProfileId;
    DWORD iatConnect, iatClosesocket, iatSend, iatRecv, iatWSAGetLastError;
    DWORD iatWinHttpConnect, iatWinHttpOpenRequest, iatInternetOpenUrlA;
    DWORD iatURLDownloadToFileA, urielObjOffset, slot2ArgBytes;
    // mod host - OPTIONAL. An ini written before the mod host existed simply
    // leaves these zero; the stub then skips mod loading and everything else
    // still works. Never make these fatal.
    DWORD pyRunLine, pyLauncherInst;
    // native attack bridge - also OPTIONAL, see AttackTick(). Absent means the
    // feature is off; an ini written before it existed keeps working untouched.
    DWORD onPressActor, playerInst, playerSubObj, getMainInstOff;
    // native autohunt drive - OPTIONAL, see HuntTick(). All derived by
    // mkoffsets.Resolver.autohunt_api() from the "/auto_hunt end" literal.
    DWORD findAndSetNewTarget, autoAtkVidOff, anchorOff;
    DWORD miniMapInst, autoHuntRangeOff;
    // native skill drive - OPTIONAL, see SkillTick(). Absent means the hunt
    // runs exactly as before, without skills.
    DWORD useAutoSkills, huntUseSkillOff, huntSkillVecOff, mountedOff;
    DWORD huntStoneOff;
    DWORD itemInst;
    DWORD enableSkipCollision, disableSkipCollision;
    DWORD skipCollisionOff, instActorOff;
    // actor pass-through - OPTIONAL. Addresses of the two IMMEDIATE FIELDS in
    // the engine's race-whitelist range check, not of the instructions: we
    // widen a data range, never rewrite control flow. Absent means the feature
    // is off. See ActorPassSet().
    DWORD actorPassLoVA, actorPassHiVA, actorPassLo, actorPassHi;
    // The SECOND hardcoded range in the same cascade. Having two is what makes
    // an exact carve-out possible: first range covers everything BELOW a band,
    // second everything ABOVE, so the band keeps its collision. Used to leave
    // metin stones solid (race 8005) while mobs (401-599) become passable.
    DWORD actorPass2LoVA, actorPass2HiVA, actorPass2Lo, actorPass2Hi;
    // CActorInstance::BlockMovement - the *stop*, which is what terrain uses.
    // OPTIONAL. See TerrainPassSet(), and read the warning there first.
    DWORD blockMovement;
    // MARKET-SCAN offline-shop price capture - OPTIONAL. Absent => feature off.
    // offlineshopRecv = CPythonNetworkStream::RecvOfflineshopPacket (FUN_00635020),
    // offlineshopInst = the CPythonOfflineshop singleton GLOBAL (double-indirect:
    // instance = *(*(global))). Both resolved structurally by mkoffsets.
    DWORD offlineshopRecv, offlineshopInst;
    // CPythonMiniMap waypoint natives - OPTIONAL. addWayPoint takes the mark
    // TYPE the Python binding hides (type 13 = the blinking target mark that
    // draws on minimap AND atlas and auto-tracks a VID), and has a nonstandard
    // convention (x in XMM3, name a by-value std::string) so it rides a
    // hand-rolled thunk rather than the generic gateway. removeWayPoint is a
    // plain thiscall(id). Both use g_singleton[THIS_MINIMAP]. Absent => the
    // minimap_mark/minimap_unmark natives are simply not registered.
    DWORD miniMapAddWayPoint, miniMapRemoveWayPoint;
};
static Offsets g_off;
static bool g_haveOffsets = false;

#if ENABLE_MODS
static void ModsTick();     // defined with the mod host below; driven by slot 1
static void AttackTick();   // native attack bridge, same driver
static void HuntTick();     // native autohunt drive, same driver
static void NativeTick();   // registers triarch_native once Python is up
#endif

static CRITICAL_SECTION g_cs;
static bool g_ready = false;
static char g_exeDir[MAX_PATH];    // where the client lives
static char g_dataDir[MAX_PATH];   // <exe>\_patcher\  - logs, all disposable
static char g_logPath[MAX_PATH];
static unsigned g_tick = 0;

// 20-byte buffer handed to the client as Uriel's "expected SHA-1 digest";
// the VerifyBufsEqual hook recognises it by address and reports a match.
static unsigned char g_digestSentinel[20] = {0};

static void LogInit()
{
    // Runtime logs go to <exe>\_patcher\ so the game folder stays clean and the
    // whole directory can be deleted. uriel_offsets.ini stays beside the exe -
    // it is required, not disposable.
    GetModuleFileNameA(NULL, g_exeDir, MAX_PATH);
    char* slash = strrchr(g_exeDir, '\\');
    if (slash) *(slash + 1) = 0; else g_exeDir[0] = 0;
    _snprintf_s(g_dataDir, sizeof(g_dataDir), _TRUNCATE, "%s_patcher\\", g_exeDir);
    CreateDirectoryA(g_dataDir, NULL);
    _snprintf_s(g_logPath, sizeof(g_logPath), _TRUNCATE, "%suriel_stub.log", g_dataDir);
    DeleteFileA(g_logPath);
}

static void Log(const char* fmt, ...)
{
    if (!g_ready) return;
    // CRITICAL: hooks run between a Win32/winsock call and the caller's
    // GetLastError()/WSAGetLastError(). fopen(...,"a") uses OPEN_ALWAYS and sets
    // ERROR_ALREADY_EXISTS (183) even on success, which made the client read 183
    // instead of WSAEWOULDBLOCK after connect() and report "server is down".
    DWORD savedErr = GetLastError();
    EnterCriticalSection(&g_cs);
    FILE* f = NULL;
    if (fopen_s(&f, g_logPath, "a") == 0 && f) {
        // Timestamp every line. Without this, `NET: connect ... :12210` could
        // not be lined up against the Python trace in mods.log or the wire
        // capture in netlog.txt - and the channel-status probe rides a SEPARATE
        // socket, so those connect lines are the only record of it. An untimed
        // event that cannot be correlated with anything is not evidence.
        SYSTEMTIME st; GetLocalTime(&st);
        fprintf(f, "%02d:%02d:%02d.%03d ",
                st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
        va_list ap; va_start(ap, fmt);
        vfprintf(f, fmt, ap);
        va_end(ap);
        fputc('\n', f);
        fclose(f);
    }
    LeaveCriticalSection(&g_cs);
    SetLastError(savedErr);
}

static void Hexdump(const char* tag, const void* p, size_t n)
{
    if (!p || IsBadReadPtr(p, n)) { Log("    %-7s %p <unreadable>", tag, p); return; }
    char line[200]; int o = 0;
    o += _snprintf_s(line + o, sizeof(line) - o, _TRUNCATE, "    %-7s %p:", tag, p);
    for (size_t i = 0; i < n; i++)
        o += _snprintf_s(line + o, sizeof(line) - o, _TRUNCATE, " %02X",
                         ((const unsigned char*)p)[i]);
    Log("%s", line);
}

// ---------------------------------------------------------------------------
// slot 1 - anticheat.Tick(void* arg1, void** arg2, DWORD* arg3)
//
//   arg1  &(28-byte stack local)  <- the object the client expects constructed
//   arg2  &(ptr to a context the client just filled with rdtsc + a timing API)
//   arg3  &(DWORD, pre-zeroed by the client)
// ---------------------------------------------------------------------------
extern "C" void __cdecl Slot1Handler(void* self, void* a1, void* a2, void* a3)
{
    g_tick++;
    if (g_tick <= 3 || (g_tick % 600) == 0) {
        Log("slot 1  anticheat.Tick #%u  this=%p a1=%p a2=%p a3=%p",
            g_tick, self, a1, a2, a3);
#if DUMP_SLOT1_BUFFERS
        Hexdump("a1[28]", a1, 28);
        Hexdump("a2[8]", a2, 8);
        if (a2 && !IsBadReadPtr(a2, 4)) Hexdump("*a2[16]", *(void**)a2, 16);
        Hexdump("a3[4]", a3, 4);
#endif
    }

#if ENABLE_SLOT1_CTOR
    // arg1 is NOT a std::string. Later in the very same function
    // (CPythonApplication::Process, 0x005aba80) the client does:
    //     mov esi, [ebp-0x144]      ; <- this slot
    //     push esi
    //     call [vtable+0x30]        ; HashTransformation::Verify(digest)
    // i.e. Uriel is expected to hand back a pointer to a 20-byte SHA-1 digest
    // which the client then verifies against a hash it computes itself.
    // Zero-filling it made that pointer NULL -> the 0xC0000005 in
    // VerifyBufsEqual. Point it at a sentinel buffer instead; the VBE hook
    // recognises that buffer and reports "equal".
    // arg1 is a std::vector<unsigned char> {_Myfirst,_Mylast,_Myend} (dtor at
    // 0x00586b10). Leave it empty: the dtor early-outs on a NULL _Myfirst, and
    // any pointer we supply would be freed by the *client's* operator delete
    // against a different CRT heap. The digest match is faked in the
    // VerifyBufsEqual hook instead, so the client never gets a bogus pointer.
    if (a1 && !IsBadWritePtr(a1, 12)) memset(a1, 0, 12);
#endif
    if (a3 && !IsBadWritePtr(a3, 4)) *(DWORD*)a3 = 0;

#if ENABLE_MODS
    // Last, and only after the anti-cheat bookkeeping above is complete, so a
    // misbehaving mod cannot disturb what the client reads back from this call.
    ModsTick();
    // After the pump, so a VID the mods set this frame is acted on immediately
    // rather than a frame late.
    AttackTick();
    HuntTick();
    NativeTick();
#endif
}

static __declspec(naked) void slot1(void)
{
    __asm {
        push ebp
        mov  ebp, esp
        pushad
        push dword ptr [ebp+16]      // arg3
        push dword ptr [ebp+12]      // arg2
        push dword ptr [ebp+8]       // arg1
        push ecx                     // this
        call Slot1Handler
        add  esp, 16
        popad
        xor  eax, eax                // caller ignores the return value
        pop  ebp
        ret  12
    }
}

// ---------------------------------------------------------------------------
// generic slots - log index + return address, clean `nbytes`, return `rv`
// ---------------------------------------------------------------------------
extern "C" void __cdecl UrielLog(int slot, void* ret)
{
    // These sit on the client's own timers, so they repeat forever from the
    // same call site: slot 6 alone contributed thousands of identical lines per
    // session. The return address is what makes a slot line worth having, and
    // it stops being news after the first few. Per-slot counters, so a rarely
    // used slot is not silenced by a chatty one.
    static long counts[16] = { 0 };
    if (slot < 0 || slot >= 16) { Log("slot %d  called from 0x%08X", slot,
                                      (unsigned)(size_t)ret); return; }
    long k = InterlockedIncrement(&counts[slot]);
    if (k > 3 && (k % 2000) != 0) return;
    Log("slot %d  called from 0x%08X  (#%ld)", slot, (unsigned)(size_t)ret, k);
}

#define SLOT(name, idx, nbytes, rv)             \
    static __declspec(naked) void name(void) {  \
        __asm { pushad }                        \
        __asm { pushfd }                        \
        __asm { mov eax, [esp+36] }             \
        __asm { push eax }                      \
        __asm { push idx }                      \
        __asm { call UrielLog }                 \
        __asm { add esp, 8 }                    \
        __asm { popfd }                         \
        __asm { popad }                         \
        __asm { mov eax, rv }                   \
        __asm { ret nbytes }                    \
    }

SLOT(slot0, 0,  8, 1)   // Initialize(5547, 106) - must return TRUE
SLOT(slot3, 3,  4, 1)   // inbound challenge, opcode 170 - never answered
SLOT(slot4, 4,  0, 1)
SLOT(slot5, 5,  0, 1)
SLOT(slot6, 6, 16, 1)
SLOT(slot7, 7,  4, 1)   // inbound challenge, opcode 178 - never answered

// slot 2 - THE LOGIN GATE.  Called from exactly one place,
// CAccountConnector::__AuthState_RecvPhase @ 0x00582838:
//     lea eax,[ebp-0x3c] / push eax      ; +4  pointer arg
//     sub esp,0x18                       ; +24 struct passed by value
//     call 0x5847e0                      ; construct that struct
//     mov ecx,[ebp-0xc0] / call [vt+8]   ; thiscall -> 28 bytes of args
//     test al,al / jne  -> send credentials
//              fall through -> abort the login, emit nothing
// So it must return TRUE and clean 28 bytes (ret 0x1C).  The old stub returned
// FALSE with ret 0, which is why the patched client completed the handshake,
// took GC_PHASE(10), built the auth packet - and then never sent it.
extern "C" void __cdecl Slot2Handler(void* self, void* ptrArg, const void* blob24)
{
    Log("slot 2  LOGIN GATE  this=%p ptr=%p", self, ptrArg);
    Hexdump("blob24", blob24, 24);
    Hexdump("ptrArg", ptrArg, 16);
}

static __declspec(naked) void slot2(void) {
    __asm {
        push ebp
        mov  ebp, esp
        pushad
        lea  eax, [ebp+8]              // the 24-byte struct
        push eax
        push dword ptr [ebp+0x20]      // the pointer arg (after the 24 bytes)
        push ecx                       // this
        call Slot2Handler
        add  esp, 12
        popad
        mov  eax, 1                    // TRUE - let the login proceed
        pop  ebp
        ret  0x1C                      // imm16 rewritten by ApplySlot2Ret()
    }
}

static void* g_vtable[16] = {
    slot0, slot1, slot2, slot3, slot4, slot5, slot6, slot7,
    slot4, slot4, slot4, slot4, slot4, slot4, slot4, slot4,
};

struct UrielObject { void** vptr; DWORD cb1; DWORD cb2; DWORD spare[32]; };
static UrielObject g_obj;

extern "C" BOOL __cdecl FireInTheHole(void** ppOut, void*)
{
    g_obj.vptr = g_vtable;
    if (ppOut) *ppOut = &g_obj;
    Log("FireInTheHole -> obj=%p vtable=%p (from 0x%08X)",
        &g_obj, g_vtable, (unsigned)(size_t)_ReturnAddress());
    return TRUE;
}

// ---------------------------------------------------------------------------
// re-enable the client's own [UDIAG] instrumentation
//
// The sink must NOT be routed into the client's own __TraceError (0x6d9110).
// Doing that turns 667 dormant call sites live inside the very machinery the
// logger uses; the client dies with 0xC00000FD (stack overflow) in the CRT
// formatting routine __TraceError calls, which carries a ~1.1 KB frame.
// Instead the sink jumps to our own thunk, which never re-enters client code.
// ---------------------------------------------------------------------------
#if ENABLE_UDIAG_PATCH

static FILE* g_trace = NULL;
static long g_traceLines = 0;
static const long kTraceLineCap = 20000;      // runaway guard
static __declspec(thread) int t_inTrace = 0;  // per-thread re-entrancy guard

// same signature as the sink it replaces:
//   void __cdecl (const char* file, int line, const char* fmt, ...)
extern "C" void __cdecl TraceThunk(const char* file, int line, const char* fmt, ...)
{
    if (t_inTrace) return;                    // never recurse
    if (g_traceLines >= kTraceLineCap) return;

    // 0x583380 is a `ret 0` no-op, so the developers called it with several
    // different arities - e.g. 0x005aa8ab passes a single string. Only the
    // (file, line, fmt, ...) shape used by the [UDIAG] sites is interpretable;
    // anything else must be ignored or we format garbage off the stack.
    if (IsBadReadPtr(fmt, 8) || strncmp(fmt, "[UDIAG", 6) != 0) return;
    if (line <= 0 || line > 100000) return;
    if (IsBadReadPtr(file, 4)) file = NULL;

    t_inTrace = 1;

    char msg[512];
    va_list ap; va_start(ap, fmt);
    _vsnprintf_s(msg, sizeof(msg), _TRUNCATE, fmt, ap);
    va_end(ap);

    EnterCriticalSection(&g_cs);
    if (!g_trace) {
        char path[MAX_PATH];
        _snprintf_s(path, sizeof(path), _TRUNCATE, "%sudiag.log", g_dataDir);
        fopen_s(&g_trace, path, "w");
        // unbuffered: this log exists to survive a crash, throughput is secondary
        if (g_trace) setvbuf(g_trace, NULL, _IONBF, 0);
    }
    if (g_trace) {
        const char* base = file ? strrchr(file, '\\') : NULL;
        fprintf(g_trace, "%s:%d  %s\n", base ? base + 1 : (file ? file : "?"), line, msg);
        ++g_traceLines;
    }
    LeaveCriticalSection(&g_cs);

    t_inTrace = 0;
}

static void PatchTraceSink()
{
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    if (!base) { Log("UDIAG: no exe module handle"); return; }
    ptrdiff_t slide = (ptrdiff_t)base - (ptrdiff_t)kImageBase;   // ASLR-safe
    BYTE* sink = base + (g_off.traceSink - kImageBase);

    // opt out at runtime without a rebuild: drop `udiag.off` next to the exe
    char off[MAX_PATH];
    _snprintf_s(off, sizeof(off), _TRUNCATE, "%sudiag.off", g_dataDir);
    if (GetFileAttributesA(off) != INVALID_FILE_ATTRIBUTES) {
        Log("UDIAG: udiag.off present - trace DISABLED (baseline run)");
        return;
    }

    // refuse unless the sink is exactly the expected `ret 0`
    if (IsBadReadPtr(sink, 3) || sink[0] != 0xC2 || sink[1] || sink[2]) {
        Log("UDIAG: sink at %p is not `ret 0` (%02X %02X %02X) - not patching",
            sink, sink[0], sink[1], sink[2]);
        return;
    }
    DWORD old = 0;
    if (!VirtualProtect(sink, 5, PAGE_EXECUTE_READWRITE, &old)) {
        Log("UDIAG: VirtualProtect failed (%lu)", GetLastError());
        return;
    }
    BYTE* dst = (BYTE*)&TraceThunk;
    sink[0] = 0xE9;                                   // jmp rel32
    *(int*)(sink + 1) = (int)(dst - (sink + 5));
    VirtualProtect(sink, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), sink, 5);
    Log("UDIAG: sink %p -> stub TraceThunk %p (slide 0x%08X) - trace ENABLED -> udiag.log",
        sink, dst, (unsigned)slide);
}
#endif

// ---------------------------------------------------------------------------
// CryptoPP::VerifyBufsEqual hook
//
// The client dies with 0xC0000005 at 0x00883688, inside the constant-time
// compare loop, called from HashTransformation::TruncatedVerify (0x0087c6f0).
// At the fault arg2 == NULL and count == 20 (a SHA-1 / HMAC-SHA1 digest), i.e.
// something calls TruncatedVerify(NULL, 20). TruncatedVerify is virtual, so the
// caller cannot be found by a static xref - hook the leaf instead.
//
// VerifyBufsEqual is `bool __cdecl (const byte* a, const byte* b, size_t n)`,
// a pure constant-time compare, so it can be reimplemented outright: no
// trampoline needed, and a NULL argument can be reported instead of faulting.
// ---------------------------------------------------------------------------
#if ENABLE_VBE_HOOK
static const BYTE kVbePrologue[5] = { 0x83, 0xEC, 0x0C, 0x8B, 0x44 };
static long g_vbeCalls = 0;
static long g_vbeEmpty = 0;

extern "C" int __cdecl VbeHandler(const unsigned char* a, const unsigned char* b,
                                  unsigned int n, void* frame)
{
    // CPythonApplication::Process integrity check: Uriel would have filled the
    // vector at [ebp-0x144] with a 20-byte SHA-1. We keep that vector empty (see
    // Slot1Handler), so report a match here rather than hand over a pointer the
    // client would try to free.
    //
    // This fires on EVERY integrity tick, so it is the expected path, not an
    // anomaly - handle it first and cheaply. Logging it unconditionally made it
    // ~91% of uriel_stub.log (8127 of 8959 lines in one session, 1.4 MB and
    // still growing), which buried the NET/AUTH/HTTP lines that carry the
    // actual signal. It also ran the frame walk below - five IsBadReadPtr calls
    // and a _snprintf_s - on every single call for a string nobody read.
    // Count it, log the first few and an occasional heartbeat, nothing more.
    if (!b && n == 20) {
        long k = InterlockedIncrement(&g_vbeEmpty);
        if (k <= 3 || (k % 5000) == 0)
            Log("VerifyBufsEqual: empty 20-byte digest -> forcing MATCH (#%ld)", k);
        return 1;
    }

    bool bad = !a || !b || IsBadReadPtr(a, n) || IsBadReadPtr(b, n);

    // Anything else is either a real comparison or a genuinely unexpected bad
    // buffer. Both are rare, so both are worth the frame walk - but rate-limit
    // even the bad case, so a NEW failure mode cannot flood the log the way the
    // empty-digest case did.
    long seen = InterlockedIncrement(&g_vbeCalls);
    if (seen <= 12 || (bad && (seen % 500) == 0)) {
        // `frame` is TruncatedVerify's ebp. Walk the saved-ebp chain:
        // TruncatedVerify is reached through the virtual Verify(digest) thunk
        // at 0x0059f260, so the interesting caller is two or three frames up.
        char chain[160]; int co = 0; chain[0] = 0;
        void* f = frame;
        for (int k = 0; k < 5 && f && !IsBadReadPtr(f, 8); k++) {
            co += _snprintf_s(chain + co, sizeof(chain) - co, _TRUNCATE,
                              " <-%p", ((void**)f)[1]);
            f = ((void**)f)[0];
        }
        Log("VerifyBufsEqual%s a=%p b=%p n=%u (#%ld)  callers:%s",
            bad ? " *** BAD BUFFER ***" : "", a, b, n, seen, chain);
    }
    if (bad) return 0;                       // report "not equal", never fault

    unsigned char acc = 0;
    for (unsigned i = 0; i < n; i++) acc |= (unsigned char)(a[i] ^ b[i]);
    return acc == 0;
}

static __declspec(naked) void VbeThunk(void)
{
    __asm {
        push ebp                        // arg4 = caller frame
        push dword ptr [esp+0x10]       // arg3 = n
        push dword ptr [esp+0x10]       // arg2 = b
        push dword ptr [esp+0x10]       // arg1 = a
        call VbeHandler
        add  esp, 16
        ret                             // cdecl: caller cleans its own args
    }
}

static void HookVerifyBufsEqual()
{
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    if (!base) return;
    BYTE* fn = base + (g_off.verifyBufsEqual - kImageBase);
    if (IsBadReadPtr(fn, 5) || memcmp(fn, kVbePrologue, 5) != 0) {
        Log("VBE: prologue at %p unexpected (%02X %02X %02X %02X %02X) - not hooking",
            fn, fn[0], fn[1], fn[2], fn[3], fn[4]);
        return;
    }
    DWORD old = 0;
    if (!VirtualProtect(fn, 5, PAGE_EXECUTE_READWRITE, &old)) return;
    BYTE* dst = (BYTE*)&VbeThunk;
    fn[0] = 0xE9;
    *(int*)(fn + 1) = (int)(dst - (fn + 5));
    VirtualProtect(fn, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), fn, 5);
    Log("VBE: VerifyBufsEqual %p -> stub %p - hooked", fn, dst);
}
#endif

// ---------------------------------------------------------------------------
// Network instrumentation
//
// Plaintext chokepoints, both thiscall(int size, void* buf) / ret 8, both with
// the same 6-byte prologue `55 8B EC 56 8B F1`:
//   0x006E2960  append to the outgoing buffer   (360 callers, all Send*Packet)
//   0x006E2710  read from the incoming buffer   (every phase, incl. login)
// These sit *inside* the crypto layer, so what we log is cleartext.
// Winsock connect/closesocket are hooked through the IAT for the connection
// lifecycle (no trampoline needed - the IAT entry is just a pointer).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// FISHING: rod-fishing bite detector (piggybacks the Recv hook; no new offset)
//
// GC_FISHING arrives as a 7-byte packet [opcode, subheader, vid:u32] through
// CPythonNetworkStream::Recv (kRecvBuf @ 006E5110, already trampolined by
// RecvHook -> NetLogRecv, which runs AFTER the buffer is filled). The opcode is
// 0x3D on this build (Frida-confirmed on the deployed exe; matches the Android
// lib, not the older Windows decompile's 0x34). subheader 2 == the BITE - the
// brief "!" reel-now window. It is native-only: NO OnFishing* Python callback
// fires at bite time, which is why a pure-Python mod could never time the reel.
// We watch every Recv(7) whose first byte is the fishing opcode and publish a
// small monotonic snapshot the mod polls via triarch_native.fishing_poll(); the
// mod reels with the attack key the instant the bite counter grows.
//   subheader: 0=start 1=stop 2=BITE 3=catch-ok 4=catch-fail 5=notify
// Detection lives in NetLogRecv (needs the Recv trampoline, hence ENABLE_NET_LOG);
// the globals + the native sit outside that guard, so with net-log off the
// native simply reports zeros (feature off) rather than vanishing.
// ---------------------------------------------------------------------------
static const BYTE    kGcFishingHdr = 0x3D;   // GC_FISHING opcode (deployed-confirmed)
static const BYTE    kCgFishingHdr = 0x34;   // CG_FISHING (our outgoing cast/reel)
static volatile LONG g_fishSeq     = 0;      // ++ on every GC_FISHING packet FOR US
static volatile LONG g_fishBiteSeq = 0;      // ++ on every bite (subheader 2) FOR US
static volatile LONG g_fishSub     = -1;     // last subheader seen FOR US
// GC_FISHING is a BROADCAST - every nearby player's fishing arrives too, so counting
// blind reels on strangers' bites ("pulling too soon"). We filter to our own VID,
// latched from the cast->start handshake: our outgoing 0x34 arms g_fishExpectStart,
// and the next start (sub 0) is ours. Re-armed every cast, so a rare mis-latch on a
// concurrent stranger start self-corrects on the next cast.
static volatile LONG g_fishOurVid      = 0;  // our character VID (0 = not yet known)
static volatile LONG g_fishExpectStart = 0;  // set by our cast; next start latches the VID
// A "fish-info" item makes the server send a sub=5 NOTIFY right after each of our
// bites whose u32 field is the biting FISH VNUM (27802 Minnow .. 27823 Goldfish,
// 0 = junk). It lets the mod pick the correct per-species reel delay. The NOTIFY's
// u32 is the fish, NOT a character VID, so it is captured OUTSIDE the VID filter.
static volatile LONG g_fishVnum = 0;         // vnum of the last biting fish (0 = junk/none)
static volatile LONG g_fishLastBiteVid = 0;  // vid of the most recent bite (for the sub=5 latch)
static volatile LONG g_fishBiteCounted = 0;  // de-dup: was the current bite already counted?
static volatile LONG g_fishCastSeq = 0;      // ++ on every OUTGOING CG_FISHING (0x34) - lets the
                                             // mod confirm a cast reached the wire (VID-free), so a
                                             // mis-latched VID can't trap it in a recast loop

#if ENABLE_NET_LOG
static const BYTE  kNetPrologue[6] = { 0x55, 0x8B, 0xEC, 0x56, 0x8B, 0xF1 };

static void* g_sendTramp = NULL;
static void* g_recvTramp = NULL;
static FILE* g_net = NULL;
static long  g_netLines = 0;

// login-relevant GC opcodes, read from the header dispatch table of this exact binary
static const char* GcName(unsigned char op)
{
    switch (op) {
        case 6:   return "GC_LOGIN_SUCCESS";
        case 7:   return "GC_LOGIN_FAILURE";
        case 62:  return "GC_EMPIRE";
        case 80:  return "GC_CHANNEL";
        case 100: return "GC_AUTH_SUCCESS";
        case 102: return "GC_HYBRIDCRYPT_KEYS";
        case 103: return "GC_HYBRIDCRYPT_SDB";
        case 104: return "GC_AUTH_SUCCESS_OPENID";
        case 114: return "GC_RESPOND_CHANNELSTATUS";
        case 129: return "GC_ANTICHEAT";
        case 154: return "GC_PLAYER_INFORMATION";
        case 170: return "GC_URIEL_CHALLENGE_170";
        case 178: return "GC_URIEL_CHALLENGE_178";
        case 253: return "GC_PHASE";
        case 255: return "GC_HANDSHAKE";
        default:  return NULL;
    }
}

static void NetOpen()
{
    if (g_net) return;
    char path[MAX_PATH];
    _snprintf_s(path, sizeof(path), _TRUNCATE, "%snetlog.txt", g_dataDir);
    // _fsopen(_SH_DENYWR), NOT fopen_s: fopen_s takes an exclusive lock, so
    // netlog.txt could not be read at all while the client was running -
    // "The process cannot access the file because it is being used by another
    // process". A capture you cannot read until the client exits is useless for
    // watching a live flow. _SH_DENYWR keeps our exclusive WRITE but lets
    // readers in, which is how mods.log and uriel_stub.log are already read.
    g_net = _fsopen(path, "w", _SH_DENYWR);
    if (g_net) setvbuf(g_net, NULL, _IONBF, 0);
}

static void NetLog(const char* dir, int size, const void* buf, int ok)
{
    if (g_netLines >= 200000) return;
    DWORD savedErr = GetLastError();
    EnterCriticalSection(&g_cs);
    NetOpen();
    if (g_net) {
        g_netLines++;
        // Timestamped so a wire line can be lined up with the Python-side
        // trace in mods.log. Without this the two logs could not be correlated
        // at all, which is the whole point of capturing both.
        SYSTEMTIME st; GetLocalTime(&st);
        fprintf(g_net, "%02d:%02d:%02d.%03d %-4s n=%-5d ok=%d ",
                st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
                dir, size, ok);
        if (buf && size > 0 && !IsBadReadPtr(buf, size)) {
            const unsigned char* p = (const unsigned char*)buf;
            int show = size < 48 ? size : 48;
            for (int i = 0; i < show; i++) fprintf(g_net, "%02X ", p[i]);
            if (size > show) fprintf(g_net, "...");
            if (size == 1) {
                const char* nm = GcName(p[0]);
                fprintf(g_net, "  <- opcode %u%s%s", p[0],
                        nm ? " " : "", nm ? nm : "");
            }
        }
        fputc('\n', g_net);
    }
    LeaveCriticalSection(&g_cs);
    SetLastError(savedErr);
}

// MARKET-SCAN: shop id + owner name from the last offline-shop OPEN header (0x57),
// consumed by ShopCapture to key each shop's rows. Same-thread, so no locking needed.
static DWORD g_shopPendId = 0;
static char  g_shopPendName[72] = {0};

extern "C" void __cdecl NetLogRecv(void* self, int size, void* buf, int ok)
{
    // MARKET-SCAN: the offline-shop OPEN response reads an 0x57-byte header via this
    // Recv path just before RecvOfflineshopPacket fills the item singleton. Header layout
    // (Frida-confirmed on the deployed client): shopId u32 @+0x00, owner-id u32 @+0x04,
    // name char[] @+0x08 formatted "seller@title". Stash them so ShopCapture (which fires
    // right after, same thread) can key each shop's rows. See ShopCapture.
    if (ok && size == 0x57 && buf && !IsBadReadPtr(buf, 0x57)) {
        g_shopPendId = *(const DWORD*)buf;
        const char* nm = (const char*)buf + 8;
        int n = 0;
        while (n < (int)sizeof(g_shopPendName) - 1 && nm[n]) { g_shopPendName[n] = nm[n]; n++; }
        g_shopPendName[n] = 0;
    }
    // rod-fishing bite detector - see the FISHING note above. GC_FISHING is [hdr, sub,
    // vid:u32]. The reliable "this is OURS" signal is the sub=5 fish-info NOTIFY - the
    // item is ours, so it fires ONLY for our own bites. So we latch our VID from it
    // (never a stranger) and treat it as the bite, and only BOOTSTRAP from the cast->
    // start handshake while our VID is still unknown - we do NOT re-latch every cast
    // (that re-raced a stranger's start each round -> 55s stalls). The bite is counted
    // exactly once (sub=2 if our VID is already right / no item, else the sub=5).
    if (ok && size == 7 && buf && !IsBadReadPtr(buf, 6) &&
        ((const BYTE*)buf)[0] == kGcFishingHdr) {
        BYTE sub = ((const BYTE*)buf)[1];
        DWORD vid = *(DWORD*)((const BYTE*)buf + 2);
        if (sub == 2) {
            g_fishLastBiteVid = (LONG)vid;
            g_fishBiteCounted = 0;
            if (g_fishOurVid && (LONG)vid == g_fishOurVid) {   // known-ours (or no item): count
                g_fishSub = 2;
                InterlockedIncrement(&g_fishSeq);
                InterlockedIncrement(&g_fishBiteSeq);
                g_fishBiteCounted = 1;
            }
        } else if (sub == 5) {                                 // OUR-only fish-info NOTIFY
            g_fishVnum = (LONG)vid;                            // u32 = fish vnum
            if (g_fishLastBiteVid) g_fishOurVid = g_fishLastBiteVid;   // reliable latch/correct
            if (!g_fishBiteCounted) {                          // sub=2 had the wrong VID - count now
                g_fishSub = 2;
                InterlockedIncrement(&g_fishSeq);
                InterlockedIncrement(&g_fishBiteSeq);
                g_fishBiteCounted = 1;
            }
        } else {                                               // start(0)/stop(1)/catch(3)/fail(4)
            if (sub == 0 && g_fishExpectStart && !g_fishOurVid) {  // bootstrap ONLY while unknown
                g_fishOurVid = (LONG)vid;
                g_fishExpectStart = 0;
            }
            if (g_fishOurVid && (LONG)vid == g_fishOurVid) {
                g_fishSub = (LONG)sub;
                InterlockedIncrement(&g_fishSeq);
            }
        }
    }
    (void)self; NetLog("RECV", size, buf, ok);
}
extern "C" void __cdecl NetLogSend(void* self, int size, const void* buf)
{
    // our outgoing cast/reel (CG_FISHING) - arm the VID latch: the next GC_FISHING
    // start that comes back is ours. See the FISHING note by the Recv hook.
    if (size >= 1 && size <= 2 && buf && ((const BYTE*)buf)[0] == kCgFishingHdr) {
        g_fishExpectStart = 1;
        InterlockedIncrement(&g_fishCastSeq);       // our cast/reel reached the wire
    }
    (void)self; NetLog("SEND", size, buf, 1);
}

static __declspec(naked) void RecvHook(void)
{
    __asm {
        push ebp
        mov  ebp, esp
        sub  esp, 4
        push ebx
        push esi
        push edi
        mov  ebx, ecx                   // this
        push dword ptr [ebp+12]         // dest
        push dword ptr [ebp+8]          // size
        mov  ecx, ebx
        mov  eax, g_recvTramp
        call eax                        // original, cleans its own 8 bytes
        movzx eax, al
        mov  [ebp-4], eax
        push eax                        // ok
        push dword ptr [ebp+12]
        push dword ptr [ebp+8]
        push ebx
        call NetLogRecv
        add  esp, 16
        mov  eax, [ebp-4]
        pop  edi
        pop  esi
        pop  ebx
        mov  esp, ebp
        pop  ebp
        ret  8
    }
}

static __declspec(naked) void SendHook(void)
{
    __asm {
        push ebp
        mov  ebp, esp
        push ebx
        push esi
        push edi
        mov  ebx, ecx                   // this
        push dword ptr [ebp+12]         // src  (already filled - log first)
        push dword ptr [ebp+8]          // size
        push ebx
        call NetLogSend
        add  esp, 12
        push dword ptr [ebp+12]
        push dword ptr [ebp+8]
        mov  ecx, ebx
        mov  eax, g_sendTramp
        call eax
        pop  edi
        pop  esi
        pop  ebx
        mov  esp, ebp
        pop  ebp
        ret  8
    }
}

static void* MakeTrampoline(BYTE* target, int stolen)
{
    BYTE* tr = (BYTE*)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE,
                                   PAGE_EXECUTE_READWRITE);
    if (!tr) return NULL;
    memcpy(tr, target, stolen);
    tr[stolen] = 0xE9;
    *(int*)(tr + stolen + 1) = (int)((target + stolen) - (tr + stolen + 5));
    return tr;
}

static bool HookFn(BYTE* target, void* hook, void** tramp, const char* what)
{
    if (IsBadReadPtr(target, 6) || memcmp(target, kNetPrologue, 6) != 0) {
        Log("NET: %s prologue at %p unexpected - not hooking", what, target);
        return false;
    }
    *tramp = MakeTrampoline(target, 6);
    if (!*tramp) { Log("NET: %s trampoline alloc failed", what); return false; }
    DWORD old = 0;
    if (!VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old)) return false;
    target[0] = 0xE9;
    *(int*)(target + 1) = (int)((BYTE*)hook - (target + 5));
    VirtualProtect(target, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, 5);
    Log("NET: %s %p hooked (tramp %p)", what, target, *tramp);
    return true;
}

// --- winsock lifecycle via the IAT (pointer swap, no trampoline) -----------
typedef unsigned int SOCK;
struct sockaddr_min { short family; unsigned short port_be; unsigned char ip[4]; };
typedef int (__stdcall *connect_t)(SOCK, const void*, int);
typedef int (__stdcall *closesocket_t)(SOCK);
typedef int (__stdcall *send_t)(SOCK, const char*, int, int);
typedef int (__stdcall *recv_t)(SOCK, char*, int, int);
typedef int (__stdcall *lasterr_t)(void);
static connect_t     g_realConnect     = NULL;
static closesocket_t g_realClosesocket = NULL;
static send_t        g_realSend        = NULL;
static recv_t        g_realRecv        = NULL;
static lasterr_t     g_realLastErr     = NULL;

static int __stdcall MySend(SOCK s, const char* b, int len, int fl)
{
    int r = g_realSend(s, b, len, fl);
    NetLog("wSND", r > 0 ? r : len, b, r);
    return r;
}
static int __stdcall MyRecv(SOCK s, char* b, int len, int fl)
{
    int r = g_realRecv(s, b, len, fl);
    NetLog("wRCV", r > 0 ? r : 0, b, r);
    return r;
}

static int __stdcall MyConnect(SOCK s, const void* name, int len)
{
    const sockaddr_min* a = (const sockaddr_min*)name;
    int r = g_realConnect(s, name, len);
    int err = (r != 0 && g_realLastErr) ? g_realLastErr() : 0;
    if (a && len >= 8)
        Log("NET: connect sock=%u %u.%u.%u.%u:%u -> ret=%d err=%d%s", s,
            a->ip[0], a->ip[1], a->ip[2], a->ip[3],
            (unsigned)((a->port_be >> 8) | (a->port_be << 8)) & 0xFFFF, r, err,
            err == 10035 ? " (WSAEWOULDBLOCK - normal for non-blocking)" : "");
    return r;
}
static int __stdcall MyClosesocket(SOCK s)
{
    Log("NET: closesocket sock=%u", s);
    return g_realClosesocket(s);
}

static void HookIat(DWORD slotVa, void* hook, void** saved, const char* what)
{
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    void** slot = (void**)(base + (slotVa - kImageBase));
    if (IsBadReadPtr(slot, 4)) return;
    DWORD old = 0;
    if (!VirtualProtect(slot, 4, PAGE_READWRITE, &old)) return;
    *saved = *slot;
    if (hook) *slot = hook;
    VirtualProtect(slot, 4, old, &old);
    Log("NET: IAT %s %p -> %p (orig %p)", what, slot, hook, *saved);
}


// --- HTTP surface: log URLs so we know whether login touches HTTP at all ----

typedef void* (__stdcall *whconnect_t)(void*, const wchar_t*, unsigned short, unsigned long);
typedef void* (__stdcall *whopenreq_t)(void*, const wchar_t*, const wchar_t*, const wchar_t*,
                                       const wchar_t*, const wchar_t**, unsigned long);
typedef void* (__stdcall *iopenurl_t)(void*, const char*, const char*, unsigned long,
                                      unsigned long, unsigned long*);
typedef long  (__stdcall *urldl_t)(void*, const char*, const char*, unsigned long, void*);
static whconnect_t g_realWhConnect = NULL;
static whopenreq_t g_realWhOpenReq = NULL;
static iopenurl_t  g_realOpenUrl   = NULL;
static urldl_t     g_realUrlDl     = NULL;

static void* __stdcall MyWhConnect(void* h, const wchar_t* host, unsigned short port, unsigned long f)
{ Log("HTTP: WinHttpConnect %ls:%u", host ? host : L"(null)", port);
  return g_realWhConnect(h, host, port, f); }

static void* __stdcall MyWhOpenReq(void* h, const wchar_t* verb, const wchar_t* path,
                                   const wchar_t* ver, const wchar_t* ref,
                                   const wchar_t** acc, unsigned long f)
{ Log("HTTP: WinHttpOpenRequest %ls %ls  (flags 0x%lX%s)",
      verb ? verb : L"GET", path ? path : L"/", f, (f & 0x00800000) ? " SECURE" : "");
  return g_realWhOpenReq(h, verb, path, ver, ref, acc, f); }

static void* __stdcall MyOpenUrl(void* h, const char* url, const char* hdr, unsigned long hl,
                                 unsigned long f, unsigned long* ctx)
{ Log("HTTP: InternetOpenUrlA %s", url ? url : "(null)");
  return g_realOpenUrl(h, url, hdr, hl, f, ctx); }

static long __stdcall MyUrlDl(void* pc, const char* url, const char* file, unsigned long r, void* cb)
{ Log("HTTP: URLDownloadToFileA %s -> %s", url ? url : "(null)", file ? file : "(null)");
  return g_realUrlDl(pc, url, file, r, cb); }

static void InstallHttpHooks()
{
    HookIat(g_off.iatWinHttpConnect,     (void*)&MyWhConnect, (void**)&g_realWhConnect, "WinHttpConnect");
    HookIat(g_off.iatWinHttpOpenRequest, (void*)&MyWhOpenReq, (void**)&g_realWhOpenReq, "WinHttpOpenRequest");
    HookIat(g_off.iatInternetOpenUrlA,   (void*)&MyOpenUrl,   (void**)&g_realOpenUrl,   "InternetOpenUrlA");
    HookIat(g_off.iatURLDownloadToFileA, (void*)&MyUrlDl,     (void**)&g_realUrlDl,     "URLDownloadToFileA");
}

static void InstallNetHooks()
{
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    HookFn(base + (g_off.recvBuf    - kImageBase), (void*)&RecvHook, &g_recvTramp, "Recv");
    HookFn(base + (g_off.sendAppend - kImageBase), (void*)&SendHook, &g_sendTramp, "SendAppend");
    HookIat(g_off.iatConnect,     (void*)&MyConnect,     (void**)&g_realConnect,     "connect");
    HookIat(g_off.iatClosesocket, (void*)&MyClosesocket, (void**)&g_realClosesocket, "closesocket");
    HookIat(g_off.iatWSAGetLastError, NULL, (void**)&g_realLastErr, "WSAGetLastError(save-only)");
    HookIat(g_off.iatSend, (void*)&MySend, (void**)&g_realSend, "send");
    HookIat(g_off.iatRecv, (void*)&MyRecv, (void**)&g_realRecv, "recv");
    InstallHttpHooks();
}
#endif


// ---------------------------------------------------------------------------
// Auth-path tracing.  The login stalls after GC_PHASE(10): the client enables
// SetSecurityMode and then emits nothing.  These tail-jump hooks record whether
// each stage is reached.  pushad/popad + `jmp tramp` keeps every calling
// convention intact - we never touch the argument stack or the return address.
// ---------------------------------------------------------------------------
#if ENABLE_AUTH_TRACE
static void* g_tAuthPhase = NULL;
static void* g_tAuthProc  = NULL;
static void* g_tPcName    = NULL;
static void* g_tHwProfile = NULL;
static long  g_authHits[4] = {0,0,0,0};

extern "C" void __cdecl TraceCall(int which)
{
    static const char* names[4] =
        { "__AuthState_RecvPhase", "__AuthState_Process",
          "getPcName", "getHwProfileId" };
    long n = ++g_authHits[which];
    if (n <= 6 || (n % 500) == 0)
        Log("AUTH: %s  (call #%ld)", names[which], n);
}

#define TAILHOOK(fn, idx, tramp)                \
    static __declspec(naked) void fn(void) {    \
        __asm { pushad }                        \
        __asm { pushfd }                        \
        __asm { push idx }                      \
        __asm { call TraceCall }                \
        __asm { add esp, 4 }                    \
        __asm { popfd }                         \
        __asm { popad }                         \
        __asm { jmp tramp }                     \
    }
TAILHOOK(AuthPhaseHook, 0, g_tAuthPhase)
TAILHOOK(AuthProcHook,  1, g_tAuthProc)
TAILHOOK(PcNameHook,    2, g_tPcName)
TAILHOOK(HwProfHook,    3, g_tHwProfile)

static bool HookLen(BYTE* target, void* hook, void** tramp, int stolen, const char* what)
{
    *tramp = MakeTrampoline(target, stolen);
    if (!*tramp) return false;
    DWORD old = 0;
    if (!VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old)) return false;
    target[0] = 0xE9;
    *(int*)(target + 1) = (int)((BYTE*)hook - (target + 5));
    VirtualProtect(target, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, 5);
    Log("AUTH: %s %p hooked (tramp %p, stole %d)", what, target, *tramp, stolen);
    return true;
}

static void InstallAuthHooks()
{
    BYTE* b = (BYTE*)GetModuleHandleW(NULL);
    // 53 8B DC 83 EC 08          -> 6 bytes
    HookLen(b + (g_off.authRecvPhase - kImageBase), (void*)&AuthPhaseHook, &g_tAuthPhase, 6, "__AuthState_RecvPhase");
    HookLen(b + (g_off.authProcess - kImageBase), (void*)&AuthProcHook,  &g_tAuthProc,  6, "__AuthState_Process");
    // 55 8B EC 81 EC xx xx xx xx -> 9 bytes
    HookLen(b + (g_off.getPcName - kImageBase), (void*)&PcNameHook,    &g_tPcName,    9, "getPcName");
    HookLen(b + (g_off.getHwProfileId - kImageBase), (void*)&HwProfHook,    &g_tHwProfile, 9, "getHwProfileId");
}
#endif


// ---------------------------------------------------------------------------
// MARKET-SCAN: offline-shop price capture
//
// Trampoline on CPythonNetworkStream::RecvOfflineshopPacket (FUN_00635020).
// After the original runs, the CPythonOfflineshop singleton's 99-byte item
// records are fully populated for a ShopOpen (case-1) response, so we read the
// instance = *(*(kOfflineshopInst)) (DOUBLE indirection), skip private-shop
// packets (IsPrivate byte @ +0x5446 != 0), and append each item's
// vnum,price,count,ts to <_patcher>\prices.csv.  A mod floods SendOpenShop; this
// captures every response's prices, decoupled from the frame pump.
//
// The handler is __thiscall (ecx=this), no stack args, plain `ret` (0xC3), so the
// detour is: call original via trampoline, then capture, then plain ret.
// Inert unless <_patcher>\shopcap.on exists (checked at install), so it costs
// nothing during normal play - drop that file (and restart) to arm a scan.
// ---------------------------------------------------------------------------
#if ENABLE_SHOP_CAPTURE
static const DWORD kShopArr     = 0x5458;   // item array, relative to instance (Frida-confirmed, deployed)
static const DWORD kShopStride  = 0x63;     // 99 bytes/record
static const DWORD kShopIsPriv  = 0x5446;   // byte: !=0 => private-shop packet, skip
static const int   kShopMaxSlot = 108;      // 0x6c
static const DWORD kRecVnum  = 0x00;        // u32
static const DWORD kRecCount = 0x04;        // u32
static const DWORD kRecSock  = 0x08;        // u32[3] metin sockets / (container: skill|monster in [0])
static const DWORD kRecAttr  = 0x20;        // 7x { int16 type, int16 value }, stride 4 (rolled bonuses)
static const DWORD kRecPrice = 0x5b;        // u64

static void* g_shopTramp = NULL;
static FILE* g_prices = NULL;
static time_t g_pricesOpened = 0;            // when the current snapshot file was opened
static const int kPriceRotateSecs = 900;     // fallback cap; primary trigger is the sweep-boundary marker
static long  g_shopHits = 0;
static long  g_priceRows = 0;
static long  g_shopPrivHits = 0;   // of the captured shops, how many had IsPriv!=0
static bool  g_shopArmed = false;

// CSV-quote a string field: wrap in quotes, double any internal quote. Shop titles
// carry commas / quotes / UTF-8, so seller + title must be quoted.
static void ShopCsvQuote(char* out, size_t outsz, const char* in)
{
    size_t o = 0;
    if (o + 1 < outsz) out[o++] = '"';
    for (const char* p = in; *p && o + 2 < outsz; p++) {
        if (*p == '"') { if (o + 3 < outsz) out[o++] = '"'; else break; }
        out[o++] = *p;
    }
    if (o + 1 < outsz) out[o++] = '"';
    out[o] = 0;
}

extern "C" void __cdecl ShopCapture()
{
    if (!g_shopArmed || !g_off.offlineshopInst) return;
    DWORD savedErr = GetLastError();
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    DWORD* g = (DWORD*)(base + (g_off.offlineshopInst - kImageBase));
    if (IsBadReadPtr(g, 4) || !*g) { SetLastError(savedErr); return; }
    DWORD* p1 = (DWORD*)(*g);                            // double indirection
    if (IsBadReadPtr(p1, 4) || !*p1) { SetLastError(savedErr); return; }
    BYTE* inst = (BYTE*)(*p1);
    if (IsBadReadPtr(inst + kShopArr, kShopStride)) { SetLastError(savedErr); return; }
    // Previously skipped when this byte != 0 ("private-shop packet"). Now captured
    // too — the sweep was likely dropping real shops. Tallied below to measure the
    // split (SHOPCAPP marker).
    BYTE isPriv = inst[kShopIsPriv];

    // shopId + owner from the OPEN header captured in NetLogRecv for THIS packet.
    DWORD shopId = g_shopPendId;
    char seller[72] = {0}, title[72] = {0};
    {
        const char* at = strchr(g_shopPendName, '@');
        if (at) {
            size_t sl = (size_t)(at - g_shopPendName);
            if (sl > sizeof(seller) - 1) sl = sizeof(seller) - 1;
            memcpy(seller, g_shopPendName, sl); seller[sl] = 0;
            strncpy_s(title, sizeof(title), at + 1, _TRUNCATE);
        } else {
            strncpy_s(seller, sizeof(seller), g_shopPendName, _TRUNCATE);
        }
    }
    char qseller[160], qtitle[160];
    ShopCsvQuote(qseller, sizeof(qseller), seller);
    ShopCsvQuote(qtitle, sizeof(qtitle), title);

    EnterCriticalSection(&g_cs);
    // Each CSV file is ONE snapshot the uploader ships. Rotate at a full-sweep boundary
    // (a mod may drop a "prices_rotate.req" marker when its rolling sweep wraps),
    // so a snapshot == one complete pass: every live shop appears exactly once and the
    // server's shop_gone diff is reliable. A time cap is the fallback if a sweep wedges.
    char rotReq[MAX_PATH];
    _snprintf_s(rotReq, sizeof(rotReq), _TRUNCATE, "%sprices_rotate.req", g_dataDir);
    bool sweepBoundary = (GetFileAttributesA(rotReq) != INVALID_FILE_ATTRIBUTES);
    bool timedOut = (g_pricesOpened && (time(NULL) - g_pricesOpened) >= kPriceRotateSecs);
    if (g_prices && (sweepBoundary || timedOut)) {
        fclose(g_prices); g_prices = NULL;
    }
    if (sweepBoundary) DeleteFileA(rotReq);   // consume the marker either way
    if (!g_prices) {
        // One timestamped file per snapshot (prices_YYYYMMDD_HHMMSS.csv).
        SYSTEMTIME st; GetLocalTime(&st);
        char path[MAX_PATH];
        _snprintf_s(path, sizeof(path), _TRUNCATE,
                    "%sprices_%04d%02d%02d_%02d%02d%02d.csv", g_dataDir,
                    st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
        g_prices = _fsopen(path, "a", _SH_DENYWR);
        if (g_prices) {
            setvbuf(g_prices, NULL, _IONBF, 0);
            g_pricesOpened = time(NULL);
            if (ftell(g_prices) == 0)
                fprintf(g_prices, "shopId,seller,title,vnum,price,count,socket0,socket1,socket2,attrs,ts\n");
            Log("SHOPCAP: writing %s", path);
        }
    }
    unsigned long ts = (unsigned long)time(NULL);
    int wrote = 0;
    for (int i = 0; i < kShopMaxSlot; i++) {
        BYTE* rec = inst + kShopArr + (DWORD)i * kShopStride;
        DWORD vnum = *(DWORD*)(rec + kRecVnum);
        if (!vnum) continue;                            // empty slot (sparse fill)
        DWORD count = *(DWORD*)(rec + kRecCount);
        unsigned long long price = *(unsigned long long*)(rec + kRecPrice);
        DWORD s0 = *(DWORD*)(rec + kRecSock);
        DWORD s1 = *(DWORD*)(rec + kRecSock + 4);
        DWORD s2 = *(DWORD*)(rec + kRecSock + 8);
        // rolled bonuses -> compact "type:value|type:value" (only nonzero types)
        char attrs[128]; int ao = 0; attrs[0] = 0;
        for (int a = 0; a < 7; a++) {
            short at = *(short*)(rec + kRecAttr + a * 4);
            short av = *(short*)(rec + kRecAttr + a * 4 + 2);
            if (at)
                ao += _snprintf_s(attrs + ao, sizeof(attrs) - ao, _TRUNCATE,
                                  "%s%d:%d", ao ? "|" : "", (int)at, (int)av);
        }
        if (g_prices) {
            fprintf(g_prices, "%u,%s,%s,%u,%llu,%u,%u,%u,%u,%s,%lu\n",
                    shopId, qseller, qtitle, vnum, price, count, s0, s1, s2, attrs, ts);
            wrote++;
        }
    }
    if (wrote) {
        g_shopHits++; g_priceRows += wrote;
        if (isPriv) g_shopPrivHits++;
        if (g_shopHits <= 3 || (g_shopHits % 1000) == 0)
            Log("SHOPCAPP: shop #%ld +%d rows (%ld total rows, %ld/%ld shops priv-flagged)",
                g_shopHits, wrote, g_priceRows, g_shopPrivHits, g_shopHits);
    }
    LeaveCriticalSection(&g_cs);
    SetLastError(savedErr);
}

static __declspec(naked) void OfflineShopHook(void)
{
    __asm {
        push ecx                 // preserve `this` (thiscall) across the capture call
        mov  eax, g_shopTramp
        call eax                 // original handler runs (ecx=this), plain ret -> back here
        push eax                 // save the handler's return value
        call ShopCapture         // cdecl, no args, cleans nothing
        pop  eax                 // restore return value
        pop  ecx
        ret                      // plain ret, matches FUN_00635020's 0xC3
    }
}

static void InstallShopCapture()
{
    if (!g_off.offlineshopRecv || !g_off.offlineshopInst) {
        Log("SHOPCAP: offsets absent - price capture off");
        return;
    }
    char on[MAX_PATH];
    _snprintf_s(on, sizeof(on), _TRUNCATE, "%sshopcap.on", g_dataDir);
    g_shopArmed = GetFileAttributesA(on) != INVALID_FILE_ATTRIBUTES;

    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    BYTE* target = base + (g_off.offlineshopRecv - kImageBase);
    // Validate the realign prologue before stealing: 53 8B DC 83 EC xx
    if (IsBadReadPtr(target, 6) || target[0] != 0x53 || target[1] != 0x8B ||
        target[2] != 0xDC || target[3] != 0x83 || target[4] != 0xEC) {
        Log("SHOPCAP: prologue at %p unexpected (%02X %02X %02X %02X %02X) - not hooking",
            target, target[0], target[1], target[2], target[3], target[4]);
        return;
    }
    g_shopTramp = MakeTrampoline(target, 6);
    if (!g_shopTramp) { Log("SHOPCAP: trampoline alloc failed"); return; }
    DWORD old = 0;
    if (!VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old)) return;
    target[0] = 0xE9;
    *(int*)(target + 1) = (int)((BYTE*)&OfflineShopHook - (target + 5));
    VirtualProtect(target, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, 5);
    Log("SHOPCAP: hooked RecvOfflineshopPacket %p (inst global %08X) armed=%d - prices.csv",
        target, g_off.offlineshopInst, g_shopArmed ? 1 : 0);
}
#endif


// ---------------------------------------------------------------------------
// Mod host
//
// The client is already a Python application (CPython 3.14, ~2,500 native
// bindings), so the DLL's whole job is to get ONE string executed inside the
// live interpreter. Everything after that is Python and hot-reloadable.
//
// The primitive is CPythonLauncher::RunLine(const char*) - a __thiscall that
// evaluates a string in the launcher's main dict. It takes a plain char*, so
// no CPython C API is needed. That matters: CPython 3.14 interns identifier
// literals as immortal _Py_ID() objects rather than `const char*`, so its own
// entry points have no string-push sites to anchor an offset on, while the
// game's own wrapper does (PythonLauncher.cpp is named in its trace calls).
//
// `this` is derived exactly as app.RunPythonFile does it - TWO derefs:
//     this = *(void**)( *(void**)kPyLauncherInst )
//
// Called from slot 1, which the client invokes per frame from
// CPythonApplication::Process - i.e. the main thread, with the interpreter up.
// ---------------------------------------------------------------------------
#if ENABLE_MODS

typedef int (__thiscall *RunLine_t)(void* self, const char* code);

static bool  g_modsOn      = false;   // mods dir present and not disabled
static bool  g_modsReady   = false;   // bootstrap succeeded
static bool  g_modsDead    = false;   // gave up; stop touching Python
static int   g_bootTries   = 0;
static int   g_pumpFails   = 0;
static char  g_modsDir[MAX_PATH];     // <exe>\mods   (no trailing slash)
static const unsigned kPumpEvery = 1; // MARKET-SCAN: pump every frame. On the
                                      // a GPU-less VM (~8 fps) kPumpEvery=6 gave
                                      // ~1.4 Hz, which frame-bound the offline-shop
                                      // scanner to ~1.4 shops/s. Every-frame -> ~8 Hz.
                                      // (was 6) ~10 Hz at 60 fps: a compile per pump,
                                      // so do not run this every frame

// Two constraints shape this string, both learned the hard way on a live run:
//
//  * `import traceback` FAILS. The client swaps __import__ for Metin2's hybrid
//    importer (system.py:461), which falls through to zipimport on the stdlib
//    appended to the exe - and that zip is broken in the unuriel-rebuilt
//    triarch_clean.exe ("bad local file header"). Only modules already resident
//    in sys.modules can be imported. os/sys/builtins are; traceback is not.
//    So the failure path formats the exception by hand.
//
//  * It must NOT let an exception escape. RunLine's error path calls the
//    client's traceback printer, which pops a modal dialog - once per retry.
//    On failure we install a no-op pump and record the reason in mods.log.
//
// %% is escaped throughout: this string goes through _snprintf_s first.
// It reports its outcome through the PROCESS ENVIRONMENT, not a file. A silent
// bootstrap already cost one debugging round: RunLine said "ok" while nothing
// reached mods.log, and both the success and failure paths swallow exceptions,
// so there was no signal at all. os.environ round-trips to the Win32 block, so
// the stub can read the result with GetEnvironmentVariableA and put it in
// uriel_stub.log - a log that is written by C and therefore known to work.
//
// It also probes whether open() accepts encoding=/errors=, since a pack-aware
// open() in the client would reject them and silently break all Python logging.
static const char* kBootstrapFmt =
    "import sys, os, builtins\n"
    "_d = r'%s'\n"
    "_l = r'%s'\n"
    "_lg = os.path.join(_l, 'mods.log')\n"
    "def _tri_w(_s):\n"
    "    try:\n"
    "        _f = open(_lg, 'a')\n"
    "        try:\n"
    "            _f.write(_s)\n"
    "        finally:\n"
    "            _f.close()\n"
    "        return 'write-ok'\n"
    "    except BaseException as _e:\n"
    "        return 'write-FAIL:' + type(_e).__name__\n"
    "def _tri_probe():\n"
    "    try:\n"
    "        _f = open(_lg, 'a', encoding='utf-8', errors='replace')\n"
    "        _f.close()\n"
    "        return 'kwargs-ok'\n"
    "    except BaseException as _e:\n"
    "        return 'kwargs-FAIL:' + type(_e).__name__\n"
    "def _tri_boot():\n"
    "    _p = os.path.join(_d, 'modhost.py')\n"
    "    _g = {'__name__': 'triarch_modhost', '__file__': _p,\n"
    "          'MODS_DIR': _d, 'LOG_DIR': _l}\n"
    "    exec(compile(open(_p, 'rb').read(), _p, 'exec'), _g)\n"
    "    builtins._triarch_pump = _g['pump']\n"
    "    _g['boot']()\n"
    "_st = 'open=%%r %%s' %% (open, _tri_probe())\n"
    "try:\n"
    "    _tri_boot()\n"
    "    _st = 'ok ' + _st\n"
    "except BaseException:\n"
    "    builtins._triarch_pump = lambda: None\n"
    "    _et, _ev, _tb = sys.exc_info()\n"
    "    _st = 'FAILED %%s: %%s ' %% (getattr(_et,'__name__',_et), _ev) + _st\n"
    "    while _tb is not None:\n"
    "        _c = _tb.tb_frame.f_code\n"
    "        _st += ' | %%s:%%d in %%s' %% (_c.co_filename, _tb.tb_lineno, _c.co_name)\n"
    "        _tb = _tb.tb_next\n"
    "_st = _st + ' ' + _tri_w('bootstrap: ' + _st + '\\n')\n"
    "try:\n"
    "    os.environ['TRIARCH_MODS'] = _st[:900]\n"
    "except BaseException:\n"
    "    pass\n";

static void* LauncherThis()
{
    if (!g_off.pyLauncherInst) return NULL;
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    void** holder = (void**)(base + (g_off.pyLauncherInst - kImageBase));
    if (IsBadReadPtr(holder, 4) || !*holder) return NULL;
    void** obj = (void**)*holder;
    if (IsBadReadPtr(obj, 4) || !*obj) return NULL;
    void* self = *obj;
    // RunLine reads the main dict at this+8 and passes it to PyRun_StringFlags
    // as both globals and locals. NULL there faults inside CPython, so refuse
    // to call until the launcher is fully constructed.
    if (IsBadReadPtr((BYTE*)self + 8, 4) || !*(void**)((BYTE*)self + 8)) return NULL;
    return self;
}

static bool PyRunLine(const char* code)
{
    void* self = LauncherThis();
    if (!self) return false;
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    RunLine_t fn = (RunLine_t)(base + (g_off.pyRunLine - kImageBase));
    return fn(self, code) != 0;
}

static void ModsInit()
{
    if (!g_off.pyRunLine || !g_off.pyLauncherInst) {
        Log("MODS: offsets absent - mod host disabled");
        return;
    }
    _snprintf_s(g_modsDir, sizeof(g_modsDir), _TRUNCATE, "%smods", g_exeDir);
    DWORD a = GetFileAttributesA(g_modsDir);
    if (a == INVALID_FILE_ATTRIBUTES || !(a & FILE_ATTRIBUTE_DIRECTORY)) {
        Log("MODS: no %s - mod host idle", g_modsDir);
        return;
    }
    char off[MAX_PATH];
    _snprintf_s(off, sizeof(off), _TRUNCATE, "%smods.off", g_dataDir);
    if (GetFileAttributesA(off) != INVALID_FILE_ATTRIBUTES) {
        Log("MODS: mods.off present - disabled for this run");
        return;
    }
    g_modsOn = true;
    Log("MODS: enabled, dir=%s", g_modsDir);
}

// ---------------------------------------------------------------------------
// Native attack bridge
//
// WHY THIS EXISTS
// The client attacks by calling CPythonPlayer::__OnPressActor, which writes an
// auto-attack target into the player object; CPythonPlayer::__Update_AutoAttack
// then sustains the swing every frame until the target dies. No mouse is
// involved anywhere. But __OnPressActor is not exposed to Python, and the two
// bindings that look like they would do the job cannot:
//
//   * AttackPickedActor() reads OLD_GetPickedInstanceVID - the actor under the
//     CURSOR - and nothing can set that pick from a script.
//   * SetAttackKeyState() never reaches the auto-attack target, and it holds
//     the key down, which blocks movement.
//
// So exactly one missing primitive is bridged here. The upstream fix is a
// two-line Python binding on the developers' side; when that ships, this block
// can be deleted and api.attack() repointed with no change to any mod.
//
// CHANNEL
// Python sets the environment variable TRIARCH_ATTACK to the target VID (empty
// or "0" to disarm). The mod bootstrap already reads status back out of
// TRIARCH_MODS the same way, so the mechanism is proven on this client and
// costs no file I/O.
//
// __OnPressActor is called only when the VID *changes*: __Update_AutoAttack
// keeps hitting the same target by itself, so re-issuing every frame would
// fight the client's own state machine.
// ---------------------------------------------------------------------------
typedef void  (__thiscall *OnPressActor_t)(void* self, void* pMain, DWORD vid, int flag);
typedef void* (__thiscall *GetMainInst_t)(void* self);

// The player's own CInstanceBase, via the vtable on the sub-object at self+4.
// HuntTick needs it in two places now (skills, and acquisition), so it is
// hoisted here. AttackTick keeps its own inline copy deliberately: it disarms
// onPressActor if the call faults, which this helper cannot express.
//
// Returns NULL on any bad read - callers must handle that, it is the normal
// state on the login and character-select phases.
static void* MainInst(void* self)
{
    if (!self || !g_off.getMainInstOff) return NULL;
    void* sub = (char*)self + g_off.playerSubObj;
    if (IsBadReadPtr(sub, sizeof(void*))) return NULL;
    void** vtbl = *(void***)sub;
    if (IsBadReadPtr(vtbl, g_off.getMainInstOff + sizeof(void*))) return NULL;
    __try {
        GetMainInst_t getMain =
            (GetMainInst_t)vtbl[g_off.getMainInstOff / sizeof(void*)];
        return getMain(sub);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return NULL; }
}

static DWORD g_lastAttackVid = 0;

static void* PlayerThis()
{
    // Two derefs, exactly as the AttackPickedActor binding does it:
    //     mov eax,[kPlayerInst] / mov edi,[eax]
    if (!g_off.playerInst) return NULL;
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    void** holder = (void**)(base + (g_off.playerInst - kImageBase));
    if (IsBadReadPtr(holder, sizeof(void*)) || !*holder) return NULL;
    void** p = (void**)*holder;
    if (IsBadReadPtr(p, sizeof(void*))) return NULL;
    return *p;
}

static void AttackTick()
{
    if (!g_off.onPressActor || !g_off.playerInst || !g_off.getMainInstOff) return;
    if ((g_tick % 3) != 0) return;

    char buf[32] = {0};
    DWORD n = GetEnvironmentVariableA("TRIARCH_ATTACK", buf, sizeof(buf));
    DWORD vid = (n && n < sizeof(buf)) ? (DWORD)strtoul(buf, NULL, 10) : 0;
    if (!vid) { g_lastAttackVid = 0; return; }
    if (vid == g_lastAttackVid) return;

    void* self = PlayerThis();
    if (!self) return;
    void* sub = (char*)self + g_off.playerSubObj;
    if (IsBadReadPtr(sub, sizeof(void*))) return;
    void** vtbl = *(void***)sub;
    if (IsBadReadPtr(vtbl, g_off.getMainInstOff + sizeof(void*))) return;

    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    OnPressActor_t fn = (OnPressActor_t)(base + (g_off.onPressActor - kImageBase));
    __try {
        GetMainInst_t getMain =
            (GetMainInst_t)vtbl[g_off.getMainInstOff / sizeof(void*)];
        void* pMain = getMain(sub);
        if (!pMain) return;
        fn(self, pMain, vid, 0);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // One bad call must not take the client down. Disarm, so we do not
        // fault once per frame for the rest of the session.
        g_off.onPressActor = 0;
        Log("ATTACK: __OnPressActor faulted - bridge disabled for this session");
        return;
    }
    g_lastAttackVid = vid;
    Log("ATTACK: __OnPressActor(vid=%u) issued", vid);
}

// ---------------------------------------------------------------------------
// Native autohunt drive
//
// The shipped autohunt is one guarded call on its tick. Verified live with
// Frida (BOT-SYSTEMS.md 7.3c): 228 ticks, 9 acquisitions, 8 completed kills,
// 0 faults.
//
//     if (player[autoAtkVidOff] == 0)
//         FindAndSetNewTarget(player, pMain, bStone, excludeVID);
//
// FindAndSetNewTarget does everything: FindVictim (including the line-of-sight
// ray march that is unreachable from Python), SetTarget, __OnPressActor and the
// walk-in. The client's own __Update_AutoAttack then sustains the swing until
// the target dies and zeroes the VID, which re-opens the guard. So one rule is
// both the acquisition guard AND the re-arm.
//
// THE GUARD IS REQUIRED, not an optimisation. Driving the call unconditionally
// at 4 Hz re-targets every tick, never commits to a kill, and eventually
// faults with an access violation - measured.
//
// Python controls it through TRIARCH_HUNT = "on|bStone|excludeVID". Level
// driven, not command driven, so there is no sequence number to keep and a
// dropped update simply corrects itself on the next tick.
//
// excludeVID is the ONLY thing the mod adds: on body-block it names the stuck
// target so the client picks a different victim itself.
// ---------------------------------------------------------------------------
typedef void (__thiscall *FindAndSet_t)(void* self, void* pMain, int bStone, DWORD excludeVid);

// CPythonPlayer::UseAutoSkills(CInstanceBase* pMain, CInstanceBase* pTarget,
//                              __int64 now)   -- thiscall, ret 16
//
// `now` is time(NULL) in SECONDS and is 64-bit on both builds, which is why the
// x86 call site pushes four dwords. Getting that wrong misaligns the stack by
// four bytes on every call.
typedef void (__thiscall *UseAutoSkills_t)(void* self, void* pMain, void* pTarget,
                                           __int64 now);

static DWORD g_huntTick = 0;
static DWORD g_lastAcquireVid = 0;
static DWORD g_skillNote = 0;       // one-shot diagnostics, see SkillTick()
static DWORD g_failAcquire = 0;     // consecutive fruitless FindAndSetNewTarget

// What the mod cannot see for itself, published back the same way it publishes
// TRIARCH_HUNT:  "<searchRadius>|<consecutiveFailedAcquisitions>"
//
// The failure count is the thing that matters. "No target right now" is not the
// same as "nothing left to hunt" - it is also true for the fraction of a second
// between one mob dying and the next being picked. Only the client repeatedly
// searching and coming back empty means the area is actually clear, and only
// the stub is in a position to count that.
static void PublishHuntStat(float radius)
{
    static char last[64] = {0};
    char buf[64];
    _snprintf_s(buf, sizeof(buf), _TRUNCATE, "%.0f|%u", radius, g_failAcquire);
    if (strcmp(buf, last) == 0) return;
    strcpy_s(last, sizeof(last), buf);
    SetEnvironmentVariableA("TRIARCH_HUNT_STAT", buf);
}

// The autohunt range slider's field, or NULL. Double indirection: the singleton
// holder holds a pointer to the holder of the object.
static float* MiniMapRange()
{
    if (!g_off.miniMapInst || !g_off.autoHuntRangeOff) return NULL;
    __try {
        BYTE* base = (BYTE*)GetModuleHandleW(NULL);
        if (!base) return NULL;
        void** holder = (void**)(base + (g_off.miniMapInst - kImageBase));
        if (IsBadReadPtr(holder, sizeof(void*)) || !*holder) return NULL;
        void* mm = *(void**)*holder;
        if (!mm || IsBadReadPtr(mm, g_off.autoHuntRangeOff + 4)) return NULL;
        return (float*)((char*)mm + g_off.autoHuntRangeOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return NULL; }
}

// ---------------------------------------------------------------------------
// Skills
//
// AutoHuntLoop ends with, in full:
//
//     if (player[huntUseSkillOff])                       // x86 0x00651EF8
//         UseAutoSkills(pMain, pTarget, time(NULL));     // x86 0x00651F0D
//
// and reaches that line whether or not a target exists - the branch at
// 0x00651E6B jumps straight to the test when pTarget is NULL. Standing (buff)
// skills need no target; the attack branch inside checks for NULL itself. So we
// mirror it exactly and do not gate on being engaged.
//
// Calling at our 4 Hz cannot outpace the shipped behaviour: UseAutoSkills opens
// with `if (nextSkillTime > now) return` and sets nextSkillTime to now+2..+4
// after every action, so the client rate limits itself.
//
// TELEMETRY - the whole reason this was deferred until now.
// UseAutoSkills is the one autohunt function that can call SendChatPacket. It
// sends "/user_horse_ride", from three sites (x86 0x650AE0 / 0x650D46 /
// 0x650D6F), to dismount before casting and to remount afterwards. Every one of
// them is downstream of IsMountingHorse(), which this build inlines as a byte
// read at CInstanceBase+0x14:
//
//     0x650C7F  jne -> dismount+send      (taken only when mounted)
//     0x650D13  jne -> dismount+send      (taken only when mounted)
//     0x650AD2  jne -> skip the send      (remount; needs pending=1, which only
//                                          the dismount branches ever set)
//
// So an unmounted player cannot reach any of them, and the remount path cannot
// arm itself without a dismount first. Refusing to call while mounted is
// therefore a complete guarantee of silence, not a heuristic - which is why the
// gate is on the mount byte rather than on a "please be careful" flag.
// ---------------------------------------------------------------------------
static void SkillTick(void* self, void* pMain, int allowMounted)
{
    if (!g_off.useAutoSkills || !g_off.huntUseSkillOff || !pMain) return;

    __try {
        if (!*(BYTE*)((char*)self + g_off.huntUseSkillOff)) {
            // The player has not ticked "use skills" in the autohunt window, or
            // the settings block was never built. Distinguishing those two is
            // the mod's job (it calls CreateAutoBotSettings); say so once.
            if (!(g_skillNote & 1)) {
                g_skillNote |= 1;
                Log("SKILL: hunt_use_skill is 0 - no skills will be cast");
            }
            return;
        }
        if (g_off.mountedOff && !allowMounted &&
            *(BYTE*)((char*)pMain + g_off.mountedOff)) {
            if (!(g_skillNote & 2)) {
                g_skillNote |= 2;
                Log("SKILL: mounted - skipping UseAutoSkills so it cannot emit "
                    "/user_horse_ride");
            }
            return;
        }
        // Report what the player actually selected, once. An empty list is the
        // single most likely reason for "skills do nothing".
        if (g_off.huntSkillVecOff && !(g_skillNote & 4)) {
            g_skillNote |= 4;
            BYTE** v = (BYTE**)((char*)self + g_off.huntSkillVecOff);
            Log("SKILL: enabled, %d slot(s) configured", (int)(v[1] - v[0]));
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { return; }

    __try {
        // The cached auto-attack actor, the same pointer __Update_AutoAttack
        // validates every frame. NULL is legal and expected between kills.
        void* pTarget = *(void**)((char*)self + g_off.autoAtkVidOff + 4);
        UseAutoSkills_t fn = (UseAutoSkills_t)((BYTE*)GetModuleHandleW(NULL) +
                                               (g_off.useAutoSkills - kImageBase));
        fn(self, pMain, pTarget, (__int64)_time64(NULL));
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_off.useAutoSkills = 0;
        Log("SKILL: UseAutoSkills faulted - skills disabled for this session");
    }
}

static void HuntTick()
{
    if (!g_off.findAndSetNewTarget || !g_off.playerInst ||
        !g_off.getMainInstOff || !g_off.autoAtkVidOff) return;
    if ((++g_huntTick % 15) != 0) return;      // ~4 Hz at 60 fps, as AutoHuntLoop

    char buf[128] = {0};
    DWORD n = GetEnvironmentVariableA("TRIARCH_HUNT", buf, sizeof(buf));
    if (!n || n >= sizeof(buf)) return;
    int on = 0, bStone = 0; unsigned exclude = 0;
    float ax = 0.0f, ay = 0.0f, range = 0.0f;
    // The last two fields were added after the first shipped stub. sscanf_s
    // stops at whatever the string actually has and leaves the rest at their
    // initialisers, so an older mod publishing six fields still parses - it
    // just gets skills off. Never make a new field a parse failure.
    int useSkills = 0, skillsMounted = 0;
    if (sscanf_s(buf, "%d|%d|%u|%f|%f|%f|%d|%d",
                 &on, &bStone, &exclude, &ax, &ay, &range,
                 &useSkills, &skillsMounted) < 1) return;
    if (!on) {                              // re-arm the per-hunt state
        g_skillNote = 0;
        g_failAcquire = 0;
        return;
    }

    void* self = PlayerThis();
    if (!self) return;

    // The hunt centre. FindVictim searches around player[anchorOff], which is
    // normally written by AutoHuntSettings when the server starts a hunt - and
    // we never send /auto_hunt, so on a fresh client it is stale or zero and
    // the search would happen around the wrong point. Python supplies it.
    //
    // Writing it is safe: AutoHuntLoop is gated on the minimap STATUS flag,
    // which is a different field and stays clear, so the native loop remains
    // inert. Only write on change, so we are not touching client state 4x a
    // second for no reason.
    if (g_off.anchorOff && (ax != 0.0f || ay != 0.0f)) {
        __try {
            float* a = (float*)((char*)self + g_off.anchorOff);
            if (a[0] != ax || a[1] != ay) { a[0] = ax; a[1] = ay; }
        } __except (EXCEPTION_EXECUTE_HANDLER) { }
    }
    // Search radius. FindAndSetNewTarget computes (minimap[rangeOff] + 40) * 35,
    // and minimap[rangeOff] is EXACTLY where the autohunt window's range slider
    // writes - the miniMapSetAutoHuntRange binding is
    // `[this+rangeOff] = f + 40; __SetPosition()`. So writing it here every tick
    // is what made the slider appear to do nothing.
    //
    // range <= 0 now means "leave it alone, the player owns it". Python sends 0
    // unless it is deliberately forcing a value.
    if (range > 0.0f) {
        __try {
            float* r = MiniMapRange();
            if (r && *r != range) *r = range;
        } __except (EXCEPTION_EXECUTE_HANDLER) { }
    }

    void* pMain = MainInst(self);
    if (!pMain) return;

    // Mobs or metin stones. AutoHuntLoop takes this straight from the settings
    // block (`movzx eax, byte [edi+huntStoneOff]` at both of its
    // FindAndSetNewTarget call sites), which is where the autohunt window's
    // "stones only" choice lands via CreateAutoBotSettings. bStone < 0 means
    // "whatever the player picked"; 0 or 1 is Python forcing it.
    if (bStone < 0) {
        int fromClient = 0;
        if (g_off.huntStoneOff) {
            __try {
                fromClient = *(BYTE*)((char*)self + g_off.huntStoneOff) ? 1 : 0;
            } __except (EXCEPTION_EXECUTE_HANDLER) { fromClient = 0; }
        }
        bStone = fromClient;
        if (!(g_skillNote & 8)) {
            g_skillNote |= 8;
            float* r = MiniMapRange();
            Log("HUNT: stones=%d (autohunt window), range=%.0f -> radius %.0f",
                bStone, r ? *r : 0.0f, r ? (*r + 40.0f) * 35.0f : 0.0f);
        }
    }

    // Before the engagement guard, because AutoHuntLoop does it that way: buffs
    // are maintained whether or not we currently have a target.
    if (useSkills) SkillTick(self, pMain, skillsMounted);

    // The guard. Non-zero means the client is already engaged - leave it be.
    DWORD vid = 0;
    __try {
        vid = *(DWORD*)((char*)self + g_off.autoAtkVidOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return; }

    if (vid) {
        // ...unless Python has told us this very target is the one we are
        // body-blocked on. Excluding it alone achieves nothing: exclusion only
        // applies at ACQUISITION, and the guard above means we never re-acquire
        // while engaged - so the client keeps swinging at a mob it cannot
        // reach. Measured: the same VID re-blocked four times in a row.
        //
        // So end the engagement, which re-opens the guard on the next tick and
        // lets FindAndSetNewTarget choose again, this time with the exclusion
        // applied.
        //
        // The three fields are written together, exactly as the client's own
        // inlined clear does. That consistency is what matters: __key__b0t__
        // trips when the cached actor pointer and the VID disagree, and
        // __Update_AutoAttack short-circuits on `if (!pTarget)` before it ever
        // compares them. Zeroing all three leaves no inconsistent window.
        //
        // Drop and re-acquire in the SAME tick. Returning here instead cost a
        // full tick of standing still before the replacement was chosen, on top
        // of the detection window and the env-var hop - and the whole point of
        // the feature is how fast it unsticks.
        bool dropped = false;
        if (exclude && vid == exclude) {
            __try {
                DWORD* p = (DWORD*)((char*)self + g_off.autoAtkVidOff);
                p[0] = 0;                       // vid
                p[-1] = 0;                      // the duplicate vid, 4 bytes below
                p[1] = 0;                       // cached actor pointer
                dropped = true;
                Log("HUNT: dropped body-blocked vid=%u, re-acquiring now", vid);
            } __except (EXCEPTION_EXECUTE_HANDLER) { }
        }
        if (!dropped) {
            g_lastAcquireVid = vid;
            g_failAcquire = 0;          // engaged: by definition not out of targets
            float* r = MiniMapRange();
            PublishHuntStat(r ? (*r + 40.0f) * 35.0f : 0.0f);
            return;
        }
    }

    __try {
        FindAndSet_t fn = (FindAndSet_t)((BYTE*)GetModuleHandleW(NULL) +
                                         (g_off.findAndSetNewTarget - kImageBase));
        fn(self, pMain, bStone, exclude);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_off.findAndSetNewTarget = 0;
        Log("HUNT: FindAndSetNewTarget faulted - drive disabled for this session");
        return;
    }

    __try {
        DWORD got = *(DWORD*)((char*)self + g_off.autoAtkVidOff);
        if (got) {
            g_failAcquire = 0;
            if (got != g_lastAcquireVid) {
                g_lastAcquireVid = got;
                Log("HUNT: acquired vid=%u (exclude=%u stone=%d)", got, exclude, bStone);
            }
        } else if (g_failAcquire < 0xFFFF) {
            g_failAcquire++;            // searched, found nothing
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { }

    {
        float* r = MiniMapRange();
        PublishHuntStat(r ? (*r + 40.0f) * 35.0f : 0.0f);
    }
}

// ===========================================================================
// triarch_native - a REAL Python module, registered with the client's own
// machinery, so mods can call unexported natives synchronously.
//
// WHY A MODULE AND NOT AN ENV-VAR CHANNEL. Mods run inside this stub's own
// per-frame tick, so anything queued for "next tick" cannot return a value to
// the caller that queued it. Registering a module makes the call an ordinary,
// in-process, synchronous Python call.
//
// WHY ONE ENTRY POINT. PyCFunction receives the MODULE as `self`, so one
// PyMethodDef per native would need a generated code thunk per native to carry
// its identity. A single `call(name, ...)` dispatching from the ini table needs
// no codegen - and that is what makes adding a native an INI EDIT rather than a
// rebuild of this file.
//
// Registration uses the client's own helper (kPyInitModule), which is
//     m = PyImport_AddModule(name); if (m && methods) PyModule_AddFunctions(...)
// so the module lands in sys.modules and `import triarch_native` just works.
// Marshalling likewise uses the client's PyTuple_Get* / Py_Build* glue rather
// than raw CPython symbols.
// ===========================================================================
#define MAX_NATIVES 96

enum { CONV_THISCALL = 0, CONV_CDECL = 1, CONV_STDCALL = 2 };
enum { RET_VOID = 0, RET_U32 = 1, RET_I64 = 2, RET_F32 = 3 };
enum { THIS_NONE = 0, THIS_PLAYER = 1, THIS_CHARMGR = 2,
       THIS_NETSTREAM = 3, THIS_MINIMAP = 4 };

struct Native {
    char  name[40];
    DWORD va;
    BYTE  conv, stack, thisKind, ret;
    bool  checked;
    bool  disarmed;
};
static Native g_nat[MAX_NATIVES];
static int    g_natN = 0;

struct PyGlue {
    DWORD initModule, getUInt, getString, getFloat, buildValue, buildNone, buildExc;
};
static PyGlue g_glue;
static DWORD  g_singleton[5];        // indexed by THIS_*
static bool   g_nativeReady = false;

typedef void* (__cdecl *InitModule_t)(const char*, void*);
typedef char  (__cdecl *GetUInt_t)(void*, int, DWORD*);
typedef char  (__cdecl *GetStr_t)(void*, int, char**);
typedef char  (__cdecl *GetFlt_t)(void*, int, float*);
// Py_BuildValue(const char* fmt, ...) - the format is NOT optional.
typedef void* (__cdecl *BuildValue_t)(const char*, ...);
typedef void* (__cdecl *BuildNone_t)(void);
typedef void* (__cdecl *BuildExc_t)(const char*, ...);

static void* Abs(DWORD va)
{
    BYTE* base = (BYTE*)GetModuleHandleW(NULL);
    return base ? (void*)(base + (va - kImageBase)) : NULL;
}

// ---- the ABI trampoline ---------------------------------------------------
// Globals rather than registers for the saved ESP: a register would be at the
// mercy of a callee that does not honour the convention, and the whole point of
// saving it is to survive exactly that.
static DWORD g_trTarget, g_trThis, g_trArgc, g_trSavedEsp;
static DWORD g_trArgs[16];
static DWORD g_trEax, g_trEdx;
static float g_trF32;
static BYTE  g_trRet;

static __declspec(naked) void TrampolineRaw()
{
    __asm {
        push ebp
        mov  ebp, esp
        push ebx
        push esi
        push edi
        mov  g_trSavedEsp, esp          // the safety net, see below

        mov  edi, g_trArgc
        test edi, edi
        jz   noargs
        lea  ebx, g_trArgs
    pushloop:                           // right-to-left: last dword first
        mov  eax, [ebx + edi*4 - 4]
        push eax
        dec  edi
        jnz  pushloop
    noargs:
        mov  ecx, g_trThis              // ignored by cdecl/stdcall callees
        mov  eax, g_trTarget
        call eax

        // Restore ESP unconditionally. If the declared convention disagreed
        // with the real one, the stack is now wrong by `stack` bytes - and this
        // single instruction makes that survivable instead of silent
        // corruption that surfaces somewhere else entirely. __try/__except
        // cannot do this: a convention mismatch raises nothing.
        mov  esp, g_trSavedEsp

        mov  g_trEax, eax
        mov  g_trEdx, edx
        cmp  byte ptr g_trRet, 3        // RET_F32
        jne  nofloat
        fstp dword ptr g_trF32          // MUST pop: an unpopped x87 return
    nofloat:                            // leaks a slot and faults on the 8th
        pop  edi
        pop  esi
        pop  ebx
        mov  esp, ebp
        pop  ebp
        ret
    }
}

static Native* FindNative(const char* name)
{
    for (int i = 0; i < g_natN; i++)
        if (_stricmp(g_nat[i].name, name) == 0) return &g_nat[i];
    return NULL;
}

static void* __cdecl TriarchCall(void* /*self*/, void* args)
{
    BuildExc_t  exc  = (BuildExc_t)Abs(g_glue.buildExc);
    BuildNone_t none = (BuildNone_t)Abs(g_glue.buildNone);
    GetStr_t    gstr = (GetStr_t)Abs(g_glue.getString);
    GetUInt_t   gu   = (GetUInt_t)Abs(g_glue.getUInt);

    char* name = NULL;
    if (!gstr(args, 0, &name) || !name)
        return exc("triarch_native.call(name, ...): first argument must be the name");

    Native* n = FindNative(name);
    if (!n)       return exc("triarch_native: no native called '%s'", name);
    if (n->disarmed)
        return exc("triarch_native: '%s' is disarmed - it faulted earlier this session", name);

    int need = n->stack / 4;
    if (need > 16) return exc("triarch_native: '%s' wants too many arguments", name);
    for (int i = 0; i < need; i++) {
        if (!gu(args, 1 + i, &g_trArgs[i]))
            return exc("triarch_native: '%s' needs %d integer argument(s) "
                       "after the name", name, need);
    }

    void* self = NULL;
    if (n->thisKind != THIS_NONE) {
        DWORD holder = g_singleton[n->thisKind];
        if (!holder) return exc("triarch_native: '%s' has no singleton", name);
        __try {
            void** p = (void**)Abs(holder);
            self = (p && *p) ? *(void**)(*p) : NULL;
        } __except (EXCEPTION_EXECUTE_HANDLER) { self = NULL; }
        if (!self)
            return exc("triarch_native: '%s' - its object does not exist yet", name);
    }

    g_trTarget = (DWORD)Abs(n->va);
    g_trThis   = (DWORD)self;
    g_trArgc   = need;
    g_trRet    = n->ret;
    g_trEax = g_trEdx = 0; g_trF32 = 0.0f;

    __try {
        TrampolineRaw();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        n->disarmed = true;
        Log("NATIVE: '%s' faulted - disarmed for this session", name);
        return exc("triarch_native: '%s' faulted and is now disarmed", name);
    }

    // Only the kinds we can marshal LOSSLESSLY get here; the rest are rejected
    // at load. An earlier draft funnelled i64 and f32 through the int builder,
    // which silently truncated a 64-bit return to its low dword and converted a
    // float to an integer - wrong answers rather than a refusal, which is the
    // one thing this design is supposed to never do.
    if (n->ret == RET_U32)
        return ((BuildValue_t)Abs(g_glue.buildValue))("i", (long)g_trEax);
    return none();
}

// Several natives take pMain - the player's own CInstanceBase - and Python has
// no way to produce a pointer. The stub already resolves it for its own use
// (MainInst), so hand it over as an opaque integer rather than inventing an
// argument-substitution rule in the schema: the pointer stays visible, and
// api.py passes it straight back into the next call.
static void* __cdecl TriarchMainInstance(void* /*self*/, void* /*args*/)
{
    void* p = NULL;
    __try {
        p = MainInst(PlayerThis());
    } __except (EXCEPTION_EXECUTE_HANDLER) { p = NULL; }
    return ((BuildValue_t)Abs(g_glue.buildValue))("i", (long)p);
}

// ---------------------------------------------------------------------------
// minimap waypoint marks - CPythonMiniMap::AddWayPoint / RemoveWayPoint
//
// The Python-exposed miniMap.AddWayPoint binding hardcodes mark type 6, which
// draws the animated swirl on the ATLAS only. The native takes the type as its
// first argument; type 13 is the blinking target mark that draws on BOTH the
// minimap and the atlas and, when given a nonzero VID, auto-tracks that
// instance every frame inside CPythonMiniMap::Update - so a moving mob stays
// marked with no per-frame work here. Proven live via Frida before this
// shipped: the record lands in the vector at instance+0x1ce0 with type=13 and
// computed screen coords, and RemoveWayPoint takes it off cleanly.
//
// AddWayPoint's convention is nonstandard, which is why it cannot ride the
// generic gateway (integer stack args only):
//     ecx  = CPythonMiniMap*
//     xmm3 = x (float)                       -- NOT on the stack
//     stack (callee cleans 0x28): [type][id][y float][vid][std::string name
//                                  BY VALUE, 24 bytes]
// The name is an MSVC SSO std::string {char buf[16]; size_t size; size_t cap}.
// We only ever build the SSO form (cap = 15, so the callee's destructor never
// frees) and truncate to 15 chars - a mark LABEL, not load-bearing.
struct SsoString { char buf[16]; DWORD size; DWORD cap; };
static DWORD g_wpTarget, g_wpThis, g_wpType, g_wpId, g_wpVid, g_wpSavedEsp;
static float g_wpX, g_wpY;
static SsoString g_wpStr;

static __declspec(naked) void MiniMapAddRaw()
{
    __asm {
        push ebp
        mov  ebp, esp
        push esi
        push edi
        push ebx
        mov  g_wpSavedEsp, esp
        sub  esp, 24                    // room for the by-value std::string
        cld
        lea  esi, g_wpStr               // copy the 24-byte SSO string onto the stack
        mov  edi, esp
        mov  ecx, 6
        rep  movsd
        push g_wpVid                    // args pushed below the string, so the
        push g_wpY                      // frame reads [type][id][y][vid][string]
        push g_wpId
        push g_wpType
        movss xmm3, dword ptr g_wpX     // x rides XMM3, not the stack
        mov  ecx, g_wpThis
        mov  eax, g_wpTarget
        call eax
        mov  esp, g_wpSavedEsp          // restore unconditionally - survives a
        pop  ebx                        // convention mismatch the same way
        pop  edi                        // TrampolineRaw does
        pop  esi
        mov  esp, ebp
        pop  ebp
        ret
    }
}

// The CPythonMiniMap instance, or NULL. Double-indirect through the singleton
// holder, exactly as the generic gateway and MiniMapRange() resolve it.
static void* MiniMapThis()
{
    DWORD holder = g_singleton[THIS_MINIMAP];
    if (!holder) return NULL;
    __try {
        void** p = (void**)Abs(holder);
        return (p && *p) ? *(void**)(*p) : NULL;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return NULL; }
}

// minimap_mark(id, x, y, vid, name) -> 1 on success, 0 if unavailable/faulted.
// A nonzero vid makes the mark follow that instance and ignore x/y; pass x/y
// with vid=0 for a fixed mark. Re-marking an existing id updates it in place
// (AddWayPoint removes the id first), so this is safe to call every sweep.
static void* __cdecl TriarchMiniMapMark(void* /*self*/, void* args)
{
    BuildValue_t bv = (BuildValue_t)Abs(g_glue.buildValue);
    if (!g_off.miniMapAddWayPoint) return bv("i", 0);

    GetUInt_t gu = (GetUInt_t)Abs(g_glue.getUInt);
    GetFlt_t  gf = (GetFlt_t)Abs(g_glue.getFloat);
    GetStr_t  gs = (GetStr_t)Abs(g_glue.getString);

    DWORD id = 0, vid = 0; float x = 0.0f, y = 0.0f; char* name = NULL;
    if (!gu(args, 0, &id) || !gf(args, 1, &x) || !gf(args, 2, &y) ||
        !gu(args, 3, &vid))
        return bv("i", 0);
    gs(args, 4, &name);                 // optional label; NULL/absent -> empty

    void* self = MiniMapThis();
    if (!self) return bv("i", 0);

    memset(&g_wpStr, 0, sizeof(g_wpStr));
    g_wpStr.cap = 15;                   // SSO: the destructor will not free
    if (name) {
        size_t n = strlen(name);
        if (n > 15) n = 15;
        memcpy(g_wpStr.buf, name, n);
        g_wpStr.size = (DWORD)n;
    }
    g_wpTarget = (DWORD)Abs(g_off.miniMapAddWayPoint);
    g_wpThis = (DWORD)self;
    g_wpType = 13; g_wpId = id; g_wpVid = vid; g_wpX = x; g_wpY = y;

    __try {
        MiniMapAddRaw();
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("NATIVE: minimap_mark faulted (id=%u vid=%u)", id, vid);
        return bv("i", 0);
    }
    return bv("i", 1);
}

// minimap_unmark(id) -> 1 on success, 0 if unavailable/faulted. Plain thiscall.
static void* __cdecl TriarchMiniMapUnmark(void* /*self*/, void* args)
{
    BuildValue_t bv = (BuildValue_t)Abs(g_glue.buildValue);
    if (!g_off.miniMapRemoveWayPoint) return bv("i", 0);

    GetUInt_t gu = (GetUInt_t)Abs(g_glue.getUInt);
    DWORD id = 0;
    if (!gu(args, 0, &id)) return bv("i", 0);

    void* self = MiniMapThis();
    if (!self) return bv("i", 0);

    typedef void (__thiscall *RemoveWP_t)(void*, DWORD);
    RemoveWP_t fn = (RemoveWP_t)Abs(g_off.miniMapRemoveWayPoint);
    __try {
        fn(self, id);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("NATIVE: minimap_unmark faulted (id=%u)", id);
        return bv("i", 0);
    }
    return bv("i", 1);
}

// fishing_poll() -> (seq, sub, bite). seq++ per GC_FISHING packet, bite++ per
// bite (subheader 2), sub = last subheader. All monotonic, so the mod remembers
// the last bite count and reels the instant it grows - a slow poll cannot miss a
// bite. sub drives the start/result transitions. See the FISHING note by the
// Recv hook. No args, no game pointers, no faults possible: just reads globals.
static void* __cdecl TriarchFishingPoll(void* /*self*/, void* /*args*/)
{
    return ((BuildValue_t)Abs(g_glue.buildValue))("(iiiiii)",
        (long)g_fishSeq, (long)g_fishSub, (long)g_fishBiteSeq, (long)g_fishOurVid,
        (long)g_fishVnum, (long)g_fishCastSeq);
}

// key_event(vk, scan, down) -> #events sent. Injects a keyboard event via SendInput
// with the SCANCODE flag, which DirectInput reads. The engine POLLS some keys (Space
// = the fishing cast/reel) directly from the device and never routes them through
// Python OnKeyDown, so a mod cannot press them any other way (and the client's Python
// has no ctypes). Goes to the focused window - the client while the user plays.
// down != 0 = key down, 0 = key up; the mod holds across a tick, like a real press.
static void* __cdecl TriarchKeyEvent(void* /*self*/, void* args)
{
    DWORD vk = 0, scan = 0, down = 0;
    ((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &vk);
    ((GetUInt_t)Abs(g_glue.getUInt))(args, 1, &scan);
    ((GetUInt_t)Abs(g_glue.getUInt))(args, 2, &down);
    INPUT in;
    ZeroMemory(&in, sizeof(in));
    in.type = INPUT_KEYBOARD;
    in.ki.wVk = 0;                                  // scancode-driven, not vk
    in.ki.wScan = (WORD)scan;
    in.ki.dwFlags = KEYEVENTF_SCANCODE | (down ? 0 : KEYEVENTF_KEYUP);
    UINT sent = SendInput(1, &in, sizeof(INPUT));
    return ((BuildValue_t)Abs(g_glue.buildValue))("i", (long)sent);
}

// ---- typed fields ---------------------------------------------------------
// NOT a generic peek/poke. A generic writer is a memory-corruption primitive
// wearing an API's clothes, and it cannot express a vector or a field with an
// invariant. Each field is DECLARED in natives.json with an owner, an offset,
// a type and read/write permission, and only declared fields are reachable.
#define MAX_FIELDS 32
enum { FT_U32 = 0, FT_I32 = 1, FT_BOOL8 = 2, FT_VEC2 = 3, FT_VEC_U8 = 4 };
enum { OWN_PLAYER = 0, OWN_INSTANCE = 1 };

struct Field {
    char  name[32];
    DWORD off;
    BYTE  owner, type;
    bool  writable;
};
static Field g_fld[MAX_FIELDS];
static int   g_fldN = 0;

static Field* FindField(const char* n)
{
    for (int i = 0; i < g_fldN; i++)
        if (_stricmp(g_fld[i].name, n) == 0) return &g_fld[i];
    return NULL;
}

static void* FieldBase(Field* f)
{
    if (f->owner == OWN_INSTANCE) return MainInst(PlayerThis());
    return PlayerThis();
}

static void* __cdecl TriarchField(void* /*self*/, void* args)
{
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    char* name = NULL;
    if (!((GetStr_t)Abs(g_glue.getString))(args, 0, &name) || !name)
        return exc("triarch_native.field(name): name must be a string");
    Field* f = FindField(name);
    if (!f) return exc("triarch_native: no field called '%s'", name);
    void* base = FieldBase(f);
    if (!base) return exc("triarch_native: '%s' - its object does not exist yet", name);

    __try {
        char* p = (char*)base + f->off;
        switch (f->type) {
            case FT_BOOL8: return bv("i", (long)(*(BYTE*)p ? 1 : 0));
            case FT_I32:   return bv("i", (long)*(int*)p);
            case FT_U32:   return bv("i", (long)*(DWORD*)p);
            case FT_VEC2:  return bv("(dd)", (double)((float*)p)[0],
                                             (double)((float*)p)[1]);
            case FT_VEC_U8: {
                // begin/end pointer pair, not a value - hand back the count and
                // let the caller ask for elements, rather than pretending a
                // std::vector is an integer.
                BYTE** v = (BYTE**)p;
                return bv("i", (long)(v[1] - v[0]));
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return exc("triarch_native: reading '%s' faulted", name);
    }
    return exc("triarch_native: '%s' has an unsupported type", name);
}

static void* __cdecl TriarchSetField(void* /*self*/, void* args)
{
    BuildExc_t  exc  = (BuildExc_t)Abs(g_glue.buildExc);
    BuildNone_t none = (BuildNone_t)Abs(g_glue.buildNone);
    char* name = NULL;
    if (!((GetStr_t)Abs(g_glue.getString))(args, 0, &name) || !name)
        return exc("triarch_native.set_field(name, ...): name must be a string");
    Field* f = FindField(name);
    if (!f) return exc("triarch_native: no field called '%s'", name);
    if (!f->writable)
        return exc("triarch_native: '%s' is read-only by declaration", name);
    void* base = FieldBase(f);
    if (!base) return exc("triarch_native: '%s' - its object does not exist yet", name);

    __try {
        char* p = (char*)base + f->off;
        if (f->type == FT_VEC2) {
            float x = 0.0f, y = 0.0f;
            GetFlt_t gf = (GetFlt_t)Abs(g_glue.getFloat);
            if (!gf || !gf(args, 1, &x) || !gf(args, 2, &y))
                return exc("triarch_native: '%s' needs two floats", name);
            ((float*)p)[0] = x;
            ((float*)p)[1] = y;
            return none();
        }
        DWORD v = 0;
        if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 1, &v))
            return exc("triarch_native: '%s' needs one integer", name);
        switch (f->type) {
            case FT_BOOL8: *(BYTE*)p = (BYTE)(v ? 1 : 0); return none();
            case FT_I32:   *(int*)p = (int)v;             return none();
            case FT_U32:   *(DWORD*)p = v;                return none();
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return exc("triarch_native: writing '%s' faulted", name);
    }
    return exc("triarch_native: '%s' is not writable as a scalar", name);
}

static BYTE FieldType(const char* s)
{
    if (_stricmp(s, "bool8") == 0)      return FT_BOOL8;
    if (_stricmp(s, "i32") == 0)        return FT_I32;
    if (_stricmp(s, "vec2") == 0)       return FT_VEC2;
    if (_stricmp(s, "vector_u8") == 0)  return FT_VEC_U8;
    return FT_U32;
}

// Drop the current engagement so the next FindAndSetNewTarget picks again.
//
// This is the ONE thing Python cannot do safely on its own. The auto-attack VID
// at player+0x50 has a duplicate 4 bytes below it and the cached actor pointer
// 4 bytes above; __Update_AutoAttack sets __key__b0t__ when the pointer and the
// VID disagree, so clearing one without the others is exactly the inconsistent
// window that flag watches for. All three go together here, in the same order
// as the client's own inlined clear at 0x64B911.
//
// Needed by TWO features: body-block exclusion (which is a no-op while engaged,
// because the drive only re-acquires when the VID is zero) and hunting a
// filtered set of mobs (reject the acquired target, pick again).
static void* __cdecl TriarchDropEngagement(void* /*self*/, void* /*args*/)
{
    BuildValue_t bv = (BuildValue_t)Abs(g_glue.buildValue);
    if (!g_off.autoAtkVidOff) return bv("i", 0L);
    void* self = PlayerThis();
    if (!self) return bv("i", 0L);
    long had = 0;
    __try {
        DWORD* p = (DWORD*)((char*)self + g_off.autoAtkVidOff);
        had = (long)p[0];
        if (had) {
            p[0] = 0;      // the VID
            p[-1] = 0;     // its duplicate
            p[1] = 0;      // the cached actor pointer
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { had = 0; }
    return bv("i", had);
}

// ---- ground items --------------------------------------------------------
// Enumerating drops, which no Python binding offers. Everything here was read
// off the WINDOWS disassembly, not the Android symbols: CPythonItem::GetCloseItem
// looks callable on ARM and is INLINED on x86, so the ARM route was a mirage.
//
//   mov eax, [0x5347168]   ; the singleton holder   -> g_off.itemInst
//   mov edi, [eax]         ; the CPythonItem object
//   lea ecx, [edi + 4]     ; the std::map lives at object + 4
//   call _Find_lower_bound ; the 374-caller "helper" is the RB-tree search
//   cmp byte ptr [esi+0xd] ; _Isnil
//   cmp eax,  [esi+0x10]   ; key  = the ground item id
//   mov eax,  [esi+0x14]   ; value = the item instance
//
// Confirmed three times over: PickCloseItem and CreateItem both take `esi+4` as
// the map, and CreateItem's erase path frees nodes of size 0x18 - exactly
// _Left/_Parent/_Right (12) + _Color/_Isnil/pad (4) + key (4) + value (4).
#define NODE_ISNIL 0x0D
#define NODE_KEY   0x10
#define NODE_VAL   0x14
#define MAX_GITEMS 256

// SGroundItemInstance + 0x04 == dwVirtualNumber (the item's vnum - WHAT it is,
// as opposed to the map key, which is WHICH drop it is). Read out of the
// creator at 0x005E0C10, which Ghidra names through
// CPythonItem::SGroundItemInstance::vftable, so the object identity is not a
// guess. That function allocates 0x3BC, stores its vnum argument at +4, and
// passes the same argument to the proto lookup at 0x007A3D40 - a map keyed by
// vnum with a base+range fallback for grouped item models. GetVirtualNumberOf-
// GroundItem is inlined on x86, exactly like GetCloseItem, so there is no
// function to call and the field has to be read directly.
#define GITEM_VNUM 0x04

// Position and ownership, from the same creator. The position is stored as
// CONCAT44(-y, x) at +8, i.e. x at +0x08 and NEGATED y at +0x0C - the client's
// usual y-flip, which GetGroundItemPosition (0x005E1AB0) undoes on the way out.
// We hand back the raw stored pair and let Python flip it, so there is exactly
// one place that knows about the sign.
#define GITEM_X    0x08
#define GITEM_Y    0x0C
// Two std::strings close the struct: 0x38C and 0x3A4, each the standard MSVC
// shape (16-byte inline union, size at +0x10, capacity at +0x14), and the
// constructor sets both capacities to 0xF. One of them is the ownership name -
// the field PickCloseItemVector compares against our own, which is why the
// blind batch can never take another player's drop. Both are exposed until it
// is confirmed in game WHICH one; guessing here would silently produce a
// pickup filter that ignores ownership.
#define GITEM_STR1 0x38C
#define GITEM_STR2 0x3A4
#define GITEM_OWNER_MAX 32

static DWORD g_gitems[MAX_GITEMS];
static DWORD g_gitemVnum[MAX_GITEMS];
static int   g_gitemX[MAX_GITEMS];
static int   g_gitemY[MAX_GITEMS];
static char  g_gitemS1[MAX_GITEMS][GITEM_OWNER_MAX];
static char  g_gitemS2[MAX_GITEMS][GITEM_OWNER_MAX];
static int   g_gitemN = 0;

// Copy an MSVC std::string out of the instance. Short strings live inline in
// the 16-byte union; only once capacity exceeds 15 does the union hold a
// pointer instead. Names are short, so the inline case is the normal one - but
// both are handled, because getting this wrong reads a pointer as text.
static void ReadStdString(void* inst, int off, char* out, int cap)
{
    out[0] = 0;
    __try {
        char* s   = (char*)inst + off;
        DWORD len = *(DWORD*)(s + 0x10);
        DWORD res = *(DWORD*)(s + 0x14);
        const char* p = (res > 15) ? *(const char**)s : s;
        if (!p || len == 0 || len > 4096) return;
        if (IsBadReadPtr(p, 1)) return;
        int n = (int)len; if (n > cap - 1) n = cap - 1;
        for (int i = 0; i < n; i++) {
            char c = p[i];
            out[i] = (c >= 32 && (unsigned char)c < 127) ? c : '?';
        }
        out[n] = 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) { out[0] = 0; }
}

// ---- actors ---------------------------------------------------------------
// Enumerating every character the client can see, which no binding offers.
// CPythonCharacterManager keeps them in a std::unordered_map<DWORD, CInstance-
// Base*>, NOT a std::map - SelectInstance's worker at 0x005C0D10 hashes the VID
// with FNV-1a before looking it up, which is MSVC's std::hash for integrals:
//
//   _Find_last(&vid, ((((vid&0xff ^ 0x811c9dc5)*0x1000193 ^ vid>>8&0xff)
//                       *0x1000193 ^ vid>>16&0xff)*0x1000193 ^ vid>>24)*0x1000193)
//   if (*(this + 0x28) != node) *(this + 0x18) = *(node + 0xC);
//
// That is a gift: an unordered_map keeps ALL its elements on one doubly-linked
// list, so enumeration is a flat walk with no tree logic and no stack.
//
// Layout, from the constructor at 0x005C1780. It builds two 0x20-byte
// unordered_maps back to back and then the std::list heads at +0x64, which
// pins the first map at +0x24 exactly: 0x24 + 0x20 = 0x44, + 0x20 = 0x64.
// Each map is MSVC's { float _Max_load; _List; size; _Vec[3]; mask; maxidx }
// with the sentinel from operator_new(0x10) self-linked on both pointers.
// 0x005C0D10 compares against `this + 0x28` = map+4 = the _List head, which is
// how we know the VID-keyed map is the one at +0x24 rather than +0x44.
#define CHR_ALIVE_MAP 0x24
#define CHR_LIST      (CHR_ALIVE_MAP + 4)
#define NODE_NEXT     0x00
#define NODE_VID      0x08
#define NODE_INST     0x0C
#define MAX_ACTORS    512

static DWORD g_actors[MAX_ACTORS];
static int   g_actorN = 0;

static void* CharMgrObject()
{
    if (!g_singleton[THIS_CHARMGR]) return NULL;
    __try {
        void** holder = (void**)Abs(g_singleton[THIS_CHARMGR]);
        if (IsBadReadPtr(holder, sizeof(void*)) || !*holder) return NULL;
        return *(void**)(*holder);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return NULL; }
}

// Flat walk of the unordered_map's element list. The sentinel is its own
// terminator, so the loop is bounded by returning to it - plus a hard cap,
// because a torn list during a map change must cost a bounded amount of work
// rather than hanging the render thread.
static void WalkActors(void* head)
{
    g_actorN = 0;
    if (!head) return;
    void* n = *(void**)((char*)head + NODE_NEXT);
    int guard = 0;
    while (n && n != head && g_actorN < MAX_ACTORS && guard++ < MAX_ACTORS * 2) {
        DWORD vid = *(DWORD*)((char*)n + NODE_VID);
        if (*(void**)((char*)n + NODE_INST))      // skip a dead/null instance
            g_actors[g_actorN++] = vid;
        n = *(void**)((char*)n + NODE_NEXT);
    }
}

// ---- actor collision ------------------------------------------------------
// The engine's OWN switch, used the way the engine intends it.
//
// skip-collision is not a bool: it is a magic dword the developers chose,
//   0x000A35F5 = skip, 0x000F35F5 = do not          (CActorInstance + off)
// and CanSkipCollision compares against the first. We do not write it by hand -
// we call the client's EnableSkipCollision/DisableSkipCollision, so the value
// and the field both come from the binary rather than from a constant here.
//
// DIRECTION MATTERS, and the client is explicit about it: CInstanceBase::
// __EnableSkipCollision refuses the main instance and traces "You should not
// skip your own collisions!!". The blocking test reads the sentinel off the
// candidate BLOCKER, so marking the mobs is what stops them blocking us. The
// main instance is skipped here for exactly that reason.
//
// TERRAIN IS UNTOUCHED. Walls and scenery go through CPythonBackground::IsBlock,
// a different path entirely; nothing below can move the character through
// geometry. That is deliberate - actor overlap is a local render-side fact,
// walking through scenery is not.
typedef void (__fastcall *SkipColl_t)(void* thisptr, void* unused);

// CORRECTED: this flag means "I ignore collisions", not "I am ignorable".
//
// The first version marked every OTHER actor and did nothing whatsoever -
// verified live, 90 actors marked, zero change in blocking. The blocking test
// reads the sentinel off the MOVER:
//
//   CInstanceBase::IsBlockObject   x0 = this->actor; tail-calls the actor one
//   CActorInstance::IsBlockObject  -> 0x790E30 with ecx = this
//   0x790E30                       cmp [ecx+0x1A64], 0xA35F5 -> "not blocking"
//
// and CPythonCharacterManager::UpdateTransform tests it on the MAIN instance to
// decide whether to run CheckAdvancing at all. It only ever means anything for
// the character doing the moving, which is us.
//
// !! THIS ALSO TURNS OFF TERRAIN COLLISION !!
// CPythonBackground::CheckAdvancing does both jobs in one function -
// SpherePackFactory::RangeTest for actors AND CMapManager::isAttrOn for the
// map's block attribute - and the flag skips the whole thing. There is no way
// to separate them at this level. While this is on nothing stops the character
// entering geometry, and position goes to the server ~2.5x/second, so the
// consequence is self-reporting even though the flag itself never is.
//
// We call CActorInstance::EnableSkipCollision directly rather than
// CInstanceBase::__EnableSkipCollision, because the latter refuses the main
// instance by design ("You should not skip your own collisions!!"). Going
// around that assert is the whole point of this experiment, so it is written
// out plainly here rather than buried.
// `out` receives the sentinel read back off the actor after the call, so the
// caller can tell "the setter did not fault" apart from "the write landed on
// the object we meant". It was referenced here without ever being a parameter,
// which meant this file did not compile - the deployed DLL predates that edit.
static int SetSkipCollisionSelf(int on, DWORD* out)
{
    if (!g_off.enableSkipCollision || !g_off.disableSkipCollision
        || !g_off.instActorOff)
        return -1;
    void* obj = CharMgrObject();
    if (!obj) return -2;

    // Three separate guards, three distinct codes. One __try around the whole
    // thing reported "fault" and told me nothing about which step - and with a
    // vtable call, a pointer walk and an indirect call in the same block, that
    // is three very different bugs wearing one error message.
    // CPythonCharacterManager inherits multiply: its ctor writes a vftable at
    // +0, +4 AND +8. The getter we want hangs off the vtable at +4, so the
    // `this` for that call is the SUBOBJECT at obj+4 - passing obj gave a
    // plausible-looking non-NULL return that was not a CInstanceBase*, and the
    // fault only showed up one dereference later.
    void* mainInst = NULL;
    __try {
        void* sub = (char*)obj + 4;
        void** vt = *(void***)sub;
        typedef void* (__fastcall *GetMain_t)(void*, void*);
        mainInst = ((GetMain_t)vt[3])(sub, NULL);          // slot 0x0C
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -5; }  // vtable call faulted
    if (!mainInst) return -3;
    if (IsBadReadPtr(mainInst, g_off.instActorOff + sizeof(void*))) return -9;

    void* actor = NULL;
    __try {
        actor = *(void**)((char*)mainInst + g_off.instActorOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -6; }  // instance->actor faulted
    if (!actor) return -7;                                 // no actor yet

    __try {
        SkipColl_t fn = (SkipColl_t)Abs(on ? g_off.enableSkipCollision
                                           : g_off.disableSkipCollision);
        fn(actor, NULL);
        // Read it straight back. "The call did not fault" is not evidence the
        // write landed on the object we meant, and assuming it did is what made
        // the previous two rounds of this look like a read-path problem when
        // they may have been a write-path one. Report the actual dword and let
        // the caller judge: 0xA35F5 = on, 0xF35F5 = off, anything else = we are
        // not looking at a CActorInstance.
        if (out && g_off.skipCollisionOff)
            *out = *(DWORD*)((char*)actor + g_off.skipCollisionOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -8; }  // the setter faulted
    return 0;
}

static void* __cdecl TriarchSkipCollision(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD on = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &on))
        return exc("skip_collision(on): expected an integer");
    DWORD flag = 0;
    int rc = SetSkipCollisionSelf((int)on, &flag);
    if (rc == -1) return exc("skip_collision: offsets unresolved - regenerate uriel_offsets.ini");
    if (rc == -2) return exc("skip_collision: CPythonCharacterManager not readable");
    if (rc == -3) return exc("skip_collision: no main instance (not in game?)");
    if (rc == -4) return exc("skip_collision: fault setting the flag");
    if (rc == -5) return exc("skip_collision: fault calling charmgr vtable slot 0x0C");
    if (rc == -6) return exc("skip_collision: fault reading mainInstance+kInstActorOff");
    if (rc == -7) return exc("skip_collision: main instance has no actor yet");
    if (rc == -8) return exc("skip_collision: fault calling EnableSkipCollision");
    if (rc == -9) return exc("skip_collision: charmgr returned a non-instance pointer");
    // The readback is the only evidence the write landed on a CActorInstance
    // rather than on some other object that happened to be reachable. Neither
    // sentinel means we are not looking at one, and reporting success there is
    // exactly the false positive that cost two rounds of debugging.
    if (g_off.skipCollisionOff && flag != 0x000A35F5 && flag != 0x000F35F5)
        return exc("skip_collision: flag reads back as 0x%08X - not a CActorInstance", flag);
    return bv("i", (long)(on ? 1 : 0));
}

// ---------------------------------------------------------------------------
// actor_pass(on) - walk through other actors, terrain untouched.
//
// The engine already ships this. CActorInstance::IsBlockObject (the movement
// one) is a cascade of exemptions, two of which are hardcoded RACE RANGES:
//
//     call GetRace ; cmp eax, LO ; jb +0x12 ; call GetRace ; cmp eax, HI ; jbe exit
//
// Widening the first range to [0, 0xFFFFFFFF] makes every actor exempt. Proven
// live: the adjuster (the thing that displaces you back out of a mob) stops
// being called ENTIRELY - 23 calls per 2s window before, 0 after - and the
// collision-loop BlockMovement disappears with it, while the movement state
// machine keeps ticking normally.
//
// WHY THE RANGE AND NOT THE FUNCTION.
// Stubbing the prologue to `xor eax,eax; ret 4` has the same effect and is one
// write instead of two. It is not what we do. Two immediates are DATA inside an
// unchanged function; a rewritten prologue is control flow, is the shape every
// scanner looks for, and throws away the other exemption checks. Widening the
// whitelist is the engine's own mechanism, used the way it was built.
//
// WHY NOT THE ADJUSTER'S TYPE EXEMPTION. It has one too - [other+0x189C] in
// {2,4,5,15} skips displacement - and it is a trap. The test still returns
// "blocked", so the loop burns its two retries and calls BlockMovement, which
// STOPS the character dead. Being displaced is strictly better than being
// stopped. Defeat the test, never the adjuster.
//
// TERRAIN IS NOT TOUCHED. Terrain is a different predicate with its own callers
// and is not reachable from here. That separation is the whole point - the
// skip-collision sentinel could not give it, because it gates terrain too.
// Write one immediate. Validating the byte BEFORE it is `3D` (the opcode of
// `cmp eax, imm32`) is what keeps this honest: it proves we are editing the
// operand of the instruction the resolver matched, not four arbitrary bytes at
// a stale address. Value checks cannot do that once several states are legal.
static int PokeImm32(DWORD va, DWORD val)
{
    if (!va) return -1;
    BYTE* op = (BYTE*)Abs(va) - 1;
    DWORD* p = (DWORD*)Abs(va);
    __try {
        if (*op != 0x3D) return -3;                 // not `cmp eax, imm32`
        if (*p == val) return 0;                    // already there
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -2; }

    DWORD old = 0;
    if (!VirtualProtect(p, sizeof(DWORD), PAGE_EXECUTE_READWRITE, &old))
        return -4;
    __try { *p = val; } __except (EXCEPTION_EXECUTE_HANDLER) {}
    VirtualProtect(p, sizeof(DWORD), old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, sizeof(DWORD));

    __try {
        if (*p != val) return -5;                   // VirtualProtect succeeding
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -2; }   // is not evidence
    return 0;
}

// on=0 restores both ranges. on=1 with no band widens range 1 to everything.
// on=1 with a band splits the two ranges around it:
//     range 1 = [0, exLo-1]      everything below stays passable
//     range 2 = [exHi+1, ~0]     everything above stays passable
// leaving [exLo, exHi] as the only races that still block. Used for metin
// stones, which sit at 8005 - a band of their own, well clear of mobs at
// 401-599 and of the mount at 20101 - so they keep collision while everything
// else loses it, with no per-frame work and no proximity guessing.
static int ActorPassSet(int on, DWORD exLo, DWORD exHi)
{
    if (!g_off.actorPassLoVA || !g_off.actorPassHiVA) return -1;
    bool band = (on && exHi >= exLo && (exLo || exHi));
    if (band && (!g_off.actorPass2LoVA || !g_off.actorPass2HiVA)) return -6;
    if (band && exLo == 0) return -7;      // [0,exHi] has no "below" half

    int rc;
    if (!on) {
        rc = PokeImm32(g_off.actorPassLoVA, g_off.actorPassLo);
        if (rc) return rc;
        rc = PokeImm32(g_off.actorPassHiVA, g_off.actorPassHi);
        if (rc) return rc;
        if (g_off.actorPass2LoVA) {
            rc = PokeImm32(g_off.actorPass2LoVA, g_off.actorPass2Lo);
            if (rc) return rc;
            rc = PokeImm32(g_off.actorPass2HiVA, g_off.actorPass2Hi);
            if (rc) return rc;
        }
        return 0;
    }

    // Widen the LOW bound first in every path. A half-applied pair must never
    // be narrower than what it replaced: widening lo before hi means the worst
    // interruption leaves a bigger exemption, never a smaller one.
    rc = PokeImm32(g_off.actorPassLoVA, 0u);
    if (rc) return rc;
    rc = PokeImm32(g_off.actorPassHiVA, band ? (exLo - 1u) : 0xFFFFFFFFu);
    if (rc) return rc;
    if (band) {
        rc = PokeImm32(g_off.actorPass2LoVA, exHi + 1u);
        if (rc) return rc;
        rc = PokeImm32(g_off.actorPass2HiVA, 0xFFFFFFFFu);
        if (rc) return rc;
    } else if (g_off.actorPass2LoVA) {
        // No band: put range 2 back to whatever it shipped as, so switching
        // from banded to unbanded does not leave our carve-out behind.
        rc = PokeImm32(g_off.actorPass2LoVA, g_off.actorPass2Lo);
        if (rc) return rc;
        rc = PokeImm32(g_off.actorPass2HiVA, g_off.actorPass2Hi);
        if (rc) return rc;
    }
    return 0;
}

static void* __cdecl TriarchActorPass(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD on = 0, exLo = 0, exHi = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &on))
        return exc("actor_pass(on[, exclude_lo, exclude_hi]): expected an integer");
    // Optional: absent args read as 0, which means "no band".
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 1, &exLo)) exLo = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 2, &exHi)) exHi = 0;
    bool band = (on && exHi >= exLo && (exLo || exHi));
    int rc = ActorPassSet((int)on, exLo, exHi);
    if (rc == -1) return exc("actor_pass: offsets unresolved - regenerate uriel_offsets.ini");
    if (rc == -2) return exc("actor_pass: fault at the range - wrong address?");
    if (rc == -3) return exc("actor_pass: not a `cmp eax,imm32` there, refusing to write");
    if (rc == -4) return exc("actor_pass: VirtualProtect failed");
    if (rc == -5) return exc("actor_pass: write did not land");
    if (rc == -6) return exc("actor_pass: this build resolved only one race range - no exclusion possible");
    if (rc == -7) return exc("actor_pass: exclude_lo must be > 0");
    // 2, not 1, when a band was actually carved out. A stub built before the
    // band existed ignores the extra arguments and still returns 1, so without
    // a distinguishable value the caller cannot tell "metins stay solid" from
    // "the stub silently made everything passable" - which is exactly the kind
    // of plausible-looking success this codebase keeps getting bitten by.
    return bv("i", (long)(on ? (band ? 2 : 1) : 0));
}

// ---------------------------------------------------------------------------
// terrain_pass(on) - neuter CActorInstance::BlockMovement, the *stop*.
//
// READ THIS BEFORE ENABLING IT ANYWHERE.
// Unlike actor_pass, this one is genuinely risky. Terrain is the map ATTRIBUTE,
// and the server knows about it: the client reports its own x/y about 2.5 times
// a second, so standing somewhere the map forbids is self-reporting even though
// no packet ever carries a "collision off" bit. Walking manually into illegal
// terrain has already been observed to raise an in-client error dialog, which
// means there is a legality check somewhere that we have not fully mapped.
// Actor pass-through is invisible; this is not. They are separate switches for
// exactly that reason and must never be merged into one.
//
// void CActorInstance::BlockMovement() is thiscall, no arguments, returns void,
// so a bare `ret` (0xC3) is a complete and correct implementation - there is no
// caller-side stack to clean and no return value to fake.
//
// This IS a control-flow patch, which actor_pass deliberately avoids. There is
// no data alternative here: the function is the stop, and there is no
// whitelist to widen. One byte, restored on the way out.
static int TerrainPassSet(int on)
{
    if (!g_off.blockMovement) return -1;
    BYTE* p = (BYTE*)Abs(g_off.blockMovement);

    // Bytes 1..11 are the rest of `mov eax,[ecx+off32]; test eax,eax;
    // cmovne ecx,eax; jmp`. The patch never touches them, so they are what
    // proves this is BlockMovement and not whatever else lives at this address
    // on a build the ini does not describe.
    BYTE cur[12];
    __try {
        for (int i = 0; i < 12; i++) cur[i] = p[i];
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -2; }

    if (cur[1] != 0x81 || cur[6] != 0x85 || cur[7] != 0xC0 ||
        cur[8] != 0x0F || cur[9] != 0x45 || cur[10] != 0xC8 || cur[11] != 0xE9)
        return -3;
    if (cur[0] != 0x8B && cur[0] != 0xC3) return -3;   // original or ours

    BYTE want = on ? 0xC3 : 0x8B;
    if (cur[0] == want) return 0;

    DWORD old = 0;
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &old)) return -4;
    __try { p[0] = want; } __except (EXCEPTION_EXECUTE_HANDLER) {}
    VirtualProtect(p, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 1);

    __try {
        if (p[0] != want) return -5;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return -2; }
    return 0;
}

static void* __cdecl TriarchTerrainPass(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD on = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &on))
        return exc("terrain_pass(on): expected an integer");
    int rc = TerrainPassSet((int)on);
    if (rc == -1) return exc("terrain_pass: offsets unresolved - regenerate uriel_offsets.ini");
    if (rc == -2) return exc("terrain_pass: fault reading BlockMovement - wrong address?");
    if (rc == -3) return exc("terrain_pass: not BlockMovement at that address, refusing to write");
    if (rc == -4) return exc("terrain_pass: VirtualProtect failed");
    if (rc == -5) return exc("terrain_pass: write did not land");
    return bv("i", (long)(on ? 1 : 0));
}

// ---- http_post: synchronous WinHTTP POST -----------------------------------
// Pure Windows, no game offsets — so no uriel_natives.ini entry, it just rides
// on the native module. Lets an upload mod push a finished scan to an HTTP
// endpoint of its own. Returns the HTTP status code (e.g. 200/201), or a DISTINCT
// negative per local failure step, so a caller can tell which step failed. v1 is
// synchronous: it blocks the caller for the request; one upload per ~4-min scan
// makes that acceptable. Timeouts bound the block.
static int HttpPostRaw(const char* url, const char* authHeader, const char* ctype,
                       const char* body, unsigned bodyLen)
{
    wchar_t urlW[2048], host[256], path[1536], headersW[1200];
    char    hbuf[1200];
    URL_COMPONENTS uc;
    HINTERNET hS = NULL, hC = NULL, hR = NULL;
    DWORD status = 0, slen = sizeof(status);
    BOOL  secure = FALSE;
    int   rc = -100;

    if (MultiByteToWideChar(CP_UTF8, 0, url, -1, urlW, 2048) == 0) return -1;
    ZeroMemory(&uc, sizeof(uc)); uc.dwStructSize = sizeof(uc);
    uc.lpszHostName = host; uc.dwHostNameLength = 256;
    uc.lpszUrlPath  = path; uc.dwUrlPathLength  = 1536;
    if (!WinHttpCrackUrl(urlW, 0, 0, &uc)) return -2;
    secure = (uc.nScheme == INTERNET_SCHEME_HTTPS);

    hS = WinHttpOpen(L"UrielStub/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                     WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!hS) return -3;
    WinHttpSetTimeouts(hS, 10000, 10000, 15000, 30000);   // resolve/connect/send/recv

    hC = WinHttpConnect(hS, host, uc.nPort, 0);
    if (!hC) { rc = -4; goto cleanup; }
    hR = WinHttpOpenRequest(hC, L"POST", path, NULL, WINHTTP_NO_REFERER,
                            WINHTTP_DEFAULT_ACCEPT_TYPES, secure ? WINHTTP_FLAG_SECURE : 0);
    if (!hR) { rc = -5; goto cleanup; }

    _snprintf_s(hbuf, sizeof(hbuf), _TRUNCATE, "Content-Type: %s\r\n%s%s",
                (ctype && *ctype) ? ctype : "application/octet-stream",
                (authHeader && *authHeader) ? "Authorization: " : "",
                (authHeader && *authHeader) ? authHeader : "");
    MultiByteToWideChar(CP_UTF8, 0, hbuf, -1, headersW, 1200);

    if (!WinHttpSendRequest(hR, headersW, (DWORD)-1L, (LPVOID)body, bodyLen, bodyLen, 0)) { rc = -6; goto cleanup; }
    if (!WinHttpReceiveResponse(hR, NULL)) { rc = -7; goto cleanup; }
    if (!WinHttpQueryHeaders(hR, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &slen, WINHTTP_NO_HEADER_INDEX)) { rc = -8; goto cleanup; }
    rc = (int)status;

cleanup:
    if (hR) WinHttpCloseHandle(hR);
    if (hC) WinHttpCloseHandle(hC);
    if (hS) WinHttpCloseHandle(hS);
    return rc;
}

static void* __cdecl TriarchHttpPost(void* /*self*/, void* args)
{
    BuildValue_t bv   = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc  = (BuildExc_t)Abs(g_glue.buildExc);
    GetStr_t     gstr = (GetStr_t)Abs(g_glue.getString);

    char *url = NULL, *bearer = NULL, *ctype = NULL, *body = NULL;
    if (!gstr(args, 0, &url) || !url)
        return exc("http_post(url, auth, content_type, body): url required");
    gstr(args, 1, &bearer);
    gstr(args, 2, &ctype);
    gstr(args, 3, &body);
    unsigned blen = body ? (unsigned)strlen(body) : 0;

    int rc;
    __try {
        rc = HttpPostRaw(url, bearer, ctype, body, blen);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return exc("http_post: fault during request");
    }
    // >=0 is the HTTP status code; <0 is a local failure step (see HttpPostRaw)
    return bv("i", (long)rc);
}

static void* __cdecl TriarchActors(void* /*self*/, void* /*args*/)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    if (!g_singleton[THIS_CHARMGR])
        return exc("actors(): kCharMgrInst unresolved - regenerate uriel_natives.ini");
    void* obj = CharMgrObject();
    if (!obj)
        return exc("actors(): CPythonCharacterManager singleton not readable");
    __try {
        WalkActors(*(void**)((char*)obj + CHR_LIST));
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_actorN = 0;
        return exc("actors(): fault walking the alive-instance map");
    }
    return bv("i", (long)g_actorN);
}

static void* __cdecl TriarchActorAt(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("actor_at(index): index must be an integer");
    if ((int)i >= g_actorN) return bv("i", 0L);
    return bv("i", (long)g_actors[i]);
}

// ---- offline-shop entity snapshot (map+channel enrichment) -----------------
// The CPythonOfflineshop singleton holds a std::vector<Entity*> of every offline
// shop currently loaded on THIS map/channel. Reading it is a targeted few-KB read
// (immune to the client memory leak) - no packet hook, no InsertEntity VA needed.
// Layout RE'd 2026-08-15 (ARM libtriarch.so symbol -> outdated ghidra renderer ->
// validated live on the deployed build; see NATIVE-CATALOGUE.md "SOLVED"):
//   inst = *(*(g_off.offlineshopInst))              (double-indirect, as ShopCapture)
//   vector<Entity*>: begin=*(inst+0x5c) end=*(inst+0x60)  (elements 4-byte Entity*)
//   per Entity*: seller std::string @ +0x35c (MSVC SSO: buf/ptr@+0, len@+0x10,
//                cap@+0x14; if cap>0xf the buffer is a heap ptr), pos float x@+0x390 y@+0x394
// The mod tags each seller with the session map+channel (Python side) and uploads;
// the server joins by seller -> shopId (one character == one shop => seller unique).
#define kShopEntVecBeg 0x5c
#define kShopEntVecEnd 0x60
#define kShopEntName   0x35c
#define kShopEntNameCap (kShopEntName + 0x14)
#define kShopEntPos    0x390
#define kMaxShopEnts   4096

struct ShopEntRec { char name[64]; int x, y; };
static ShopEntRec g_shopEnts[kMaxShopEnts];
static int g_shopEntN = 0;

static void SnapshotOfflineShops()
{
    g_shopEntN = 0;
    if (!g_off.offlineshopInst) return;
    __try {
        BYTE* base = (BYTE*)GetModuleHandleW(NULL);
        DWORD* g = (DWORD*)(base + (g_off.offlineshopInst - kImageBase));
        if (IsBadReadPtr(g, 4) || !*g) return;
        DWORD* p1 = (DWORD*)(*g);                          // double indirection
        if (IsBadReadPtr(p1, 4) || !*p1) return;
        BYTE* inst = (BYTE*)(*p1);
        if (IsBadReadPtr(inst + kShopEntVecEnd, 4)) return;
        DWORD beg = *(DWORD*)(inst + kShopEntVecBeg);
        DWORD end = *(DWORD*)(inst + kShopEntVecEnd);
        if (!beg || end < beg) return;
        int n = (int)((end - beg) / 4);
        if (n < 0 || n > 30000) return;                   // torn/implausible vector
        for (int i = 0; i < n && g_shopEntN < kMaxShopEnts; i++) {
            DWORD slot = beg + (DWORD)i * 4;
            if (IsBadReadPtr((void*)slot, 4)) break;
            DWORD entp = *(DWORD*)slot;
            if (!entp || IsBadReadPtr((void*)entp, kShopEntPos + 8)) continue;
            BYTE* ent = (BYTE*)entp;
            DWORD cap = *(DWORD*)(ent + kShopEntNameCap);
            const char* nm = (cap > 0xf) ? *(const char**)(ent + kShopEntName)
                                         : (const char*)(ent + kShopEntName);
            if (!nm || IsBadStringPtrA(nm, 63)) continue;
            ShopEntRec& e = g_shopEnts[g_shopEntN++];
            strncpy_s(e.name, sizeof(e.name), nm, _TRUNCATE);
            e.x = (int)(*(float*)(ent + kShopEntPos));
            e.y = (int)(*(float*)(ent + kShopEntPos + 4));
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { g_shopEntN = 0; }
}

static void* __cdecl TriarchOfflineShops(void* /*self*/, void* /*args*/)
{
    SnapshotOfflineShops();
    return ((BuildValue_t)Abs(g_glue.buildValue))("i", (long)g_shopEntN);
}

static void* __cdecl TriarchOfflineShopAt(void* /*self*/, void* args)
{
    BuildExc_t exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("offline_shop_at(index): index must be an integer");
    if ((int)i >= g_shopEntN)
        return ((BuildNone_t)Abs(g_glue.buildNone))();
    ShopEntRec& e = g_shopEnts[i];
    return ((BuildValue_t)Abs(g_glue.buildValue))("(sii)", e.name, (long)e.x, (long)e.y);
}

static void* ItemObject()
{
    if (!g_off.itemInst) return NULL;
    __try {
        void** holder = (void**)Abs(g_off.itemInst);
        if (IsBadReadPtr(holder, sizeof(void*)) || !*holder) return NULL;
        return *(void**)(*holder);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return NULL; }
}

// In-order walk of an MSVC red-black tree, iterative so a corrupt tree costs a
// bounded amount of work rather than the stack.
static void WalkTree(void* head)
{
    g_gitemN = 0;
    if (!head) return;
    void* stack[64];
    int sp = 0;
    void* cur = *(void**)((char*)head + 4);      // _Myhead->_Parent == root
    if (cur == head) return;                     // empty
    int guard = 0;
    while ((sp || (cur && cur != head)) && guard++ < MAX_GITEMS * 8) {
        while (cur && cur != head && sp < 64) {
            stack[sp++] = cur;
            cur = *(void**)cur;                  // _Left
        }
        if (!sp) break;
        void* n = stack[--sp];
        if (!*(BYTE*)((char*)n + NODE_ISNIL) && g_gitemN < MAX_GITEMS) {
            void* inst = *(void**)((char*)n + NODE_VAL);
            int k = g_gitemN;
            g_gitems[k]    = *(DWORD*)((char*)n + NODE_KEY);
            g_gitemVnum[k] = 0;
            g_gitemX[k] = g_gitemY[k] = 0;
            g_gitemS1[k][0] = g_gitemS2[k][0] = 0;
            if (inst) {
                g_gitemVnum[k] = *(DWORD*)((char*)inst + GITEM_VNUM);
                // Positions are floats in the struct but whole world units in
                // practice; truncating keeps the whole snapshot integer, which
                // is all the int-only return path can carry anyway.
                g_gitemX[k] = (int)*(float*)((char*)inst + GITEM_X);
                g_gitemY[k] = (int)*(float*)((char*)inst + GITEM_Y);
                ReadStdString(inst, GITEM_STR1, g_gitemS1[k], GITEM_OWNER_MAX);
                ReadStdString(inst, GITEM_STR2, g_gitemS2[k], GITEM_OWNER_MAX);
            }
            g_gitemN++;
        }
        cur = *(void**)((char*)n + 8);           // _Right
    }
}

static void* __cdecl TriarchGroundItems(void* /*self*/, void* /*args*/)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    // Never report "not wired up" as "nothing on the floor". Returning 0 for
    // both cost an hour: the walk was reporting an empty map over a field
    // covered in drops, and a zero is exactly what a correct walk returns over
    // an empty field, so the reading was consistent with success. An
    // unresolved singleton is a configuration fault and must say so.
    if (!g_off.itemInst)
        return exc("ground_items(): kItemInst unresolved - regenerate uriel_natives.ini");
    void* obj = ItemObject();
    if (!obj)
        return exc("ground_items(): CPythonItem singleton not readable");
    __try {
        WalkTree(*(void**)((char*)obj + 4));     // the map's _Myhead
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_gitemN = 0;
        return exc("ground_items(): fault walking the ground-item map");
    }
    return bv("i", (long)g_gitemN);
}

static void* __cdecl TriarchGroundItemAt(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("ground_item_at(index): index must be an integer");
    if ((int)i >= g_gitemN) return bv("i", 0L);
    return bv("i", (long)g_gitems[i]);
}

// The filter key. Pair it with ground_item_at(i) for the same i: that one says
// which drop, this one says what kind of item it is, and only the second can
// answer "is this on the allow-list".
static void* __cdecl TriarchGroundItemVnum(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("ground_item_vnum(index): index must be an integer");
    if ((int)i >= g_gitemN) return bv("i", 0L);
    return bv("i", (long)g_gitemVnum[i]);
}

// (x, y) of a drop, y already un-flipped so it is in the same frame as every
// position Python already has. Lets a filtered sweep apply the same radius the
// client's own batch pickup applies natively, instead of asking the server
// about items it is going to refuse.
static void* __cdecl TriarchGroundItemPos(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("ground_item_pos(index): index must be an integer");
    if ((int)i >= g_gitemN) return bv("(ii)", 0L, 0L);
    return bv("(ii)", (long)g_gitemX[i], (long)-g_gitemY[i]);
}

// The two trailing std::strings; which=0 -> +0x38C, otherwise +0x3A4. One of
// them is the ownership name. Exposed as a pair on purpose - see the note at
// GITEM_STR1.
static void* __cdecl TriarchGroundItemStr(void* /*self*/, void* args)
{
    BuildValue_t bv  = (BuildValue_t)Abs(g_glue.buildValue);
    BuildExc_t   exc = (BuildExc_t)Abs(g_glue.buildExc);
    DWORD i = 0, which = 0;
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 0, &i))
        return exc("ground_item_str(index, which): index must be an integer");
    if (!((GetUInt_t)Abs(g_glue.getUInt))(args, 1, &which))
        which = 0;
    if ((int)i >= g_gitemN) return bv("s", "");
    return bv("s", which ? g_gitemS2[i] : g_gitemS1[i]);
}

static char  g_modName[] = "triarch_native";
static char  g_docCall[] = "call(name, *dword_args) -> result";
static char  g_docMain[] = "main_instance() -> int, the player's CInstanceBase* (0 if not in game)";
struct PyMethodDefX { const char* name; void* meth; int flags; const char* doc; };
static PyMethodDefX g_methods[24];      // N entries + a zeroed sentinel

static void RegisterNativeModule()
{
    if (g_nativeReady || !g_glue.initModule || !g_natN) return;
    g_methods[0].name = "call";
    g_methods[0].meth = (void*)&TriarchCall;
    g_methods[0].flags = 1;                       // METH_VARARGS
    g_methods[0].doc = g_docCall;
    g_methods[1].name = "main_instance";
    g_methods[1].meth = (void*)&TriarchMainInstance;
    g_methods[1].flags = 1;                       // METH_VARARGS
    g_methods[1].doc = g_docMain;
    g_methods[2].name = "field";
    g_methods[2].meth = (void*)&TriarchField;
    g_methods[2].flags = 1;
    g_methods[2].doc = "field(name) -> value of a DECLARED field";
    g_methods[3].name = "set_field";
    g_methods[3].meth = (void*)&TriarchSetField;
    g_methods[3].flags = 1;
    g_methods[3].doc = "set_field(name, value[, value2]) -> None";
    g_methods[4].name = "drop_engagement";
    g_methods[4].meth = (void*)&TriarchDropEngagement;
    g_methods[4].flags = 1;
    g_methods[4].doc = "drop_engagement() -> the VID that was dropped, or 0";
    g_methods[5].name = "ground_items";
    g_methods[5].meth = (void*)&TriarchGroundItems;
    g_methods[5].flags = 1;
    g_methods[5].doc = "ground_items() -> count, and snapshots the ids";
    g_methods[6].name = "ground_item_at";
    g_methods[6].meth = (void*)&TriarchGroundItemAt;
    g_methods[6].flags = 1;
    g_methods[6].doc = "ground_item_at(i) -> item id from the last snapshot";
    g_methods[7].name = "ground_item_vnum";
    g_methods[7].meth = (void*)&TriarchGroundItemVnum;
    g_methods[7].flags = 1;
    g_methods[7].doc = "ground_item_vnum(i) -> item vnum from the last snapshot";
    g_methods[8].name = "ground_item_pos";
    g_methods[8].meth = (void*)&TriarchGroundItemPos;
    g_methods[8].flags = 1;
    g_methods[8].doc = "ground_item_pos(i) -> (x, y) of a drop";
    g_methods[9].name = "ground_item_str";
    g_methods[9].meth = (void*)&TriarchGroundItemStr;
    g_methods[9].flags = 1;
    g_methods[9].doc = "ground_item_str(i, which) -> trailing std::string (0=+0x38C, 1=+0x3A4)";
    g_methods[10].name = "actors";
    g_methods[10].meth = (void*)&TriarchActors;
    g_methods[10].flags = 1;
    g_methods[10].doc = "actors() -> count of visible characters, snapshots the VIDs";
    g_methods[11].name = "actor_at";
    g_methods[11].meth = (void*)&TriarchActorAt;
    g_methods[11].flags = 1;
    g_methods[11].doc = "actor_at(i) -> VID from the last actors() snapshot";
    g_methods[12].name = "skip_collision";
    g_methods[12].meth = (void*)&TriarchSkipCollision;
    g_methods[12].flags = 1;
    g_methods[12].doc = "skip_collision(on) -> 1/0. SELF only. WARNING: disables terrain too.";
    g_methods[13].name = "actor_pass";
    g_methods[13].meth = (void*)&TriarchActorPass;
    g_methods[13].flags = 1;
    g_methods[13].doc = "actor_pass(on[,ex_lo,ex_hi]) -> 1/0. Walk through actors; races in [ex_lo,ex_hi] stay solid. Terrain unaffected.";
    g_methods[14].name = "terrain_pass";
    g_methods[14].meth = (void*)&TriarchTerrainPass;
    g_methods[14].flags = 1;
    g_methods[14].doc = "terrain_pass(on) -> 1/0. WARNING: walks through WALLS. Server-visible.";
    g_methods[15].name = "http_post";
    g_methods[15].meth = (void*)&TriarchHttpPost;
    g_methods[15].flags = 1;                      // METH_VARARGS
    g_methods[15].doc = "http_post(url, auth, content_type, body) -> HTTP status, or <0 on local failure";
    g_methods[16].name = "fishing_poll";
    g_methods[16].meth = (void*)&TriarchFishingPoll;
    g_methods[16].flags = 1;                      // METH_VARARGS (args ignored)
    g_methods[16].doc = "fishing_poll() -> (seq, sub, bite): rod-fishing packet state";
    g_methods[17].name = "key_event";
    g_methods[17].meth = (void*)&TriarchKeyEvent;
    g_methods[17].flags = 1;                      // METH_VARARGS
    g_methods[17].doc = "key_event(vk, scan, down) -> events sent. Inject a scancode key (DirectInput-visible)";
    g_methods[18].name = "offline_shops";
    g_methods[18].meth = (void*)&TriarchOfflineShops;
    g_methods[18].flags = 1;                      // METH_VARARGS (args ignored)
    g_methods[18].doc = "offline_shops() -> count of loaded offline-shop entities (snapshots seller+pos)";
    g_methods[19].name = "offline_shop_at";
    g_methods[19].meth = (void*)&TriarchOfflineShopAt;
    g_methods[19].flags = 1;                      // METH_VARARGS
    g_methods[19].doc = "offline_shop_at(i) -> (seller, x, y) from the last offline_shops()";
    g_methods[20].name = "minimap_mark";
    g_methods[20].meth = (void*)&TriarchMiniMapMark;
    g_methods[20].flags = 1;                      // METH_VARARGS
    g_methods[20].doc = "minimap_mark(id, x, y, vid, name) -> 1/0. Type-13 target mark on minimap+atlas; nonzero vid auto-tracks.";
    g_methods[21].name = "minimap_unmark";
    g_methods[21].meth = (void*)&TriarchMiniMapUnmark;
    g_methods[21].flags = 1;                      // METH_VARARGS
    g_methods[21].doc = "minimap_unmark(id) -> 1/0. Removes a waypoint/target mark by id.";
    memset(&g_methods[22], 0, sizeof(g_methods[22]));   // sentinel

    void* m = NULL;
    __try {
        m = ((InitModule_t)Abs(g_glue.initModule))(g_modName, g_methods);
    } __except (EXCEPTION_EXECUTE_HANDLER) { m = NULL; }

    if (m) {
        g_nativeReady = true;
        Log("NATIVE: module 'triarch_native' registered, %d native(s) armed", g_natN);
    } else {
        Log("NATIVE: module registration FAILED - natives unavailable");
        g_glue.initModule = 0;                    // do not retry every tick
    }
}

static BYTE ConvOf(const char* s)
{
    if (_stricmp(s, "cdecl") == 0)   return CONV_CDECL;
    if (_stricmp(s, "stdcall") == 0) return CONV_STDCALL;
    return CONV_THISCALL;
}
static BYTE RetOf(const char* s)
{
    if (_stricmp(s, "u32") == 0 || _stricmp(s, "vid") == 0) return RET_U32;
    if (_stricmp(s, "i64") == 0 || _stricmp(s, "u64") == 0) return RET_I64;
    if (_stricmp(s, "f32") == 0) return RET_F32;
    return RET_VOID;
}
static BYTE ThisOf(const char* s)
{
    if (_stricmp(s, "player") == 0)    return THIS_PLAYER;
    if (_stricmp(s, "charmgr") == 0)   return THIS_CHARMGR;
    if (_stricmp(s, "netstream") == 0) return THIS_NETSTREAM;
    if (_stricmp(s, "minimap") == 0)   return THIS_MINIMAP;
    return THIS_NONE;
}

static void LoadNatives()
{
    char path[MAX_PATH];
    _snprintf_s(path, sizeof(path), _TRUNCATE, "%suriel_natives.ini", g_exeDir);
    if (GetFileAttributesA(path) == INVALID_FILE_ATTRIBUTES) {
        Log("NATIVE: no uriel_natives.ini - gateway disabled (this is fine)");
        return;
    }
    struct { const char* k; DWORD* v; } gl[] = {
        {"kPyInitModule", &g_glue.initModule},
        {"kPyTupleGetUInt", &g_glue.getUInt},
        {"kPyTupleGetString", &g_glue.getString},
        {"kPyTupleGetFloat", &g_glue.getFloat},
        {"kPyBuildValue", &g_glue.buildValue},
        {"kPyBuildNone", &g_glue.buildNone},
        {"kPyBuildException", &g_glue.buildExc},
    };
    char buf[64];
    for (int i = 0; i < 7; i++) {
        GetPrivateProfileStringA("pyglue", gl[i].k, "", buf, sizeof(buf), path);
        *gl[i].v = (DWORD)strtoul(buf, NULL, 16);
        if (!*gl[i].v) {
            Log("NATIVE: pyglue '%s' missing - gateway disabled", gl[i].k);
            memset(&g_glue, 0, sizeof(g_glue));
            return;
        }
    }
    struct { const char* k; int slot; } sg[] = {
        {"kPlayerInst", THIS_PLAYER}, {"kCharMgrInst", THIS_CHARMGR},
        {"kNetStreamInst", THIS_NETSTREAM}, {"kMiniMapInst", THIS_MINIMAP},
    };
    for (int i = 0; i < 4; i++) {
        GetPrivateProfileStringA("singletons", sg[i].k, "", buf, sizeof(buf), path);
        g_singleton[sg[i].slot] = (DWORD)strtoul(buf, NULL, 16);
    }
    // kItemInst is emitted here, in [singletons], but it is consumed through
    // g_off rather than the `this` slots - it is not a callable target, it is
    // the root of the ground-item walk. Reading it only from uriel_offsets.ini
    // (where LoadOffsets still looks for it) left it permanently zero, and
    // ItemObject() then returned NULL, which TriarchGroundItems reported as an
    // empty floor. Take it from whichever file actually carries it.
    GetPrivateProfileStringA("singletons", "kItemInst", "", buf, sizeof(buf), path);
    if (!g_off.itemInst)
        g_off.itemInst = (DWORD)strtoul(buf, NULL, 16);
    Log("NATIVE: kItemInst=%08X", g_off.itemInst);

    // The [natives] section: name = addr|conv|stackBytes|this|ret|checked
    char names[8192] = {0};
    GetPrivateProfileSectionA("natives", names, sizeof(names), path);
    for (char* p = names; *p; p += strlen(p) + 1) {
        if (g_natN >= MAX_NATIVES) break;
        char line[256];
        _snprintf_s(line, sizeof(line), _TRUNCATE, "%s", p);
        char* eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        char* f[6] = {0};
        int nf = 0;
        for (char* t = eq + 1; nf < 6; ) {
            f[nf++] = t;
            char* bar = strchr(t, '|');
            if (!bar) break;
            *bar = 0; t = bar + 1;
        }
        if (nf < 6) continue;

        Native n; memset(&n, 0, sizeof(n));
        _snprintf_s(n.name, sizeof(n.name), _TRUNCATE, "%s", line);
        n.va       = (DWORD)strtoul(f[0], NULL, 16);
        n.conv     = ConvOf(f[1]);
        n.stack    = (BYTE)atoi(f[2]);
        n.thisKind = ThisOf(f[3]);
        n.ret      = RetOf(f[4]);
        n.checked  = (f[5][0] == '1');

        // FAIL CLOSED. Everything here is a reason the call could corrupt the
        // stack or return garbage, and none of it is detectable at call time.
        if (!n.va)                       { Log("NATIVE: %s has no address - skipped", n.name); continue; }
        if (n.stack % 4)                 { Log("NATIVE: %s stack %d not a multiple of 4 - skipped", n.name, n.stack); continue; }
        if (n.stack / 4 > 16)            { Log("NATIVE: %s takes too many arguments - skipped", n.name); continue; }
        if (!n.checked && n.conv != CONV_CDECL)
                                         { Log("NATIVE: %s not cross-checked against its ret immediate - skipped", n.name); continue; }
        if (n.ret == RET_I64 || n.ret == RET_F32)
                                         { Log("NATIVE: %s returns a kind we cannot marshal losslessly yet - skipped", n.name); continue; }
        if (n.thisKind != THIS_NONE && !g_singleton[n.thisKind])
                                         { Log("NATIVE: %s wants a singleton we did not resolve - skipped", n.name); continue; }
        g_nat[g_natN++] = n;
    }
    char fsec[4096] = {0};
    GetPrivateProfileSectionA("fields", fsec, sizeof(fsec), path);
    for (char* p = fsec; *p && g_fldN < MAX_FIELDS; p += strlen(p) + 1) {
        char line[192];
        _snprintf_s(line, sizeof(line), _TRUNCATE, "%s", p);
        char* eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        char* f[4] = {0};
        int nf = 0;
        for (char* t = eq + 1; nf < 4; ) {
            f[nf++] = t;
            char* bar = strchr(t, '|');
            if (!bar) break;
            *bar = 0; t = bar + 1;
        }
        if (nf < 4) continue;
        Field fl; memset(&fl, 0, sizeof(fl));
        _snprintf_s(fl.name, sizeof(fl.name), _TRUNCATE, "%s", line);
        fl.owner    = (_stricmp(f[0], "instance") == 0) ? OWN_INSTANCE : OWN_PLAYER;
        fl.off      = (DWORD)strtoul(f[1], NULL, 16);
        fl.type     = FieldType(f[2]);
        fl.writable = (strchr(f[3], 'w') != NULL);
        if (!fl.off) { Log("NATIVE: field %s has no offset - skipped", fl.name); continue; }
        g_fld[g_fldN++] = fl;
    }
    Log("NATIVE: %d native(s), %d field(s) loaded from uriel_natives.ini",
        g_natN, g_fldN);
}

// Registration needs a live interpreter, so it rides the same readiness signal
// the mod host already waits for rather than inventing a second one.
static void NativeTick()
{
    if (g_nativeReady || !g_glue.initModule || !g_natN) return;
    if ((g_tick % 30) != 2) return;
    if (!LauncherThis()) return;
    RegisterNativeModule();
}

static void ModsTick()
{
    if (!g_modsOn || g_modsDead) return;

    if (!g_modsReady) {
        // The launcher is built well after DllMain, so poll rather than assume.
        if ((g_tick % 30) != 1) return;
        if (!LauncherThis()) return;
        // strip the trailing backslash: the path goes into a Python r'' literal
        // and a trailing backslash would escape the closing quote.
        char logDir[MAX_PATH];
        _snprintf_s(logDir, sizeof(logDir), _TRUNCATE, "%s", g_dataDir);
        size_t n = strlen(logDir);
        if (n && logDir[n - 1] == '\\') logDir[n - 1] = 0;

        char boot[4096];
        _snprintf_s(boot, sizeof(boot), _TRUNCATE, kBootstrapFmt, g_modsDir, logDir);
        if (PyRunLine(boot)) {
            g_modsReady = true;
            // RunLine only reports "nothing escaped"; the bootstrap swallows its
            // own errors, so ask the Python side what actually happened.
            char st[1024] = {0};
            DWORD n = GetEnvironmentVariableA("TRIARCH_MODS", st, sizeof(st));
            Log("MODS: bootstrap ran (tick %u) status: %s", g_tick,
                (n && n < sizeof(st)) ? st : "(no status - Python could not report)");
        } else if (++g_bootTries >= 5) {
            g_modsDead = true;
            Log("MODS: bootstrap failed %d times - giving up", g_bootTries);
        }
        return;
    }

    if ((g_tick % kPumpEvery) != 0) return;
    if (PyRunLine("_triarch_pump()")) {
        g_pumpFails = 0;
    } else if (++g_pumpFails >= 30) {
        // Python side is wedged; stop rather than print a traceback per pump.
        g_modsDead = true;
        Log("MODS: pump failed %d times - mod host stopped", g_pumpFails);
    }
}
#endif


// ---------------------------------------------------------------------------
// runtime offset loading
// ---------------------------------------------------------------------------
static DWORD IniHex(const char* path, const char* key)
{
    char buf[32] = {0};
    GetPrivateProfileStringA("offsets", key, "", buf, sizeof(buf), path);
    return (DWORD)strtoul(buf, NULL, 16);
}

static bool LoadOffsets()
{
    char path[MAX_PATH];
    _snprintf_s(path, sizeof(path), _TRUNCATE, "%suriel_offsets.ini", g_exeDir);
    if (GetFileAttributesA(path) == INVALID_FILE_ATTRIBUTES) {
        Log("FATAL: %s not found - run the patcher for this build", path);
        return false;
    }
    struct { const char* k; DWORD* v; } m[] = {
        {"kTraceSink",&g_off.traceSink}, {"kTraceReal",&g_off.traceReal},
        {"kVerifyBufsEqual",&g_off.verifyBufsEqual}, {"kSendAppend",&g_off.sendAppend},
        {"kRecvBuf",&g_off.recvBuf}, {"kAuthRecvPhase",&g_off.authRecvPhase},
        {"kAuthProcess",&g_off.authProcess}, {"kGetPcName",&g_off.getPcName},
        {"kGetHwProfileId",&g_off.getHwProfileId}, {"kIatConnect",&g_off.iatConnect},
        {"kIatClosesocket",&g_off.iatClosesocket}, {"kIatSend",&g_off.iatSend},
        {"kIatRecv",&g_off.iatRecv}, {"kIatWSAGetLastError",&g_off.iatWSAGetLastError},
        {"kIatWinHttpConnect",&g_off.iatWinHttpConnect},
        {"kIatWinHttpOpenRequest",&g_off.iatWinHttpOpenRequest},
        {"kIatInternetOpenUrlA",&g_off.iatInternetOpenUrlA},
        {"kIatURLDownloadToFileA",&g_off.iatURLDownloadToFileA},
        {"kUrielObjOffset",&g_off.urielObjOffset},
        {"SLOT2_ARG_BYTES",&g_off.slot2ArgBytes},
    };
    int missing = 0;
    for (int i = 0; i < (int)(sizeof(m) / sizeof(m[0])); i++) {
        *m[i].v = IniHex(path, m[i].k);
        if (*m[i].v == 0) { Log("FATAL: offset '%s' missing/zero", m[i].k); missing++; }
    }
    if (missing) return false;

    // optional block - absence disables a feature, it is never fatal
    struct { const char* k; DWORD* v; } opt[] = {
        {"kPyRunLine",&g_off.pyRunLine}, {"kPyLauncherInst",&g_off.pyLauncherInst},
        {"kOnPressActor",&g_off.onPressActor}, {"kPlayerInst",&g_off.playerInst},
        {"kPlayerSubObj",&g_off.playerSubObj}, {"kGetMainInstOff",&g_off.getMainInstOff},
        {"kFindAndSetNewTarget",&g_off.findAndSetNewTarget},
        {"kAutoAtkVidOff",&g_off.autoAtkVidOff}, {"kAnchorOff",&g_off.anchorOff},
        {"kMiniMapInst",&g_off.miniMapInst}, {"kAutoHuntRangeOff",&g_off.autoHuntRangeOff},
        {"kUseAutoSkills",&g_off.useAutoSkills},
        {"kHuntUseSkillOff",&g_off.huntUseSkillOff},
        {"kHuntSkillVecOff",&g_off.huntSkillVecOff},
        {"kMountedOff",&g_off.mountedOff},
        {"kHuntStoneOff",&g_off.huntStoneOff},
        {"kItemInst",&g_off.itemInst},
        {"kEnableSkipCollision",&g_off.enableSkipCollision},
        {"kDisableSkipCollision",&g_off.disableSkipCollision},
        {"kSkipCollisionOff",&g_off.skipCollisionOff},
        {"kInstActorOff",&g_off.instActorOff},
        {"kActorPassLoVA",&g_off.actorPassLoVA},
        {"kActorPassHiVA",&g_off.actorPassHiVA},
        // Not addresses: the ORIGINAL range values, so a restore writes back
        // what the build actually shipped rather than a constant baked in here.
        // The mount vnums differ per server, so hardcoding them would silently
        // corrupt the whitelist on any other build.
        {"kActorPassLo",&g_off.actorPassLo},
        {"kActorPassHi",&g_off.actorPassHi},
        {"kBlockMovement",&g_off.blockMovement},
        {"kActorPass2LoVA",&g_off.actorPass2LoVA},
        {"kActorPass2HiVA",&g_off.actorPass2HiVA},
        {"kActorPass2Lo",&g_off.actorPass2Lo},
        {"kActorPass2Hi",&g_off.actorPass2Hi},
        {"kOfflineshopRecv",&g_off.offlineshopRecv},
        {"kOfflineshopInst",&g_off.offlineshopInst},
        {"kMiniMapAddWayPoint",&g_off.miniMapAddWayPoint},
        {"kMiniMapRemoveWayPoint",&g_off.miniMapRemoveWayPoint},
    };
    for (int i = 0; i < (int)(sizeof(opt) / sizeof(opt[0])); i++) {
        *opt[i].v = IniHex(path, opt[i].k);
        if (*opt[i].v == 0)
            Log("note: optional offset '%s' absent - regenerate the ini to enable mods",
                opt[i].k);
    }
    // Tell the Python side whether the native attack bridge is usable, so a mod
    // can degrade gracefully on a client patched before it existed instead of
    // silently failing to attack. api.attack_available() reads this.
    bool haveAttack = g_off.onPressActor && g_off.playerInst && g_off.getMainInstOff;
    SetEnvironmentVariableA("TRIARCH_ATTACK_OK", haveAttack ? "1" : "0");
    Log("attack bridge: %s", haveAttack ? "armed" : "absent (offsets missing)");

    bool haveHunt = haveAttack && g_off.findAndSetNewTarget && g_off.autoAtkVidOff;
    SetEnvironmentVariableA("TRIARCH_HUNT_OK", haveHunt ? "1" : "0");
    Log("autohunt drive: %s", haveHunt ? "armed" : "absent (offsets missing)");

    // Separate from the hunt flag on purpose: skills are additive, so a client
    // whose ini predates them must still hunt.
    bool haveSkills = haveHunt && g_off.useAutoSkills && g_off.huntUseSkillOff;
    SetEnvironmentVariableA("TRIARCH_SKILL_OK", haveSkills ? "1" : "0");
    Log("autohunt skills: %s", haveSkills ? "armed" : "absent (offsets missing)");

    LoadNatives();          // the gateway table; absence is not fatal
    Log("offsets loaded from %s", path);
    return true;
}

// slot2 must clean exactly the argument bytes the client pushes. That is an
// imm16 baked into `ret`, so rewrite our own instruction rather than guess.
static void ApplySlot2Ret(void)
{
    BYTE* p = (BYTE*)&slot2;
    for (int i = 0; i < 0x80; i++) {
        if (p[i] == 0xC2 && p[i + 1] == 0x1C && p[i + 2] == 0x00) {
            if ((WORD)g_off.slot2ArgBytes == 0x1C) return;      // already correct
            DWORD old;
            if (VirtualProtect(p + i, 3, PAGE_EXECUTE_READWRITE, &old)) {
                *(WORD*)(p + i + 1) = (WORD)g_off.slot2ArgBytes;
                VirtualProtect(p + i, 3, old, &old);
                FlushInstructionCache(GetCurrentProcess(), p + i, 3);
                Log("slot2 ret imm16 rewritten to 0x%02X", g_off.slot2ArgBytes);
            }
            return;
        }
    }
    Log("WARN: could not locate slot2 `ret` to rewrite");
}

BOOL APIENTRY DllMain(HMODULE, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_cs);
        LogInit();
        g_ready = true;
        g_obj.vptr = g_vtable;
        Log("uriel_stub attached (exe base %p)", GetModuleHandleW(NULL));
        g_haveOffsets = LoadOffsets();
        if (!g_haveOffsets) { Log("no offsets - running inert (no hooks)"); return TRUE; }
        ApplySlot2Ret();
#if ENABLE_UDIAG_PATCH
        PatchTraceSink();
#endif
#if ENABLE_VBE_HOOK
        HookVerifyBufsEqual();
#endif
#if ENABLE_NET_LOG
        InstallNetHooks();
#endif
#if ENABLE_AUTH_TRACE
        InstallAuthHooks();
#endif
#if ENABLE_SHOP_CAPTURE
        InstallShopCapture();
#endif
#if ENABLE_MODS
        ModsInit();
#endif
    }
    return TRUE;
}
