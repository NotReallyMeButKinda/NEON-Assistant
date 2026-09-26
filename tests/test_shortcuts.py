"""Start menu and desktop shortcuts (shortcuts.py), from Settings and the welcome tour. use_temp_config()
points both folders at throwaway ones: the real Start menu and desktop are never touched."""

import unittest

import shortcuts

from tests import common
from tests.common import StubController, backend, pump


class ShortcutTests(unittest.TestCase):
    def setUp(self):
        common.use_temp_config()

    def test_made_read_back_and_removed(self):
        self.assertNotIn("Start Menu", str(shortcuts.path("start_menu")))      # the throwaway folder, not yours
        self.assertEqual(shortcuts.apply({"start_menu": True, "desktop": True}), [])
        self.assertTrue(shortcuts.exists("start_menu") and shortcuts.exists("desktop"))
        from win32com.client import Dispatch
        link = Dispatch("WScript.Shell").CreateShortCut(str(shortcuts.path("desktop")))
        target, arguments, folder, _icon = shortcuts.launch_target()
        self.assertEqual((link.TargetPath.lower(), link.Arguments), (target.lower(), arguments))
        self.assertTrue(link.IconLocation.lower().endswith("neon.ico,0"))
        del link
        self.assertEqual(shortcuts.apply({"desktop": False}), [])
        self.assertFalse(shortcuts.exists("desktop"))
        self.assertTrue(shortcuts.exists("start_menu"))

    def test_old_shortcuts_are_renamed_neon_assistant(self):
        self.assertEqual(shortcuts.NAME, "Neon Assistant.lnk")
        keep = shortcuts.NAME
        shortcuts.NAME = "NEON Assistant.lnk"                                 # made by an older version
        try:
            shortcuts.apply({"start_menu": True})
        finally:
            shortcuts.NAME = keep
        folder = shortcuts.path("start_menu").parent
        self.assertEqual([p.name for p in folder.iterdir()], ["NEON Assistant.lnk"])
        self.assertTrue(shortcuts.exists("start_menu"))
        self.assertEqual(len(shortcuts.rename_old()), 1)
        self.assertEqual([p.name for p in folder.iterdir()], ["Neon Assistant.lnk"])     # renamed, not deleted
        self.assertEqual(shortcuts.rename_old(), [])                                    # and only once
        from win32com.propsys import propsys, pscon
        store = propsys.SHGetPropertyStoreFromParsingName(str(shortcuts.path("start_menu")))
        self.assertEqual(store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue(), shortcuts.APP_ID)
        del store

    def test_settings_show_and_change_them(self):
        from ui.settings_dialog import SettingsDialog
        shortcuts.set_enabled("desktop", True)
        dialog = SettingsDialog(StubController())
        self.assertTrue(dialog._get["desktop_shortcut"]())                    # what's on disk
        self.assertFalse(dialog._get["start_menu_shortcut"]())
        from PySide6.QtWidgets import QCheckBox
        boxes = {b.text(): b for b in dialog.findChildren(QCheckBox)}
        boxes["Put me in the Start menu"].setChecked(True)
        boxes["Put a shortcut to me on the desktop"].setChecked(False)
        dialog._save()
        pump(20)
        self.assertTrue(shortcuts.exists("start_menu"))
        self.assertFalse(shortcuts.exists("desktop"))
        self.assertTrue(backend.CONFIG["start_menu_shortcut"])            # saving closed the dialog

    def test_the_welcome_tour_adds_the_start_menu_one_by_default(self):
        from ui.onboarding import OnboardingDialog
        backend.CONFIG["onboarding_done"] = False
        tour = OnboardingDialog(StubController())
        self.assertTrue(tour.start_menu_box.isChecked())
        self.assertFalse(tour.desktop_box.isChecked())
        tour._finish()
        self.assertTrue(shortcuts.exists("start_menu"))
        self.assertFalse(shortcuts.exists("desktop"))
        tour.deleteLater()


if __name__ == "__main__":
    unittest.main()
