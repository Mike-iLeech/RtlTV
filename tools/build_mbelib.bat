@echo off
rem Сборка rtltv\driver\mbelib.dll (64 бит, MSVC) из исходников https://github.com/szechyjs/mbelib
rem Использование: tools\build_mbelib.bat [папка_с_исходниками_mbelib]
rem Без аргумента исходники клонируются в %TEMP%\mbelib-src.
rem Компилируются только *.c через cl.exe; CMake и скрипты репозитория не используются.
rem
rem Windows SDK (UCRT-заголовки и ucrt.lib) не требуется: mbelib нужно лишь несколько функций
rem libc, поэтому заголовки stdlib.h/stdio.h/math.h подменяются минимальными (строки "::@" в конце
rem файла), а cosf/exp/log/powf/sqrtf/rand/memcpy/memset берутся прямо из системного ucrtbase.dll
rem (есть в любой Windows 10/11) через import-библиотеку, собранную lib.exe из .def.
rem printf/fprintf (отладочная печать mbelib) превращены в пустышки.
setlocal
set "ROOT=%~dp0.."
set "SRC=%~1"
if "%SRC%"=="" set "SRC=%TEMP%\mbelib-src"
if not exist "%SRC%\mbelib.h" git clone -q https://github.com/szechyjs/mbelib "%SRC%"
if not exist "%SRC%\mbelib.h" (echo Net iskhodnikov mbelib v "%SRC%" & exit /b 1)
where cl >nul 2>nul || call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>nul
where cl >nul 2>nul || (echo Ne naiden cl.exe [MSVC x64] & exit /b 1)
for /f "delims=" %%p in ('where cl') do set "CLDIR=%%~dpp"
set "VCDIR=%CLDIR%..\..\.."
set "OUT=%TEMP%\mbelib-build"
set "SHIM=%OUT%\shim"
if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%SHIM%"
for /f "tokens=1,2,* delims=@" %%a in ('findstr /l /b /c:"::@" "%~f0"') do >>"%SHIM%\%%b" echo(%%c
set "INCLUDE=%SHIM%;%VCDIR%\include"
set "LIB=%VCDIR%\lib\x64"
lib /nologo /machine:x64 /def:"%SHIM%\ucrtbase.def" /out:"%OUT%\ucrtbase.lib" >nul
if errorlevel 1 (echo Oshibka lib.exe & exit /b 1)
cl /nologo /O2 /GS- /Zl /W1 /D_USE_MATH_DEFINES /I"%SRC%" ^
  "%SHIM%\mbelib_shim.c" "%SRC%\ecc.c" "%SRC%\mbelib.c" "%SRC%\ambe3600x2400.c" "%SRC%\ambe3600x2450.c" ^
  "%SRC%\imbe7100x4400.c" "%SRC%\imbe7200x4400.c" ^
  /Fo"%OUT%\\" /Fe"%OUT%\mbelib.dll" ^
  /link /DLL /NOENTRY /NODEFAULTLIB /DEF:"%SHIM%\mbelib.def" "%OUT%\ucrtbase.lib" "%VCDIR%\lib\x64\chkstk.obj"
if errorlevel 1 (echo Oshibka kompilyatsii & exit /b 1)
copy /y "%OUT%\mbelib.dll" "%ROOT%\rtltv\driver\mbelib.dll" >nul
copy /y "%SRC%\COPYRIGHT" "%ROOT%\rtltv\driver\mbelib-LICENSE.txt" >nul
echo OK: %ROOT%\rtltv\driver\mbelib.dll
endlocal
exit /b 0

rem ---- Встроенные файлы: ::@имя@строка ----
::@stdlib.h@#pragma once
::@stdlib.h@#ifndef NULL
::@stdlib.h@#define NULL ((void *)0)
::@stdlib.h@#endif
::@stdlib.h@#define RAND_MAX 0x7fff
::@stdlib.h@int __cdecl rand(void);
::@stdlib.h@long __cdecl strtol(const char *s, char **e, int b);
::@stdio.h@#pragma once
::@stdio.h@#define stderr ((void *)0)
::@stdio.h@#define stdout ((void *)0)
::@stdio.h@int mbe_shim_printf(const char *f, ...);
::@stdio.h@int mbe_shim_fprintf(void *s, const char *f, ...);
::@stdio.h@int mbe_shim_sprintf(char *d, const char *f, ...);
::@stdio.h@#define printf mbe_shim_printf
::@stdio.h@#define fprintf mbe_shim_fprintf
::@stdio.h@#define sprintf mbe_shim_sprintf
::@math.h@#pragma once
::@math.h@#define M_PI 3.14159265358979323846
::@math.h@#define M_E 2.71828182845904523536
::@math.h@#define M_SQRT2 1.41421356237309504880
::@math.h@float __cdecl cosf(float x);
::@math.h@float __cdecl powf(float x, float y);
::@math.h@float __cdecl sqrtf(float x);
::@math.h@double __cdecl exp(double x);
::@math.h@double __cdecl log(double x);
::@math.h@static __inline float fabsf(float x) { return x < 0.0f ? -x : x; }
::@mbelib_shim.c@#include <stdarg.h>
::@mbelib_shim.c@int _fltused = 0;
::@mbelib_shim.c@int mbe_shim_printf(const char *f, ...) { (void)f; return 0; }
::@mbelib_shim.c@int mbe_shim_fprintf(void *s, const char *f, ...) { (void)s; (void)f; return 0; }
::@mbelib_shim.c@int mbe_shim_sprintf(char *d, const char *f, ...) { va_list ap; const char *s; int n = 0; va_start(ap, f); if (f[0] == '%' && f[1] == 's' && f[2] == 0) { s = va_arg(ap, const char *); while (s[n]) { d[n] = s[n]; n++; } } d[n] = 0; va_end(ap); return n; }
::@ucrtbase.def@LIBRARY ucrtbase.dll
::@ucrtbase.def@EXPORTS
::@ucrtbase.def@  cosf
::@ucrtbase.def@  powf
::@ucrtbase.def@  sqrtf
::@ucrtbase.def@  exp
::@ucrtbase.def@  log
::@ucrtbase.def@  rand
::@ucrtbase.def@  strtol
::@ucrtbase.def@  memcpy
::@ucrtbase.def@  memset
::@mbelib.def@LIBRARY mbelib
::@mbelib.def@EXPORTS
::@mbelib.def@  mbe_checkGolayBlock
::@mbelib.def@  mbe_golay2312
::@mbelib.def@  mbe_hamming1511
::@mbelib.def@  mbe_7100x4400hamming1511
::@mbelib.def@  mbe_eccAmbe3600x2400C0
::@mbelib.def@  mbe_eccAmbe3600x2400Data
::@mbelib.def@  mbe_decodeAmbe2400Parms
::@mbelib.def@  mbe_demodulateAmbe3600x2400Data
::@mbelib.def@  mbe_processAmbe2400Dataf
::@mbelib.def@  mbe_processAmbe2400Data
::@mbelib.def@  mbe_processAmbe3600x2400Framef
::@mbelib.def@  mbe_processAmbe3600x2400Frame
::@mbelib.def@  mbe_eccAmbe3600x2450C0
::@mbelib.def@  mbe_eccAmbe3600x2450Data
::@mbelib.def@  mbe_decodeAmbe2450Parms
::@mbelib.def@  mbe_demodulateAmbe3600x2450Data
::@mbelib.def@  mbe_processAmbe2450Dataf
::@mbelib.def@  mbe_processAmbe2450Data
::@mbelib.def@  mbe_processAmbe3600x2450Framef
::@mbelib.def@  mbe_processAmbe3600x2450Frame
::@mbelib.def@  mbe_eccImbe7200x4400C0
::@mbelib.def@  mbe_eccImbe7200x4400Data
::@mbelib.def@  mbe_decodeImbe4400Parms
::@mbelib.def@  mbe_demodulateImbe7200x4400Data
::@mbelib.def@  mbe_processImbe4400Dataf
::@mbelib.def@  mbe_processImbe4400Data
::@mbelib.def@  mbe_processImbe7200x4400Framef
::@mbelib.def@  mbe_processImbe7200x4400Frame
::@mbelib.def@  mbe_eccImbe7100x4400C0
::@mbelib.def@  mbe_eccImbe7100x4400Data
::@mbelib.def@  mbe_demodulateImbe7100x4400Data
::@mbelib.def@  mbe_convertImbe7100to7200
::@mbelib.def@  mbe_processImbe7100x4400Framef
::@mbelib.def@  mbe_processImbe7100x4400Frame
::@mbelib.def@  mbe_printVersion
::@mbelib.def@  mbe_moveMbeParms
::@mbelib.def@  mbe_useLastMbeParms
::@mbelib.def@  mbe_initMbeParms
::@mbelib.def@  mbe_spectralAmpEnhance
::@mbelib.def@  mbe_synthesizeSilencef
::@mbelib.def@  mbe_synthesizeSilence
::@mbelib.def@  mbe_synthesizeSpeechf
::@mbelib.def@  mbe_synthesizeSpeech
::@mbelib.def@  mbe_floattoshort
