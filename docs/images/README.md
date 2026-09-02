# Screenshots to add

Placeholders in the tutorials reference the files below. Capture each one on a lab VM, crop to the relevant panel, save as PNG under `docs/images/` with exactly this file name, and the Markdown will pick it up.

| id | file | what to capture | referenced from |
|---|---|---|---|
| 01-A | `01-A-pebear-sections-and-entropy.png` | PE-bear 'Section Hdrs' of the PROTECTED triarch.exe (injected 3-letter section visible) side by side with Detect It Easy's entropy view showing .text at ~8.0 | 01, end of 'Background: what a PE file looks like' |
| 01-B | `01-B-hxd-page0-and-ksattack.png` | HxD at offset 0x400 of the protected exe (page 0 of .text) beside a terminal showing `python tools/ksattack.py triarch.exe` output | 01, end of 'Transformation 1' |
| 01-C | `01-C-sysinformer-text-protections.png` | System Informer → Memory tab of the RUNNING protected client, sorted by address, showing alternating R / RX pages inside .text (0x410000+) | 01, end of 'Transformation 2' |
| 01-D | `01-D-pebear-imports-and-iat-bytes.png` | PE-bear Imports tab (only client_x86.dll → FireInTheHole) plus the hex view at the IAT data directory showing RVAs, 0x8000xxxx ordinals and zero separators | 01, end of 'Transformation 3' |
| 01-E | `01-E-pebear-entrypoint-nopsled.png` | PE-bear Optional Hdr with Entry Point = injected section RVA, and the Disasm pane at that RVA full of 90 (NOP) bytes | 01, end of 'Transformation 4' |
| 01-F | `01-F-x32dbg-memory-map.png` | x32dbg attached to the running PROTECTED client: Memory Map (client_x86.dll present) and the CPU pane showing readable code at an already-executed .text address | 01, end of 'How each transformation was discovered' |
| 02-A | `02-A-ksattack-output-and-key.png` | Terminal running `python tools/ksattack.py triarch.exe` (with the 4096/4096 / 0 weak lines) and HxD open on the saved key.bin | 02, end of section 1 (many-time-pad attack) |
| 02-B | `02-B-decode-one-name-and-profile.png` | Python REPL decoding one XORed hint/name entry into an API name, beside profile.json (from `unuriel.py derive`) open in an editor showing the iat entries | 02, end of section 2 (decoding the import names) |
| 02-C | `02-C-derive-groups.png` | Terminal output of `unuriel.py derive` showing the 22-line DLL group table and the OLEAUT32/WS2_32 tie-break line | 02, end of section 3 (groups and DLL attribution) |
| 02-D | `02-D-oep-call-jmp.png` | PE-bear (or Ghidra) at the RESTORED entry point of triarch_clean.exe: CC padding, then `call` immediately followed by `jmp` | 02, end of section 4 (finding the OEP) |
| 02-E | `02-E-before-after-imports.png` | Two PE-bear windows side by side: Imports of the protected exe (1 DLL) vs the clean exe (22 DLLs); clean exe's Section Hdrs showing .unuriel | 02, end of section 5 (rebuild) |
| 03-A | `03-A-patcher-window-and-folder.png` | TriarchPatcher.exe log window after a successful 7-step run, plus an Explorer view of the game folder with triarch_clean.exe, uriel_stub.dll, the two .ini files, mods\ and _patcher\ | 03, end of 'The steps in order' (before the --live section) |
| 03-B | `03-B-stub-and-mods-logs.png` | _patcher\\uriel_stub.log and mods\mods.log opened side by side right after the first launch of the patched client | 03, end of 'Output files and what breaks without each' |

Conventions: 1x scale, no personal data in window titles or paths, the protected file is always called `triarch.exe` and the rebuilt one `triarch_clean.exe`.

## Generated figures (not screenshots)

`02-fig1` … `02-fig5` are produced by `python tools/attack_figures.py <triarch.exe> docs/images` from the protected executable. Regenerate them rather than editing; do not replace them with screenshots.
