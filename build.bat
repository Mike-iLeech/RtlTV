@echo off
rem Сборка dist\SDR4NOOB\SDR4NOOB.exe в чистом окружении (в общем Python много лишнего — EXE раздувается).
rem Один раз:  py -3.12 -m venv .venv-build
rem            .venv-build\Scripts\pip install -r requirements-dev.txt
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
  --hidden-import PySide6.QtWebEngineWidgets ^
  --exclude-module tkinter ^
  run.py
rem QtWebEngine (схема в справке): из языков Chromium оставить ru и en-US
".venv-build\Scripts\python.exe" tools\prune_webengine.py dist\SDR4NOOB
copy /y LICENSE dist\SDR4NOOB\ >nul
copy /y THIRD_PARTY.md dist\SDR4NOOB\ >nul
copy /y README.md dist\SDR4NOOB\ >nul
copy /y CHANGELOG.md dist\SDR4NOOB\ >nul
rem установщик (нужен Inno Setup 6): iscc /DAppVersion=<версия> installer\sdr4noob.iss → release\SDR4NOOB-<версия>-setup.exe
