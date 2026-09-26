"""Colors, fonts, themes and the application stylesheet.

COLORS / STATE_COLORS are module-level dicts that are updated *in place* when the theme
changes, so any code reading COLORS[...] at paint time picks the new colors up. Widgets
that bake colors into stylesheets listen to `signals.changed` and restyle themselves.
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"

# ---------------------------------------------------------------------------
# Palettes. Only the base colors are required; the rest is derived by _finish().
#   accent   = the brand color (buttons, highlights)      accent2 = secondary (your messages)
# ---------------------------------------------------------------------------
THEMES: dict[str, dict] = {
    "neon": {
        "label": "Neon (pink on black)", "dark": True,
        "bg": "#050308", "panel": "#0e0a12", "panel_alt": "#171019", "border": "#3a1030",
        "accent": "#ff2fb4", "accent_dim": "#7a1a5e", "accent_bright": "#ff6cd6", "accent2": "#33fff0",
        "text": "#f5f5f7", "muted": "#8a8a92", "success": "#39ff8c", "error": "#ff4d6d",
        "bubble_user": "#0d2427", "bubble_user_border": "#155c60",
        "bubble_bot": "#2a1026", "bubble_bot_border": "#7a1a5e",
    },
    "midnight": {
        "label": "Midnight (blue)", "dark": True,
        "bg": "#060a14", "panel": "#0b1120", "panel_alt": "#111a2e", "border": "#1d2b4a",
        "accent": "#4d8dff", "accent2": "#33e0ff",
        "text": "#eaf0ff", "muted": "#8391ad", "success": "#3ddc97", "error": "#ff5c7a",
    },
    "ember": {
        "label": "Ember (orange)", "dark": True,
        "bg": "#0c0705", "panel": "#150d09", "panel_alt": "#20130d", "border": "#4a2314",
        "accent": "#ff6a1a", "accent2": "#ffd23f",
        "text": "#fff3ea", "muted": "#a08a7c", "success": "#7be04a", "error": "#ff4040",
    },
    "matrix": {
        "label": "Matrix (green)", "dark": True,
        "bg": "#020603", "panel": "#06100a", "panel_alt": "#0b1a10", "border": "#12401f",
        "accent": "#22ff6a", "accent2": "#b6ff3a",
        "text": "#e6ffee", "muted": "#6f9a80", "success": "#22ff6a", "error": "#ff4d4d",
    },
    "aurora": {
        "label": "Aurora (violet & teal)", "dark": True,
        "bg": "#070512", "panel": "#0e0a1f", "panel_alt": "#161030", "border": "#2f2260",
        "accent": "#a26bff", "accent2": "#3cf2c4",
        "text": "#f1eeff", "muted": "#8b86a8", "success": "#3cf2c4", "error": "#ff5c8a",
    },
    "mono": {
        "label": "Mono (grayscale)", "dark": True,
        "bg": "#0a0a0a", "panel": "#121212", "panel_alt": "#1b1b1b", "border": "#333333",
        "accent": "#e8e8e8", "accent2": "#9aa0a6",
        "text": "#f2f2f2", "muted": "#8a8a8a", "success": "#9be3a5", "error": "#ff6b6b",
    },
    "daylight": {
        "label": "Daylight (light)", "dark": False,
        "bg": "#f4f5f8", "panel": "#ffffff", "panel_alt": "#eceef3", "border": "#d3d7e0",
        "accent": "#c2147f", "accent2": "#0a8f87",
        "text": "#1b1d24", "muted": "#5f6475", "success": "#1f9d55", "error": "#d6284a",
    },
    "paper": {
        "label": "Paper (light, blue)", "dark": False,
        "bg": "#f7f5ef", "panel": "#fffdf8", "panel_alt": "#efece2", "border": "#d9d4c4",
        "accent": "#2f5bd6", "accent2": "#b4570f",
        "text": "#22201a", "muted": "#6a6553", "success": "#2f8a4d", "error": "#c53030",
    },
    # High contrast: pure black / white with saturated accents, every pair well above WCAG AAA (7:1).
    "contrast": {
        "label": "High contrast (dark)", "dark": True,
        "bg": "#000000", "panel": "#000000", "panel_alt": "#0d0d0d", "border": "#ffffff",
        "accent": "#ffff00", "accent2": "#00ffff", "accent_dim": "#5c5c00", "accent_bright": "#ffff80",
        "text": "#ffffff", "muted": "#d8d8d8", "success": "#3dff3d", "error": "#ff7b7b",
        "bubble_user": "#001a1a", "bubble_user_border": "#00ffff", "bubble_bot": "#1a1a00", "bubble_bot_border": "#ffff00",
    },
    "contrast_light": {
        "label": "High contrast (light)", "dark": False,
        "bg": "#ffffff", "panel": "#ffffff", "panel_alt": "#f0f0f0", "border": "#000000",
        "accent": "#0000b8", "accent2": "#6b00a8", "accent_dim": "#b8b8e8", "accent_bright": "#0000ff",
        "text": "#000000", "muted": "#2e2e2e", "success": "#005c00", "error": "#a80000",
        "bubble_user": "#f3e8ff", "bubble_user_border": "#6b00a8", "bubble_bot": "#e8e8ff", "bubble_bot_border": "#0000b8",
    },
}
DEFAULT_THEME = "neon"
# Follow Windows' own high-contrast mode: while it is on, the high-contrast theme replaces the chosen one
# (the choice itself is kept). main.py / Settings set this from the "follow_high_contrast" setting.
FOLLOW = {"high_contrast": True}

# Status colors a user can override (label shown in Settings). "muted" defaults to the error color.
STATE_NAMES: list[tuple[str, str]] = [
    ("idle", "Idle"), ("listening", "Listening"), ("thinking", "Thinking"),
    ("speaking", "Speaking"), ("error", "Error"), ("muted", "Muted"),
]

COLORS: dict[str, str] = {}
STATE_COLORS: dict[str, str] = {}
_current = {"name": DEFAULT_THEME, "dark": True}


def _mix(a: str, b: str, t: float) -> str:
    """Blend colour a toward colour b by t (0 = a, 1 = b)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


def _luminance(color: str) -> float:
    """WCAG relative luminance (0 = black, 1 = white)."""
    c = QColor(color)

    def lin(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c.red()) + 0.7152 * lin(c.green()) + 0.0722 * lin(c.blue())


def _text_on(color: str) -> str:
    """Black or white, whichever has the higher contrast ratio against `color`."""
    lum = _luminance(color)
    return "#000000" if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else "#ffffff"


def text_on(color: str) -> str:
    """Black or white text, whichever reads better on `color` (public wrapper)."""
    return _text_on(color)


def _finish(p: dict) -> dict:
    """Fills in every derived color a palette didn't spell out."""
    p = dict(p)
    bg = p["bg"]
    p.setdefault("accent_dim", _mix(p["accent"], bg, 0.55))
    p.setdefault("accent_bright", QColor(p["accent"]).lighter(135).name())
    p.setdefault("bubble_user", _mix(p["accent2"], p["panel"], 0.86))
    p.setdefault("bubble_user_border", _mix(p["accent2"], p["panel"], 0.6))
    p.setdefault("bubble_bot", _mix(p["accent"], p["panel"], 0.88))
    p.setdefault("bubble_bot_border", p["accent_dim"])
    p["on_accent"] = _text_on(p["accent"])   # readable text on accent-colored fills
    return p


def _with_accent(p: dict, accent: str) -> dict:
    """The same palette re-tinted around a user-chosen accent color."""
    p = {k: v for k, v in p.items()
         if k not in ("accent", "accent_dim", "accent_bright", "bubble_bot", "bubble_bot_border")}
    p["accent"] = accent
    p["border"] = _mix(accent, p["bg"], 0.78 if p.get("dark", True) else 0.82)
    return p


# ---------------------------------------------------------------------------
# Applying a theme
# ---------------------------------------------------------------------------
class _ThemeSignals(QObject):
    changed = Signal()


signals = _ThemeSignals()


def theme_names() -> list[tuple[str, str]]:
    return [(key, spec["label"]) for key, spec in THEMES.items()]


def current_theme() -> str:
    return _current["name"]


def is_dark() -> bool:
    return _current["dark"]


class _HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint), ("lpszDefaultScheme", ctypes.c_wchar_p)]


def windows_high_contrast() -> bool:
    """True while Windows' high-contrast (Contrast themes) mode is switched on."""
    if sys.platform != "win32":
        return False
    try:
        info = _HIGHCONTRASTW()
        info.cbSize = ctypes.sizeof(_HIGHCONTRASTW)
        if ctypes.windll.user32.SystemParametersInfoW(0x0042, info.cbSize, ctypes.byref(info), 0):   # SPI_GETHIGHCONTRAST
            return bool(info.dwFlags & 0x1)                                                         # HCF_HIGHCONTRASTON
    except Exception:  # noqa: BLE001
        pass
    return False


def effective_theme(name: str) -> str:
    """The theme actually shown: `name`, or a high-contrast one while Windows is in high contrast."""
    if FOLLOW["high_contrast"] and not name.startswith("contrast") and windows_high_contrast():
        return "contrast" if THEMES.get(name, THEMES[DEFAULT_THEME]).get("dark", True) else "contrast_light"
    return name


def apply_theme(name: str = DEFAULT_THEME, accent: str = "", state_colors: dict | None = None) -> None:
    """Switches the whole app to a theme (optionally re-tinted with a custom accent color and
    with per-state status color overrides), updating COLORS / STATE_COLORS in place,
    re-styling the QApplication and notifying listening widgets. Overrides are applied on top
    of the theme every time, so they survive theme switches."""
    name = effective_theme(name if name in THEMES else DEFAULT_THEME)
    spec = THEMES[name]
    accent = "" if name.startswith("contrast") else str(accent or "").strip()   # high contrast keeps its own
    if accent and QColor(accent).isValid():
        spec = _with_accent(spec, QColor(accent).name())
    palette = _finish(spec)

    COLORS.clear()
    COLORS.update({k: v for k, v in palette.items() if k not in ("label", "dark")})
    STATE_COLORS.clear()
    STATE_COLORS.update({
        "idle": COLORS["accent"], "listening": COLORS["accent_bright"],
        "thinking": COLORS["accent"], "speaking": COLORS["accent2"], "error": COLORS["error"],
        "muted": COLORS["error"],
    })
    for state, color in (state_colors or {}).items():
        if state in STATE_COLORS and QColor(str(color)).isValid():
            STATE_COLORS[state] = QColor(str(color)).name()
    _current.update(name=name, dark=bool(palette.get("dark", True)))

    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(build_stylesheet())
    signals.changed.emit()


_ARROW_DIR = Path(tempfile.gettempdir()) / "neon-ui"
_ARROW_PATHS = {"up": "M2 7 L6 3 L10 7", "down": "M2 4 L6 8 L10 4"}


def _arrow(direction: str, colour: str) -> str:
    """A small chevron as an SVG file, for the style sheet's arrow images (a style sheet that restyles
    spin and combo boxes also removes their native arrows, leaving empty squares)."""
    name = f"{direction}-{colour.lstrip('#')}.svg"
    path = _ARROW_DIR / name
    if not path.exists():
        try:
            _ARROW_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="11" viewBox="0 0 12 11">'
                            f'<path d="{_ARROW_PATHS[direction]}" fill="none" stroke="{colour}" stroke-width="1.8" '
                            f'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
        except OSError:
            return ""
    return path.as_posix()


def build_stylesheet() -> str:
    c = COLORS
    up, down, down_muted = _arrow("up", c["text"]), _arrow("down", c["text"]), _arrow("down", c["muted"])
    up_muted = _arrow("up", c["muted"])
    return f"""
* {{ font-family: "{FONT_FAMILY}"; color: {c['text']}; }}
QMainWindow, QDialog, QWidget#root {{ background: {c['bg']}; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {c['muted']}; }}
QLabel#error {{ color: {c['error']}; }}
*:disabled {{ color: {c['muted']}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ background: {c['panel']}; border-color: {c['panel_alt']}; }}
QPushButton:disabled {{ background: {c['panel']}; }}
QLabel#section {{ color: {c['accent']}; font-weight: 700; letter-spacing: 1px; }}

QLineEdit, QPlainTextEdit, QComboBox {{
    background: {c['panel_alt']}; border: 1px solid {c['border']};
    border-radius: 10px; padding: 8px 12px; selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border: 1px solid {c['accent']}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 12px; height: 11px; }}
QComboBox::down-arrow:disabled {{ image: url("{down_muted}"); }}

QSpinBox, QDoubleSpinBox, QTimeEdit {{ padding-right: 26px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button {{
    subcontrol-origin: border; subcontrol-position: top right; width: 24px; border: none;
    border-left: 1px solid {c['border']}; border-top-right-radius: 8px; background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right; width: 24px; border: none;
    border-left: 1px solid {c['border']}; border-bottom-right-radius: 8px; background: transparent;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QTimeEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover, QTimeEdit::down-button:hover {{
    background: {c['accent_dim']};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QTimeEdit::up-arrow {{ image: url("{up}"); width: 10px; height: 9px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QTimeEdit::down-arrow {{ image: url("{down}"); width: 10px; height: 9px; }}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled, QTimeEdit::up-arrow:disabled {{ image: url("{up_muted}"); }}
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled, QTimeEdit::down-arrow:disabled {{ image: url("{down_muted}"); }}
QComboBox QAbstractItemView {{
    background: {c['panel_alt']}; border: 1px solid {c['border']};
    selection-background-color: {c['accent']}; selection-color: {c['on_accent']};
}}

QPushButton {{
    background: {c['panel_alt']}; border: 1px solid {c['border']};
    border-radius: 10px; padding: 8px 16px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {c['accent']}; color: {c['accent_bright']}; }}
QPushButton:pressed {{ background: {c['accent_dim']}; }}
QPushButton:disabled {{ color: {c['muted']}; border-color: {c['panel_alt']}; }}
QPushButton#primary {{ background: {c['accent']}; color: {c['on_accent']}; border: none; }}
QPushButton#primary:hover {{ background: {c['accent_bright']}; color: {c['on_accent']}; }}
QPushButton#primary:disabled {{ background: {c['accent_dim']}; color: {c['on_accent']}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['accent_dim']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {c['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; height: 0; }}

QMenu {{ background: {c['panel']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['accent']}; color: {c['on_accent']}; }}
QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['accent_dim']}; }}
"""


# Kept for callers that still import the constant; reflects the theme at import time only.
STYLESHEET = ""


def enable_dark_title_bar(widget) -> None:
    """Best-effort window chrome matching the theme (dark or light) on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if is_dark() else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old id)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001 -- purely cosmetic
        pass


apply_theme(DEFAULT_THEME)   # COLORS is populated from the first import
