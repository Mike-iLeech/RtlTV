"""Автообновление: версии, разбор релиза GitHub, загрузка с проверкой SHA-256, распаковка, установочный скрипт."""
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rtltv import updater  # noqa: E402


@pytest.fixture(autouse=True)
def _windows_logic(monkeypatch):
    """Эти тесты — про сборку Windows (zip/установщик); на macOS её логика проверяется так же (см. test_updater_mac)."""
    monkeypatch.setattr(updater.osplat, "IS_MAC", False)


def test_versions():
    assert updater.parse_version("v2.0.1") == (2, 0, 1)
    assert updater.parse_version("2.1") == (2, 1, 0)
    assert updater.parse_version("v3.0.0-beta") == (3, 0, 0)
    assert updater.is_newer("v2.0.1", "2.0.0")
    assert not updater.is_newer("v2.0.0", "2.0.0")
    assert not updater.is_newer("v1.9.9", "2.0.0")
    assert updater.is_newer("v2.10.0", "2.9.9")


def _release(digest=None, extra=()):
    assets = [{"name": "SDR4NOOB-2.0.1-win64.zip", "browser_download_url": "https://x/SDR4NOOB-2.0.1-win64.zip",
               "size": 123, **({"digest": digest} if digest else {})}, *extra]
    return json.dumps({"tag_name": "v2.0.1", "body": "* новое", "html_url": "https://g/r", "assets": assets}).encode()


def test_check_latest_digest(monkeypatch):
    monkeypatch.setattr(updater, "_get", lambda url, timeout=15: _release("sha256:ABCDEF"))
    info = updater.check_latest()
    assert info["version"] == "2.0.1" and info["asset"]["sha256"] == "abcdef" and info["notes"] == "* новое"


def test_check_latest_sha_file(monkeypatch):
    def get(url, timeout=15):
        if url.endswith(".sha256"):
            return b"0123abcd  SDR4NOOB-2.0.1-win64.zip\n"
        return _release(None, [{"name": "SDR4NOOB-2.0.1-win64.zip.sha256", "browser_download_url": "https://x/s.sha256"}])
    monkeypatch.setattr(updater, "_get", get)
    assert updater.check_latest()["asset"]["sha256"] == "0123abcd"


def test_check_latest_error(monkeypatch):
    def boom(url, timeout=15):
        raise OSError("нет сети")
    monkeypatch.setattr(updater, "_get", boom)
    with pytest.raises(updater.UpdateError):
        updater.check_latest()


def test_schedule_and_skip():
    s = updater.settings.load("app", {})
    s.update(update_check="daily", update_last=0, update_skip=None)
    updater.settings.save("app", s)
    t0 = 1.9e9
    assert updater.due(now=t0)                       # ни разу не проверяли
    updater.mark_checked(now=t0)
    assert not updater.due(now=t0 + 3600) and updater.due(now=t0 + 25 * 3600)
    s = updater.settings.load("app", {})
    s["update_check"] = "never"
    updater.settings.save("app", s)
    assert not updater.due(now=10 ** 10)
    updater.skip("2.0.1")
    assert updater.skipped("2.0.1") and not updater.skipped("2.0.2")


def _zip_bytes(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


class _Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_checks_sha(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.paths, "LOCAL", str(tmp_path))
    data = _zip_bytes({"SDR4NOOB/SDR4NOOB.exe": b"exe", "SDR4NOOB/RtlTV.exe": b"exe", "SDR4NOOB/_internal/a.txt": b"a"})
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: _Resp(data))
    good = {"asset": {"name": "SDR4NOOB-2.0.1-win64.zip", "url": "https://x", "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}}
    path = updater.download(good)
    assert open(path, "rb").read() == data
    new = updater.prepare(path)
    assert os.path.isfile(os.path.join(new, "SDR4NOOB.exe")) and os.path.isdir(os.path.join(new, "_internal"))
    bad = {"asset": dict(good["asset"], sha256="00" * 32)}
    with pytest.raises(updater.UpdateError):
        updater.download(bad)


def test_prepare_rejects_traversal(tmp_path):
    p = tmp_path / "evil.zip"
    p.write_bytes(_zip_bytes({"../evil.txt": b"x", "SDR4NOOB/SDR4NOOB.exe": b"e", "SDR4NOOB/_internal/x": b"x"}))
    with pytest.raises(updater.UpdateError):
        updater.prepare(str(p))


def _run_ps(tmp_path, app, new):
    ps1 = tmp_path / "install.ps1"
    ps1.write_text(updater.INSTALL_PS1, encoding="utf-8-sig")
    log = tmp_path / "log.txt"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                        "-ProcId", "999999", "-New", str(new), "-App", str(app), "-Log", str(log), "-NoStart"],
                       capture_output=True, timeout=120)
    return r, log.read_text(encoding="utf-8-sig") if log.exists() else ""


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_install_script_replaces_files(tmp_path):
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "RtlTV.exe").write_bytes(b"old-exe")
    (app / "_internal" / "old_only.txt").write_text("старый")
    new = tmp_path / "new"
    (new / "_internal").mkdir(parents=True)
    (new / "RtlTV.exe").write_bytes(b"new-exe")
    (new / "_internal" / "lib.txt").write_text("новый")
    r, log = _run_ps(tmp_path, app, new)
    assert r.returncode == 0, (r.stdout, r.stderr, log)
    assert (app / "RtlTV.exe").read_bytes() == b"new-exe"
    assert (app / "_internal" / "lib.txt").read_text() == "новый"
    assert not (app / "_internal" / "old_only.txt").exists()      # старое _internal заменено целиком
    assert not (app / "_internal.old").exists() and not (app / "RtlTV.exe.old").exists()
    assert "обновление установлено" in log


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_install_script_rolls_back(tmp_path):
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "RtlTV.exe").write_bytes(b"old-exe")
    (app / "_internal" / "lib.txt").write_text("старый")
    new = tmp_path / "new"
    new.mkdir()
    (new / "RtlTV.exe").write_bytes(b"new-exe")       # нет _internal — копирование упадёт → откат
    r, log = _run_ps(tmp_path, app, new)
    assert r.returncode != 0
    assert (app / "RtlTV.exe").read_bytes() == b"old-exe"
    assert (app / "_internal" / "lib.txt").read_text() == "старый"
    assert "откат" in log


def test_download_requires_sha(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.paths, "LOCAL", str(tmp_path))
    with pytest.raises(updater.UpdateError):
        updater.download({"asset": {"name": "SDR4NOOB-2.0.1-win64.zip", "url": "https://x", "sha256": None}})


def test_temp_root(tmp_path):
    root = os.path.join(updater.tempfile.gettempdir(), "rtltv-update-abc")
    assert updater.temp_root(os.path.join(root, "SDR4NOOB")) == root
    assert updater.temp_root(str(tmp_path / "other")) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_install_script_rolls_back_when_exe_locked(tmp_path):
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "RtlTV.exe").write_bytes(b"old-exe")
    (app / "_internal" / "lib.txt").write_text("старый")
    new = tmp_path / "new"
    (new / "_internal").mkdir(parents=True)
    (new / "RtlTV.exe").write_bytes(b"new-exe")
    with open(app / "RtlTV.exe", "rb"):          # exe занят (антивирус) — второе переименование упадёт
        r, log = _run_ps(tmp_path, app, new)
    assert r.returncode != 0
    assert (app / "_internal" / "lib.txt").read_text() == "старый"   # _internal вернулся на место
    assert (app / "RtlTV.exe").read_bytes() == b"old-exe"
    assert "откат" in log



@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_install_script_removes_old_rtltv_exe(tmp_path):
    """Установка 2.x/3.0 (с RtlTV.exe) обновляется архивом без копии: RtlTV.exe убирается, ставится SDR4NOOB.exe."""
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "RtlTV.exe").write_bytes(b"old-exe")
    (app / "SDR4NOOB.exe").write_bytes(b"old-exe")
    new = tmp_path / "new"
    (new / "_internal").mkdir(parents=True)
    (new / "SDR4NOOB.exe").write_bytes(b"new-exe")
    (new / "LICENSE").write_text("GPL")
    r, log = _run_ps(tmp_path, app, new)
    assert r.returncode == 0, (r.stdout, r.stderr, log)
    assert (app / "SDR4NOOB.exe").read_bytes() == b"new-exe"
    assert not (app / "RtlTV.exe").exists() and not (app / "RtlTV.exe.old").exists()
    assert (app / "LICENSE").exists()


def test_prepare_accepts_old_layout_and_start_name(tmp_path):
    p = tmp_path / "old.zip"
    p.write_bytes(_zip_bytes({"RtlTV/RtlTV.exe": b"e", "RtlTV/_internal/x": b"x"}))
    new = updater.prepare(str(p))
    assert updater.start_name(new) == "RtlTV.exe"
    p2 = tmp_path / "new.zip"
    p2.write_bytes(_zip_bytes({"SDR4NOOB/SDR4NOOB.exe": b"e", "SDR4NOOB/RtlTV.exe": b"e", "SDR4NOOB/_internal/x": b"x"}))
    assert updater.start_name(updater.prepare(str(p2))) == "SDR4NOOB.exe"


def test_check_latest_setup_asset(monkeypatch):
    def fake(url, timeout=15):
        if url.endswith("setup.exe.sha256"):
            return b"feedbeef  SDR4NOOB-3.1.0-setup.exe\n"
        return json.dumps({"tag_name": "v3.1.0", "body": "", "html_url": "https://g/r", "assets": [
            {"name": "SDR4NOOB-3.1.0-win64.zip", "browser_download_url": "https://x/z", "size": 5, "digest": "sha256:AA"},
            {"name": "SDR4NOOB-3.1.0-setup.exe", "browser_download_url": "https://x/s", "size": 7},
            {"name": "SDR4NOOB-3.1.0-setup.exe.sha256", "browser_download_url": "https://x/setup.exe.sha256"},
        ]}).encode()
    monkeypatch.setattr(updater, "_get", fake)
    info = updater.check_latest()
    assert info["asset"]["sha256"] == "aa"
    assert info["setup"]["name"] == "SDR4NOOB-3.1.0-setup.exe" and info["setup"]["sha256"] == "feedbeef"


def test_update_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.osplat, "IS_LINUX", False)   # логика Windows (Linux — AppImage, tests/test_linux.py)
    info = {"asset": {"url": "z"}, "setup": {"url": "s"}}
    monkeypatch.setattr(updater, "app_dir", lambda: str(tmp_path))
    assert updater.update_kind(info) == "asset"              # переносная копия — архивом
    (tmp_path / "unins000.exe").write_bytes(b"x")
    assert updater.update_kind(info) == "setup"              # поставлено установщиком — установщиком
    assert updater.update_kind({"asset": {"url": "z"}}) == "asset"   # в релизе нет установщика


def test_configure_writes_settings(tmp_path, monkeypatch):
    from rtltv import __main__ as m, paths, settings
    monkeypatch.setattr(paths, "DEFAULT_DATA", str(tmp_path / "Документы" / "SDR4NOOB"))
    data = tmp_path / "Мои записи"
    assert m.configure(str(data), "never") == 0
    s = settings.load("app", {})
    assert s["data_dir"] == str(data) and s["update_check"] == "never" and data.is_dir()
    m.configure(paths.DEFAULT_DATA, "daily")
    s = settings.load("app", {})
    assert s["data_dir"] is None and s["update_check"] == "daily"


def test_legacy_items_finds_old_default_data(tmp_path, monkeypatch):
    r"""Обновление с 2.x: data_dir не задан, записи в «Документы\RtlTV» по раскладке 2.x, а установщик выбрал
    «Документы\SDR4NOOB» — «Настройки» должны предложить перенос и без data_dir_prev."""
    from rtltv import paths
    old, new = tmp_path / "RtlTV", tmp_path / "SDR4NOOB"
    (old / paths.IQ).mkdir(parents=True)
    (old / paths.IQ / "pass.cu8").write_bytes(b"iq")
    (old / paths.PICS / paths.PICS_METEOR).mkdir(parents=True)
    (old / paths.PICS / paths.PICS_METEOR / "m.png").write_bytes(b"png")
    monkeypatch.setattr(paths, "OLD_DEFAULT_DATA", str(old))
    monkeypatch.setattr(paths, "_app", lambda: {"data_dir_prev": str(old)})   # и без дублей, если она же prev
    items = paths.legacy_items(str(new), with_service=False)
    got = sorted((src, dst) for src, dst, _s, _f in items if src.startswith(str(old)))
    assert got == sorted([(str(old / paths.IQ), str(new / paths.IQ)),
                          (os.path.join(str(old), paths.PICS, paths.PICS_METEOR),
                           os.path.join(str(new), paths.PICS, paths.PICS_METEOR))])
    monkeypatch.setattr(paths, "_app", lambda: {"data_dir": str(old)})   # ещё работает в ней — из неё не предлагать
    assert not [i for i in paths.legacy_items(str(new), with_service=False) if i[0].startswith(str(old))]
    monkeypatch.setattr(paths, "_app", lambda: {})
    assert len([i for i in paths.legacy_items(str(new), with_service=False) if i[0].startswith(str(old))]) == 2
    assert not [i for i in paths.legacy_items(str(old), with_service=False) if i[0].startswith(str(old))]


def test_migrate_merges_and_retries(tmp_path, monkeypatch):
    from rtltv import paths
    old, new = tmp_path / "RtlTV", tmp_path / "SDR4NOOB"
    (old / "tiles" / "5").mkdir(parents=True)
    (old / "tiles" / "5" / "a.png").write_bytes(b"old-a")
    (old / "tiles" / "5" / "b.png").write_bytes(b"old-b")
    (old / "adsb_history.json").write_text("old")
    (old / "updates").mkdir()
    (new / "tiles" / "5").mkdir(parents=True)
    (new / "tiles" / "5" / "a.png").write_bytes(b"new-a")            # уже перенесено/скачано заново
    monkeypatch.setattr(paths, "OLD_LOCAL", str(old))
    monkeypatch.setattr(paths, "LOCAL", str(new))
    monkeypatch.setattr(paths, "OLD_DEFAULT_DATA", str(tmp_path / "нет"))
    monkeypatch.delenv("RTLTV_NO_MIGRATE", raising=False)
    paths.migrate()
    assert (new / "tiles" / "5" / "a.png").read_bytes() == b"new-a"   # новое не перезаписано
    assert (new / "tiles" / "5" / "b.png").read_bytes() == b"old-b"   # недостающее дозаехало
    assert (new / "adsb_history.json").read_text() == "old"
    assert (old / "updates").is_dir() and not (new / "updates").exists()
    assert (new / paths.MIGRATED_MARK).exists()


def test_update_kind_manual_without_write_access(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.osplat, "IS_LINUX", False)
    monkeypatch.setattr(updater, "app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "writable", lambda d: False)
    (tmp_path / "unins000.exe").write_bytes(b"x")
    assert updater.update_kind({"asset": {"url": "z"}, "setup": {"url": "s"}}) == "manual"


def test_cleanup_legacy_exe_only_in_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "app_dir", lambda: None)
    assert updater.cleanup_legacy_exe() is False                  # из исходников — ничего не трогаем
    monkeypatch.setattr(updater, "app_dir", lambda: str(tmp_path))
    assert updater.cleanup_legacy_exe() is False                  # RtlTV.exe нет — нечего убирать


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell")
def test_relink_script_removes_old_exe(tmp_path):
    old = tmp_path / "RtlTV.exe"
    old.write_bytes(b"x")
    ps1 = tmp_path / "relink.ps1"
    ps1.write_text(updater._RELINK_PS1, encoding="utf-8-sig")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                        "-Old", str(old), "-New", str(tmp_path / "SDR4NOOB.exe")], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert not old.exists()
