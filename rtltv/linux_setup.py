"""Linux: доступ к USB-приёмникам (SDR) без root.

Нужны две вещи, и обе делаются один раз с правами администратора:
  1. Правило udev /etc/udev/rules.d/60-sdr4noob.rules — даёт вошедшему пользователю доступ к приёмникам
     (TAG+="uaccess"; запасной вариант — группа plugdev).
  2. Запрет драйвера ядра для ТВ-тюнеров /etc/modprobe.d/blacklist-sdr4noob.conf — иначе модуль
     dvb_usb_rtl28xxu (или rtl2832_sdr) захватывает приёмник на RTL2832U как DVB-T-тюнер.
     Встроенная librtlsdr умеет отцеплять драйвер ядра сама, но только когда у неё есть доступ, и до
     следующего переподключения — запрет надёжнее.
Кнопка «Настроить доступ…» в «Режим → Устройства…» выполняет setup_script() через pkexec (система спросит
пароль администратора). Пакет .deb ставит те же файлы при установке.
"""
import os
import shlex
import shutil
import subprocess
import tempfile

RULES_PATH = "/etc/udev/rules.d/60-sdr4noob.rules"
BLACKLIST_PATH = "/etc/modprobe.d/blacklist-sdr4noob.conf"
KERNEL_MODULES = ("dvb_usb_rtl28xxu", "rtl2832_sdr", "rtl2832", "rtl2830")

# приёмники на RTL2832U (как в rtl-sdr.rules проекта osmocom) и другие SDR, которые SDR4NOOB умеет открыть
RTL_IDS = [
    (0x0BDA, 0x2832), (0x0BDA, 0x2838), (0x0413, 0x6680), (0x0413, 0x6F0F), (0x0458, 0x707F),
    (0x0CCD, 0x00A9), (0x0CCD, 0x00B3), (0x0CCD, 0x00B4), (0x0CCD, 0x00B5), (0x0CCD, 0x00B7),
    (0x0CCD, 0x00B8), (0x0CCD, 0x00B9), (0x0CCD, 0x00C0), (0x0CCD, 0x00C6), (0x0CCD, 0x00D3),
    (0x0CCD, 0x00D7), (0x0CCD, 0x00E0), (0x1554, 0x5020), (0x15F4, 0x0131), (0x15F4, 0x0133),
    (0x185B, 0x0620), (0x185B, 0x0650), (0x185B, 0x0680), (0x1B80, 0xD393), (0x1B80, 0xD394),
    (0x1B80, 0xD395), (0x1B80, 0xD397), (0x1B80, 0xD398), (0x1B80, 0xD39D), (0x1B80, 0xD3A4),
    (0x1B80, 0xD3A8), (0x1B80, 0xD3AF), (0x1B80, 0xD3B0), (0x1D19, 0x1101), (0x1D19, 0x1102),
    (0x1D19, 0x1103), (0x1D19, 0x1104), (0x1F4D, 0xA803), (0x1F4D, 0xB803), (0x1F4D, 0xC803),
    (0x1F4D, 0xD286), (0x1F4D, 0xD803),
]
OTHER_IDS = [
    (0x1D50, 0x6089, "HackRF One"), (0x1D50, 0x604B, "HackRF Jawbreaker"), (0x1D50, 0xCC15, "rad1o"),
    (0x1D50, 0x60A1, "Airspy R2 / Mini"), (0x03EB, 0x800C, "Airspy HF+"),
]


def udev_rules_text():
    head = ("# SDR4NOOB: доступ к USB-приёмникам (SDR) для вошедшего пользователя.\n"
            "# Установлено программой SDR4NOOB; удалить можно в любой момент (sudo rm этот файл).\n")
    rule = 'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{:04x}", ATTRS{{idProduct}}=="{:04x}", ' \
           'MODE="0660", GROUP="plugdev", TAG+="uaccess"'
    lines = [head, "# приёмники на RTL2832U (RTL-SDR и совместимые)"]
    lines += [rule.format(v, p) for v, p in RTL_IDS]
    for v, p, name in OTHER_IDS:   # udev понимает комментарий только с начала строки
        lines += [f"# {name}", rule.format(v, p)]
    return "\n".join(lines) + "\n"


def blacklist_text():
    return ("# SDR4NOOB: не отдавать приёмники на RTL2832U драйверу ТВ-тюнера ядра — их открывает librtlsdr.\n"
            + "".join(f"blacklist {m}\n" for m in KERNEL_MODULES))


def setup_script(rules_src, blacklist_src):
    """Команды для pkexec sh -c: поставить оба файла, перечитать правила udev, выгрузить модули ядра."""
    mods = " ".join(KERNEL_MODULES)
    return (f"set -e\n"
            f"install -m 0644 {shlex.quote(rules_src)} {RULES_PATH}\n"
            f"install -m 0644 {shlex.quote(blacklist_src)} {BLACKLIST_PATH}\n"
            f"udevadm control --reload-rules || true\n"
            f"udevadm trigger --subsystem-match=usb || true\n"
            f"for m in {mods}; do modprobe -r \"$m\" 2>/dev/null || true; done\n")


PACKAGE_RULES = ("/usr/lib/udev/rules.d/60-sdr4noob.rules", "/lib/udev/rules.d/60-sdr4noob.rules")   # пакет .deb
PACKAGE_BLACKLIST = "/usr/lib/modprobe.d/blacklist-sdr4noob.conf"   # пакет .deb (уходит вместе с пакетом)


def is_configured():
    """Правило udev и запрет драйвера уже стоят (поставлены этой кнопкой или пакетом .deb)."""
    rules = any(os.path.isfile(p) for p in (RULES_PATH,) + PACKAGE_RULES)
    return rules and (os.path.isfile(BLACKLIST_PATH) or os.path.isfile(PACKAGE_BLACKLIST))


def manual_commands():
    """То же самое — командами для терминала (если pkexec нет)."""
    return ("sudo tee " + RULES_PATH + " >/dev/null <<'EOF'\n" + udev_rules_text() + "EOF\n"
            "sudo tee " + BLACKLIST_PATH + " >/dev/null <<'EOF'\n" + blacklist_text() + "EOF\n"
            "sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb\n"
            "sudo modprobe -r " + " ".join(KERNEL_MODULES) + "\n")


def run_setup(timeout=300):
    """Выполнить настройку через pkexec. Возвращает (ok, текст для пользователя)."""
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return False, ("Не найдена программа pkexec (polkit). Выполните в терминале:\n\n" + manual_commands())
    d = tempfile.mkdtemp(prefix="sdr4noob-setup-")
    rules, black = os.path.join(d, "60-sdr4noob.rules"), os.path.join(d, "blacklist-sdr4noob.conf")
    try:
        with open(rules, "w", encoding="utf-8") as f:
            f.write(udev_rules_text())
        with open(black, "w", encoding="utf-8") as f:
            f.write(blacklist_text())
        os.chmod(d, 0o755)
        r = subprocess.run([pkexec, "sh", "-c", setup_script(rules, black)], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Не удалось запустить pkexec: {e}"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode == 126:
        return False, "Настройка отменена (пароль администратора не введён)."
    if r.returncode == 127:
        return False, ("pkexec не смог запросить пароль: нет агента polkit или нет прав администратора. "
                       "Выполните в терминале:\n\n" + manual_commands())
    if r.returncode != 0:
        return False, f"Настройка не удалась (код {r.returncode}): {(r.stderr or r.stdout).strip()[:500]}"
    return True, ("Готово. Выдерните приёмник из USB и вставьте снова — после этого SDR4NOOB откроет его "
                  "без прав администратора.")
