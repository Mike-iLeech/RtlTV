@echo off
rem Сборка rtltv\driver\lrpt_dsp.dll (MSVC x64): Витерби, Рид — Соломон, демодулятор QPSK/OQPSK.
rem DLL без CRT и Windows SDK: /NODEFAULTLIB /NOENTRY, /GS- (нет проверок стека из CRT).
setlocal
set VC="C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
call %VC% >nul 2>nul
where cl >nul 2>nul || (echo Не найден cl.exe & exit /b 1)
set ROOT=%~dp0..
set OBJ=%TEMP%\rtltv_lrpt_build
if not exist "%OBJ%" mkdir "%OBJ%"
cl /nologo /O2 /Oi /fp:fast /GS- /LD /TC "%ROOT%\tools\lrpt_dsp.c" /Fo"%OBJ%\\" /Fe"%ROOT%\rtltv\driver\lrpt_dsp.dll" /link /NOLOGO /NODEFAULTLIB /NOENTRY /IMPLIB:"%OBJ%\lrpt_dsp.lib" || exit /b 1
echo OK: %ROOT%\rtltv\driver\lrpt_dsp.dll
