"""Offline tests for tools/unuriel.py and tools/ksattack.py.

They run against synthetic executables written by tools/make_fixture.py, so
no copy of the game is needed and nothing here touches a live process. See
tests/README.md for what the fixture does and does not reproduce.

    python3 -m pytest tests/
"""
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
sys.path.insert(0, TOOLS)

import make_fixture  # noqa: E402
import unuriel  # noqa: E402

SEED = 1
PAGES = 600


# ---------------------------------------------------------------- fixtures
class Fixture:
    def __init__(self, path):
        self.path = path
        self.raw = open(path, "rb").read()
        self.truth = json.load(open(path + ".truth.json"))
        self.pe = unuriel.PE(self.raw)
        self.key = base64.b64decode(self.truth["key_b64"])

    @property
    def text(self):
        t = self.pe.sec(".text")
        return self.raw[t["raw"]:t["raw"] + t["rsize"]]

    def plaintext(self):
        return bytes(b ^ self.key[i % unuriel.PAGE] for i, b in enumerate(self.text))

    def expected_slots(self):
        """slot_rva -> (dll, name_as_derive_reports_it, ordinal_or_None)."""
        out = {}
        for g in self.truth["groups"]:
            for s in g["slots"]:
                if "ordinal" in s:
                    out[int(s["rva"], 16)] = (g["dll"], s["ordinal_name"] or s["name"], s["ordinal"])
                else:
                    out[int(s["rva"], 16)] = (g["dll"], s["name"], None)
        return out


def _build(tmp, name, **kw):
    path = str(tmp / name)
    make_fixture.build(path, kw.pop("pages", PAGES), kw.pop("seed", SEED),
                       kw.pop("plain", False), kw.pop("wrong_name_key", False))
    return Fixture(path)


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("fx"), "protected.exe")


@pytest.fixture(scope="module")
def fx_plain(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("fxp"), "plain.exe", plain=True)


@pytest.fixture(scope="module")
def fx_wrong(tmp_path_factory):
    return _build(tmp_path_factory.mktemp("fxw"), "wrongkey.exe", wrong_name_key=True)


@pytest.fixture(scope="module")
def derived(fx, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("prof") / "profile.json")
    unuriel.derive(types.SimpleNamespace(exe=fx.path, out=out, db=None))
    return out, json.load(open(out))


def _rebuild(fx, profile, out, stub_dll):
    unuriel.rebuild(types.SimpleNamespace(exe=fx.path, profile=profile, out=out,
                                          force=False, stub_dll=stub_dll))
    return unuriel.PE(open(out, "rb").read())


def parse_imports(pe):
    """Read the import directory of a (rebuilt) PE ourselves: list of
    (dll, first_thunk_rva, [name or '#n', ...]) in descriptor order."""
    rva, size = pe.ddir(1)
    off = pe.rva2off(rva)
    out = []
    while True:
        oft, _ts, _fc, name_rva, ft = struct.unpack_from("<IIIII", pe.d, off)
        if name_rva == 0:
            break
        dll = _cstr(pe, name_rva)
        thunks = []
        toff = pe.rva2off(oft or ft)
        while True:
            v = struct.unpack_from("<I", pe.d, toff)[0]
            if v == 0:
                break
            if v & 0x80000000:
                thunks.append("#%d" % (v & 0xFFFF))
            else:
                thunks.append(_cstr(pe, v + 2))       # skip the hint word
            toff += 4
        out.append((dll, ft, thunks))
        off += 20
    return out


def _cstr(pe, rva):
    o = pe.rva2off(rva)
    e = pe.d.index(b"\0", o)
    return bytes(pe.d[o:e]).decode("latin1")


# ---------------------------------------------------------------- the fixture itself
def test_fixture_is_uriel_shaped(fx):
    """The synthetic file has the on-disk layout docs/01 describes."""
    pe, t = fx.pe, fx.truth
    text = pe.sec(".text")
    assert not (text["chars"] & unuriel.IMAGE_SCN_MEM_EXECUTE)
    assert text["chars"] & unuriel.IMAGE_SCN_MEM_READ and text["chars"] & 0x20
    inj = pe.secs[-1]
    assert inj["name"] == t["injected_section"] and len(inj["name"]) == 3
    assert struct.unpack_from("<I", pe.d, pe.opt + 16)[0] == inj["rva"]     # EP in the sled
    assert bytes(pe.d[inj["raw"]:inj["raw"] + 64]) == b"\x90" * 64
    imp_rva, _ = pe.ddir(1)
    assert inj["rva"] <= imp_rva < inj["rva"] + inj["vsize"]               # fake import dir
    assert parse_imports(pe) == [("client_x86.dll", int(t["fire_slot_rva"], 16), ["FireInTheHole"])]
    iat_rva, iat_size = pe.ddir(12)
    rd = pe.sec(".rdata")
    assert iat_rva == rd["rva"] and iat_rva == int(t["iat_rva"], 16)         # original IAT kept
    nslots = len(t["slots"]) + len(t["groups"])
    assert iat_size == 4 * nslots
    assert struct.unpack_from("<H", pe.d, pe.opt + 70)[0] & 0x40           # ASLR on
    assert hashlib.sha256(fx.raw).hexdigest() == t["file_sha256"]
    assert fx.pe.parse_relocs()                                              # .reloc is real
    assert hashlib.sha256(fx.plaintext()).hexdigest() == t["text_sha256"]


# ---------------------------------------------------------------- stage 1: keystream
def test_static_keystream_recovers_key_exactly(fx):
    key, weak = unuriel.static_keystream(fx.raw, fx.pe)
    assert weak == 0
    assert key == fx.key


def test_ksattack_scores_4096_of_4096_against_plain_twin(fx, fx_plain, tmp_path):
    assert fx_plain.truth["key_b64"] == fx.truth["key_b64"]
    assert fx_plain.text == fx.plaintext()
    r = subprocess.run([sys.executable, os.path.join(TOOLS, "ksattack.py"), fx.path, fx_plain.path],
                       cwd=str(tmp_path), capture_output=True, text=True, check=True)
    assert "assume plaintext 0x00        : 4096/4096" in r.stdout, r.stdout
    assert "keystream constant across 400/400" in r.stdout
    assert "weak mode (<5%): 0" in r.stdout


# ---------------------------------------------------------------- stage 2: import names
def test_decode_iat_groups_names_and_ordinals(fx):
    groups = unuriel.decode_iat(fx.raw, fx.pe, fx.key)
    truth_groups = fx.truth["groups"]
    assert len(groups) == len(truth_groups)
    for g, tg in zip(groups, truth_groups):
        assert [e["name"] for e in g] == [s["name"] for s in tg["slots"]]
        assert [e["slot_rva"] for e in g] == [int(s["rva"], 16) for s in tg["slots"]]
    names = [e["name"] for g in groups for e in g]
    assert "#6" in names and "#115" in names and "FireInTheHole" in names
    assert fx.truth["nul_name"] in names


def test_decode_iat_trusts_length_not_nul(fx):
    """One obfuscated name has a 0x00 in the middle: name[i] == key[i]."""
    t = fx.truth
    off = fx.pe.rva2off(int(t["nul_entry_rva"], 16))
    n = struct.unpack_from("<H", fx.raw, off)[0]
    assert n == len(t["nul_name"])
    cipher = fx.raw[off + 2:off + 2 + n]
    assert cipher[t["nul_index"]] == 0 and 0 < t["nul_index"] < n - 1
    truncated = cipher.split(b"\0")[0]
    assert len(truncated) < n                                # a NUL-stopper would truncate
    assert bytes(b ^ fx.key[i] for i, b in enumerate(cipher)).decode() == t["nul_name"]


# ---------------------------------------------------------------- stage 3: DLL attribution
def test_attribute_dlls_resolves_every_group(fx):
    db = json.load(open(unuriel.IMPORTS_DB))
    groups = unuriel.decode_iat(fx.raw, fx.pe, fx.key)
    log = []
    unknown = unuriel.attribute_dlls(groups, db, log=log.append)
    assert unknown == fx.truth["inherited_names"]
    expected = fx.expected_slots()
    for g in groups:
        for e in g:
            dll, name, ordinal = expected[e["slot_rva"]]
            assert e["dll"] == dll, (e, dll)
            if ordinal is not None:
                assert e["name"] == "#%d" % ordinal
                assert e.get("ordinal_name") == name
    assert not [e for g in groups for e in g if not e["dll"]]
    dlls = [g[0]["dll"] for g in groups]
    assert dlls == ["ADVAPI32.dll", "KERNEL32.dll", "OLEAUT32.dll", "USER32.dll",
                    "WS2_32.dll", "client_x86.dll"]
    # ordinal-only groups: WS2_32 via the ordinal table alone (ordinals only it
    # has), OLEAUT32 (#6 only) via the sorted-descriptor tie-break
    tie = [l for l in log if "ordinal-only" in l]
    assert len(tie) == 1 and "-> OLEAUT32.dll" in tie[0] and "UNRESOLVED" not in tie[0]
    assert "between KERNEL32.dll and USER32.dll" in tie[0]


def test_attribute_dlls_tie_break_depends_on_neighbours():
    """The same #6-only group placed after USER32 must become WS2_32."""
    db = json.load(open(unuriel.IMPORTS_DB))
    mk = lambda i, *names: [{"slot_rva": 0x1000 + 4 * i + 4 * k, "value": 1, "name": n}
                            for k, n in enumerate(names)]
    groups = [mk(0, "CloseHandle"), mk(10, "BeginPaint"), mk(20, "#6"), mk(30, "FireInTheHole")]
    unuriel.attribute_dlls(groups, db, log=lambda *_: None)
    assert [g[0]["dll"] for g in groups] == ["KERNEL32.dll", "USER32.dll", "WS2_32.dll",
                                             "client_x86.dll"]
    assert groups[2][0]["ordinal_name"] == "getsockname"


# ---------------------------------------------------------------- stage 4: OEP
def test_find_oep_returns_planted_entry(fx):
    plain = fx.plaintext()
    oep, cands = unuriel.find_oep(fx.pe, plain)
    assert oep is not None
    assert oep - fx.pe.base == int(fx.truth["oep_rva"], 16)
    score, va, ct, jt = cands[0]
    assert score == 11                                       # cookie write + never-called jmp target
    assert ct - fx.pe.base == int(fx.truth["init_cookie_rva"], 16)
    assert jt - fx.pe.base == int(fx.truth["main_seh_rva"], 16)
    i = oep - fx.pe.base - fx.pe.sec(".text")["rva"]
    assert plain[i - 1] == 0xCC and plain[i] == 0xE8 and plain[i + 5] == 0xE9


# ---------------------------------------------------------------- derive
def test_derive_profile_matches_truth(fx, derived):
    _, prof = derived
    t = fx.truth
    assert prof["method"] == "static"
    assert prof["source_sha256"] == t["file_sha256"]
    assert prof["image_base"] == t["image_base"]
    assert prof["key_b64"] == t["key_b64"]
    assert prof["key_weak_offsets"] == 0
    assert prof["oep_rva"] == t["oep_rva"]
    assert prof["iat_rva"] == t["iat_rva"] and prof["iat_size"] == t["iat_size"]
    assert prof["iat_inherited_names"] == t["inherited_names"]
    expected = fx.expected_slots()
    seps = 0
    for e in prof["iat"]:
        if e["value"] == 0:
            seps += 1
            assert e["slot_rva"] not in expected and e["dll"] is None
            continue
        dll, name, ordinal = expected[e["slot_rva"]]
        assert (e["dll"], e["name"]) == (dll, name)
        assert e.get("ordinal") == ordinal
    assert seps == len(t["groups"])
    assert len(prof["iat"]) == int(t["iat_size"], 16) // 4


def test_derive_rejects_wrong_name_key(fx_wrong):
    assert fx_wrong.truth["wrong_name_key"]
    out = fx_wrong.path + ".profile.json"
    with pytest.raises(SystemExit) as ei:
        unuriel.derive(types.SimpleNamespace(exe=fx_wrong.path, out=out, db=None))
    assert "decoded import names are garbage" in str(ei.value)
    assert not os.path.exists(out)


# ---------------------------------------------------------------- rebuild
def _check_clean(fx, clean):
    t = fx.truth
    text = clean.sec(".text")
    assert text["chars"] & unuriel.IMAGE_SCN_MEM_EXECUTE
    assert struct.unpack_from("<I", clean.d, clean.opt + 16)[0] == int(t["oep_rva"], 16)
    assert not struct.unpack_from("<H", clean.d, clean.opt + 70)[0] & 0x40
    assert clean.secs[-1]["name"] == ".unuriel" and clean.nsec == fx.pe.nsec + 1
    assert clean.ddir(12) == fx.pe.ddir(12)
    assert clean.secs[-1]["rva"] <= clean.ddir(1)[0] < clean.secs[-1]["rva"] + clean.secs[-1]["vsize"]
    assert clean.size_of_image >= clean.secs[-1]["rva"] + clean.secs[-1]["vsize"]
    return bytes(clean.d[text["raw"]:text["raw"] + text["rsize"]])


def _expected_descriptors(fx, protector_as):
    out = []
    for g in fx.truth["groups"]:
        dll = g["dll"]
        if dll == "client_x86.dll":
            if protector_as is None:
                continue
            dll = protector_as
        names = [s.get("ordinal_name") or s["name"] for s in g["slots"]]
        out.append((dll, int(g["slots"][0]["rva"], 16), names))
    return out


def test_rebuild_with_stub_dll(fx, derived, tmp_path):
    prof_path, _ = derived
    clean = _rebuild(fx, prof_path, str(tmp_path / "clean.exe"), "uriel_stub")
    text = _check_clean(fx, clean)
    assert hashlib.sha256(text).hexdigest() == fx.truth["text_sha256"]   # .text untouched
    assert parse_imports(clean) == _expected_descriptors(fx, "uriel_stub")


def test_rebuild_without_stub_redirects_protector_call(fx, derived, tmp_path):
    prof_path, _ = derived
    clean = _rebuild(fx, prof_path, str(tmp_path / "clean2.exe"), None)
    text = _check_clean(fx, clean)
    assert parse_imports(clean) == _expected_descriptors(fx, None)
    plain = fx.plaintext()
    i = int(fx.truth["fire_call_rva"], 16) - clean.sec(".text")["rva"]
    assert plain[i:i + 2] == b"\xFF\x15"                     # was call [FireInTheHole]
    assert text[i] == 0xE8 and text[i + 5] == 0x90            # now call stub; nop
    stub = clean.base + int(fx.truth["fire_call_rva"], 16) + 5 + struct.unpack_from("<i", text, i + 1)[0]
    new = clean.secs[-1]
    assert clean.base + new["rva"] <= stub < clean.base + new["rva"] + new["vsize"]
    assert text[:i] == plain[:i] and text[i + 6:] == plain[i + 6:]     # only that one site changed


def test_rebuild_refuses_profile_from_another_build(fx, fx_wrong, derived, tmp_path):
    prof_path, _ = derived
    with pytest.raises(SystemExit) as ei:
        unuriel.rebuild(types.SimpleNamespace(exe=fx_wrong.path, profile=prof_path,
                                              out=str(tmp_path / "x.exe"), force=False, stub_dll=None))
    assert "refusing" in str(ei.value)
