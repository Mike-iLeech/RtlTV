@echo off
rem Сборка dist\RtlTV\RtlTV.exe в чистом окружении (в общем Python много лишнего — EXE раздувается).
rem Один раз:  py -3.12 -m venv .venv-build
rem            .venv-build\Scripts\pip install numpy scipy PySide6-Essentials sounddevice sgp4 pyinstaller
rem Режимы подключаются через importlib — поэтому --collect-submodules rtltv.
rem Все DLL из rtltv\driver (librtlsdr, mbelib, lrpt_dsp) кладутся рядом с пакетом.
cd /d "%~dp0"
".venv-build\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name RtlTV ^
  --collect-submodules rtltv ^
  --add-data "rtltv\driver;rtltv\driver" ^
  --add-data "rtltv\guide;rtltv\guide" ^
  --hidden-import PySide6.QtSvg ^
  --exclude-module tkinter ^
  run.py
copy /y LICENSE dist\RtlTV\ >nul
copy /y THIRD_PARTY.md dist\RtlTV\ >nul
copy /y README.md dist\RtlTV\ >nul
copy /y CHANGELOG.md dist\RtlTV\ >nul
