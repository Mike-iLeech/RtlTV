"""Цветовые схемы интерфейса и палитры изображения."""
import numpy as np

from .i18n import N_

# ключи — внутренние имена (хранятся в настройках как есть), на экран — через tr(name)

THEMES = {
    N_("Тёмная"): dict(bg="#15181d", panel="#1d2128", panel2="#252a33", fg="#d8dee9", dim="#8a93a3",
                   accent="#4fb3ff", grid="#2f3542", line="#7fd1ff", fill="#1e5d8a", lcd="#7fffd4",
                   cmap=[(0, "#000010"), (0.35, "#1b2a8a"), (0.6, "#1fa3c9"), (0.8, "#f0e04a"), (1, "#ffffff")]),
    N_("Светлая"): dict(bg="#eef1f5", panel="#ffffff", panel2="#e3e8ef", fg="#1d2430", dim="#5d6878",
                    accent="#0a66c2", grid="#c9d1dc", line="#0a66c2", fill="#9cc6ef", lcd="#0a3d6b",
                    cmap=[(0, "#ffffff"), (0.4, "#9cc6ef"), (0.7, "#0a66c2"), (0.9, "#3a0a6b"), (1, "#000000")]),
    N_("Ночная (красная)"): dict(bg="#0b0505", panel="#140808", panel2="#1e0c0c", fg="#e07a6a", dim="#8a4a40",
                             accent="#ff5533", grid="#3a1612", line="#ff6a4d", fill="#5a1a10", lcd="#ff7755",
                             cmap=[(0, "#000000"), (0.5, "#5a1000"), (0.8, "#ff3300"), (1, "#ffd0b0")]),
    N_("Матрица"): dict(bg="#020802", panel="#061206", panel2="#0b1d0b", fg="#6dff8a", dim="#3a8a4a",
                    accent="#39ff6a", grid="#11331a", line="#39ff6a", fill="#0d4a1d", lcd="#39ff6a",
                    cmap=[(0, "#000000"), (0.5, "#004d14"), (0.8, "#22dd55"), (1, "#e0ffe0")]),
    N_("Ретро (янтарь)"): dict(bg="#120d05", panel="#1b1408", panel2="#261c0b", fg="#ffb347", dim="#a0702a",
                           accent="#ffa31a", grid="#3d2c10", line="#ffb84d", fill="#5c3d0a", lcd="#ffb000",
                           cmap=[(0, "#000000"), (0.5, "#5c3200"), (0.8, "#ff9900"), (1, "#fff0c0")]),
}

DEFAULT_THEME = "Тёмная"  # noqa: i18n — внутреннее имя темы по умолчанию


def _hex(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) for i in (0, 2, 4)]


def gradient_lut(stops):
    xs = [s[0] for s in stops]
    cols = np.array([_hex(s[1]) for s in stops], dtype=np.float32)
    t = np.linspace(0, 1, 256)
    return np.stack([np.interp(t, xs, cols[:, i]) for i in range(3)], axis=1).astype(np.uint8)


PALETTES = {
    N_("Ч/Б"): [(0, "#000000"), (1, "#ffffff")],
    N_("Тёплый кинескоп"): [(0, "#050302"), (1, "#fff3dc")],
    N_("Холодный кинескоп"): [(0, "#020308"), (1, "#e6f0ff")],
    N_("Янтарный монитор"): [(0, "#000000"), (1, "#ffb000")],
    N_("Зелёный люминофор"): [(0, "#000000"), (0.8, "#33ff66"), (1, "#d0ffd8")],
    N_("Сепия"): [(0, "#1a1008"), (0.5, "#8a6a45"), (1, "#f5e6c8")],
    N_("Тепловизор"): [(0, "#000000"), (0.25, "#3b0f70"), (0.5, "#b5367a"), (0.75, "#fb8861"), (1, "#fcfdbf")],
    N_("Радуга"): [(0, "#000080"), (0.25, "#0080ff"), (0.5, "#00ff80"), (0.75, "#ffff00"), (1, "#ff0000")],
    N_("Негатив"): [(0, "#ffffff"), (1, "#000000")],
}


def qss(t):
    return f"""
    QWidget {{ background: {t['bg']}; color: {t['fg']}; font-size: 10pt; }}
    QGroupBox {{ background: {t['panel']}; border: 1px solid {t['grid']}; border-radius: 6px;
                margin-top: 24px; padding: 10px 6px 6px 6px; font-weight: bold; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 3px;
                       padding: 0 2px; color: {t['accent']}; background: transparent; }}
    QGroupBox QWidget {{ background: {t['panel']}; font-weight: normal; }}
    QLabel#dim, QGroupBox QLabel#dim {{ color: {t['dim']}; }}
    QPushButton {{ background: {t['panel2']}; border: 1px solid {t['grid']}; border-radius: 4px; padding: 4px 10px; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:checked {{ background: {t['accent']}; color: {t['bg']}; }}
    QPushButton:disabled {{ color: {t['dim']}; }}
    QComboBox, QSpinBox, QDoubleSpinBox, QListWidget {{ background: {t['panel2']}; border: 1px solid {t['grid']};
                border-radius: 4px; padding: 2px 4px; }}
    QComboBox QAbstractItemView {{ background: {t['panel2']}; selection-background-color: {t['accent']}; }}
    QListWidget::item:selected {{ background: {t['accent']}; color: {t['bg']}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {t['grid']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {t['accent']}; width: 14px; margin: -6px 0; border-radius: 7px; }}
    QSlider::sub-page:horizontal {{ background: {t['fill']}; border-radius: 2px; }}
    QProgressBar {{ background: {t['panel2']}; border: 1px solid {t['grid']}; border-radius: 4px; text-align: center; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 1px 2px 1px; border: none; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 1px 2px 1px 2px; border: none; }}
    QScrollBar::handle:vertical {{ background: {t['grid']}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::handle:horizontal {{ background: {t['grid']}; border-radius: 4px; min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {t['dim']}; }}
    QScrollBar::handle:pressed {{ background: {t['accent']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    QAbstractScrollArea::corner {{ background: {t['bg']}; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: {t['bg']}; }}
    QStatusBar {{ background: {t['panel']}; color: {t['dim']}; }}
    QSplitter::handle {{ background: {t['grid']}; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
    QTabWidget::pane {{ border: 1px solid {t['grid']}; border-radius: 4px; }}
    QTabBar::tab {{ background: {t['panel2']}; color: {t['dim']}; border: 1px solid {t['grid']};
                   padding: 4px 12px; border-top-left-radius: 4px; border-top-right-radius: 4px; }}
    QTabBar::tab:selected {{ background: {t['panel']}; color: {t['fg']}; border-bottom-color: {t['accent']}; }}
    QHeaderView::section {{ background: {t['panel2']}; color: {t['dim']}; border: none;
                           border-right: 1px solid {t['grid']}; padding: 3px 6px; }}
    QTableView, QTableWidget, QTreeView {{ background: {t['panel']}; alternate-background-color: {t['panel2']};
                           gridline-color: {t['grid']}; selection-background-color: {t['accent']};
                           selection-color: {t['bg']}; }}
    QMenuBar {{ background: {t['panel']}; }}
    QMenuBar::item:selected, QMenu::item:selected {{ background: {t['accent']}; color: {t['bg']}; }}
    QMenu {{ background: {t['panel2']}; border: 1px solid {t['grid']}; }}
    QLineEdit {{ background: {t['panel2']}; border: 1px solid {t['grid']}; border-radius: 4px; padding: 2px 4px; }}
    QToolTip {{ background: {t['panel2']}; color: {t['fg']}; border: 1px solid {t['accent']}; }}
    """
