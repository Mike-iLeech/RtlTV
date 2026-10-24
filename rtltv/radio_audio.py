"""Звук режима «Радио»: AudioOut с удержанием задержки.

Часы донгла и звуковой карты расходятся (до сотен ppm), а блоки приходят пачками по ~50 мс.
Базовый AudioOut при переполнении буфера (0,5 с) выбрасывает целый блок — это слышимый пропуск,
а задержка звука при этом остаётся около полусекунды. Здесь буфер держится в коридоре 80–220 мс
незаметным пересчётом темпа на ±0,5 % (сжатие/растяжение блока), а сброс — только аварийный.
"""
import numpy as np

from .audio import IN_RATE, AudioOut


class RadioAudioOut(AudioOut):
    LO, HI, HARD = 0.08, 0.22, 0.45
    STEP = 0.005

    def __init__(self):
        super().__init__()
        self.ema = None
        self.adjust = 0.0      # текущая поправка темпа (для отладки)

    def buffered(self):
        """Сколько секунд звука ждёт вывода."""
        with self.lock:
            return self.size / max(self.rate, 1)

    def latency(self):
        lat = self.buffered()
        try:
            if self.stream is not None:
                lat += float(self.stream.latency)
        except Exception:  # noqa: BLE001
            pass
        return lat

    def push(self, a):
        if a.size == 0:
            return
        if self.stream is not None:
            lvl = self.buffered()
            self.ema = lvl if self.ema is None else 0.8 * self.ema + 0.2 * lvl
            if lvl > self.HARD:
                with self.lock:            # аварийно: оставить только последний HI звука
                    while self.size > self.rate * self.HI and len(self.chunks) > 1:
                        self.size -= self.chunks.pop(0).shape[0]
                self.ema = self.HI
            k = 0.0
            if self.ema > self.HI:
                k = -self.STEP          # буфер растёт — чуть ускоряем (короче блок)
            elif self.ema < self.LO:
                k = self.STEP
            self.adjust = k
            if k:
                a = stretch(a, 1.0 + k)
        super().push(a)


def stretch(a, factor):
    """Линейная передискретизация блока по времени (factor > 1 — длиннее). Края блока сохраняются."""
    a = np.asarray(a, np.float32)
    n = a.shape[0]
    m = max(2, int(round(n * factor)))
    if n < 2 or m == n:
        return a
    pos = np.linspace(0, n - 1, m)
    i0 = np.minimum(pos.astype(np.int64), n - 2)
    fr = (pos - i0).astype(np.float32)
    if a.ndim == 2:
        fr = fr[:, None]
    return a[i0] * (1 - fr) + a[i0 + 1] * fr


__all__ = ["RadioAudioOut", "stretch", "IN_RATE"]
