# 09 — Lab setup

How to work on a protected client without hurting yourself. None of this is
specific to this game; it is the working method the rest of the repository
assumes.

## Contents

1. [The one rule](#the-one-rule)
2. [A disposable Windows VM](#a-disposable-windows-vm)
3. [Tools](#tools)
4. [Driving the VM from outside](#driving-the-vm-from-outside)
5. [The measure-don't-infer loop](#the-measure-dont-infer-loop)
6. [Keeping notes that survive you](#keeping-notes-that-survive-you)
7. [Things never to do](#things-never-to-do)

## The one rule

**Never analyse on the machine you play on.** An anti-cheat is software that
watches other software; a debugger, a hooking framework or a patched
executable on your real account is at best a ban and at worst a wiped
folder. Everything in this project was developed inside a virtual machine
with a snapshot to fall back to and a throw-away account.

## A disposable Windows VM

Any hypervisor works (VirtualBox, VMware, Hyper-V, QEMU/KVM). What matters:

- **Windows 10/11 x64.** The client is 32-bit and runs under WoW64.
- **Snapshot before the first launch** of anything, and again once the tools
  are installed. Roll back freely.
- **GPU is optional.** A software-rendered client runs at a few frames per
  second and is perfectly good for tracing and for the mod host; only the
  fishing-style timing mods care about frame rate.
- **A separate game account** created for the lab. Your real credentials
  never enter the VM.
- **Shared folder or SSH** to move files in and out. The patcher writes its
  log to `_patcher\patcher_run.log`, the stub to `uriel_stub.log`, the mod
  host to `mods\mods.log`; you will be reading those from outside.

## Tools

| tool | for | notes |
|---|---|---|
| Python 3.11+ | everything in `tools/` and `patcher/` | `pip install capstone pyinstaller` |
| MSVC 2017 Build Tools | `stub/uriel_stub.cpp` | the 32-bit toolchain (`vcvars32.bat`); newer versions build it too |
| Ghidra | reading the rebuilt exe | import `triarch_clean.exe`, x86:LE:32, let it analyse overnight |
| x64dbg (x32dbg) | stepping through the live client | attach to `triarch_clean.exe`, not the protected one |
| Frida | tracing calls without stopping the game | `frida-trace -p <pid> -a 'triarch_clean.exe!0x...'`; the fastest way to *measure* |
| a hex editor | looking at bytes when a tool disagrees with you | any |

Ghidra on a 68 MB code section is slow. The rebuild disables ASLR precisely
so that the addresses Ghidra shows are the addresses you see in the debugger
and in `uriel_offsets.ini`; keep one project per build.

## Driving the VM from outside

A pattern that keeps the analysis notes and scripts on the host, and only the
game in the guest:

- Host runs the editor, Ghidra, the notes, git.
- Guest runs the client, the patcher exe and Frida.
- A small `push`/`pull`/`run` trio over SSH (or a shared folder) moves files
  and runs commands. Anything that needs a desktop (the game, Frida attaching
  to it) must run in the interactive session; on Windows that means a
  scheduled task created with `/it`, not a plain SSH command.
- Several clients can run at once; resolve which PID is which *before*
  attaching to anything.

## The measure-don't-infer loop

Every wrong conclusion in this project's history came from reading a
disassembly and assuming. The loop that replaced it:

1. **Form a hypothesis from static reading.** "This `mov [esi+0x58], eax` is
   where the walk state is stored."
2. **Write the smallest possible probe.** A Frida script that logs the value
   at that instruction, or a one-line mod that prints `api.field(...)` every
   second.
3. **Trigger the behaviour in the game and watch the probe.** If it prints
   zeros, suspect the address before the conclusion.
4. **Only then** write the resolver, the native or the mod.
5. **Resolve on two builds.** If the anchor holds on both and the values
   make sense in both disassemblies, it is a resolver. If not, it is a
   coincidence.

The stub's log, the mod host's log and the patcher's log are all designed to
be read in this loop: they say what they found and what they refused.

## Keeping notes that survive you

Three files, kept from day one, paid for themselves many times over:

- **failed-experiments.md** — every dead end, with what was tried, what was
  observed, and how to verify it. The purpose is to stop the next person (or
  you in a month) from re-running it.
- **decisions.md** — why things are the way they are ("terrain pass-through
  is not exposed because the client reports its own position").
- **blockers.md** — what is open right now.

Write down what you *measured*, with the build it was measured on. A number
without a build is a future bug.

## Things never to do

- **Send crafted, duplicated or malformed packets to a live server.** Server
  operators run analytics; a malformed packet is a fingerprint and possibly a
  crash on their side. Test protocol ideas against a server you own.
- **Walk through terrain.** The map attribute is the server's collision
  model and the client reports x/y several times a second. Actor
  pass-through (mobs, players) is client-only.
- **Keep credentials in anything you might share** — configs, logs,
  screenshots, this repository.
- **Trust a hook that reports all zeros.** It is almost always pointed at the
  wrong address.
