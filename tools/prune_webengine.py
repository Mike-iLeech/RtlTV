"""Ужать QtWebEngine в собранном dist\\SDR4NOOB: из ~70 языковых файлов Chromium (qtwebengine_locales\\*.pak)
оставить русский и английский — остальные интерфейсу SDR4NOOB не нужны (Chromium без своего языка берёт en-US).

    py -3.12 tools/prune_webengine.py [dist\\SDR4NOOB]
"""
import os
import sys

KEEP = {"ru.pak", "en-US.pak"}


def prune(dist):
    """Удалить лишние *.pak во всех qtwebengine_locales внутри dist. -> (число файлов, байт)."""
    n = size = 0
    for dp, _dn, fn in os.walk(dist):
        if os.path.basename(dp) != "qtwebengine_locales":
            continue
        for name in fn:
            if name.endswith(".pak") and name not in KEEP:
                p = os.path.join(dp, name)
                size += os.path.getsize(p)
                os.remove(p)
                n += 1
    return n, size


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                              "dist", "SDR4NOOB")
    n, size = prune(root)
    print(f"QtWebEngine: удалено языков {n}, {size / 1e6:.1f} МБ")
