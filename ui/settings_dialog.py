"""Settings window: a sidebar of pages, a search box, and consistent aligned rows.

  * Pages marked \u26a1 (Appearance, Status bar, Voice & sounds, Persona) apply the moment you change
    something and are saved right away. Other pages are applied by Save.
  * Rows that don't apply to the current choices are greyed out (`_enable_when`), e.g. the Piper rows
    while the Windows voice is selected.
  * The search box filters every row on every page.
  * Export / Import (General) move all settings through a JSON file.

CONFIG is mutated in place because the backend (speaker, listener, tools) reads it live.
"""

from __future__ import annotations

import json
import os
import threading

from PySide6.QtCore import Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QGraphicsOpacityEffect, QHBoxLayout,
                               QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
                               QPushButton, QSpinBox, QStackedWidget, QTimeEdit, QVBoxLayout, QWidget)

import app_paths
import assistant as backend
import bitwarden
import browser_bridge
import clipboard
import hotkeys
import memory_store
import neon_log
import persona
import plugins
import settings_schema
import startup
import stt
import tts
import wakeword
import websearch

from . import frame
from .model_picker import ModelPicker
from .settings_widgets import MemoryList, RoutinesEditor, Row, RulesEditor, SettingsPage, StripRulesEditor
from .sound_picker import SoundPicker
from .status_bar import list_monitors
from .theme import COLORS, STATE_COLORS, STATE_NAMES, apply_theme, enable_dark_title_bar, theme_names
from .theme import signals as theme_signals
from .voice_picker import PiperVoicePicker

HOTKEY_ROWS = [("hotkey_talk", "Start listening"), ("hotkey_wake", "Toggle wake word"),
               ("hotkey_window", "Show / hide main window"), ("hotkey_quick", "Quick command box"),
               ("hotkey_mute", "Mute microphone"), ("hotkey_hold", "Hold to talk"),
               ("hotkey_dictation", "Dictation on / off")]

DEFAULT_LABEL = "Default (system)"
LIVE_DELAY_MS = 350          # a burst of changes (dragging a spinner) is saved once
DISABLED_OPACITY = 0.45      # rows that don't apply to the current choices


class SettingsDialog(QDialog):
    ytm_status = Signal(str)    # result of "Test connection", emitted from a worker thread
    ha_status = Signal(str)     # ...and of Home Assistant's

    @staticmethod
    def raise_open() -> bool:
        """Bring an already-open Settings window to the front (True), so the gear, the tray and
        the main window don't stack up copies now that it no longer blocks them."""
        for w in QApplication.topLevelWidgets():
            if isinstance(w, SettingsDialog) and w.isVisible():
                w.raise_()
                w.activateWindow()
                return True
        return False

    def __init__(self, controller, parent=None, start_page: str | None = None):
        super().__init__(parent)
        self._controller = controller
        self._get: dict[str, callable] = {}
        self._set: dict[str, callable] = {}
        self._validators: dict[str, callable] = {}
        self._signals: dict[str, tuple] = {}
        self._rows_by_key: dict[str, object] = {}
        self._live_keys: set[str] = set()
        self._live_dirty: set[str] = set()
        self._pages: list[SettingsPage] = []
        self._page: SettingsPage | None = None
        self._loading = True                       # setting the initial values must not count as changes

        self.setWindowTitle("Settings")
        self.setMinimumSize(900, 620)
        self.resize(1000, 720)
        # Modal to its parent only, and not modal at all without one (Qt treats a parentless modal
        # dialog as application-modal, and so does exec(): open it with show()). Application-modal
        # disables every other window, so clicks on the status bar only played the error sound.
        self.setWindowModality(Qt.WindowModal if parent is not None else Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose)     # don't pile up hidden dialogs listening for theme changes

        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(LIVE_DELAY_MS)
        self._live_timer.timeout.connect(self._flush_live)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)
        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # ---- sidebar: search + page list
        side = QVBoxLayout()
        side.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search settings...  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(232)
        self.nav.setUniformItemSizes(True)
        self.nav.currentRowChanged.connect(self._on_nav)
        self.empty = QLabel("Nothing matches that search.")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)
        self.empty.hide()
        side.addWidget(self.search)
        side.addWidget(self.nav, 1)
        side.addWidget(self.empty)
        body.addLayout(side)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        for build in (self._build_general, self._build_appearance, self._build_statusbar, self._build_voice,
                      self._build_listening, self._build_notifications, self._build_hotkeys, self._build_ai,
                      self._build_persona, self._build_memory, self._build_board, self._build_routines,
                      self._build_apps, self._build_browser, self._build_passwords, self._build_music,
                      self._build_home, self._build_plugins):
            build()
        self._wire_across_pages()

        # ---- bottom bar
        self.error = QLabel("")
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        bar = QHBoxLayout()
        hint = QLabel("\u26a1 pages apply instantly. Everything else applies when you press Save.")
        hint.setObjectName("muted")
        restore = QPushButton("Restore this page's defaults")
        restore.clicked.connect(self._restore_page_defaults)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        bar.addWidget(hint)
        bar.addStretch(1)
        bar.addWidget(restore)
        bar.addWidget(cancel)
        bar.addWidget(save)
        root.addLayout(bar)

        for keys, action in ((("Ctrl+F", "Ctrl+K"), self._focus_search), (("Ctrl+Tab", "Ctrl+PgDown"), lambda: self._step_page(1)),
                             (("Ctrl+Shift+Tab", "Ctrl+PgUp"), lambda: self._step_page(-1))):
            for sequence in keys:
                QShortcut(QKeySequence(sequence), self, activated=action)

        theme_signals.changed.connect(self._apply_dialog_styles)
        self._apply_dialog_styles()
        self._loading = False
        target = next((i for i, p in enumerate(self._pages) if p.key == start_page), 0)
        self.nav.setCurrentRow(target)
        frame.install(self, minimize=True, maximize=True)   # NEON's title bar, or the native one (Settings > Appearance)

    # ---- navigation + search -------------------------------------------------------------------
    def _new_page(self, key: str, title: str, icon: str, description: str = "", live: bool = False) -> SettingsPage:
        page = SettingsPage(key, title, icon, description, live)
        self._pages.append(page)
        self._page = page
        self.stack.addWidget(page)
        self.nav.addItem(QListWidgetItem(f"{icon}   {title}" + ("   \u26a1" if live else "")))
        return page

    def _on_nav(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)

    def _on_search(self, text: str) -> None:
        query = text.strip().lower()
        first_visible = -1
        for i, page in enumerate(self._pages):
            has = page.filter(query)
            self.nav.item(i).setHidden(not has)
            if has and first_visible < 0:
                first_visible = i
        self.empty.setVisible(bool(query) and first_visible < 0)
        current = self.nav.currentRow()
        if first_visible >= 0 and (current < 0 or self.nav.item(current).isHidden()):
            self.nav.setCurrentRow(first_visible)

    def _focus_search(self) -> None:
        self.search.setFocus(Qt.ShortcutFocusReason)
        self.search.selectAll()

    def _step_page(self, direction: int) -> None:
        """Ctrl+Tab / Ctrl+Shift+Tab: the next / previous page that the search hasn't hidden."""
        count = self.nav.count()
        row = self.nav.currentRow()
        for _ in range(count):
            row = (row + direction) % count
            if not self.nav.item(row).isHidden():
                self.nav.setCurrentRow(row)
                return

    def current_page(self) -> SettingsPage:
        return self._pages[max(0, self.stack.currentIndex())]

    # ---- styling --------------------------------------------------------------------------------
    def _apply_dialog_styles(self) -> None:
        """Colors baked into stylesheets; re-run on every theme change (live theme preview)."""
        c = COLORS
        self.setStyleSheet(f"""
            QListWidget#nav {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 12px;
                               padding: 6px; outline: 0; }}
            QListWidget#nav::item {{ padding: 7px 12px; border-radius: 8px; }}
            QListWidget#nav::item:hover:!selected {{ background: {c['panel_alt']}; }}
            QListWidget#nav::item:selected {{ background: {c['accent']}; color: {c['on_accent']}; }}
            QLabel#pageTitle {{ color: {c['accent']}; font-size: 24px; font-weight: 800; }}
            QLabel#error {{ color: {c['error']}; }}
            QLabel#liveBadge {{ color: {c['success']}; font-size: 11px; font-weight: 600; padding-top: 2px; }}
            QFrame#rule {{ background: {c['border']}; border: none; }}
            QCheckBox {{ spacing: 8px; }}
            QSpinBox, QDoubleSpinBox, QTimeEdit {{ background: {c['panel_alt']}; border: 1px solid {c['border']};
                                                   border-radius: 8px; padding: 5px 28px 5px 8px; min-width: 90px; }}
            QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus {{ border-color: {c['accent']}; }}
            QTableWidget {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: 8px;
                            gridline-color: {c['border']}; }}
            QListWidget {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: 8px;
                           padding: 4px; outline: 0; }}
            QListWidget::item {{ padding: 4px 6px; border-radius: 6px; }}
            QListWidget::item:hover:!selected {{ background: {c['panel']}; }}
            QListWidget::item:selected {{ background: {c['accent']}; color: {c['on_accent']}; }}
            QPlainTextEdit {{ background: {c['panel_alt']}; border: 1px solid {c['border']};
                              border-radius: 8px; padding: 6px; }}
            QHeaderView::section {{ background: {c['panel']}; color: {c['muted']}; border: none; padding: 6px; }}
            QSpinBox:disabled, QDoubleSpinBox:disabled, QTimeEdit:disabled {{ background: {c['panel']}; color: {c['muted']}; }}
            SettingsPage, SettingsPage > QWidget, SettingsPage > QWidget > QWidget {{ background: transparent; }}
            QStackedWidget {{ background: transparent; }}
        """)
        self.error.setStyleSheet(f"color: {c['error']};")
        refresh = getattr(self, "_refresh_state_swatches", None)
        if refresh is not None:
            refresh()
        enable_dark_title_bar(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        enable_dark_title_bar(self)

    # ---- registration: every setting is a getter / setter pair (+ the signals that mean "changed") -------
    def _register(self, key: str, getter, setter, *signals) -> None:
        self._get[key], self._set[key] = getter, setter
        self._signals[key] = signals
        self._page.keys.append(key)
        setter(backend.CONFIG.get(key, backend.DEFAULT_CONFIG.get(key)))
        if self._page.live:
            self._live_keys.add(key)
            for signal in signals:
                signal.connect(lambda *_a, k=key: self._live_changed(k))

    def _live_changed(self, key: str) -> None:
        """A control on an instant-apply page changed: apply it now, save it shortly."""
        if self._loading:
            return
        check = self._validators.get(key)
        problem = check() if check else None
        if problem:
            self.error.setText(problem.strip())     # not applied until it's valid
            return
        self.error.setText("")
        backend.CONFIG[key] = self._get[key]()      # in memory at once: everything reads CONFIG live
        self._live_dirty.add(key)
        self._live_timer.start()

    def _flush_live(self) -> None:
        keys = sorted(self._live_dirty)
        self._live_dirty.clear()
        if not keys:
            return
        backend.persist_keys({k: backend.CONFIG[k] for k in keys})
        self._controller.apply_live(keys)

    # ---- row builders ------------------------------------------------------------------------------
    def _text(self, key, label, placeholder="", help_text="", keywords="") -> QLineEdit:
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        self._register(key, lambda: w.text().strip(), lambda v: w.setText("" if v is None else str(v)),
                       w.editingFinished)
        self._attach_reset(key, self._page.row(label, w, help_text, keywords, resettable=key in backend.DEFAULT_CONFIG), w.textChanged)
        return w

    def _multiline(self, key, label, height=150, help_text="") -> QPlainTextEdit:
        w = QPlainTextEdit()
        w.setMinimumHeight(height)
        self._register(key, lambda: w.toPlainText().strip(), lambda v: w.setPlainText(str(v or "")))
        self._attach_reset(key, self._page.row(label, w, help_text, resettable=key in backend.DEFAULT_CONFIG), w.textChanged)
        return w

    def _number(self, key, label, lo, hi, step, decimals=None, suffix="", help_text="") -> QWidget:
        if decimals is None:
            w = QSpinBox()
            w.setRange(int(lo), int(hi))
            w.setSingleStep(int(step))
            self._register(key, w.value, lambda v: w.setValue(int(float(v))), w.valueChanged)
        else:
            w = QDoubleSpinBox()
            w.setRange(lo, hi)
            w.setDecimals(decimals)
            w.setSingleStep(step)
            self._register(key, w.value, lambda v: w.setValue(float(v)), w.valueChanged)
        if suffix:
            w.setSuffix(suffix)
        w.setMaximumWidth(190)
        self._attach_reset(key, self._page.row(label, w, help_text, resettable=key in backend.DEFAULT_CONFIG))
        return w

    def _check(self, key, text, help_text="", keywords="") -> QCheckBox:
        w = QCheckBox(text)
        self._register(key, w.isChecked, lambda v: w.setChecked(backend.cfg_bool_value(v)), w.toggled)
        self._attach_reset(key, self._page.check(w, help_text, keywords, resettable=key in backend.DEFAULT_CONFIG))
        return w

    def _choice(self, key, label, options, editable=False, help_text="", keywords="") -> QComboBox:
        """options: list of (value, label) pairs, or plain strings when value == label."""
        w = QComboBox()
        w.setEditable(editable)
        for opt in options:
            value, text = opt if isinstance(opt, tuple) else (opt, opt)
            w.addItem(text, value)

        def get():
            if editable:
                text = w.currentText().strip()
                idx = w.findText(text)
                return w.itemData(idx) if idx >= 0 else text
            return w.currentData()

        def set_(v):
            idx = w.findData(v)
            if idx >= 0:
                w.setCurrentIndex(idx)
            elif editable:
                w.setCurrentText("" if v is None else str(v))
            else:
                w.setCurrentIndex(0)

        self._register(key, get, set_, w.currentIndexChanged)
        self._attach_reset(key, self._page.row(label, w, help_text, keywords, resettable=key in backend.DEFAULT_CONFIG))
        return w

    # ---- per-row "reset to default" ------------------------------------------------------------------
    def _is_default(self, key: str) -> bool:
        default, current = backend.DEFAULT_CONFIG.get(key), self._get[key]()
        if isinstance(default, bool):
            return backend.cfg_bool_value(current) == default
        if isinstance(default, (int, float)):
            try:
                return abs(float(current) - float(default)) < 1e-9
            except (TypeError, ValueError):
                return False
        return str(current or "").strip() == str(default or "").strip()

    def _attach_reset(self, key: str, row, *also) -> None:
        """Wire the row's small reset button: visible only while the setting differs from its default."""
        button = row.reset
        if button is None:
            return
        self._rows_by_key[key] = row

        def refresh(*_args) -> None:
            button.setVisible(not self._is_default(key))

        def reset() -> None:
            self._set[key](backend.DEFAULT_CONFIG[key])
            refresh()

        button.clicked.connect(reset)
        for signal in (*self._signals.get(key, ()), *also):
            signal.connect(refresh)
        refresh()

    def _buttons(self, label: str, *buttons, help_text: str = "", keywords: str = ""):
        """A row of buttons that keep their natural width instead of stretching across the column."""
        box = QWidget()
        line = QHBoxLayout(box)
        line.setContentsMargins(0, 0, 0, 0)
        for button in buttons:
            line.addWidget(button)
        line.addStretch(1)
        return self._page.row(label, box, help_text, keywords or " ".join(b.text() for b in buttons))

    def _section(self, title: str) -> None:
        self._page.section(title)

    def _note(self, text: str) -> None:
        self._page.note(text)

    # ---- greying out what doesn't apply ------------------------------------------------------------
    @staticmethod
    def _row_of(widget: QWidget) -> QWidget:
        """The whole row a control sits in, so its label and help text grey out with it."""
        w = widget
        while w is not None and not isinstance(w, Row):
            w = w.parentWidget()
        return w or widget

    def _enable_when(self, condition, widgets, *signals):
        """Grey out `widgets` (their whole rows) while `condition()` is False; checked again whenever
        one of `signals` fires. Returns the check, for callers that need to run it themselves."""
        def sync(*_args) -> None:
            on = bool(condition())
            for w in widgets:
                row = self._row_of(w)
                row.setEnabled(on)
                # The disabled colors alone are subtle (help text is already muted): fade the whole row.
                if on:
                    row.setGraphicsEffect(None)
                elif row.graphicsEffect() is None:
                    fade = QGraphicsOpacityEffect(row)
                    fade.setOpacity(DISABLED_OPACITY)
                    row.setGraphicsEffect(fade)
        for signal in signals:
            signal.connect(sync)
        sync()
        return sync

    # ================================================================================================
    # Pages
    # ================================================================================================
    def _build_general(self) -> None:
        p = self._new_page("general", "General", "⚙", "Our names, how I start, and where you are.")
        self._section("Name")
        self._text("assistant_name", "Assistant name", "Nova",
                   "Shown in the window and used when I talk about myself. You can also say \"call yourself Jarvis\".")
        self._text("user_name", "Your name", "What should I call you?",
                   "I use it now and then, and greet you with it. You can also say \"call me\" and your name.")
        self._section("Startup")
        autostart = QCheckBox("Start automatically when I sign in to Windows")
        self._register("start_with_windows", autostart.isChecked,
                       lambda v: autostart.setChecked(startup.is_enabled()))  # the registry is the truth
        import shortcuts
        for key, kind, text in (("start_menu_shortcut", "start_menu", "Put me in the Start menu"),
                                ("desktop_shortcut", "desktop", "Put a shortcut to me on the desktop")):
            box = QCheckBox(text)
            self._register(key, box.isChecked, lambda v, b=box, k=kind: b.setChecked(shortcuts.exists(k)))
            p.check(box, keywords="shortcut start menu desktop icon launch")
        p.check(autostart)
        self._check("start_minimized", "Start hidden",
                    "Only the tray icon and the status bar show until you open the window.")

        self._section("Location and units")
        place = self._text("location_override", "Location", "e.g. Seattle", "A town or city, used for the weather.")
        from_ip = self._check("location_from_ip", "When no location is set, work it out from my internet connection",
                              "Checked once at startup. Otherwise, name a city when you ask about the weather.")
        self._enable_when(lambda: not place.text().strip(), [from_ip], place.textChanged)
        self._choice("temperature_unit", "Temperature",
                     [("auto", "Automatic"), ("celsius", "Celsius"), ("fahrenheit", "Fahrenheit")],
                     help_text="Automatic picks what's usual where you are.")
        self._choice("time_format", "Clock", [("12h", "12-hour"), ("24h", "24-hour")])

        self._section("Maintenance")
        tour = QPushButton("Run the welcome tour again")
        tour.setToolTip("Closes Settings (unsaved changes are discarded) and opens the first-run walkthrough.")
        tour.clicked.connect(self._rerun_onboarding)
        log_btn = QPushButton("Open the log file")
        log_btn.clicked.connect(lambda: self._open_path(neon_log.LOG_PATH))
        data_btn = QPushButton("Open the data folder")
        data_btn.clicked.connect(lambda: self._open_path(app_paths.DATA_DIR))
        export_btn = QPushButton("Export settings...")
        export_btn.clicked.connect(self._export_settings)
        import_btn = QPushButton("Import settings...")
        import_btn.clicked.connect(self._import_settings)
        self._buttons("Welcome tour", tour, keywords="tour onboarding")
        self._buttons("Backup", export_btn, import_btn, keywords="backup export import")
        self._buttons("Troubleshooting", log_btn, data_btn, keywords="log folder data")
        self.status_note = QLabel("")
        self.status_note.setObjectName("muted")
        self.status_note.setWordWrap(True)
        p.row("", self.status_note)

    # ---- appearance (instant) ----------------------------------------------------------------------
    def _build_appearance(self) -> None:
        p = self._new_page("appearance", "Appearance", "\U0001f3a8",
                           "Theme and colors. Everything changes as you click.", live=True)
        self._section("Theme")
        combo = QComboBox()
        for key, label in theme_names():
            combo.addItem(label, key)
        p.row("Theme", combo)

        swatch = QLabel()
        swatch.setFixedSize(34, 22)
        pick, reset = QPushButton("Choose..."), QPushButton("Theme default")
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        for w in (swatch, pick, reset):
            h.addWidget(w)
        h.addStretch(1)
        p.row("Accent color", row, "Tints buttons, highlights and my chat bubbles in any theme.")
        accent = {"value": ""}

        overrides: dict[str, str] = {}
        state_swatches: dict[str, QLabel] = {}

        def show_accent() -> None:
            shown = accent["value"] or COLORS["accent"]
            swatch.setStyleSheet(f"background: {shown}; border: 1px solid {COLORS['border']}; border-radius: 6px;")
            swatch.setToolTip(accent["value"] or "the theme's own accent")

        def show_states() -> None:
            for key, sw in state_swatches.items():
                sw.setStyleSheet(f"background: {STATE_COLORS[key]}; border: 1px solid {COLORS['border']}; "
                                 "border-radius: 6px;")
                sw.setToolTip(overrides.get(key) or "the theme's own color")

        def restyle() -> None:
            show_accent()
            show_states()
        self._refresh_state_swatches = restyle           # re-run on every theme change

        def changed(*keys: str) -> None:
            apply_theme(combo.currentData(), accent["value"], overrides)
            for k in keys or ("theme", "theme_accent", "state_colors"):
                self._live_changed(k)

        self._register("theme", combo.currentData, lambda v: combo.setCurrentIndex(max(0, combo.findData(v))))
        self._register("theme_accent", lambda: accent["value"],
                       lambda v: (accent.update(value=str(v or "").strip()), show_accent()))
        self._live_keys.update(("theme", "theme_accent", "state_colors"))
        combo.currentIndexChanged.connect(lambda _i: changed("theme"))
        follow = self._check("follow_high_contrast", "Switch to high contrast when Windows does",
                             "Your theme comes back when Windows' contrast themes are turned off.",
                             keywords="accessibility contrast screen reader")

        def follow_changed(on: bool) -> None:
            from . import theme as theme_module
            theme_module.FOLLOW["high_contrast"] = bool(on)
            apply_theme(combo.currentData(), accent["value"], overrides)
        follow.toggled.connect(follow_changed)

        self._check("custom_titlebar", "Use NEON's own title bar on its windows",
                    "In the theme's colors. Off gives Windows' usual title bar. Snapping, resizing and "
                    "double-click to maximize work either way.", keywords="title bar frame window chrome native")

        def choose() -> None:
            picked = QColorDialog.getColor(QColor(accent["value"] or COLORS["accent"]), self, "Accent color")
            if picked.isValid():
                accent["value"] = picked.name()
                changed("theme_accent")

        def clear() -> None:
            accent["value"] = ""
            changed("theme_accent")
        pick.clicked.connect(choose)
        reset.clicked.connect(clear)

        self._section("Status colors")
        for key, label in STATE_NAMES:
            sw = QLabel()
            sw.setFixedSize(34, 22)
            state_swatches[key] = sw
            choose_btn, reset_btn = QPushButton("Choose..."), QPushButton("Reset")
            line = QWidget()
            lh = QHBoxLayout(line)
            lh.setContentsMargins(0, 0, 0, 0)
            for w in (sw, choose_btn, reset_btn):
                lh.addWidget(w)
            lh.addStretch(1)
            p.row(label, line)

            def choose_state(_=False, key=key, label=label) -> None:
                picked = QColorDialog.getColor(QColor(STATE_COLORS[key]), self, f"{label} color")
                if picked.isValid():
                    overrides[key] = picked.name()
                    changed("state_colors")

            def reset_state(_=False, key=key) -> None:
                overrides.pop(key, None)
                changed("state_colors")
            choose_btn.clicked.connect(choose_state)
            reset_btn.clicked.connect(reset_state)
        self._note("The orb, the status dot and the edge of the status bar use these colors.")

        def set_states(value) -> None:
            overrides.clear()
            if isinstance(value, dict):
                overrides.update({k: str(v) for k, v in value.items()
                                  if k in state_swatches and v and QColor(str(v)).isValid()})
            show_states()
        self._register("state_colors", lambda: dict(overrides), set_states)
        restyle()

    # ---- status bar (instant) ----------------------------------------------------------------------
    def _build_statusbar(self) -> None:
        self._new_page("statusbar", "Status bar", "\U0001f4ca",
                       "The slim bar along the edge of your screen: where it sits and what it shows.", live=True)
        self._section("Position")
        show = self._check("show_status_bar", "Show the status bar")
        everything = [self._choice("bar_position", "Edge of the screen", [("top", "Top"), ("bottom", "Bottom")])]
        monitors = list_monitors()
        options = [(i, f"Monitor {i + 1}{', the main one' if i == 0 else ''}  -  {m[2] - m[0]} x {m[3] - m[1]}")
                   for i, m in enumerate(monitors)] or [(0, "Main monitor")]
        mon = self._choice("bar_monitor", "Monitor", options, help_text="Only connected monitors are listed.")
        mon.setEnabled(len(options) > 1)
        everything += [mon, self._number("bar_height", "Height", 30, 80, 2, None, " px"),
                       self._number("bar_text_size", "Text size", 10, 20, 1, None, " px")]

        self._section("Shade")
        shade = self._check("shade_enabled", "Use the shade",
                            "While the status bar is off or another app is fullscreen, my replies drop down from "
                            "the top of the screen in large text. Clicks go straight through it.",
                            keywords="shade overlay fullscreen banner")
        shade_parts = [self._check("shade_notifications", "Show notifications in it too"),
                       self._number("shade_seconds", "Keep it down for", 1.0, 60.0, 0.5, 1, " s",
                                    "After I finish speaking.")]
        preview = QPushButton("Show the shade")
        preview.clicked.connect(self._controller.shade_preview.emit)
        self._buttons("", preview)
        shade_parts.append(preview)
        self._enable_when(shade.isChecked, shade_parts, shade.toggled)

        self._section("Status")
        everything += [self._check("bar_show_orb", "Status orb"),
                       self._check("bar_show_state", "What I'm doing", "Listening, Thinking and so on."),
                       self._check("bar_show_wave", "Waveform while I speak")]
        self.bar_spectrum = self._check("bar_show_spectrum", "Voice spectrum",
                                        "A live graph of my voice while I speak. Piper voices only.")
        everything += [self._check("bar_show_persona", "Persona badge", "Shows which persona I'm using, "
                                                                        "unless it's the usual one.")]

        self._section("Captions")
        caption = self._check("bar_show_caption", "Show what you said and my reply")
        animate = self._check("bar_animate", "Roll captions in and out")
        follow_os = self._check("follow_os_animations", "Not when Windows' animation effects are off")
        linger = self._number("bar_caption_seconds", "Keep captions up for", 0.0, 30.0, 0.5, 1, " s")
        everything.append(caption)

        self._section("Clock and weather")
        clock = self._check("bar_show_clock", "Clock")
        clock_parts = [self._choice("bar_clock_format", "Clock format",
                                    [("auto", "Same as General"), ("12h", "12-hour"), ("24h", "24-hour")]),
                       self._check("bar_clock_seconds", "Show seconds"),
                       self._check("bar_clock_date", "Show the date")]
        weather = self._check("bar_show_weather", "Weather", "The current temperature where you are.")
        weather_text = self._check("bar_weather_text", "Include the conditions, like \"Overcast\"")
        everything += [clock, weather]

        self._section("Widgets")
        everything += [
            self._check("bar_show_music", "Now playing",
                        "Artist and title from Pear Desktop, Spotify or your browser. Click it to play or pause."),
            self._check("bar_show_timer", "Timer countdown",
                        "Your next timer. It turns red in the last minute; right-click it to cancel your timers."),
            self._check("bar_show_stopwatch", "Stopwatch", "Click to start or pause, right-click to reset."),
            self._check("bar_show_cpu", "Processor load"),
            self._check("bar_show_ram", "Memory use"),
            self._check("bar_show_battery", "Battery", "Laptops only.")]
        self.bar_calendar = self._check("bar_show_calendar", "Next calendar event",
                                        "Add your calendar under Board & calendar first.")

        self._section("Buttons")
        for key, label in (("bar_show_talk", "Talk"), ("bar_show_stop", "Stop"), ("bar_show_wake", "Wake word on / off"),
                           ("bar_show_mute", "Mute"), ("bar_show_window", "Open the window"),
                           ("bar_show_settings", "Settings gear")):
            everything.append(self._check(key, label))
        self._note("Without the gear you can still open Settings from the tray icon or the main window.")

        on = show.isChecked
        self._enable_when(on, everything, show.toggled)
        self._enable_when(lambda: on() and caption.isChecked(), [animate, linger], show.toggled, caption.toggled)
        self._enable_when(lambda: on() and caption.isChecked() and animate.isChecked(), [follow_os],
                          show.toggled, caption.toggled, animate.toggled)
        self._enable_when(lambda: on() and clock.isChecked(), clock_parts, show.toggled, clock.toggled)
        self._enable_when(lambda: on() and weather.isChecked(), [weather_text], show.toggled, weather.toggled)
        self._bar_on = on                                # the spectrum and calendar rows also depend on other pages

    # ---- voice & sounds (instant) --------------------------------------------------------------------
    def _build_voice(self) -> None:
        p = self._new_page("voice", "Voice & sounds", "\U0001f50a",
                           "How I sound, and what you hear while I work on an answer.", live=True)
        self._section("Voice")
        engine = self._choice("tts_engine", "Voice engine", [("piper", "Piper"), ("sapi", "Windows")],
                              help_text="Piper voices sound natural and work offline. Windows' own voices need "
                                        "no download.")
        self.tts_engine = engine
        test = QPushButton("Test voice")
        test.clicked.connect(self._controller.test_voice)
        self._buttons("", test)

        self._section("Piper voice")
        voice = PiperVoicePicker(str(backend.CONFIG.get("tts_voice") or ""))
        self._register("tts_voice", voice.value, voice.set_value, voice.changed)

        def check_voice():
            name = voice.value()
            if voice.is_custom() and not name:
                return " "                       # still typing: don't fall back to the default voice meanwhile
            if name and engine.currentData() == "piper" and not tts.valid_voice_name(name):
                return f"'{name}' isn't a Piper voice name. They look like en_US-amy-medium."
            return None
        self._validators["tts_voice"] = check_voice
        p.row("Voice", voice)
        piper = [voice,
                 self._number("tts_speed", "Speed", 0.5, 2.0, 0.05, 2, "x"),
                 self._number("tts_volume", "Volume", 0.0, 2.0, 0.05, 2),
                 self._number("tts_noise", "Expressiveness", 0.0, 1.5, 0.05, 2, help_text="Lower sounds steadier."),
                 self._number("tts_noise_w", "Rhythm", 0.0, 1.5, 0.05, 2, help_text="Lower gives more even pacing.")]

        self._section("Windows voice")
        sapi = [("", DEFAULT_LABEL)] + [(n, n) for n in backend.list_sapi_voices()]
        windows = [self._choice("sapi_voice", "Voice", sapi),
                   self._number("sapi_rate", "Speed", 80, 320, 5, None, " words/min")]
        self._enable_when(lambda: engine.currentData() == "piper", piper, engine.currentIndexChanged)
        self._enable_when(lambda: engine.currentData() == "sapi", windows, engine.currentIndexChanged)

        self._section("Speaking")
        outs = [("", DEFAULT_LABEL)] + [(n, n) for n in backend.list_output_devices()]
        self._choice("output_device", "Play through", outs)
        self._check("stream_replies", "Start speaking before the whole answer is ready",
                    "Long answers begin sooner.")

        self._section("While I think")
        kind = self._choice("ack_type", "Let you know I heard you",
                            [("sound", "Play a sound"), ("speech", "Say a short phrase"), ("off", "Don't")])
        sound = SoundPicker(volume_fn=lambda: float(self._get["ack_volume"]()),
                            device_fn=lambda: self._get["output_device"]() or None)
        self._register("ack_sound", sound.name, sound.set_name)
        self._register("ack_sound_file", sound.file, sound.set_file)
        p.row("Sound", sound, "Choosing one plays it. You can also use your own .wav file.")
        self._number("ack_volume", "Sound volume", 0.0, 1.0, 0.05, 2,
                     help_text="Notification sounds use this volume too.")
        text = self._text("ack_text", "Phrase", "Thinking...")
        preview = QPushButton("Say it")
        preview.clicked.connect(lambda: self._controller.say_preview(self._get["ack_text"]()))
        self._buttons("", preview)
        delay = self._number("ack_delay", "Wait before", 0.0, 5.0, 0.25, 2, " s",
                             "Above 0, it only plays when an answer is slow.")
        mode = kind.currentData
        self._enable_when(lambda: mode() == "sound", [sound], kind.currentIndexChanged)
        self._enable_when(lambda: mode() == "speech", [text, preview], kind.currentIndexChanged)
        self._enable_when(lambda: mode() != "off", [delay], kind.currentIndexChanged)

        slow = self._check("thinking_lines_enabled", "Say something when an answer is slow",
                           "One of the lines below, picked at random, so you know I'm still on it.",
                           keywords="filler flavor text thinking waiting slow")
        after = self._number("thinking_after", "After", 0.5, 30.0, 0.5, 1, " s")
        lines = QPlainTextEdit()
        lines.setMinimumHeight(110)
        lines.setPlaceholderText("Hmm, let me think.\nOne moment.")
        self._register("thinking_lines",
                       lambda: [x.strip() for x in lines.toPlainText().splitlines() if x.strip()],
                       lambda v: lines.setPlainText("\n".join(v) if isinstance(v, (list, tuple)) else str(v or "")),
                       lines.textChanged)
        self._attach_reset("thinking_lines", p.row("Lines to say", lines, "One per line.", "filler flavor text",
                                                   resettable=True), lines.textChanged)
        self._enable_when(slow.isChecked, [after, lines], slow.toggled)

        self._section("When I start listening")
        listen = SoundPicker(allow_off=True, volume_fn=lambda: float(self._get["listen_sound_volume"]()),
                             device_fn=lambda: self._get["output_device"]() or None)
        self._register("listen_sound", listen.name, listen.set_name, listen.combo.currentIndexChanged)
        self._register("listen_sound_file", listen.file, listen.set_file, listen.path.editingFinished)
        p.row("Sound", listen, "Plays as the microphone opens for your command: after the wake word, the talk "
                               "button or a hotkey.", "listening wake beep chime")
        listen_volume = self._number("listen_sound_volume", "Sound volume", 0.0, 1.0, 0.05, 2)
        self._enable_when(lambda: listen.name() != "off", [listen_volume], listen.combo.currentIndexChanged)

    # ---- listening ------------------------------------------------------------------------------------
    def _build_listening(self) -> None:
        self._new_page("listening", "Listening", "\U0001f3a4",
                       "Your microphone, the wake word, and how I understand what you say.")
        self._section("Microphone")
        mics = [("", DEFAULT_LABEL)] + [(n, n) for n in backend.list_input_devices()]
        mic = self._choice("mic_device", "Microphone", mics,
                           help_text="Changing it re-measures the room when you save.")
        mix_note = QLabel("This looks like a mix (a stream or chat mix), which usually carries game sound and music "
                          "too. Picking your microphone itself makes me understand you better.")
        mix_note.setObjectName("muted")
        mix_note.setWordWrap(True)
        mix_row = self._page.row("", mix_note)
        def sync_mix(*_args) -> None:                  # only shown when it applies (search included)
            mix_row.suppressed = " mix" not in f" {str(mic.currentData() or '').lower()}"
            mix_row.setVisible(not mix_row.suppressed)
        mic.currentIndexChanged.connect(sync_mix)
        sync_mix()
        voice = self._check("vad_enabled", "Tell my voice apart from background sound",
                            "Music, games and fans don't count as talking, and a quiet microphone still works. "
                            "Off: anything loud enough counts.", keywords="vad voice activity detection music noise")
        self._check("mic_auto_gain", "Raise a quiet microphone",
                    "Quiet recordings are turned up before I work out what you said, and for the wake word.",
                    keywords="gain volume quiet boost level")
        sensitivity = self._number("energy_multiplier", "Sensitivity", 1.0, 10.0, 0.25, 2,
                                   help_text="How much louder than the room counts as speech. Lower is more "
                                             "sensitive.")
        adapt = self._check("adaptive_threshold", "Keep adjusting to the room's noise",
                            "So a fan switching on doesn't throw the sensitivity off.")
        self._enable_when(lambda: not voice.isChecked(), [sensitivity, adapt], voice.toggled)
        self._check("mute_mic_while_speaking", "Ignore the microphone while I'm speaking")

        self._section("Speech recognition")
        stt_engine = self._choice("stt_engine", "Recognize speech", [("whisper", "On this PC"), ("google", "With Google")],
                                  help_text="On this PC works without internet and your voice never leaves it. "
                                            "Google sends the audio of each command to Google.",
                                  keywords="whisper google offline online")
        if not stt.whisper_available():
            self._note("Recognition on this PC isn't installed yet. To add it, run: pip install faster-whisper")
        sizes = [("tiny.en", "Fastest  -  75 MB download"), ("base.en", "Balanced  -  145 MB download"),
                 ("small.en", "Most accurate  -  480 MB download")]
        local = [self._choice("whisper_model", "Accuracy", sizes, help_text="Downloaded the first time it's used.",
                              keywords="whisper model")]
        gpu = stt.cuda_available()
        local.append(self._choice("whisper_device", "Run it on",
                                  [("auto", "Automatic"), ("cpu", "The processor"), ("cuda", "The graphics card")],
                                  help_text=("Your NVIDIA graphics card is several times faster than the processor."
                                             if gpu else "No supported graphics card was found, so the processor "
                                                         "is used."),
                                  keywords="gpu cuda nvidia"))
        local.append(self._check("stt_partials", "Show my words in the status bar while I'm still speaking"))
        local.append(self._check("stt_online_fallback", "Use Google if recognition on this PC stops working",
                                 "Otherwise nothing is sent online and I tell you what's wrong."))
        on_pc = lambda: stt_engine.currentData() == "whisper"      # noqa: E731
        self._enable_when(on_pc, local, stt_engine.currentIndexChanged)

        self._section("Wake word")
        engine = self._choice("wake_engine", "Listen for it with",
                              [("openwakeword", "A trained phrase"), ("stt", "Speech recognition")],
                              help_text="A trained phrase uses very little of the processor and works offline. "
                                        "Speech recognition can use any phrase, including my name, but has to "
                                        "transcribe everything it hears.",
                              keywords="openwakeword hey nova hey dan train custom")
        model = self._choice("wake_model", "Phrase", self._wake_model_options(),
                             help_text="\"Hey nova\" and \"hey dan\" set themselves up the first time they're used, "
                                       "which takes a few minutes. Training with your own voice makes any phrase "
                                       "more reliable.",
                             keywords="hey nova hey dan jarvis alexa custom")
        self.wake_model_combo = model
        threshold = self._number("wake_threshold", "How sure it must be", 0.1, 0.95, 0.05, 2,
                                 help_text="Raise it if I wake by accident; lower it if I miss you.")
        train = QPushButton("Train a wake word...")
        train.clicked.connect(self._open_wake_trainer)
        delete = QPushButton("Delete this phrase")
        delete.setToolTip("Removes a phrase you trained. A ready-made one can be retrained any time.")
        delete.clicked.connect(self._delete_wake_model)
        self._buttons("", train, delete, keywords="train record custom wake word delete")
        self.wake_note = QLabel("")
        self.wake_note.setObjectName("muted")
        self.wake_note.setWordWrap(True)
        self._page.row("", self.wake_note)

        by_name = self._check("name_is_wake_word", "Use my name as the wake word")
        word = self._text("wake_word", "Wake word", help_text="What you say to get my attention hands-free.")
        wake_size = self._choice("wake_whisper_model", "Recognition for the wake word",
                                 [("", "Same as for commands")] + [(k, t.split("  -  ")[0]) for k, t in sizes],
                                 help_text="A faster setting here leaves more of the processor free.")
        noise = self._check("wake_skip_noise", "Ignore steady background noise",
                            "Fans, hum and rain aren't sent for recognition, which saves effort and stops "
                            "made-up words.")
        longest = self._number("wake_phrase_limit", "Longest wake phrase", 2.0, 30.0, 1.0, 0, " s")

        detector = lambda: engine.currentData() == "openwakeword"      # noqa: E731
        changed = (engine.currentIndexChanged,)
        self._enable_when(detector, [model, threshold], *changed)
        engine.currentIndexChanged.connect(lambda _i: delete.setEnabled(detector()))
        delete.setEnabled(detector())
        self._enable_when(lambda: not detector(), [by_name, noise, longest], *changed)
        self._enable_when(lambda: not detector() and not by_name.isChecked(), [word], *changed, by_name.toggled)
        self._enable_when(lambda: not detector() and on_pc(), [wake_size], *changed, stt_engine.currentIndexChanged)
        engine.currentIndexChanged.connect(lambda _i: self._describe_wake_model())
        model.currentIndexChanged.connect(lambda _i: self._describe_wake_model())
        self._describe_wake_model()

        self._section("Follow-ups")
        talk_back = self._check("conversation_mode", "Keep listening for a moment after each reply",
                                "So a follow-up like \"and tomorrow?\" needs no wake word. Say nothing and I go "
                                "quiet again.")
        window = self._number("conversation_seconds", "Listen for", 2.0, 30.0, 0.5, 1, " s")
        self._enable_when(talk_back.isChecked, [window], talk_back.toggled)

        self._section("Timing")
        self._number("listen_timeout", "Wait for you to start speaking", 1.0, 30.0, 0.5, 1, " s")
        self._number("silence_duration", "Pause that ends a command", 0.3, 4.0, 0.1, 1, " s")
        self._check("fast_endpoint", "Respond sooner after short commands")
        self._number("phrase_time_limit", "Longest command", 3.0, 60.0, 1.0, 0, " s")

    @staticmethod
    def _wake_model_options() -> list[tuple[str, str]]:
        have_oww = wakeword.available()
        options = []
        for key, phrase, kind in wakeword.list_models():
            suffix = {"custom": "trained on this PC", "preset": "sets itself up when first used",
                      "builtin": "ready" if have_oww else "not installed"}[kind]
            options.append((key, f"\"{phrase}\"  -  {suffix}"))
        return options

    def _describe_wake_model(self) -> None:
        key = self.wake_model_combo.currentData() or ""
        text = ""
        if wakeword.is_custom(key):
            info = wakeword.custom_info(key)
            if info:
                text = (f"Trained {info.get('created', '').replace('T', ' ')[:16]} from {info.get('recordings', 0)} "
                        f"of your recordings.")
            else:
                text = "Not trained yet. It trains itself the first time it's used, or train it now with your voice."
        elif not wakeword.available():
            text = ("This phrase needs openWakeWord, which isn't installed (pip install openwakeword). "
                    "Until then I use speech recognition.")
        self.wake_note.setText(text)

    def _refresh_wake_models(self, select: str | None = None) -> None:
        combo = self.wake_model_combo
        current = select or combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for value, label in self._wake_model_options():
            combo.addItem(label, value)
        index = combo.findData(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)
        self._describe_wake_model()

    def _open_wake_trainer(self) -> None:
        from .wake_trainer import WakeTrainerDialog
        key = self.wake_model_combo.currentData() or ""
        phrase = wakeword.phrase_of(key) if wakeword.is_custom(key) else ""
        dialog = WakeTrainerDialog(self._controller, phrase, self)
        dialog.exec()
        if dialog.report:
            self._refresh_wake_models(dialog.report["model"])
            self._set["wake_engine"]("openwakeword")     # takes effect on Save, like the rest of this page
            self._controller.reload_wake_word()           # (or now, if it's the phrase already in use)

    def _delete_wake_model(self) -> None:
        key = self.wake_model_combo.currentData() or ""
        if not wakeword.trained(key):
            self.wake_note.setText("Only a phrase trained on this PC can be deleted.")
            return
        wakeword.delete_custom(key)
        self._refresh_wake_models(key)

    # ---- persona (instant) ----------------------------------------------------------------------------
    def _build_persona(self) -> None:
        p = self._new_page("persona", "Persona", "\U0001f3ad",
                           "How I word things. Only the phrasing changes: every command works the same.", live=True)
        self._section("Character")
        options = [(key, f"{persona.PERSONAS[key]['label']}  -  {persona.PERSONAS[key]['blurb']}")
                   for key in persona.ORDER]
        combo = self._choice("persona", "Persona", options,
                             help_text="You can also say \"be a pirate\" or \"act normal\".",
                             keywords="pirate shakespeare noir coach robot zen butler gremlin haiku voice character")
        sample = QLabel(persona.sample(backend.CONFIG.get("persona", "default")))
        sample.setObjectName("muted")
        sample.setWordWrap(True)
        combo.currentIndexChanged.connect(lambda _i: sample.setText(persona.sample(combo.currentData())))
        p.row("Sounds like", sample, keywords="sample preview example")
        speak = QPushButton("Hear it")
        speak.clicked.connect(lambda: self._controller.preview_persona(combo.currentData()))
        self._buttons("", speak)
        self._note("Personas change how the AI words its answers, so they need the AI running. Fixed replies "
                   "like \"Opening Chrome.\" stay the same.")

        self._section("Persona voices")
        auto = self._check("persona_auto_voice", "Give each persona a voice that suits it",
                           "Picks from the Piper voices you've installed, like a British one for the butler. "
                           "Add more under Voice & sounds.")
        installed = tts.list_installed_voices()
        voice_pick = QComboBox()
        voice_pick.addItem("Automatic", "")
        for name in installed:
            voice_pick.addItem(name, name)
        chosen = {"map": dict(backend.CONFIG.get("persona_voices") or {})}

        def show_choice() -> None:
            voice_pick.blockSignals(True)
            index = voice_pick.findData(chosen["map"].get(combo.currentData(), ""))
            voice_pick.setCurrentIndex(max(0, index))
            voice_pick.blockSignals(False)

        def picked() -> None:
            key = combo.currentData()
            value = voice_pick.currentData() or ""
            if value:
                chosen["map"][key] = value
            else:
                chosen["map"].pop(key, None)

        voice_pick.currentIndexChanged.connect(lambda _i: picked())
        combo.currentIndexChanged.connect(lambda _i: show_choice())
        self._register("persona_voices", lambda: dict(chosen["map"]),
                       lambda v: (chosen.update(map=dict(v or {})), show_choice()), voice_pick.currentIndexChanged)
        p.row("Voice for this persona", voice_pick, "For the persona chosen above.", keywords="persona voice piper")
        engine = self.tts_engine
        self._enable_when(lambda: engine.currentData() == "piper", [auto, voice_pick], engine.currentIndexChanged)

        self._section("Fun")
        self._check("fun_enabled", "Dice, coins, jokes and nonsense",
                    "\"Flip a coin\", \"roll a d20\", \"tell me a joke\", \"do a barrel roll\".")

    # ---- memory ---------------------------------------------------------------------------------------
    def _build_memory(self) -> None:
        p = self._new_page("memory", "Memory", "\U0001f4dd", "What I remember for you, and what you've copied.")
        self._section("Long-term memory")
        enabled = self._check("memory_enabled", "Remember things you ask me to",
                              "Say \"remember that my gate code is 4821\", then ask \"what's my gate code?\". "
                              "Memories stay on this PC.")
        in_prompt = self._check("memory_in_prompt", "Let the AI use them when answering",
                                "So \"what should I cook?\" knows you're vegetarian.")
        meaning = self._check("memory_semantic", "Match memories by meaning, not just by words",
                              "Finds \"the wifi password\" when you ask \"how do I get online?\". Needs one extra "
                              "Ollama model: ollama pull nomic-embed-text")
        model = self._text("memory_embed_model", "Model for matching", "nomic-embed-text")
        self._enable_when(enabled.isChecked, [in_prompt, meaning], enabled.toggled)
        self._enable_when(lambda: enabled.isChecked() and meaning.isChecked(), [model], enabled.toggled,
                          meaning.toggled)
        self.memory_list = MemoryList(memory_store)
        p.row("Remembered", self.memory_list, full_width=True, keywords="memory browser edit expiry forget")

        self._section("Clipboard")
        self._check("clipboard_history", "Keep a short clipboard history",
                    "The last 25 things you copied, kept only while I'm running and never saved. Ask \"what did "
                    "I copy?\". Anything that looks like a password is never read aloud.")
        clear_clip = QPushButton("Clear the clipboard history")
        clear_clip.clicked.connect(lambda: clear_clip.setText(clipboard.clear()))
        self._buttons("", clear_clip)

    # ---- board & calendar -----------------------------------------------------------------------------
    def _build_board(self) -> None:
        p = self._new_page("board", "Board & calendar", "\U0001f5c2",
                           "Your board of cards with due dates, and your calendar's events.")
        self._section("Board")
        board = self._check("board_enabled", "Use the board",
                            "Columns of cards with due dates. Open it with the \U0001f5c2 button or say \"open my board\".")
        scroll = self._check("board_scroll_sideways", "Scroll sideways with the mouse wheel",
                             "Hold Shift to scroll a column's cards instead.")
        remind = self._check("board_reminders", "Remind me when a card is due",
                             "Once a day, for every card due or overdue that isn't in a \"Done\" column.")
        remind_at = QTimeEdit()
        remind_at.setDisplayFormat("HH:mm")
        remind_at.setMaximumWidth(140)
        self._register("board_remind_time", lambda: remind_at.time().toString("HH:mm"),
                       lambda v: remind_at.setTime(QTime.fromString(str(v or "09:00"), "HH:mm")))
        p.row("Remind me at", remind_at, "If I start later than this, you're told when I start.",
              "board due date reminder time")
        self._enable_when(board.isChecked, [scroll, remind], board.toggled)
        self._enable_when(lambda: board.isChecked() and remind.isChecked(), [remind_at], board.toggled, remind.toggled)

        self._section("Calendar")
        ics = self._text("calendar_ics", "Calendar address", "https://... or a .ics file",
                         "Paste your calendar's iCal address. Google Calendar calls it the \"secret address in "
                         "iCal format\"; Outlook calls it a published calendar link. A saved .ics file works too.",
                         keywords="google outlook ical ics")
        self.calendar_ics = ics
        alerts = self._check("calendar_alerts", "Remind me before events",
                             "A card, a sound and a spoken heads-up.")
        lead = self._number("calendar_alert_minutes", "How early", 1, 120, 1, 0, " min")
        has_calendar = lambda: bool(ics.text().strip())      # noqa: E731
        self._enable_when(has_calendar, [alerts], ics.textChanged)
        self._enable_when(lambda: has_calendar() and alerts.isChecked(), [lead], ics.textChanged, alerts.toggled)

    # ---- routines ---------------------------------------------------------------------------------------
    def _build_routines(self) -> None:
        p = self._new_page("routines", "Routines", "⚡",
                           "One phrase, several things. Say the routine's name, like \"work mode\", or \"run\" "
                           "and the name.")
        self._section("Your routines")
        editor = RoutinesEditor()
        self._register("routines", editor.value, editor.set_value, editor.changed)
        p.row("", editor, full_width=True, keywords="routine macro scene work mode steps keys hotkey")
        self._note("Each line is one step: anything you'd say to me, like \"pause the music\" or \"set the volume "
                   "to 20 percent\". A step can also start with open: (an app), url: (a website), run: (a program), "
                   "keys: (a key combination, like ctrl+shift+esc), wait: (seconds) or say: (something to say).")

    # ---- plugins ----------------------------------------------------------------------------------------
    def _build_plugins(self) -> None:
        p = self._new_page("plugins", "Plugins", "\U0001f9e9",
                           "Add your own commands by dropping a Python file in the plugins folder.")
        self._section("Plugins")
        self._check("plugins_enabled", "Load plugins when I start")
        self.plugin_status = QLabel("")
        self.plugin_status.setObjectName("muted")
        self.plugin_status.setWordWrap(True)
        self._refresh_plugin_status()
        p.row("Loaded", self.plugin_status)
        open_folder = QPushButton("Open the plugins folder")
        open_folder.clicked.connect(lambda: self._open_path(plugins.folder()))
        reload = QPushButton("Reload now")

        def do_reload() -> None:
            self._controller.reload_plugins()
            self._refresh_plugin_status()
        reload.clicked.connect(do_reload)
        self._buttons("", open_folder, reload, keywords="plugin folder reload")
        self._note("To write one, copy example.py in the plugins folder. Plugins add new commands but never "
                   "replace my own. You can also say \"reload plugins\".")

    def _refresh_plugin_status(self) -> None:
        loaded, problems = plugins.loaded(), plugins.errors()
        if not loaded and not problems:
            text = "None loaded."
        else:
            text = ", ".join(f"{p['name']}" + (f" -- {p['description']}" if p["description"] else "")
                             for p in loaded) or "None loaded."
            if problems:
                text += "\n\nFailed: " + "; ".join(problems)
        self.plugin_status.setText(text)

    # ---- notifications --------------------------------------------------------------------------------------
    def _build_notifications(self) -> None:
        p = self._new_page("notifications", "Notifications", "\U0001f4e8",
                           "Show Windows notifications here, and hear them summarized or read out.")
        self._section("Windows notifications")
        enabled = self._check("notify_enabled", "Show Windows notifications here",
                              "New ones appear in the status bar and the chat, with a sound.")
        ask = self._choice("notify_ask", "When one arrives",
                           [("speech", "Ask whether to summarize it"), ("summarize", "Summarize it out loud"),
                            ("read", "Read it out loud"), ("message", "Read just the message, like \"Alex says ...\""),
                            ("card", "Show a card with Summarize and Dismiss"), ("none", "Just show it")])
        seconds = self._number("notify_seconds", "Keep the card up for", 3, 60, 1, 0, " s")
        sound = SoundPicker(allow_off=True, volume_fn=lambda: float(self._get["ack_volume"]()),
                            device_fn=lambda: self._get["output_device"]() or None)
        self._register("notify_sound", sound.name, sound.set_name)
        self._register("notify_sound_file", sound.file, sound.set_file)
        p.row("Sound", sound, "Its volume is the Sound volume under Voice & sounds.")
        catchup = self._check("notify_catchup", "Mention the ones that arrived while I was closed")

        self._section("Per-app rules")
        rules = RulesEditor()
        self._register("notify_rules", rules.value, rules.set_value)
        p.row("Rules", rules, "The first rule whose text is in the app's name wins. Other apps follow "
                              "\"When one arrives\" above.", "steam discord outlook ignore silent")

        def merge_ignored(value) -> None:
            """The old "always ignore these apps" box is now just rules at the top of the list."""
            names = [a.strip() for a in str(value or "").split(",") if a.strip()]
            have = {r["app"].lower() for r in rules.value()}
            new = [{"app": a, "mode": "ignore"} for a in names if a.lower() not in have]
            if new:
                rules.set_value(new + rules.value())
        self._register("notify_ignore", lambda: "", merge_ignored)

        self._section("Quiet hours")
        quiet = self._check("notify_quiet_enabled", "Quiet hours",
                            "Cards only: no sound and no questions.")
        times = []
        for key, label in (("notify_quiet_start", "From"), ("notify_quiet_end", "Until")):
            edit = QTimeEdit()
            edit.setDisplayFormat("HH:mm")
            edit.setMaximumWidth(140)
            self._register(key, lambda e=edit: e.time().toString("HH:mm"),
                           lambda v, e=edit: e.setTime(QTime.fromString(str(v or ""), "HH:mm")))
            p.row(label, edit)
            times.append(edit)
        vip = self._text("notify_vip", "Always let through", "mom, urgent, on-call",
                         "Notifications with any of these words still come through. Separate them with commas.")

        self._section("Reading them out")
        self._check("notify_offer_card", "After a summary, offer to add it to my board",
                    "Only when the notification names a day or time. Say yes and it becomes a card, due then.",
                    keywords="board card task todo summary notification")
        strip = StripRulesEditor()
        self._register("notify_strip", strip.value, strip.set_value, strip.changed)
        self._attach_reset("notify_strip", self._page.row(
            "Remove before reading", strip,
            "Words to take out of app names, titles and messages before I read or summarize them, one per line, "
            "like a server name or \"- Outlook\". Capitals don't matter. A line between slashes is a pattern: "
            "/\\([^)]*\\)/ removes anything in parentheses. \"Add a common rule\" has ready-made ones.",
            "strip remove names parentheses brackets regex pattern clean read summarize", resettable=True),
            strip.changed)

        self._section("Windows' own pop-ups")
        silence = self._check("notify_silence", "Hide Windows' own pop-ups while I'm running",
                              "Notifications still reach me. If they stop arriving, I turn Windows' pop-ups back on.")
        clear = self._check("notify_clear", "Remove each one from Windows' notification list once I've shown it")
        windows = QPushButton("Open Windows notification settings")
        windows.clicked.connect(lambda: os.startfile("ms-settings:notifications"))
        self._buttons("", windows)
        self._note("Windows' Do Not Disturb works too: notifications keep arriving silently and I still show "
                   "them. Say \"pause notifications for an hour\" any time to quiet me.")

        self._section("Try it")
        result = QLabel("")
        result.setObjectName("muted")
        result.setWordWrap(True)
        test = QPushButton("Show a test notification")
        test.setToolTip("A pretend notification, handled with the saved settings. Nothing is sent to Windows.")
        test.clicked.connect(lambda: result.setText(self._controller.test_notification()))
        self._buttons("", test)
        p.row("", result)

        on = enabled.isChecked
        self._enable_when(on, [ask, seconds, sound, catchup, rules, quiet, silence, clear, test], enabled.toggled)
        self._enable_when(lambda: on() and quiet.isChecked(), times + [vip], enabled.toggled, quiet.toggled)

    # ---- hotkeys ---------------------------------------------------------------------------------------------
    def _build_hotkeys(self) -> None:
        p = self._new_page("hotkeys", "Hotkeys", "⌨",
                           "Shortcuts that work in any app. Click a box and press a key combination.")
        self._section("Shortcuts")
        for key, label in HOTKEY_ROWS:
            seq = QKeySequenceEdit()
            seq.setMaximumSequenceLength(1)
            self._register(key, lambda seq=seq: seq.keySequence().toString(QKeySequence.PortableText),
                           lambda v, seq=seq: seq.setKeySequence(QKeySequence(str(v or ""))))
            clear = QPushButton("Clear")
            clear.clicked.connect(seq.clear)
            box = QWidget()
            h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(seq, 1)
            h.addWidget(clear)
            help_text = "Hold the keys while you speak; let go when you're done." if key == "hotkey_hold" else ""
            p.row(label, box, help_text)

        self._section("Copilot key")
        enabled = self._check("copilot_key_enabled", "Use the Copilot key and Win+C for me instead",
                              "Windows keeps these keys for Copilot, so they can't be set above.")
        action = self._choice("copilot_key_action", "They should",
                              [(key.removeprefix("hotkey_"), label) for key, label in HOTKEY_ROWS
                               if key != "hotkey_hold"])
        self._enable_when(enabled.isChecked, [action], enabled.toggled)

        self._section("Quick command box")
        self._number("quick_reply_seconds", "Keep the reply up for", 1.0, 60.0, 0.5, 1, " s",
                     "How long my answer stays under the box after I finish speaking it.")

    # ---- AI ------------------------------------------------------------------------------------------------------
    def _build_ai(self) -> None:
        self._new_page("ai", "AI & chat", "\U0001f9e0",
                       "The AI that answers questions and chats. It runs on this PC, in Ollama.")
        self._section("Model")
        picker = ModelPicker(str(backend.CONFIG.get("ollama_model") or ""))
        self._register("ollama_model", picker.value, picker.set_value, picker.changed)
        self._attach_reset("ollama_model", self._page.row(
            "Model", picker, "Models on this PC come first, then ones worth downloading. You can also type any "
                             "model name from ollama.com.", "ollama model llm qwen llama gemma download",
            resettable=True))
        light = self._check("ollama_light_enabled", "Answer simple questions with a faster model",
                            "A small model answers greetings, chit-chat and notification summaries almost at once. "
                            "It decides for itself when a question needs real knowledge, maths or careful thought, "
                            "and hands those to the model above.",
                            keywords="fast light small model llama switch speed")
        light_picker = ModelPicker(str(backend.CONFIG.get("ollama_light_model") or ""))
        self._register("ollama_light_model", light_picker.value, light_picker.set_value, light_picker.changed)
        self._attach_reset("ollama_light_model", self._page.row(
            "Faster model", light_picker, "A small model such as llama3.2 or qwen3:4b.",
            "light fast small model llama", resettable=True))
        self._enable_when(light.isChecked, [light_picker], light.toggled)
        self._multiline("ollama_system_prompt", "Instructions", 160,
                        "How the AI should behave. Write {name} where my name should go.")
        self._section("Answers")
        self._check("fast_chat_lane", "Reply to small talk straight away",
                    "Greetings and chit-chat go straight to the AI, so they're answered sooner.")
        self._number("ollama_timeout", "Give up after", 5, 300, 5, 0, " s")

        self._section("Understanding and accuracy")
        self._check("smart_commands", "Understand commands however you phrase them",
                    "When you word something in a way I don't recognise (\"shut the music up for a sec\"), the AI "
                    "works out which command you meant. Adds about half a second to those.",
                    keywords="natural speech intent phrasing understand")
        self._check("ground_facts", "Look up facts instead of answering from memory",
                    "Questions like \"when did Silksong come out\" or \"who directed Dune\" are answered from "
                    "Wikipedia and the web. The AI's own memory often gets these wrong.",
                    keywords="accuracy release date facts search")
        self._check("fact_check", "Check facts before answering",
                    "Answers with dates, numbers or names are checked against the web before I say them, and "
                    "corrected if they're wrong. Slower: I only start speaking once the check is done.",
                    keywords="fact check verify accuracy correct")

        self._section("Searching the web")
        provider = self._choice("search_provider", "Search with",
                                [("duckduckgo", "DuckDuckGo"), ("brave", "Brave Search"),
                                 ("searxng", "SearXNG, my own server"), ("off", "Wikipedia only")],
                                help_text="Wikipedia is always checked first. The search engine adds newer and "
                                          "more specific results.", keywords="search engine google web")
        brave = QLineEdit()
        brave.setEchoMode(QLineEdit.Password)
        brave.setPlaceholderText("Saved" if websearch.brave_key() else "Paste your key")
        self._brave_key = brave
        self._page.row("Brave Search key", brave, "Free from brave.com/search/api. Kept in Windows Credential "
                                                   "Manager, not in the settings file.", "brave api key")
        searx = self._text("searxng_url", "SearXNG address", "http://127.0.0.1:8888",
                           "Its JSON format must be turned on in the server's settings.")
        results = self._number("web_search_results", "Results to read", 1, 10, 1)
        remember = self._check("lookup_cache", "Reuse recent lookups",
                               "The answers to your last 50 fact questions are kept for 12 hours, the search results "
                               "behind them for a day, and places for a month, so asking again is instant and "
                               "doesn't search again. Kept on this PC only.",
                               keywords="cache recent searches lookups remember reuse history")
        forget = QPushButton("Forget recent lookups")
        forget.clicked.connect(self._forget_lookups)
        self._buttons("", forget)
        self._enable_when(remember.isChecked, [forget], remember.toggled)
        chosen = provider.currentData
        self._enable_when(lambda: chosen() == "brave", [brave], provider.currentIndexChanged)
        self._enable_when(lambda: chosen() == "searxng", [searx], provider.currentIndexChanged)
        self._enable_when(lambda: chosen() != "off", [results], provider.currentIndexChanged)

        self._section("Wikipedia pop-up")
        popup = self._check("wiki_popup", "Show the article I found in a pop-up",
                            "A small card at the edge of the screen, with a close button in its top corner.",
                            keywords="wikipedia popup article picture image")
        popup_parts = [self._check("wiki_popup_image", "With its picture"),
                       self._check("wiki_popup_text", "With the article's opening"),
                       self._choice("wiki_popup_side", "Side of the screen", [("right", "Right"), ("left", "Left")])]
        self._enable_when(popup.isChecked, popup_parts, popup.toggled)
        self._section("Connection")
        self._text("ollama_url", "Ollama address")
        self._text("ollama_keep_alive", "Keep the model loaded for", "30m, or -1 for always",
                   "A loaded model answers sooner but uses memory.")

    # ---- apps & PC -----------------------------------------------------------------------------------------------
    def _build_apps(self) -> None:
        p = self._new_page("apps", "Apps & PC", "\U0001f680",
                           "Opening apps and websites, and what I may do to the PC itself.")
        self._section("Opening apps")
        avoid = QLineEdit()
        avoid.setPlaceholderText("Microsoft Edge, ...")
        self._register("avoid_apps", lambda: [a.strip() for a in avoid.text().split(",") if a.strip()],
                       lambda v: avoid.setText(", ".join(v) if isinstance(v, (list, tuple)) else str(v or "")))
        p.row("Never open these", avoid, "Separate them with commas. They're also skipped when you ask for "
                                         "something vague like \"open a browser\".")
        self._number("app_match_threshold", "How closely names must match", 0.1, 1.0, 0.05, 2,
                     help_text="Higher avoids opening the wrong app; lower forgives mispronounced names.")
        self._check("app_descriptions_online", "Look up what new apps do online",
                    "App names are sent to Wikipedia and DuckDuckGo, so requests like \"the photo editor\" find "
                    "the right app. Otherwise apps are matched by name only.")
        rescan = QPushButton("Rescan installed apps")
        rescan.clicked.connect(self._controller.rescan_apps)
        self._buttons("", rescan)

        self._section("Finding files")
        files = self._check("files_enabled", "Find files by name",
                            "\"Find the file called resume\", then \"open the second one\". Uses Everything, the free "
                            "file search from voidtools. Programs are never run from a search.",
                            keywords="everything search files find folder voidtools locate")
        system_files = self._check("files_include_system", "Also search Windows and program folders",
                                   "Off: Windows, Program Files, AppData and hidden tool folders are left out, "
                                   "so your own files come first.")
        self.files_state = QLabel("")
        self.files_state.setObjectName("muted")
        self.files_state.setWordWrap(True)
        self._page.row("Everything", self.files_state)
        self._enable_when(files.isChecked, [system_files, self.files_state], files.toggled)
        self._refresh_files_state()

        self._section("The PC itself")
        control = self._check("system_control_enabled", "Let me control volume, power and screenshots",
                              "\"Set the volume to 40 percent\", \"lock the PC\", \"take a screenshot\", "
                              "\"how much disk space is left\".")
        confirm = self._check("confirm_destructive", "Ask before shutting down, signing out or emptying the recycle bin",
                              "Recommended: speech recognition sometimes mishears.")
        self._enable_when(control.isChecked, [confirm], control.toggled)

        self._section("Closing windows")
        self._check("confirm_window_close", "Ask before closing a window",
                    "\"Close Discord\" asks first, with a glowing outline around the window it means. Say yes "
                    "to close it.", keywords="close quit window outline highlight confirm")

    # ---- browser ----------------------------------------------------------------------------------------------
    def _build_browser(self) -> None:
        p = self._new_page("browser", "Browser", "\U0001f310",
                           "Reading the page you're on and switching tabs, through the NEON extension for Chrome, "
                           "Edge, Brave, Firefox or Zen.")
        self._section("Connection")
        enabled = self._check("browser_enabled", "Talk to the browser extension",
                              "The extension only talks to NEON on this PC. Pages are read only when you ask about "
                              "them, and nothing is saved.", keywords="zen firefox extension browser page tabs")
        port = self._number("browser_port", "Port", 1024, 65535, 1,
                            help_text="Change it only if another program already uses it. The extension's "
                                      "options need the same number.")
        self.browser_state = QLabel("")
        self.browser_state.setObjectName("muted")
        self.browser_state.setWordWrap(True)
        p.row("Status", self.browser_state)
        copy = QPushButton("Copy the token")
        copy.clicked.connect(self._copy_browser_token)
        folder = QPushButton("Open the extension folder")
        folder.clicked.connect(lambda: self._open_path(app_paths.resource_path("browser_extension")))
        token_row = self._buttons("Setting up", copy, folder, keywords="token install extension xpi",
                                  help_text="Paste the token into the extension's options (click its icon). To "
                                            "install it: about:debugging, This Firefox, Load Temporary Add-on, "
                                            "and pick manifest.json in the extension folder. See the README for "
                                            "a permanent install.")
        self._enable_when(enabled.isChecked, [port, self.browser_state, token_row], enabled.toggled)
        self._refresh_browser_state()
        timer = QTimer(self)
        timer.timeout.connect(self._refresh_browser_state)
        timer.start(2000)
        self._section("What you can say")
        self._note("\"Summarize this page\" · \"what does this page say about shipping\" · \"what's the price on "
                   "this page\" · \"read this article\" · \"find refund policy on this page\" · \"what tabs do I "
                   "have open\" · \"switch to the GitHub tab\" · \"close this tab\"")

    def _refresh_browser_state(self) -> None:
        if browser_bridge.connected():
            text = f"Connected to {browser_bridge.browser_name()}."
        elif not backend.cfg_bool("browser_enabled"):
            text = "Off."
        else:
            text = "Waiting for the extension. Is the browser open, with the token pasted in?"
        self.browser_state.setText(text)

    def _copy_browser_token(self) -> None:
        QApplication.clipboard().setText(browser_bridge.token())
        self.browser_state.setText("Token copied. Paste it into the extension's options.")

    # ---- passwords ----------------------------------------------------------------------------------------------
    def _build_passwords(self) -> None:
        p = self._new_page("passwords", "Passwords", "\U0001f511",
                           "Usernames, passwords and two-factor codes from Bitwarden.")
        self._section("Bitwarden")
        enabled = self._check("bitwarden_enabled", "Use my Bitwarden vault",
                              "Needs Bitwarden's command-line tool: run winget install Bitwarden.CLI, then bw login "
                              "once in a terminal. Your master password is typed into a box, never said out loud. If Bitwarden "
                              "is already unlocked on this PC, I use it without asking for the password.",
                              keywords="bitwarden password manager vault")
        self.vault_state = QLabel("")
        self.vault_state.setObjectName("muted")
        self.vault_state.setWordWrap(True)
        p.row("Status", self.vault_state)
        unlock = QPushButton("Unlock...")
        unlock.clicked.connect(self._controller.unlock_bitwarden)
        lock = QPushButton("Lock now")
        lock.clicked.connect(lambda: (self._controller.lock_bitwarden(), self._refresh_vault_state()))
        buttons = self._buttons("", unlock, lock)
        minutes = self._number("bitwarden_lock_minutes", "Lock again after", 1, 240, 1, 0, " min",
                               "Each time I use the vault, the countdown starts again.")
        typing = self._check("bitwarden_allow_typing", "Let me type passwords into the box you're in",
                             "\"Type my Proton password\" asks first, then types it. Otherwise passwords are only "
                             "ever copied, and cleared from the clipboard after 30 seconds.")
        cli = self._text("bitwarden_cli", "Bitwarden tool", "Found automatically",
                         "Only if bw.exe is somewhere unusual.")
        self._enable_when(enabled.isChecked, [self.vault_state, buttons, minutes, typing, cli], enabled.toggled)
        self._refresh_vault_state()
        timer = QTimer(self)
        timer.timeout.connect(self._refresh_vault_state)
        timer.start(3000)
        self._section("What you can say")
        self._note("\"What's my username for Proton Mail\" · \"copy my GitHub password\" · \"type my Proton "
                   "password\" · \"what's my GitHub 2FA code\" · \"lock Bitwarden\". Passwords are never said "
                   "out loud.")

    def _forget_lookups(self) -> None:
        import lookup_cache
        lookup_cache.clear()
        self._controller.status.emit("Forgot recent lookups")

    def _refresh_files_state(self) -> None:
        import filesearch
        self.files_state.setText({
            "ready": "Running. Searches are instant.",
            "indexing": "Running, still building its index.",
            "stopped": "Installed, not running. I'll start it the first time you ask for a file.",
            "missing": "Not installed. Install it with winget install voidtools.Everything.",
        }[filesearch.status()])

    def _refresh_vault_state(self) -> None:
        if bitwarden.is_unlocked():
            text = "Unlocked."
        elif not bitwarden.find_cli():
            text = "Bitwarden's command-line tool isn't installed."
        else:
            text = "Locked."
        self.vault_state.setText(text)

    # ---- music --------------------------------------------------------------------------------------------------
    def _build_music(self) -> None:
        p = self._new_page("music", "Music", "\U0001f3b5", "Play, pause and skip songs by voice.")
        self._section("Pear Desktop")
        pear = self._check("ytm_enabled", "Control Pear Desktop when it's running",
                           "Play, pause, next and previous go to Pear Desktop, with the media keys as a fallback.")
        url = self._text("ytm_url", "Pear Desktop address", "http://127.0.0.1:26538",
                         "Turn on the API server plugin in Pear Desktop first.")
        result = QLabel("")
        result.setObjectName("muted")
        result.setWordWrap(True)
        test = QPushButton("Test connection")

        def run_test() -> None:
            keys = {k: self._get[k]() for k in ("ytm_enabled", "ytm_url")}
            backend.CONFIG.update(keys)              # tested with what's on screen; only Save keeps it
            result.setText("Connecting... approve the request in Pear Desktop if it asks.")
            test.setEnabled(False)
            threading.Thread(target=lambda: self._emit_ytm(backend.YTM.status()), daemon=True).start()

        def show(text: str) -> None:
            result.setText(text)
            test.setEnabled(True)
        test.clicked.connect(run_test)
        self.ytm_status.connect(show)
        self._buttons("", test)
        p.row("", result)
        self._enable_when(pear.isChecked, [url, test], pear.toggled)

        self._section("Other players")
        self._check("media_any_player", "Control other players too",
                    "Spotify, browsers and anything else in Windows' media controls, when Pear Desktop isn't "
                    "running.")
        self._note("Try \"what's playing\", \"like this song\", \"skip ahead 30 seconds\" or \"play <song> on "
                   "YouTube Music\".")

    # ---- smart home ---------------------------------------------------------------------------------------------
    def _build_home(self) -> None:
        import homeassistant
        p = self._new_page("home", "Smart home", "\U0001f3e0",
                           "Lights, heating, blinds and locks, through Home Assistant.")
        self._section("Home Assistant")
        enabled = self._check("ha_enabled", "Control my home through Home Assistant",
                              "\"Turn off the kitchen lights\", \"set the thermostat to 70\", \"is the front door "
                              "locked\". I pass these to Home Assistant's Assist, which knows your devices and rooms.",
                              keywords="home assistant smart home lights thermostat hass iot")
        url = self._text("ha_url", "Address", "http://homeassistant.local:8123",
                         "The address you open Home Assistant at.", keywords="home assistant url address")
        key = QLineEdit()
        key.setEchoMode(QLineEdit.Password)
        key.setPlaceholderText("Saved" if homeassistant.token() else "Paste a long-lived access token")
        self._ha_token = key
        p.row("Access token", key, "In Home Assistant: your profile, Security, Long-lived access tokens, Create "
                                   "token. Kept in Windows Credential Manager, not in the settings file.",
              "home assistant token key")
        confirm = self._check("ha_confirm", "Ask before unlocking, opening the garage or disarming the alarm",
                              "Recommended: speech recognition sometimes mishears.")
        result = QLabel("")
        result.setObjectName("muted")
        result.setWordWrap(True)
        test = QPushButton("Test connection")

        def run_test() -> None:
            typed = key.text().strip()
            if typed:
                homeassistant.set_token(typed)       # tested with what's on screen
                key.clear()
                key.setPlaceholderText("Saved")
            backend.CONFIG["ha_url"] = self._get["ha_url"]()
            homeassistant.forget_names()
            result.setText("Connecting...")
            test.setEnabled(False)
            threading.Thread(target=lambda: self._emit_ha(homeassistant.check(backend.CONFIG)), daemon=True).start()

        def show(text: str) -> None:
            result.setText(text)
            test.setEnabled(True)
        test.clicked.connect(run_test)
        self.ha_status.connect(show)
        self._buttons("", test)
        p.row("", result)
        self._enable_when(enabled.isChecked, [url, key, confirm, test], enabled.toggled)
        self._note("Only devices you've exposed to Assist can be controlled (Home Assistant: Settings, Voice "
                   "assistants, Expose). Commands for the PC itself, like \"lock the PC\", stay with me.")

    def _emit_ha(self, text: str) -> None:
        try:
            self.ha_status.emit(text)
        except RuntimeError:
            pass    # the dialog was closed while the test was running

    def _emit_ytm(self, text: str) -> None:
        try:
            self.ytm_status.emit(text)
        except RuntimeError:
            pass    # the dialog was closed while the test was running

    def _wire_across_pages(self) -> None:
        """Rows that depend on a setting on another page."""
        engine, ics, on = self.tts_engine, self.calendar_ics, self._bar_on
        bar_toggled = self._signals["show_status_bar"][0]
        self._enable_when(lambda: on() and engine.currentData() == "piper", [self.bar_spectrum],
                          engine.currentIndexChanged, bar_toggled)
        self._enable_when(lambda: on() and bool(ics.text().strip()), [self.bar_calendar], ics.textChanged, bar_toggled)

    # ================================================================================================
    # Actions
    # ================================================================================================
    @staticmethod
    def _open_path(path) -> None:
        try:
            os.startfile(str(path))
        except OSError:
            pass

    def _rerun_onboarding(self) -> None:
        self.reject()
        QTimer.singleShot(0, self._controller.onboarding_requested.emit)

    def _export_settings(self) -> None:
        picked, _ = QFileDialog.getSaveFileName(self, "Export settings", "neon-settings.json", "JSON (*.json)")
        if not picked:
            return
        data = {k: (self._get[k]() if k in self._get else v) for k, v in backend.CONFIG.items()}
        try:
            with open(picked, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self.status_note.setText(f"Exported {len(data)} settings to {picked}")
        except OSError as exc:
            self.error.setText(f"Couldn't write that file: {exc}")

    def _import_settings(self) -> None:
        picked, _ = QFileDialog.getOpenFileName(self, "Import settings", "", "JSON (*.json)")
        if not picked:
            return
        try:
            with open(picked, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("that isn't a settings file")
        except (OSError, ValueError) as exc:
            self.error.setText(f"Couldn't read that file: {exc}")
            return
        clean, problems = settings_schema.sanitize({**backend.DEFAULT_CONFIG, **settings_schema.migrate(data)},
                                                   backend.DEFAULT_CONFIG)
        applied = 0
        for key, value in sorted(clean.items(), key=lambda kv: kv[0] == "notify_ignore"):
            if key in self._set and key in data:
                self._set[key](value)
                applied += 1
        self.error.setText("")
        self.status_note.setText(f"Imported {applied} settings" + (f" ({len(problems)} fixed)" if problems else "") +
                                 ". Review them, then press Save.")

    def _restore_page_defaults(self) -> None:
        for key in self.current_page().keys:
            self._set[key](backend.DEFAULT_CONFIG.get(key))
        if self.current_page().live:                     # instant pages: apply the defaults now
            for key in self.current_page().keys:
                self._live_changed(key)

    def reject(self) -> None:
        self._live_timer.stop()
        self._flush_live()                               # a change made just before closing is still saved
        super().reject()

    # ---- save -------------------------------------------------------------------------------------------------------
    def _show_error(self, message: str, page_key: str) -> None:
        self.error.setText(message)
        for i, page in enumerate(self._pages):
            if page.key == page_key:
                self.search.clear()
                self.nav.setCurrentRow(i)

    def _save(self) -> None:
        new = {key: getter() for key, getter in self._get.items()}

        seen: dict[str, str] = {}
        for key, label in HOTKEY_ROWS:
            spec = str(new.get(key) or "").strip()
            if not spec:
                continue
            try:
                hotkeys.parse_hotkey(spec)
            except ValueError as exc:
                self._show_error(f"{label}: {exc}", "hotkeys")
                return
            if spec.lower() in seen:
                self._show_error(f"{label} and {seen[spec.lower()]} can't share {spec}.", "hotkeys")
                return
            seen[spec.lower()] = label
        voice = str(new.get("tts_voice") or "").strip()
        if new.get("tts_engine") == "piper" and voice and not tts.valid_voice_name(voice):
            self._show_error(f"'{voice}' isn't a Piper voice name. They look like en_US-amy-medium.", "voice")
            return

        new["assistant_name"] = (backend.sanitize_name(new.get("assistant_name"))
                                 or backend.DEFAULT_CONFIG["assistant_name"])
        if "user_name" in new:
            new["user_name"] = backend.sanitize_name(new.get("user_name"))
        if new.get("name_is_wake_word"):
            new["wake_word"] = new["assistant_name"].lower()
        if not new.get("wake_word"):
            new["wake_word"] = backend.DEFAULT_CONFIG["wake_word"]
        if not new.get("ollama_system_prompt"):
            new["ollama_system_prompt"] = backend.DEFAULT_CONFIG["ollama_system_prompt"]
        new, _fixed = settings_schema.sanitize(new, backend.DEFAULT_CONFIG)
        key = self._brave_key.text().strip()
        if key and not websearch.set_brave_key(key):          # never in the settings file
            self._controller.message.emit("system", "Couldn't save the Brave Search key.")
        import homeassistant
        ha_key = self._ha_token.text().strip()
        if ha_key and not homeassistant.set_token(ha_key):    # never in the settings file either
            self._controller.message.emit("system", "Couldn't save the Home Assistant token.")
        homeassistant.forget_names()                          # a new address or token: read the devices again

        old = dict(backend.CONFIG)
        listener_changed = (new.get("mic_device") != (old.get("mic_device") or "")
                            or new.get("energy_multiplier") != old.get("energy_multiplier"))
        engine_changed = new.get("tts_engine") != old.get("tts_engine")
        ai_changed = any(new.get(k) != old.get(k) for k in ("ollama_model", "ollama_url", "ollama_keep_alive",
                                                               "ollama_light_enabled", "ollama_light_model"))
        browser_changed = any(new.get(k) != old.get(k) for k in ("browser_enabled", "browser_port"))
        vault_off = old.get("bitwarden_enabled") and not new.get("bitwarden_enabled")

        import shortcuts
        for problem in shortcuts.apply({"start_menu": new.get("start_menu_shortcut"),
                                        "desktop": new.get("desktop_shortcut")}):
            self._controller.message.emit("system", problem)
        new["start_menu_shortcut"], new["desktop_shortcut"] = shortcuts.exists("start_menu"), shortcuts.exists("desktop")
        try:
            startup.set_enabled(bool(new.get("start_with_windows")))
        except OSError as exc:
            new["start_with_windows"] = startup.is_enabled()
            self._controller.message.emit("system", f"Couldn't change the startup setting: {exc}")

        self._live_timer.stop()
        self._live_dirty.clear()
        backend.CONFIG.update(new)
        apply_theme(str(new.get("theme", "neon")), str(new.get("theme_accent", "")), new.get("state_colors"))
        backend.save_config(backend.CONFIG)
        self.error.setText("")
        super().accept()
        self._controller.apply_settings(listener_changed, engine_changed, ai_changed)
        if browser_changed:
            self._controller.apply_browser_setting()
        if vault_off:
            self._controller.lock_bitwarden()
        self._controller.settings_saved.emit()
