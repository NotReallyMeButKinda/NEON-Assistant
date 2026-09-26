"""Visual regression: every window is rendered offscreen, audited for clipped text, and compared with
a stored screenshot (tests/baselines). See tests/render_utils.py; NEON_UPDATE_BASELINES=1 accepts changes."""

import unittest

from PySide6.QtWidgets import QApplication

from controller import Assistant
from tests import common
from tests.common import StubController, backend, pump
from tests.render_utils import check_screenshot, clip_problems
from ui import status_bar as sb
from ui import theme
from ui.main_window import MainWindow
from ui.onboarding import OnboardingDialog
from ui.quick_input import QuickInput
from ui.settings_dialog import SettingsDialog

sb.StatusBar._register_appbar = lambda self: None          # never reserve real screen space in a test


class RenderCase(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()
        QApplication.setCursorFlashTime(0)                  # a blinking caret would make screenshots differ
        theme.apply_theme("ember", "", None)

    def audit(self, name, widget):
        """Fail on clipped text or a changed picture; both are reported at once."""
        pump(150)
        problems = clip_problems(widget)
        changed = check_screenshot(name, widget)
        if changed:
            problems.append(changed)
        self.assertEqual(problems, [], f"{name}:\n  " + "\n  ".join(problems))


class SettingsRender(RenderCase):
    def test_every_page(self):
        dlg = SettingsDialog(StubController())
        dlg.resize(1000, 720)
        dlg.show()
        pump(200)
        try:
            failures = []
            for i, page in enumerate(dlg._pages):
                dlg.nav.setCurrentRow(i)
                pump(120)
                try:
                    self.audit(f"settings-{page.key}", dlg)
                except AssertionError as exc:
                    failures.append(str(exc))
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            dlg.reject()

    def test_minimum_size_still_fits_the_pages(self):
        dlg = SettingsDialog(StubController())
        dlg.resize(dlg.minimumWidth(), dlg.minimumHeight())
        dlg.show()
        pump(200)
        try:
            failures = []
            for i, page in enumerate(dlg._pages):
                dlg.nav.setCurrentRow(i)
                pump(100)
                failures += [f"{page.key}: {p}" for p in clip_problems(dlg)]
            self.assertEqual(failures, [])
        finally:
            dlg.reject()


class OnboardingRender(RenderCase):
    def test_every_step(self):
        wiz = OnboardingDialog(StubController())
        wiz.show()
        pump(250)
        try:
            failures = []
            for i in range(len(wiz._pages)):
                wiz._show_page(i)
                pump(250)
                try:
                    self.audit(f"onboarding-{i}", wiz)
                except AssertionError as exc:
                    failures.append(str(exc))
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            wiz.reject()


class WindowsRender(RenderCase):
    def test_main_window(self):
        backend.CONFIG["assistant_name"] = "Nova"
        window = MainWindow(Assistant())
        window.resize(900, 700)
        window.show()
        try:
            self.audit("main-window", window)
        finally:
            window.hide()

    def test_quick_box_with_a_reply(self):
        # The quick box opens "over a fullscreen app" when one really is in front (a game being played while
        # the tests run), and then installs a real keyboard hook that swallows the typing. Never here.
        import hotkeys
        import ui.quick_input as quick_module
        from tests.test_ui import FakeKeyCapture
        saved = hotkeys.KeyCapture, quick_module.fullscreen_screen
        hotkeys.KeyCapture, quick_module.fullscreen_screen = FakeKeyCapture, lambda: None
        self.addCleanup(lambda: (setattr(hotkeys, "KeyCapture", saved[0]),
                                 setattr(quick_module, "fullscreen_screen", saved[1])))
        ctl = Assistant()
        ctl.submit = lambda text, echo=True: None
        box = QuickInput(ctl)
        box.open_box()
        pump(350)
        try:
            box._awaiting = True
            box._show_reply("It's 4:24 PM, and the weather is clear with a light breeze from the west.")
            self.audit("quick-box-reply", box)
        finally:
            box.hide()

    def test_status_bar(self):
        backend.CONFIG.update(bar_height=44, bar_show_clock=False, bar_show_weather=False)
        ctl = Assistant()
        bar = sb.StatusBar(ctl)
        bar.resize(1500, 44)
        bar.show()
        pump(200)
        try:
            self.audit("status-bar", bar)
        finally:
            bar.hide()


if __name__ == "__main__":
    unittest.main()
