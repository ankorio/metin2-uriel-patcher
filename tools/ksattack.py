"""Feasibility test: recover Uriel's .text keystream statically, no live run.

Uriel XORs .text with one fixed 4096-byte keystream, tiled once per page. With
~17 000 pages that is a many-time pad: every page reuses the same key, so for a
given page offset j the ciphertext bytes C_i[j] are just P_i[j] ^ K[j]. The
plaintext is x86 code, whose byte distribution is very far from uniform (0x00
dominates, 0xCC pads between MSVC functions), so K[j] falls out of the
per-offset histogram without needing any crib.

    python ksattack.py <encrypted.exe> [decrypted.exe] [--pages N]

With the decrypted exe supplied it scores the recovery against ground truth.
"""
import collections
import struct
import sys


def text_section(path):
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    off = pe + 24 + optsz
    for _ in range(nsec):
        nm = d[off:off + 8].rstrip(b"\0").decode("latin1")
        vs, va, rs, ro = struct.unpack_from("<IIII", d, off + 8)
        if nm == ".text":
            return d, ro, min(rs, vs)
        off += 40
    raise SystemExit("no .text")


def main():
    enc_path = sys.argv[1]
    dec_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
    npages = int(sys.argv[sys.argv.index("--pages") + 1]) if "--pages" in sys.argv else 4000

    d, ro, size = text_section(enc_path)
    total = size // 4096
    step = max(1, total // npages)
    pages = list(range(0, total, step))
    print("%s: .text raw 0x%x, %d pages, sampling %d" % (enc_path, ro, total, len(pages)))

    # --- the attack: per-offset histogram over ciphertext only -------------
    guess = bytearray(4096)
    second = bytearray(4096)
    margin = [0.0] * 4096
    for j in range(4096):
        c = collections.Counter(d[ro + p * 4096 + j] for p in pages)
        top = c.most_common(2)
        guess[j] = top[0][0]                       # assumes plaintext 0x00
        second[j] = top[1][0] if len(top) > 1 else top[0][0]
        margin[j] = top[0][1] / float(len(pages))

    if not dec_path:
        open("keystream.bin", "wb").write(bytes(guess))
        print("wrote keystream.bin (unverified)")
        return

    # --- ground truth: K = C ^ P, must be constant across pages ------------
    dd, dro, dsize = text_section(dec_path)
    truth = bytes(d[ro + j] ^ dd[dro + j] for j in range(4096))
    consistent = sum(
        1 for p in pages[:400]
        if all(d[ro + p * 4096 + j] ^ dd[dro + p * 4096 + j] == truth[j] for j in range(0, 4096, 16))
    )
    print("keystream constant across %d/400 sampled pages" % consistent)

    exact = sum(1 for j in range(4096) if guess[j] == truth[j])
    # a byte is also 'recovered' if the modal plaintext was 0xCC rather than 0x00
    with_cc = sum(1 for j in range(4096)
                  if guess[j] == truth[j] or (guess[j] ^ 0xCC) == truth[j])
    top2 = sum(1 for j in range(4096)
               if truth[j] in (guess[j], second[j], guess[j] ^ 0xCC, second[j] ^ 0xCC))
    print("assume plaintext 0x00        : %4d/4096  (%.1f%%)" % (exact, 100.0 * exact / 4096))
    print("allow 0x00 or 0xCC           : %4d/4096  (%.1f%%)" % (with_cc, 100.0 * with_cc / 4096))
    print("truth within top-2 x {00,CC} : %4d/4096  (%.1f%%)" % (top2, 100.0 * top2 / 4096))
    lo = sum(1 for j in range(4096) if margin[j] < 0.05)
    print("offsets with a weak mode (<5%%): %d  <- the ones needing a real model" % lo)


if __name__ == "__main__":
    main()
