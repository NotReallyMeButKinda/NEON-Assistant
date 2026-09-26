"""Entry point: Qt app, controller, main window, status bar, tray icon, hotkey."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import app_paths


def give_output_a_home() -> None:
    """The .exe (and pythonw) has no console, so sys.stdout / sys.stderr are None, and any library that
    prints a progress bar (downloads) or flushes them crashes the app. Point them at console.log in the
    data folder instead, and let faulthandler write a trace there if native code (Qt, ONNX, PortAudio)
    takes the process down, so a silent crash still leaves a clue. Runs before the heavy imports."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import faulthandler
    try:
        sink = open(app_paths.data_path("console.log"), "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        sink = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink
    if sink.name != os.devnull:
        faulthandler.enable(sink)


give_output_a_home()

from PySide6.QtCore import QTimer  # noqa: E402  (after give_output_a_home: imports may print)
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import assistant as backend
import neon_log
import persona
from controller import Assistant
from hotkeys import CopilotKeyHook, HoldKeyHook, HotkeyManager
from ui.main_window import MainWindow
from ui.onboarding import OnboardingDialog
from ui.notification_history import NotificationHistory
from ui.board import BoardWindow
from ui.timer_panel import TimerPanel
from ui.quick_input import QuickInput
from ui.settings_dialog import HOTKEY_ROWS, SettingsDialog
from ui.shade import Shade
from ui.wiki_popup import WikiPopup
from ui import app_icon, frame
from ui.unlock_dialog import UnlockDialog
from ui.window_highlight import WindowHighlight
from ui.status_bar import StatusBar
from ui import theme
from ui.theme import COLORS, apply_theme
from ui.theme import signals as theme_signals

ERROR_ALREADY_EXISTS = 183


def already_running():
    """Named mutex so the autostarted copy and a manually launched one don't both run
    (two status bars would fight over the top of the screen). Returns the handle to keep
    alive, or None if another instance owns it."""
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "NeonAssistantSingleton")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def end_process(code: int = 0) -> None:
    """End the process now. TerminateProcess, not os._exit: os._exit still runs DLL shutdown code
    (ONNX Runtime, CTranslate2, PortAudio), which crashes if one of their threads is mid-work, and the
    quit watchdog calls this from a background thread while Qt may still be busy."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    logging.shutdown()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess(ctypes.c_void_p(kernel32.GetCurrentProcess()), code)
    os._exit(code)                                   # not reached


def make_icon() -> QIcon:
    """The glowing dot (the orb), painted from the current theme's colors (see ui/app_icon.py)."""
    return app_icon.make_icon(COLORS)


def relaunch_without_console() -> bool:
    """Double-clicking main.py (or a shortcut to python.exe) opens a console window just for us.
    Start again under pythonw.exe and let this copy exit (True), so no terminal stays up. From a
    terminal you already had open, or with --console, stay put so print() output shows."""
    if "--console" in sys.argv or getattr(sys, "frozen", False):
        return False
    kernel32 = ctypes.windll.kernel32
    if not kernel32.GetConsoleWindow():
        return False                                  # pythonw, an IDE's run pane, ...
    pids = (ctypes.c_ulong * 4)()
    if kernel32.GetConsoleProcessList(pids, 4) != 1:
        return False                                  # a shell shares the console: it's theirs
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        return False
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), *sys.argv[1:]],
                     creationflags=DETACHED_PROCESS, close_fds=True)
    return True


def main() -> None:
    if relaunch_without_console():
        return
    neon_log.install()      # a rotating log file + uncaught-exception hooks, before anything can fail
    mutex = already_running()
    if mutex is None:
        print("NEON ASSISTANT is already running (check the tray).")
        return

    selftest = "--selftest" in sys.argv          # see selftest.py: everything off-screen, then quit
    if selftest:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import shortcuts
    shortcuts.claim_app_id()                     # "Neon Assistant" on the taskbar, matched to its Start menu entry
    app = QApplication(sys.argv)
    app.setApplicationName("NEON ASSISTANT")
    app.setApplicationDisplayName("Neon Assistant")
    if not selftest:
        threading.Thread(target=shortcuts.rename_old, name="Nova-Shortcuts", daemon=True).start()
    theme.FOLLOW["high_contrast"] = backend.cfg_bool("follow_high_contrast")
    apply_theme(str(backend.CONFIG.get("theme", "neon")), str(backend.CONFIG.get("theme_accent", "")),
                backend.CONFIG.get("state_colors"))
    app.setQuitOnLastWindowClosed(False)  # main window hides to tray; the bar keeps running
    icon = make_icon()
    app.setWindowIcon(icon)

    controller = Assistant()
    window = MainWindow(controller)
    bar = StatusBar(controller)
    app.shade = Shade(controller)                # held by the app; it runs itself from the controller's signals
    app.wiki_popup = WikiPopup(controller)       # likewise: shows what a search found
    app.window_highlight = WindowHighlight(controller)   # outlines the window a "close X?" question is about
    quick = QuickInput(controller)

    def toggle_window() -> None:
        if window.isVisible():
            window.hide()
        else:
            window.show()
            window.raise_()
            window.activateWindow()

    open_dialogs: set = set()     # parentless dialogs shown without exec() need a Python reference

    def show_dialog(dialog) -> None:
        """Show without exec(): exec() makes a dialog application-modal when the main window is hidden
        (nothing to be modal to), which disables the status bar -- clicking it only played the error sound."""
        open_dialogs.add(dialog)
        dialog.finished.connect(lambda _result: open_dialogs.discard(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_settings() -> None:
        if not SettingsDialog.raise_open():
            show_dialog(SettingsDialog(controller, window if window.isVisible() else None))

    history: dict = {"panel": None}

    def open_history() -> None:
        panel = history["panel"]
        if panel is None:
            panel = history["panel"] = NotificationHistory(controller)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    controller.history_requested.connect(open_history)

    timer_window: dict = {"panel": None}

    def open_timers() -> None:
        panel = timer_window["panel"]
        if panel is None:
            panel = timer_window["panel"] = TimerPanel(controller)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    controller.timers_requested.connect(open_timers)

    board_window: dict = {"window": None}

    def open_board() -> None:
        win = board_window["window"]
        if win is None:
            win = board_window["window"] = BoardWindow(controller)
        win.show()
        win.raise_()
        win.activateWindow()

    controller.board_requested.connect(open_board)

    def open_unlock() -> None:
        if not UnlockDialog.raise_open():
            show_dialog(UnlockDialog(controller))

    controller.bitwarden_unlock_requested.connect(open_unlock)

    def open_onboarding() -> None:
        show_dialog(OnboardingDialog(controller, window if window.isVisible() else None))

    quitting = {"started": False}

    def quit_app() -> None:
        """Shut everything down. The UI disappears first (so it never lingers), every step is
        isolated (one failing can't skip the rest), and a watchdog guarantees the process ends."""
        if quitting["started"]:
            return
        quitting["started"] = True

        def step(fn) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 -- keep shutting down whatever happens
                traceback.print_exc()

        step(tray.hide)                  # remove the tray icon now, not when the process dies
        step(window.hide)
        step(bar.unregister_appbar)      # give the reserved screen space back
        step(bar.hide)
        step(hotkeys.unregister)
        step(copilot_hook.uninstall)
        step(hold_hook.uninstall)
        step(quick.hide)
        step(controller.shutdown)

        # If anything below stalls (native audio threads, interpreter teardown), don't leave a
        # zombie process holding the single-instance mutex.
        watchdog = threading.Timer(3.0, end_process)
        watchdog.daemon = True
        watchdog.start()
        step(app.quit)

    bar.toggle_main_requested.connect(toggle_window)
    bar.settings_requested.connect(open_settings)

    # ---- global hotkeys ------------------------------------------------------------
    # Every action has its own bind (all unmapped by default); the Copilot key / Win+C can be
    # pointed at any of them too.
    actions = {
        "talk": controller.hotkey_talk,
        "wake": lambda: controller.set_wake(not controller.wake_on),
        "window": toggle_window,
        "quick": quick.toggle,
        "mute": controller.toggle_mute,
        "dictation": lambda: controller.set_dictation(not controller.dictating),
    }

    def on_copilot_key() -> None:
        actions.get(str(backend.CONFIG.get("copilot_key_action", "talk")), controller.hotkey_talk)()

    hotkeys = HotkeyManager()
    copilot_hook = CopilotKeyHook(on_copilot_key)
    hold_hook = HoldKeyHook(controller.hold_start, controller.hold_stop)
    app.installNativeEventFilter(hotkeys)

    def apply_hotkey() -> None:
        errors = hotkeys.apply([(label, backend.CONFIG.get(key, ""), actions[key.removeprefix("hotkey_")])
                                for key, label in HOTKEY_ROWS if key != "hotkey_hold"])   # hold needs key-up: own hook
        hold_error = hold_hook.set_combo(str(backend.CONFIG.get("hotkey_hold", "")))
        if hold_error:
            errors.append(hold_error)
        if backend.cfg_bool("copilot_key_enabled"):
            if not copilot_hook.install():
                errors.append("Copilot key: Windows wouldn't let me listen for it.")
        else:
            copilot_hook.uninstall()
        for error in errors:
            controller.message.emit("system", error)

    controller.settings_saved.connect(apply_hotkey)
    controller.live_changed.connect(lambda keys: frame.refresh_all() if "custom_titlebar" in keys else None)
    controller.quit_requested.connect(quit_app)
    controller.onboarding_requested.connect(open_onboarding)

    # ---- tray ----------------------------------------------------------------------
    tray = QSystemTrayIcon(icon, app)
    menu = QMenu()
    show_action = QAction("Show / hide window", menu)
    show_action.triggered.connect(toggle_window)
    stop_action = QAction("Stop speaking", menu)
    stop_action.triggered.connect(controller.stop_speaking)
    mute_action = QAction("Mute microphone", menu)
    mute_action.setCheckable(True)
    mute_action.triggered.connect(controller.set_muted)
    controller.muted_changed.connect(mute_action.setChecked)
    dictation_action = QAction("Dictation (type what I say)", menu)
    dictation_action.setCheckable(True)
    dictation_action.triggered.connect(controller.set_dictation)
    controller.dictation_changed.connect(dictation_action.setChecked)

    # Every persona in one submenu, so trying one on is a click rather than a sentence.
    persona_menu = QMenu("Persona", menu)
    persona_group = QActionGroup(persona_menu)
    persona_group.setExclusive(True)
    persona_actions = {}
    for key in persona.ORDER:
        entry = QAction(persona.PERSONAS[key]["label"], persona_menu)
        entry.setCheckable(True)
        entry.setActionGroup(persona_group)
        entry.triggered.connect(lambda _checked=False, k=key: controller.submit(f"persona: {k}", echo=False))
        persona_menu.addAction(entry)
        persona_actions[key] = entry

    def show_persona(key: str) -> None:
        entry = persona_actions.get(persona.valid(key))
        if entry is not None:
            entry.setChecked(True)
    show_persona(str(backend.CONFIG.get("persona", "default")))
    controller.persona_changed.connect(show_persona)
    controller.settings_saved.connect(lambda: show_persona(str(backend.CONFIG.get("persona", "default"))))

    notify_action = QAction("Take over Windows notifications", menu)
    notify_action.setCheckable(True)
    notify_action.setChecked(backend.cfg_bool("notify_enabled"))

    def toggle_notifications(on: bool) -> None:
        backend.persist_keys({"notify_enabled": bool(on)})
        controller.apply_notification_settings()
    notify_action.toggled.connect(toggle_notifications)
    pause_action = QAction("Pause notifications for 1 hour", menu)
    pause_action.triggered.connect(lambda: controller.pause_notifications(3600))
    board_action = QAction("Board...", menu)
    board_action.triggered.connect(open_board)
    timers_action = QAction("Timers...", menu)
    timers_action.triggered.connect(open_timers)
    history_action = QAction("Recent notifications...", menu)
    history_action.triggered.connect(open_history)
    resume_action = QAction("Resume notifications", menu)
    resume_action.triggered.connect(controller.resume_notifications)
    controller.settings_saved.connect(lambda: notify_action.setChecked(backend.cfg_bool("notify_enabled")))
    settings_action = QAction("Settings...", menu)
    settings_action.triggered.connect(open_settings)
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(quit_app)
    for a in (show_action, stop_action, mute_action, dictation_action, board_action, timers_action):
        menu.addAction(a)
    menu.addMenu(persona_menu)
    menu.addSeparator()
    for a in (history_action, notify_action, pause_action, resume_action):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(settings_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)

    def refresh_icons() -> None:   # the icon is painted from theme colors
        fresh = make_icon()
        tray.setIcon(fresh)
        app.setWindowIcon(fresh)

    theme_signals.changed.connect(refresh_icons)
    tray.setToolTip(f"{backend.assistant_name()} - NEON ASSISTANT")
    controller.name_changed.connect(lambda n: tray.setToolTip(f"{n} - NEON ASSISTANT"))
    tray.activated.connect(
        lambda reason: toggle_window() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    app.aboutToQuit.connect(bar.unregister_appbar)
    app.aboutToQuit.connect(hotkeys.unregister)
    app.aboutToQuit.connect(copilot_hook.uninstall)
    app.aboutToQuit.connect(hold_hook.uninstall)

    # Autostart launches with --minimized: straight to tray + status bar.
    minimized = "--minimized" in sys.argv or backend.cfg_bool("start_minimized")
    if not minimized:
        window.show()
    if backend.cfg_bool("show_status_bar"):
        bar.show()
    apply_hotkey()
    controller.start()
    if not backend.cfg_bool("onboarding_done") and "--minimized" not in sys.argv:
        QTimer.singleShot(500, open_onboarding)   # first run: walk the user through the basics
    if selftest:
        import selftest as selftest_run
        after = sys.argv[sys.argv.index("--selftest") + 1:]
        selftest_run.schedule({"main window": window.show, "status bar": bar.show, "settings": open_settings,
                               "welcome tour": open_onboarding, "notification history": open_history,
                               "timers": open_timers, "board": open_board},
                              quit_app, backend.CONFIG, after[0] if after else None)
    code = app.exec()
    ctypes.windll.kernel32.CloseHandle(mutex)
    # Everything is cleaned up and all remaining threads are daemons; end the process directly
    # rather than relying on interpreter teardown (PortAudio / ONNX / Qt objects) to finish.
    end_process(code)


if __name__ == "__main__":
    main()
