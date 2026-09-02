"""attack_figures.py - draw the many-time-pad attack from a protected exe.

Produces the five figures used in docs/02-unpacking-offline.md, from the
ciphertext alone: the column vote, one offset as a histogram, the winner vs
runner-up margins at all 4096 positions, a page as a grey image before and
after, and a hexdump before and after.

    python tools/attack_figures.py <triarch.exe> <output dir>

Needs numpy and matplotlib. Takes ~20 s.
"""
import collections
import struct
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import colors
import numpy as np

EXE = sys.argv[1]
OUT = sys.argv[2]
PAGE = 4096
d = open(EXE, "rb").read()
pe = struct.unpack_from("<I", d, 0x3C)[0]
nsec = struct.unpack_from("<H", d, pe + 6)[0]
optsz = struct.unpack_from("<H", d, pe + 20)[0]
off = pe + 24 + optsz
for _ in range(nsec):
    nm = d[off:off + 8].rstrip(b"\0").decode("latin1")
    vs, va, rs, ro = struct.unpack_from("<IIII", d, off + 8)
    if nm == ".text":
        break
    off += 40
size = min(rs, vs)
total = size // PAGE
text = np.frombuffer(d[ro:ro + total * PAGE], dtype=np.uint8).reshape(total, PAGE)
print("pages", total)

# full vote: counts[j, v] = number of pages with byte v at offset j
counts = np.zeros((PAGE, 256), dtype=np.int32)
for j in range(PAGE):
    counts[j] = np.bincount(text[:, j], minlength=256)
key = counts.argmax(axis=1).astype(np.uint8)
srt = np.sort(counts, axis=1)
mode_share = srt[:, -1] / total
runner_share = srt[:, -2] / total
plain = text ^ key[None, :]

MONO = {"family": "DejaVu Sans Mono"}
plt.rcParams["font.family"] = "DejaVu Sans"


def hexgrid(ax, rows, ncols, labels, title, highlight=None, cmap_zero=True):
    """rows: list of byte arrays (len ncols). highlight: (row_idx, col_idx) cells to box."""
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlim(-0.5, ncols - 0.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xticks(range(ncols))
    ax.set_xticklabels(["%02X" % c for c in range(ncols)], fontsize=7, **MONO)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=8, **MONO)
    ax.tick_params(length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    for r, row in enumerate(rows):
        for c in range(ncols):
            v = int(row[c])
            bg = "#ffe08a" if (cmap_zero and v == 0) else ("#f6c1c1" if (cmap_zero and v == 0xCC) else "white")
            ax.add_patch(plt.Rectangle((c - 0.5, r - 0.5), 1, 1, facecolor=bg, edgecolor="#dddddd", lw=0.5))
            ax.text(c, r, "%02X" % v, ha="center", va="center", fontsize=7.5, **MONO)
    if highlight:
        for (r, c) in highlight:
            ax.add_patch(plt.Rectangle((c - 0.5, r - 0.5), 1, 1, fill=False, edgecolor="#d62728", lw=1.8))


# ---------- Figure 1: pages stacked, one column = one key position -------------
NCOL, NROW = 24, 12
pages = list(range(0, total, total // NROW))[:NROW]
rows = [text[p, :NCOL] for p in pages]
labels = ["page %5d" % p for p in pages]
fig, axes = plt.subplots(2, 1, figsize=(11.5, 7.6), gridspec_kw={"height_ratios": [NROW, 3.2]})
# highlight column mode cells
hl = []
for c in range(NCOL):
    kv = key[c]
    for r, p in enumerate(pages):
        if text[p, c] == kv:
            hl.append((r, c))
hexgrid(axes[0], rows, NCOL,  labels,
        "Ciphertext: the first %d bytes of %d pages of .text, one page per row  (red boxes = bytes equal to the column's most common value)" % (NCOL, NROW),
        highlight=hl, cmap_zero=False)
axes[0].set_xlabel("offset j inside the page  ->  the SAME key byte K[j] was XORed into every cell of a column", fontsize=9)
# bottom: mode per column + share + the key
bot_rows = [key[:NCOL]]
hexgrid(axes[1], bot_rows, NCOL, ["K[j] = mode"], "Most common byte per column over ALL %d pages = the recovered key" % total, cmap_zero=False)
for c in range(NCOL):
    axes[1].text(c, 0.75, "%d%%" % round(100 * mode_share[c]), ha="center", va="center", fontsize=6.5, color="#444")
axes[1].set_ylim(1.4, -0.5)
axes[1].set_yticks([0, 0.75]); axes[1].set_yticklabels(["K[j] = mode", "share of pages"], fontsize=8, **MONO)
fig.tight_layout()
fig.savefig(OUT + "/02-fig1-column-vote.png", dpi=150)
plt.close(fig)

# ---------- Figure 2: histogram at one offset, ciphertext vs after XOR ---------
j = 7
fig, axes = plt.subplots(1, 2, figsize=(11.5, 3.8))
c_hist = counts[j]
axes[0].bar(range(256), c_hist, width=1.0, color="#4c72b0")
axes[0].set_title("Ciphertext bytes seen at offset j=%d across %d pages" % (j, total), fontsize=10, loc="left")
axes[0].set_xlabel("byte value"); axes[0].set_ylabel("pages")
axes[0].annotate("spike at 0x%02X = K[%d]" % (key[j], j), xy=(key[j], c_hist[key[j]]), xytext=(key[j] + 40 if key[j] < 180 else key[j] - 120, c_hist[key[j]] * 0.85),
                 arrowprops=dict(arrowstyle="->", color="#d62728"), fontsize=9, color="#d62728")
p_hist = np.bincount(plain[:, j], minlength=256)
axes[1].bar(range(256), p_hist, width=1.0, color="#55a868")
axes[1].set_title("The same bytes after XOR with K[%d]: the plaintext distribution" % j, fontsize=10, loc="left")
axes[1].set_xlabel("byte value"); axes[1].set_ylabel("pages")
axes[1].annotate("0x00: %d%% of pages" % round(100 * p_hist[0] / total), xy=(0, p_hist[0]), xytext=(40, p_hist[0] * 0.9),
                 arrowprops=dict(arrowstyle="->", color="#d62728"), fontsize=9, color="#d62728")
axes[1].annotate("0xCC (int3 padding)", xy=(0xCC, p_hist[0xCC]), xytext=(150, p_hist[0] * 0.55),
                 arrowprops=dict(arrowstyle="->", color="#555"), fontsize=8, color="#555")
for ax in axes:
    ax.set_xlim(-2, 258)
    ax.set_xticks([0, 0x40, 0x80, 0xC0, 0xFF]); ax.set_xticklabels(["00", "40", "80", "C0", "FF"], **MONO)
fig.tight_layout()
fig.savefig(OUT + "/02-fig2-histogram.png", dpi=150)
plt.close(fig)

# ---------- Figure 3: margins across all 4096 positions ------------------------
fig, ax = plt.subplots(figsize=(11.5, 3.2))
ax.plot(mode_share * 100, lw=0.6, color="#4c72b0", label="winner (share of pages voting for K[j])")
ax.plot(runner_share * 100, lw=0.6, color="#c44e52", label="runner-up")
ax.set_xlim(0, PAGE); ax.set_ylim(0, max(30, mode_share.max() * 100 + 2))
ax.set_xlabel("key position j (0..4095)"); ax.set_ylabel("% of pages")
ax.set_title("How decisive the vote is at every key position: winner vs runner-up  (min winner %.1f%%, max runner-up %.1f%%)"
             % (mode_share.min() * 100, runner_share.max() * 100), fontsize=10, loc="left")
ax.legend(loc="upper right", fontsize=8, frameon=False)
fig.tight_layout()
fig.savefig(OUT + "/02-fig3-margins.png", dpi=150)
plt.close(fig)

# ---------- Figure 4: a page before and after, as a byte image ------------------
p = pages[3]
fig, axes = plt.subplots(1, 3, figsize=(11.5, 3.9), gridspec_kw={"width_ratios": [1, 1, 1]})
for ax, arr, title in ((axes[0], text[p], "ciphertext page %d" % p),
                       (axes[1], key, "the 4096-byte key"),
                       (axes[2], plain[p], "page %d after XOR (plaintext)" % p)):
    ax.imshow(arr.reshape(64, 64), cmap="gray", vmin=0, vmax=255, interpolation="nearest")
    ax.set_title(title, fontsize=10)
    ax.set_xticks([]); ax.set_yticks([])
fig.suptitle("Each 64x64 image is one 4096-byte block (black = 0x00). Ciphertext and key look like noise; the plaintext shows the zero-heavy texture of x86 code.", fontsize=9)
fig.tight_layout()
fig.savefig(OUT + "/02-fig4-page-images.png", dpi=150)
plt.close(fig)

# ---------- Figure 5: hexdump before/after with readable prologues ------------
# find a decrypted page with a 55 8B EC prologue near its start for a nice dump
best = None
for q in range(total):
    row = plain[q]
    idx = bytes(row[:256]).find(b"\x55\x8b\xec")
    if idx != -1 and idx < 48:
        best = (q, idx)
        break
q, idx = best
NB = 16 * 6
fig, axes = plt.subplots(2, 1, figsize=(12.5, 6.4))
enc_rows = [text[q, r * 16:(r + 1) * 16] for r in range(NB // 16)]
dec_rows = [plain[q, r * 16:(r + 1) * 16] for r in range(NB // 16)]
labs = ["+%03X" % (r * 16) for r in range(NB // 16)]
hexgrid(axes[0], enc_rows, 16, labs, "Page %d as stored on disk (ciphertext)" % q, cmap_zero=False)
hl = [(idx // 16 + k // 16, (idx + k) % 16) for k in range(3)]
hexgrid(axes[1], dec_rows, 16, labs, "Page %d after XOR with the key. Yellow = 0x00, pink = 0xCC padding, red box = 55 8B EC (push ebp; mov ebp,esp: a function prologue)" % q, highlight=hl)
for ax in axes:
    ax.set_xticklabels(["%X" % c for c in range(16)], fontsize=7, **MONO)
fig.tight_layout()
fig.savefig(OUT + "/02-fig5-hexdump-before-after.png", dpi=150)
plt.close(fig)

print("key[:8]", key[:8].tobytes().hex(), "mode_share min/mean", mode_share.min(), mode_share.mean(), "runner max", runner_share.max(),
      "p_hist0 share at j", p_hist[0] / total, "page for fig5", q, "prologue at", idx)
