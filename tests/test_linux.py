"""Платформенный слой и Linux-части: xdg-user-dirs, имена библиотек, sysfs-перечисление USB, подсказки по доступу,
правила udev. Чистая логика проверяется на любой ОС; загрузка настоящих .so — только в Linux (CI собирает их
tools/build_linux_libs.sh перед тестами)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtltv import devices, linux_setup, osplat  # noqa: E402

LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------- osplat
def test_xdg_user_dir_parsing(tmp_path):
    home = str(tmp_path / "home")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "user-dirs.dirs").write_text(
        '# комментарий\n'
        'XDG_DESKTOP_DIR="$HOME/Рабочий стол"\n'
        'XDG_DOCUMENTS_DIR="$HOME/Документы"\n'
        'XDG_MUSIC_DIR="/mnt/data/Музыка"\n'
        'XDG_PICTURES_DIR="$HOME/"\n'
        'XDG_VIDEOS_DIR="relative/путь"\n', encoding="utf-8")
    assert osplat.xdg_user_dir("DOCUMENTS", str(cfg), home) == os.path.normpath(os.path.join(home, "Документы"))
    assert osplat.xdg_user_dir("MUSIC", str(cfg), home) == os.path.normpath("/mnt/data/Музыка")
    assert osplat.xdg_user_dir("PICTURES", str(cfg), home) is None      # = домашняя папка: «не задано»
    assert osplat.xdg_user_dir("VIDEOS", str(cfg), home) is None        # не абсолютный путь
    assert osplat.xdg_user_dir("DOWNLOAD", str(cfg), home) is None      # нет строки
    assert osplat.xdg_user_dir("DOCUMENTS", str(tmp_path / "нет"), home) is None


def test_lib_file_names():
    if osplat.IS_WIN:
        assert osplat.lib_file("librtlsdr") == "librtlsdr.dll" and osplat.lib_file("mbelib") == "mbelib.dll"
    else:
        ext = ".dylib" if osplat.IS_MAC else ".so"
        assert osplat.lib_file("librtlsdr") == "librtlsdr" + ext
        assert osplat.lib_file("mbelib") == "libmbelib" + ext and osplat.lib_file("lrpt_dsp") == "liblrpt_dsp" + ext
    assert os.path.dirname(osplat.bundled_lib("mbelib")) == osplat.driver_dir()


@pytest.mark.skipif(not LINUX, reason="Linux")
def test_linux_lib_names_and_dirs(monkeypatch, tmp_path):
    assert osplat.is_lib_name("libhackrf.so") and osplat.is_lib_name("libhackrf.so.0.8.0")
    assert not osplat.is_lib_name("hackrf.dll") and not osplat.is_lib_name("readme.txt")
    assert "*.so" in osplat.lib_filter()
    monkeypatch.delenv("RTLTV_CONFIG_DIR", raising=False)
    monkeypatch.delenv("RTLTV_LOCAL_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    assert osplat.config_dir() == str(tmp_path / "c" / "SDR4NOOB")
    assert osplat.local_dir() == str(tmp_path / "d" / "SDR4NOOB")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")             # по спецификации — игнорировать
    assert osplat.config_dir() == os.path.join(osplat.HOME, ".config", "SDR4NOOB")
    monkeypatch.setenv("RTLTV_LOCAL_DIR", str(tmp_path / "x"))
    assert osplat.local_dir() == str(tmp_path / "x")


def test_env_overrides_win_too(monkeypatch, tmp_path):
    monkeypatch.setenv("RTLTV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("RTLTV_LOCAL_DIR", str(tmp_path / "loc"))
    assert osplat.config_dir() == str(tmp_path / "cfg") and osplat.local_dir() == str(tmp_path / "loc")


def test_bundled_portaudio_hook(monkeypatch, tmp_path):
    import ctypes.util
    orig = ctypes.util.find_library
    monkeypatch.setattr(osplat, "IS_LINUX", True)
    monkeypatch.setattr(osplat, "driver_dir", lambda: str(tmp_path))
    try:
        assert osplat.use_bundled_portaudio() is None                 # встроенной нет — ничего не трогаем
        (tmp_path / "libportaudio.so.2").write_bytes(b"\x7fELF")
        monkeypatch.setattr(ctypes.util, "find_library", lambda n: "libportaudio.so.2" if n == "portaudio" else None)
        assert osplat.use_bundled_portaudio() is None                 # системная есть — она главнее
        monkeypatch.setattr(ctypes.util, "find_library", lambda n: None)
        p = osplat.use_bundled_portaudio()
        assert p == str(tmp_path / "libportaudio.so.2") and ctypes.util.find_library("portaudio") == p
        assert ctypes.util.find_library("usb-1.0") is None
    finally:
        ctypes.util.find_library = orig


# ---------------------------------------------------------------- USB в Linux
def _sysfs(tmp_path, devs):
    """Ненастоящий /sys/bus/usb/devices: devs = [(имя, vid, pid, bus, dev, {интерфейс: драйвер})]."""
    root = tmp_path / "sys"
    drv = tmp_path / "drivers"
    root.mkdir()
    for name, vid, pid, bus, dev, ifaces in devs:
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text(f"{vid:04x}\n")
        (d / "idProduct").write_text(f"{pid:04x}\n")
        (d / "busnum").write_text(f"{bus}\n")
        (d / "devnum").write_text(f"{dev}\n")
        (d / "product").write_text("RTL2838UHIDIR\n")
        for num, module in ifaces.items():
            i = root / f"{name}:1.{num}"
            i.mkdir()
            (i / "bInterfaceNumber").write_text(f"{num:02x}\n")
            if module:
                (drv / module).mkdir(parents=True, exist_ok=True)
                try:
                    os.symlink(drv / module, i / "driver", target_is_directory=True)
                except OSError:
                    pytest.skip("нельзя создать символическую ссылку (Windows без прав)")
    return str(root)


@pytest.mark.skipif(os.name == "nt", reason="двоеточие в имени папки sysfs")
def test_usb_raw_linux_sysfs(tmp_path):
    root = _sysfs(tmp_path, [("1-2", 0x0BDA, 0x2838, 1, 5, {0: "dvb_usb_rtl28xxu", 1: ""}),
                             ("1-3", 0x046D, 0xC52B, 1, 6, {0: "usbhid"})])
    devroot = tmp_path / "dev"
    (devroot / "001").mkdir(parents=True)
    (devroot / "001" / "005").write_bytes(b"")
    raw = {r["name"]: r for r in devices.usb_raw_linux(root, str(devroot))}
    assert set(raw) == {"1-2", "1-3"}
    r = raw["1-2"]
    assert (r["vid"], r["pid"]) == (0x0BDA, 0x2838) and r["drivers"] == {0: "dvb_usb_rtl28xxu", 1: ""}
    assert r["node"] == os.path.join(str(devroot), "001", "005") and r["access"]
    assert not raw["1-3"]["access"]          # узла /dev/bus/usb/001/006 нет
    found = devices.usb_devices(list(raw.values()))
    assert [d["kind"] for d in found] == ["rtl"]                 # мышь отброшена


def test_linux_driver_states():
    def one(vid, pid, access, drv0):
        raw = [{"vid": vid, "pid": pid, "name": "1-1", "node": "/dev/bus/usb/001/002", "access": access,
                "product": "", "drivers": {0: drv0}}]
        return devices.usb_devices(raw)[0]
    d = one(0x0BDA, 0x2838, False, "dvb_usb_rtl28xxu")
    assert not d["ready"] and "Настроить доступ" in d["hint"] and "dvb_usb_rtl28xxu" in d["state"]
    d = one(0x0BDA, 0x2838, True, "dvb_usb_rtl28xxu")
    assert d["ready"] and "отцепит" in d["hint"] and "настроить доступ" in d["hint"].lower()
    d = one(0x0BDA, 0x2838, True, "")
    assert d["ready"] and d["hint"].startswith("Готово")
    d = one(0x0BDA, 0x2838, True, "usbfs")
    assert d["ready"] and "уже открыт" in d["hint"]
    d = one(0x1D50, 0x6089, False, "")
    assert d["kind"] == "hackrf" and not d["ready"] and "Настроить доступ" in d["hint"]
    d = one(0x0BDA, 0x2832, True, "")                             # второй PID RTL2832U
    assert d["kind"] == "rtl"
    d = one(0x0CCD, 0x00A9, True, "")                             # Terratec — из rtl-sdr.rules
    assert d["kind"] == "rtl" and d["model"].startswith("RTL-SDR")


def test_udev_rules_and_blacklist():
    rules = linux_setup.udev_rules_text()
    assert 'ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838"' in rules and 'TAG+="uaccess"' in rules
    assert 'ATTRS{idProduct}=="6089"' in rules                      # HackRF
    assert rules.count("SUBSYSTEM==") == len(linux_setup.RTL_IDS) + len(linux_setup.OTHER_IDS)
    bl = linux_setup.blacklist_text()
    assert "blacklist dvb_usb_rtl28xxu\n" in bl and "blacklist rtl2832_sdr\n" in bl
    sc = linux_setup.setup_script("/tmp/a b/r.rules", "/tmp/a b/b.conf")
    assert f"install -m 0644 '/tmp/a b/r.rules' {linux_setup.RULES_PATH}" in sc
    assert "udevadm control --reload-rules" in sc and "modprobe -r" in sc
    assert linux_setup.RULES_PATH in linux_setup.manual_commands()


def test_run_setup_without_pkexec(monkeypatch):
    monkeypatch.setattr(linux_setup.shutil, "which", lambda n: None)
    ok, msg = linux_setup.run_setup()
    assert not ok and "sudo tee " + linux_setup.RULES_PATH in msg


def test_run_setup_cancel_and_ok(monkeypatch):
    import subprocess
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, codes.pop(0), "", "")
    codes = [126, 0]
    monkeypatch.setattr(linux_setup.shutil, "which", lambda n: "/usr/bin/pkexec")
    monkeypatch.setattr(linux_setup.subprocess, "run", fake_run)
    ok, msg = linux_setup.run_setup()
    assert not ok and "отменена" in msg
    ok, msg = linux_setup.run_setup()
    assert ok and "Выдерните" in msg
    assert calls[0][:3] == ["/usr/bin/pkexec", "sh", "-c"] and "60-sdr4noob.rules" in calls[0][3]


# ---------------------------------------------------------------- настоящие .so (Linux CI)
@pytest.mark.skipif(not LINUX, reason="Linux")
def test_bundled_libs_load_linux():
    import ctypes
    for stem in ("librtlsdr", "mbelib", "lrpt_dsp"):
        p = osplat.bundled_lib(stem)
        if not os.path.isfile(p):
            pytest.skip(f"нет {p} — tools/build_linux_libs.sh")
        ctypes.CDLL(p)
    info = devices.native_lib_info(osplat.bundled_lib("librtlsdr"))
    assert info["dll"] and info["x64"] and "rtlsdr_open" in info["exports"]
    kind, _info = devices.check_dll(osplat.bundled_lib("librtlsdr"))
    assert kind == "rtl"


@pytest.mark.skipif(not LINUX, reason="Linux")
def test_check_dll_rejects_linux(tmp_path):
    p = tmp_path / "fake.so"
    p.write_text("not a library")
    with pytest.raises(devices.DriverError, match="ELF"):
        devices.check_dll(str(p))
    with pytest.raises(devices.DriverError, match="нужен файл .so"):
        devices.check_dll(str(tmp_path / "x.txt") if (tmp_path / "x.txt").write_text("x") else "")
    import ctypes.util
    libm = ctypes.util.find_library("m")
    real = next((os.path.join(d, libm) for d in ("/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu", "/usr/lib64",
                                                  "/lib64", "/usr/lib") if libm and os.path.isfile(os.path.join(d, libm))), None)
    if real:
        with pytest.raises(devices.DriverError, match="не похожа на драйвер SDR"):
            devices.check_dll(real)


# ---------------------------------------------------------------- обновление AppImage
def test_updater_linux_kind_and_replace(tmp_path, monkeypatch):
    import hashlib
    import io
    from rtltv import updater_linux as U
    img = tmp_path / "apps" / "SDR4NOOB.AppImage"
    img.parent.mkdir()
    img.write_bytes(b"old")
    info = {"asset": {"name": "SDR4NOOB-9.9.9-x86_64.AppImage", "url": "https://example/x", "size": 3}}
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert U.update_kind(info) == "manual" and ".deb" in U.manual_hint()
    monkeypatch.setenv("APPIMAGE", str(img))
    monkeypatch.setattr(U.sys, "frozen", True, raising=False)
    assert U.appimage_path() == str(img) and U.update_kind(info) == "appimage"
    assert U.update_kind({"asset": None}) == "manual"
    assert U.asset_suffix().endswith(".appimage") and U.arch() in U.asset_suffix()

    body = b"new-appimage"

    class R(io.BytesIO):
        headers = {"Content-Length": str(len(body))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    a = dict(info["asset"], sha256=hashlib.sha256(body).hexdigest())
    dl = tmp_path / "dl" / "new.AppImage"
    dl.parent.mkdir()
    U.download_to(a, str(dl), "ua", opener=lambda req, timeout: R(body))
    assert dl.read_bytes() == body and not (tmp_path / "dl" / "new.AppImage.part").exists()
    bad = dict(a, sha256="0" * 64)
    with pytest.raises(RuntimeError, match="Контрольная сумма"):
        U.download_to(bad, str(tmp_path / "dl" / "bad"), "ua", opener=lambda req, timeout: R(body))
    assert not list((tmp_path / "dl").glob("bad*"))
    with pytest.raises(RuntimeError, match="контрольную сумму"):
        U.download_to(dict(a, sha256=None), str(tmp_path / "dl" / "x"), "ua", opener=lambda req, timeout: R(body))
    U.replace_appimage(str(dl), str(img))
    assert img.read_bytes() == body and not dl.exists() and not (img.parent / "SDR4NOOB.AppImage.new").exists()


def test_udev_rules_syntax_each_line():
    """udev понимает «#» только в начале строки — комментарий в конце правила ломает всё правило."""
    for line in linux_setup.udev_rules_text().splitlines():
        line = line.strip()
        assert not line or line.startswith("#") or ("#" not in line and line.startswith("SUBSYSTEM==")), line


def test_run_setup_127_is_not_cancel(monkeypatch):
    import subprocess
    monkeypatch.setattr(linux_setup.shutil, "which", lambda n: "/usr/bin/pkexec")
    monkeypatch.setattr(linux_setup.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 127, "", ""))
    ok, msg = linux_setup.run_setup()
    assert not ok and "polkit" in msg and "sudo tee" in msg
    sc = linux_setup.setup_script("/tmp/it's/r.rules", "/tmp/b.conf")
    assert "'/tmp/it'\"'\"'s/r.rules'" in sc        # shlex.quote


def test_preload_only_from_drivers_dir(monkeypatch, tmp_path):
    loaded = []
    monkeypatch.setattr(devices.osplat, "IS_WIN", False)
    monkeypatch.setattr(devices.C, "CDLL", lambda p, mode=0: loaded.append(os.path.basename(p)))
    if not hasattr(devices.C, "RTLD_GLOBAL"):
        monkeypatch.setattr(devices.C, "RTLD_GLOBAL", 0x100, raising=False)
    monkeypatch.setattr(devices.osplat, "is_lib_name", lambda n: n.endswith(".so") or ".so." in n)
    sysdir, drv = tmp_path / "usr_lib", tmp_path / "drivers"
    for d in (sysdir, drv):
        d.mkdir()
        (d / "libusb-1.0.so.0").write_bytes(b"x")
        (d / "libfftw3_mpi.so.3").write_bytes(b"x")
    monkeypatch.setattr(devices, "DRIVERS_DIR", str(drv))
    devices._dll_dir_handles.clear()
    devices.ensure_dll_dir(str(sysdir))
    assert loaded == []                                       # системный каталог не трогаем
    devices.ensure_dll_dir(str(drv))
    assert sorted(loaded) == ["libfftw3_mpi.so.3", "libusb-1.0.so.0"]
    devices._dll_dir_handles.clear()


def test_child_env_strips_bundle_libs(monkeypatch):
    monkeypatch.setattr(osplat, "IS_LINUX", True)
    monkeypatch.setattr(osplat.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/sdr4noob/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    monkeypatch.setenv("QT_PLUGIN_PATH", "/opt/sdr4noob/_internal/PySide6/Qt/plugins")
    env = osplat.child_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lib" and "QT_PLUGIN_PATH" not in env
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in osplat.child_env()


def test_updater_picks_appimage_on_linux(monkeypatch):
    import json
    from rtltv import updater, updater_linux
    rel = {"tag_name": "v9.9.9", "body": "", "html_url": "u", "assets": [
        {"name": "SDR4NOOB-9.9.9-win64.zip", "browser_download_url": "https://x/w.zip", "size": 1,
         "digest": "sha256:" + "a" * 64},
        {"name": f"SDR4NOOB-9.9.9-{updater_linux.arch()}.AppImage", "browser_download_url": "https://x/a", "size": 2,
         "digest": "sha256:" + "b" * 64},
        {"name": "sdr4noob_9.9.9_amd64.deb", "browser_download_url": "https://x/d", "size": 3}]}
    monkeypatch.setattr(updater, "_get", lambda url, timeout=15: json.dumps(rel).encode())
    info = updater.check_latest()
    assert info["appimage"]["url"] == "https://x/a" and info["appimage"]["sha256"] == "b" * 64
    assert info["asset"]["url"] == "https://x/w.zip"           # Windows-ветка не изменилась
    monkeypatch.setattr(updater.osplat, "IS_LINUX", True)
    monkeypatch.setattr(updater_linux, "appimage_path", lambda: None)
    assert updater.update_kind(info) == "manual"               # .deb / исходники — только ссылка
    monkeypatch.setattr(updater_linux, "appimage_path", lambda: "/home/u/SDR4NOOB.AppImage")
    monkeypatch.setattr(updater_linux, "can_replace", lambda p: True)
    assert updater.update_kind(info) == "appimage"
    assert updater.update_kind(dict(info, appimage=None)) == "manual"   # Windows-zip AppImage не заменит
