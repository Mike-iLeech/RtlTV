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


def test_versions():
    assert updater.parse_version("v2.0.1") == (2, 0, 1)
    assert updater.parse_version("2.1") == (2, 1, 0)
    assert updater.parse_version("v3.0.0-beta") == (3, 0, 0)
    assert updater.is_newer("v2.0.1", "2.0.0")
    assert not updater.is_newer("v2.0.0", "2.0.0")
    assert not updater.is_newer("v1.9.9", "2.0.0")
    assert updater.is_newer("v2.10.0", "2.9.9")


def _release(digest=None, extra=()):
    assets = [{"name": "RtlTV-2.0.1-win64.zip", "browser_download_url": "https://x/RtlTV-2.0.1-win64.zip",
               "size": 123, **({"digest": digest} if digest else {})}, *extra]
    return json.dumps({"tag_name": "v2.0.1", "body": "* новое", "html_url": "https://g/r", "assets": assets}).encode()


def test_check_latest_digest(monkeypatch):
    monkeypatch.setattr(updater, "_get", lambda url, timeout=15: _release("sha256:ABCDEF"))
    info = updater.check_latest()
    assert info["version"] == "2.0.1" and info["asset"]["sha256"] == "abcdef" and info["notes"] == "* новое"


def test_check_latest_sha_file(monkeypatch):
    def get(url, timeout=15):
        if url.endswith(".sha256"):
            return b"0123abcd  RtlTV-2.0.1-win64.zip\n"
        return _release(None, [{"name": "RtlTV-2.0.1-win64.zip.sha256", "browser_download_url": "https://x/s.sha256"}])
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
    data = _zip_bytes({"RtlTV/RtlTV.exe": b"exe", "RtlTV/_internal/a.txt": b"a"})
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=30: _Resp(data))
    good = {"asset": {"name": "RtlTV-2.0.1-win64.zip", "url": "https://x", "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}}
    path = updater.download(good)
    assert open(path, "rb").read() == data
    new = updater.prepare(path)
    assert os.path.isfile(os.path.join(new, "RtlTV.exe")) and os.path.isdir(os.path.join(new, "_internal"))
    bad = {"asset": dict(good["asset"], sha256="00" * 32)}
    with pytest.raises(updater.UpdateError):
        updater.download(bad)


def test_prepare_rejects_traversal(tmp_path):
    p = tmp_path / "evil.zip"
    p.write_bytes(_zip_bytes({"../evil.txt": b"x", "RtlTV/RtlTV.exe": b"e", "RtlTV/_internal/x": b"x"}))
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
        updater.download({"asset": {"name": "RtlTV-2.0.1-win64.zip", "url": "https://x", "sha256": None}})


def test_temp_root(tmp_path):
    root = os.path.join(updater.tempfile.gettempdir(), "rtltv-update-abc")
    assert updater.temp_root(os.path.join(root, "RtlTV")) == root
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
