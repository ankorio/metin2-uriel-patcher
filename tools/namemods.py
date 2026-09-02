"""Recover module / type names for the PyMethodDef tables in pysyms.json.

A PyMethodDef array is never referenced anonymously: CPython points at it from
either a PyModuleDef (field m_methods) or a PyTypeObject (field tp_methods).
Both structures carry a name string at a fixed offset from that pointer, so
finding the 4-byte reference to the table gives us the name for free.

32-bit CPython 3.14 layouts:

  PyModuleDef                       PyTypeObject
    +0  m_base (20 bytes)             +12  tp_name
    +20 m_name   <- methods-12        ...
    +24 m_doc    <- methods-8         +116 tp_methods  -> tp_name = methods-104
    +28 m_size
    +32 m_methods

    python namemods.py <decrypted.exe> <pysyms.json> [-o out.json]
"""
import json
import re
import struct
import sys

MODDEF_NAME_DELTA = -12   # m_name  relative to m_methods
MODDEF_DOC_DELTA = -8     # m_doc   relative to m_methods
TYPE_NAME_DELTA = -104    # tp_name relative to tp_methods

IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_.]{0,63}\x00")


def load_pe(path):
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    base = struct.unpack_from("<I", d, pe + 0x34)[0]
    secs = []
    off = pe + 24 + optsz
    for _ in range(nsec):
        name = d[off:off + 8].rstrip(b"\0").decode("latin1")
        vs, va, rs, ro = struct.unpack_from("<IIII", d, off + 8)
        secs.append((name, base + va, vs, ro, rs))
        off += 40
    return d, base, secs


class Image:
    def __init__(self, path):
        self.data, self.base, self.secs = load_pe(path)

    def off(self, va):
        """file offset for a virtual address, or None"""
        for _n, sva, vs, ro, rs in self.secs:
            if sva <= va < sva + max(vs, rs):
                delta = va - sva
                if delta < rs:
                    return ro + delta
                return None
        return None

    def u32(self, va):
        o = self.off(va)
        if o is None or o + 4 > len(self.data):
            return None
        return struct.unpack_from("<I", self.data, o)[0]

    def cstr(self, va, limit=64):
        """read a NUL-terminated identifier-ish string, or None"""
        o = self.off(va)
        if o is None:
            return None
        m = IDENT.match(self.data, o)
        if not m:
            return None
        return m.group()[:-1].decode("latin1")

    def section_of(self, va):
        for n, sva, vs, ro, rs in self.secs:
            if sva <= va < sva + max(vs, rs):
                return n
        return None


def find_refs(img, target):
    """every file offset holding the little-endian dword `target`"""
    needle = struct.pack("<I", target)
    out = []
    start = 0
    while True:
        i = img.data.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def off_to_va(img, off):
    for _n, sva, vs, ro, rs in img.secs:
        if ro <= off < ro + rs:
            return sva + (off - ro)
    return None


def main():
    exe, syms = sys.argv[1], sys.argv[2]
    out_path = None
    if "-o" in sys.argv:
        out_path = sys.argv[sys.argv.index("-o") + 1]

    img = Image(exe)
    tables = json.load(open(syms))

    named, unnamed = {}, {}
    for key, entries in tables.items():
        m = re.match(r"<unnamed_table_0x([0-9a-fA-F]+)>", key)
        if m:
            unnamed[int(m.group(1), 16)] = (key, entries)
        else:
            named[key] = entries

    print("tables: %d total  (%d already named, %d anonymous)"
          % (len(tables), len(named), len(unnamed)))

    resolved = {}
    stats = {"module": 0, "type": 0, "none": 0, "multi": 0}

    for tva, (key, entries) in sorted(unnamed.items()):
        cands = []
        for off in find_refs(img, tva):
            rva = off_to_va(img, off)
            if rva is None:
                continue
            sec = img.section_of(rva)
            if sec not in (".data", ".rdata", "PyRuntim"):
                continue

            # PyModuleDef?
            nptr = img.u32(rva + MODDEF_NAME_DELTA)
            if nptr:
                nm = img.cstr(nptr)
                if nm and len(nm) >= 2:
                    doc = None
                    dptr = img.u32(rva + MODDEF_DOC_DELTA)
                    if dptr:
                        doc = img.cstr(dptr, 200)
                    cands.append(("module", nm, doc, rva))

            # PyTypeObject?
            tptr = img.u32(rva + TYPE_NAME_DELTA)
            if tptr:
                nm = img.cstr(tptr)
                if nm and len(nm) >= 2:
                    cands.append(("type", nm, None, rva))

        # prefer a unique answer; dedupe by (kind,name)
        uniq = {(k, n) for k, n, _d, _r in cands}
        if not uniq:
            stats["none"] += 1
            resolved[key] = {"name": None, "kind": None, "n_funcs": len(entries)}
            continue
        if len(uniq) > 1:
            stats["multi"] += 1
        kind, name, doc, rva = cands[0]
        stats[kind] += 1
        resolved[key] = {
            "name": name,
            "kind": kind,
            "doc": doc,
            "struct_va": rva,
            "table_va": tva,
            "n_funcs": len(entries),
            "ambiguous": sorted("%s:%s" % (k, n) for k, n in uniq) if len(uniq) > 1 else None,
        }

    print("resolved as module : %d" % stats["module"])
    print("resolved as type   : %d" % stats["type"])
    print("ambiguous          : %d" % stats["multi"])
    print("unresolved         : %d" % stats["none"])

    print("\n=== named tables ===")
    rows = [(v["name"], v["kind"], v["n_funcs"], k)
            for k, v in resolved.items() if v["name"]]
    for name, kind, n, _k in sorted(rows, key=lambda r: (-r[2], r[0])):
        print("  %-6s %-44s %4d funcs" % (kind, name, n))

    if out_path:
        merged = {}
        for k, entries in tables.items():
            info = resolved.get(k, {})
            label = info.get("name") or k
            merged.setdefault(label, {"kind": info.get("kind"),
                                      "doc": info.get("doc"),
                                      "funcs": []})
            merged[label]["funcs"].extend(entries)
        json.dump(merged, open(out_path, "w"), indent=1, sort_keys=True)
        print("\nwrote %s (%d groups)" % (out_path, len(merged)))


if __name__ == "__main__":
    main()
