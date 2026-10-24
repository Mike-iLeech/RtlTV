@echo off
rem Сборка dist\SDR4NOOB\SDR4NOOB.exe в чистом окружении (в общем Python много лишнего — EXE раздувается).
rem Один раз:  py -3.12 -m venv .venv-build
rem            .venv-build\Scripts\pip install numpy scipy PySide6-Essentials sounddevice sgp4 pyinstaller
rem Режимы подключаются через importlib — поэтому --collect-submodules rtltv.
rem Все DLL из rtltv\driver (librtlsdr, mbelib, lrpt_dsp) кладутся рядом с пакетом.
rem Иконка EXE — rtltv\assets\sdr4noob.ico (пересобрать из логотипа: py -3.12 tools\make_icon.py).
cd /d "%~dp0"
".venv-build\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name SDR4NOOB ^
  --collect-submodules rtltv ^
  --add-data "rtltv\driver;rtltv\driver" ^
  --add-data "rtltv\guide;rtltv\guide" ^
  --add-data "rtltv\assets;rtltv\assets" ^
  --icon "rtltv\assets\sdr4noob.ico" ^
  --hidden-import PySide6.QtSvg ^
  --exclude-module tkinter ^
  run.py
rem копия под прежним именем: обновлятор версий 2.x ищет RtlTV.exe и запускает его после обновления
copy /y dist\SDR4NOOB\SDR4NOOB.exe dist\SDR4NOOB\RtlTV.exe >nul
copy /y LICENSE dist\SDR4NOOB\ >nul
copy /y THIRD_PARTY.md dist\SDR4NOOB\ >nul
copy /y README.md dist\SDR4NOOB\ >nul
copy /y CHANGELOG.md dist\SDR4NOOB\ >nul
