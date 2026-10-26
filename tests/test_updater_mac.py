"""Автообновление сборки для macOS (rtltv/updater_mac.py): выбор архива по архитектуре, путь к SDR4NOOB.app,
распаковка ditto и скрипт замены пакета с откатом. Скрипт и ditto проверяются только на macOS (CI)."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rtltv import updater, updater_mac  # noqa: E402

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="ditto/bash-скрипт — только macOS")


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(updater.osplat, "IS_MAC", True)


def test_arch_and_suffix(monkeypatch):
    monkeypatch.setattr(updater_mac.platform, "machine", lambda: "arm64")
    assert updater_mac.asset_suffix() == "-macos-arm64.zip"
    monkeypatch.setattr(updater_mac.platform, "machine", lambda: "x86_64")
    assert updater_mac.asset_suffix() == "-macos-x86_64.zip"


def test_bundle_path():
    exe = os.path.join(os.sep, "Applications", "SDR4NOOB.app", "Contents", "MacOS", "SDR4NOOB")
    assert updater_mac.bundle_path(exe) == os.path.abspath(os.path.join(os.sep, "Applications", "SDR4NOOB.app"))
    assert updater_mac.bundle_path(os.path.join(os.sep, "opt", "x", "python3")) is None


def test_check_latest_picks_own_arch(monkeypatch, mac):
    monkeypatch.setattr(updater_mac, "arch", lambda: "x86_64")
    assets = [{"name": n, "browser_download_url": "https://x/" + n, "size": 5, "digest": "sha256:AA"}
              for n in ("SDR4NOOB-3.4.0-win64.zip", "SDR4NOOB-3.4.0-setup.exe", "SDR4NOOB-3.4.0-macos-arm64.zip",
                        "SDR4NOOB-3.4.0-macos-x86_64.zip", "SDR4NOOB-3.4.0-macos-x86_64.dmg")]
    monkeypatch.setattr(updater, "_get", lambda url, timeout=15: json.dumps(
        {"tag_name": "v3.4.0", "assets": assets}).encode())
    info = updater.check_latest()
    assert info["asset"]["name"] == "SDR4NOOB-3.4.0-macos-x86_64.zip"


def test_download_names_missing_mac_asset(monkeypatch, mac):
    monkeypatch.setattr(updater_mac, "arch", lambda: "arm64")
    with pytest.raises(updater.UpdateError, match="macos-arm64.zip"):
        updater.download({"asset": None})


def test_update_kind_mac(monkeypatch, tmp_path, mac):
    app = tmp_path / "SDR4NOOB.app"
    monkeypatch.setattr(updater, "frozen", lambda: True)
    monkeypatch.setattr(updater_mac, "bundle_path", lambda executable=None: str(app))
    info = {"asset": {"url": "u"}, "setup": {"url": "s"}}
    assert updater.update_kind(info) == "asset"          # установщика на macOS нет, даже если он есть в релизе
    monkeypatch.setattr(updater, "writable", lambda d: False)
    assert updater.update_kind(info) == "manual"


def test_temp_root(tmp_path):
    new = tmp_path / "rtltv-update-abc" / "x" / "SDR4NOOB.app"
    assert updater_mac.temp_root(str(new)) == str(tmp_path / "rtltv-update-abc")
    assert updater_mac.temp_root(str(tmp_path / "a" / "SDR4NOOB.app")) == ""


def test_install_requires_bundle(tmp_path):
    with pytest.raises(updater_mac.MacUpdateError, match="SDR4NOOB.app"):
        updater_mac.install_and_restart(str(tmp_path), str(tmp_path), executable=str(tmp_path / "python3"))


def _bundle(root, marker):
    app = root / "SDR4NOOB.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "SDR4NOOB").write_text(marker)
    os.symlink("SDR4NOOB", app / "Contents" / "MacOS" / "link")   # как ссылки внутри фреймворков Qt
    return app


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


@on_mac
def test_prepare_keeps_symlinks(tmp_path, mac):
    _bundle(tmp_path / "src", "new")
    z = tmp_path / "SDR4NOOB-9.9.9-macos-arm64.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(tmp_path / "src" / "SDR4NOOB.app"), str(z)], check=True)
    app = updater.prepare(str(z))
    try:
        assert app.endswith("SDR4NOOB.app")
        assert os.path.islink(os.path.join(app, "Contents", "MacOS", "link"))
    finally:
        import shutil
        shutil.rmtree(updater_mac.temp_root(app), ignore_errors=True)


def _run_script(tmp_path, new, app):
    sh = tmp_path / "install_update.sh"
    sh.write_text(updater_mac.INSTALL_SH)
    log = tmp_path / "log.txt"
    env = dict(os.environ, SDR4NOOB_NO_START="1")
    r = subprocess.run(["/bin/bash", str(sh), str(_dead_pid()), str(new), str(app), str(log), ""], env=env,
                       timeout=60)
    return r.returncode, log.read_text() if log.exists() else ""


@on_mac
def test_install_script_replaces_bundle(tmp_path):
    app = _bundle(tmp_path / "Applications", "old")
    new = _bundle(tmp_path / "rtltv-update-1", "new")
    code, log = _run_script(tmp_path, new, app)
    assert code == 0, log
    assert (app / "Contents" / "MacOS" / "SDR4NOOB").read_text() == "new"
    assert os.path.islink(app / "Contents" / "MacOS" / "link")
    assert not (tmp_path / "Applications" / "SDR4NOOB.app.old").exists()


@on_mac
def test_install_script_rolls_back(tmp_path):
    app = _bundle(tmp_path / "Applications", "old")
    code, log = _run_script(tmp_path, tmp_path / "нет-такого" / "SDR4NOOB.app", app)
    assert code == 1 and "откат" in log
    assert (app / "Contents" / "MacOS" / "SDR4NOOB").read_text() == "old"
    assert not (tmp_path / "Applications" / "SDR4NOOB.app.old").exists()
