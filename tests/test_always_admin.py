"""Settings > Always open as administrator: the installed copy uses a scheduled task, the portable copy asks Windows
each time it opens (no task on the PC). Nothing here creates or deletes a task: elevation is replaced by mocks."""
import unittest
from unittest import mock

from rshub import elevation
from rshub.api import Api


class AlwaysAdmin(unittest.TestCase):
    def setUp(self):
        self.api = Api()
        p = mock.patch.object(Api, "settings_set", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_portable_turned_on_while_elevated_succeeds_without_a_task(self):
        """It showed "Could not set up starting as administrator." although the setting works for a portable copy."""
        with mock.patch("rshub.api.settings_api.config.PORTABLE", True), \
                mock.patch("rshub.api.settings_api.config.is_admin", return_value=True), \
                mock.patch.object(elevation, "ensure_admin_task", return_value=False) as make, \
                mock.patch.object(elevation, "delete_admin_task") as drop:
            self.assertTrue(self.api.set_always_admin(True))
            self.assertTrue(self.api.set_always_admin(False))
        make.assert_not_called()
        drop.assert_not_called()  # the installed copy's task (a PC with both) is not the portable copy's to remove

    def test_installed_copy_uses_the_task(self):
        with mock.patch("rshub.api.settings_api.config.PORTABLE", False), \
                mock.patch.object(elevation, "uses_admin_task", return_value=True), \
                mock.patch("rshub.api.settings_api.config.is_admin", return_value=True), \
                mock.patch.object(elevation, "ensure_admin_task", return_value=False) as make, \
                mock.patch.object(elevation, "delete_admin_task") as drop:
            self.assertFalse(self.api.set_always_admin(True))  # a real failure is still reported
            self.assertTrue(self.api.set_always_admin(False))
        make.assert_called_once()
        drop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
