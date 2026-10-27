"""Capture widget checks using generated pixels only, with no screen capture.

All drag methods are called directly.  Escape is a Tk-local generated event;
these tests never send mouse or keyboard input to Windows.
"""

from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import app


class CaptureOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.desktop = Image.new("RGB", (180, 120))
        self.desktop.putdata([(x, y, (x * 7 + y * 11) % 256)
                              for y in range(120) for x in range(180)])
        self.completed = []
        self.win32 = SimpleNamespace(GetAncestor=Mock(return_value=0x100001234),
                                     SetWindowPos=Mock(return_value=True))
        self.grab_patch = patch.object(app.ImageGrab, "grab", return_value=self.desktop.copy())
        self.metrics_patch = patch.object(app, "get_virtual_screen", return_value=(-90, -45, 180, 120))
        self.win32_patch = patch.object(app.ctypes, "WinDLL", return_value=self.win32)
        self.fake_grab = self.grab_patch.start()
        self.metrics_patch.start()
        self.win32_patch.start()
        self.addCleanup(self.grab_patch.stop)
        self.addCleanup(self.metrics_patch.stop)
        self.addCleanup(self.win32_patch.stop)
        self.overlay = app.CaptureOverlay(self.root, self.completed.append)
        self.root.update()
        self.addCleanup(self._clean_overlay)

    def _clean_overlay(self):
        if self.overlay.top_level.winfo_exists():
            self.overlay.finish(None)
        self.root.update_idletasks()

    @staticmethod
    def event(x, y):
        # Include deliberately unrelated local coordinates: conversion must use
        # virtual desktop coordinates, including a monitor left/above primary.
        return SimpleNamespace(x_root=x, y_root=y, x=999, y=999)

    def assert_crop_equals(self, bounds):
        self.assertEqual(len(self.completed), 1)
        result = self.completed[0]
        self.assertIsInstance(result, Image.Image)
        expected = self.desktop.crop(bounds)
        self.assertEqual(result.size, expected.size)
        self.assertEqual(result.tobytes(), expected.tobytes())
        self.assertFalse(self.overlay.top_level.winfo_exists())

    def test_negative_virtual_origin_preserves_exact_pixels(self):
        self.overlay.press(self.event(-70, -25))
        self.overlay.move(self.event(-30, 15))
        self.overlay.release(self.event(-30, 15))
        self.assert_crop_equals((20, 20, 60, 60))
        self.fake_grab.assert_called_once_with(all_screens=True)
        positioned = self.win32.SetWindowPos.call_args.args
        self.assertEqual(positioned[0], 0x100001234)  # preserve 64-bit HWND
        self.assertEqual(positioned[2:6], (-90, -45, 180, 120))

    def test_reverse_drag_normalizes_bounds(self):
        self.overlay.press(self.event(50, 40))
        self.overlay.move(self.event(-50, -15))
        self.overlay.release(self.event(-50, -15))
        self.assert_crop_equals((40, 30, 140, 85))

    def test_tiny_selection_cancels_without_exporting_pixels(self):
        self.overlay.press(self.event(-70, -25))
        self.overlay.release(self.event(-63, 10))  # width seven pixels
        self.assertEqual(self.completed, [None])
        self.assertFalse(self.overlay.top_level.winfo_exists())

    def test_escape_binding_cancels(self):
        self.overlay.press(self.event(-70, -25))
        self.overlay.move(self.event(-30, 15))
        self.overlay.top_level.event_generate("<Escape>", when="now")
        self.root.update()
        self.assertEqual(self.completed, [None])
        self.assertFalse(self.overlay.top_level.winfo_exists())


if __name__ == "__main__":
    unittest.main()
