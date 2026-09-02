@echo off
REM  Build the stub DLL, then freeze the patcher into a single exe.
REM  Only needed when the stub source changes - the DLL is build-independent.
setlocal
cd /d "%~dp0"

REM  build_stub.bat does `cd /d %~dp0`, which moves THIS script's working
REM  directory too - wrap it so the copy afterwards resolves correctly.
if "%1"=="stub" (
    pushd "%~dp0"
    call ..\stub\build_stub.bat || goto :err
    popd
    cd /d "%~dp0"
)

REM  ALWAYS stage the inputs (tools/, mods/, the freshly built DLL and its
REM  sha256) into the package, not just after `stub`. This shipped a stale stub
REM  once: a build with no argument left the old DLL in resources, froze THAT
REM  into the exe, then recomputed the sha256 from it - so the integrity check
REM  happily confirmed the wrong binary. The hash only ever proves "the bytes
REM  survived PyInstaller", never "these are the bytes you built"; copying
REM  unconditionally is what closes that.
python sync.py || goto :err

REM  Fail loudly if the stub predates its own source. A patcher that deploys a
REM  stub older than uriel_stub.cpp is the single most confusing failure mode
REM  here: everything reports OK and the features are just missing.
python -c "import os,sys;d='src/triarch_patcher/resources/uriel_stub.dll';s='../stub/uriel_stub.cpp';sys.exit(0 if not os.path.exists(s) or os.path.getmtime(d)>=os.path.getmtime(s) else 1)" || (echo STALE STUB: uriel_stub.cpp is newer than the built DLL - run "build.bat stub" && goto :err)

REM  --noupx: UPX is on PATH here and PyInstaller will happily repack the stub,
REM  which changes the bytes we self-patch at load time.
REM  capstone is a NEW dependency: mkoffsets reads `ret` immediates to
REM  cross-check every native's declared stack width against the binary, and
REM  that needs real instruction boundaries rather than byte patterns. It ships
REM  a native library, so --collect-all is required; a bare hidden-import gets
REM  the Python package and leaves the DLL behind, which fails at run time.
python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx ^
  --name TriarchPatcher ^
  --paths src ^
  --add-data "src/triarch_patcher/resources/uriel_stub.dll;triarch_patcher/resources" ^
  --add-data "src/triarch_patcher/resources/uriel_stub.dll.sha256;triarch_patcher/resources" ^
  --add-data "src/triarch_patcher/resources/natives.json;triarch_patcher/resources" ^
  --add-data "src/triarch_patcher/vendor/imports_db.json;triarch_patcher/vendor" ^
  --add-data "src/triarch_patcher/resources/mods;triarch_patcher/resources/mods" ^
  --collect-all capstone ^
  --hidden-import triarch_patcher.vendor.namemods ^
  --hidden-import triarch_patcher.vendor.unuriel ^
  --hidden-import triarch_patcher.vendor.mkoffsets ^
  --hidden-import triarch_patcher.ui ^
  --hidden-import triarch_patcher.pipeline ^
  --hidden-import triarch_patcher.winproc ^
  entry.py || goto :err

echo.
echo BUILT: dist\TriarchPatcher.exe
goto :eof
:err
echo BUILD FAILED
exit /b 1
