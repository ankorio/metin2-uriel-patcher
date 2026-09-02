@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools\VC\Auxiliary\Build\vcvars32.bat" >nul
cd /d "%~dp0"
cl /nologo /LD /O2 /GS- uriel_stub.cpp /link /DEF:uriel_stub.def /OUT:uriel_stub.dll /SUBSYSTEM:WINDOWS
echo EXITCODE=%ERRORLEVEL%
dir uriel_stub.dll
