# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('src/triarch_patcher/resources/uriel_stub.dll', 'triarch_patcher/resources'), ('src/triarch_patcher/resources/uriel_stub.dll.sha256', 'triarch_patcher/resources'), ('src/triarch_patcher/resources/natives.json', 'triarch_patcher/resources'), ('src/triarch_patcher/vendor/imports_db.json', 'triarch_patcher/vendor'), ('src/triarch_patcher/resources/mods', 'triarch_patcher/resources/mods')]
binaries = []
hiddenimports = ['triarch_patcher.vendor.namemods', 'triarch_patcher.vendor.unuriel', 'triarch_patcher.vendor.mkoffsets', 'triarch_patcher.ui', 'triarch_patcher.pipeline', 'triarch_patcher.winproc']
tmp_ret = collect_all('capstone')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['entry.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TriarchPatcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
