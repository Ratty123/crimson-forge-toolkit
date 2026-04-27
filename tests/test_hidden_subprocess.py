import os
import subprocess
import unittest

from cdmw.core.common import hidden_subprocess_kwargs


class HiddenSubprocessTests(unittest.TestCase):
    def test_windows_hidden_subprocess_kwargs_hide_window(self) -> None:
        kwargs = hidden_subprocess_kwargs()
        if os.name != "nt":
            self.assertEqual({}, kwargs)
            return

        startupinfo = kwargs.get("startupinfo")
        self.assertIsInstance(startupinfo, subprocess.STARTUPINFO)
        self.assertEqual(int(getattr(subprocess, "SW_HIDE", 0)), startupinfo.wShowWindow)
        self.assertTrue(startupinfo.dwFlags & int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0)))
        if getattr(subprocess, "CREATE_NO_WINDOW", 0):
            self.assertEqual(getattr(subprocess, "CREATE_NO_WINDOW", 0), kwargs.get("creationflags"))


if __name__ == "__main__":
    unittest.main()
