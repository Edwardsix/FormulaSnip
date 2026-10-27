import ctypes
from ctypes import wintypes
import os
import queue
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

import windows_native as native


class ClipboardFormatTests(unittest.TestCase):
    def test_cf_html_offsets_are_utf8_byte_offsets(self):
        fragment = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>α</mi><mtext>中文 🧮</mtext></math>'
        data = native.build_cf_html(fragment)
        header = data[:data.index(b"<html>")].decode("ascii")
        offsets = {name: int(value) for name, value in
                   re.findall(r"(StartHTML|EndHTML|StartFragment|EndFragment):(\d+)", header)}
        self.assertEqual(data[offsets["StartFragment"]:offsets["EndFragment"]].decode("utf-8"), fragment)
        self.assertEqual(data[offsets["StartHTML"]:offsets["EndHTML"]], data[len(header):])
        self.assertTrue(data[offsets["StartHTML"]:].startswith(b"<html>"))

    def test_full_document_extracts_body(self):
        data = native.build_cf_html('<html><head><title>A</title></head><body><p>x</p></body></html>')
        self.assertNotIn(b"<title>", data)
        self.assertIn(b"<p>x</p>", data)

    def test_html_rejects_scripts_and_nul(self):
        for fragment in ('<script>alert(1)</script>', '<p onclick="x()">a</p>', 'a\x00b'):
            with self.subTest(fragment=fragment), self.assertRaises(ValueError):
                native.build_cf_html(fragment)


class HotkeyTests(unittest.TestCase):
    def test_parser_normalization(self):
        self.assertEqual(native.parse_hotkey(" shift + alt + control + m "), (7, ord("M"), "Ctrl+Alt+Shift+M"))
        self.assertEqual(native.parse_hotkey("Alt+F11"), (1, 0x7A, "Alt+F11"))
        self.assertEqual(native.parse_hotkey("Ctrl+7"), (2, ord("7"), "Ctrl+7"))

    def test_parser_rejects_reserved_and_invalid(self):
        for value in ("M", "Ctrl+M+Q", "Ctrl++M", "Ctrl+Ctrl+M", "Win+M", "Ctrl+F12", "Ctrl+F13", "Ctrl+", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.parse_hotkey(value)

    def test_registration_conflict_keeps_old_and_close_unregisters(self):
        first, second = native.HotkeyManager(queue.Queue()), native.HotkeyManager(queue.Queue())
        try:
            # Use an uncommon combination to avoid normal application shortcuts.
            try:
                first.register("Ctrl+Alt+Shift+F10")
                second.register("Ctrl+Alt+Shift+F11")
            except ValueError as exc:
                self.skipTest(str(exc))
            with self.assertRaises(ValueError):
                second.register("Ctrl+Alt+Shift+F10")
            # The failed replacement must preserve the second manager's old shortcut.
            with self.assertRaises(ValueError):
                first.register("Ctrl+Alt+Shift+F11")
            first.register("Ctrl+Alt+Shift+F10")  # setting same shortcut is idempotent
            first.close()
            first.close()
            self.assertFalse(first._thread.is_alive())
            second.register("Ctrl+Alt+Shift+F10")
        finally:
            first.close()
            second.close()

    def test_worker_notifies_queue_without_tk(self):
        events = queue.Queue()
        manager = native.HotkeyManager(events)
        try:
            try:
                manager.register("Ctrl+Alt+Shift+F9")
            except ValueError as exc:
                self.skipTest(str(exc))
            # Inject only a thread message into this helper's own queue; no user input.
            self.assertTrue(native._PostThreadMessage(manager._thread_id, 0x0312, 101, 0))
            self.assertEqual(events.get(timeout=2), ("hotkey", None))
        finally:
            manager.close()

    def test_worker_lifecycle_with_mocked_registration(self):
        registered = {}

        def fake_register(hwnd, ident, modifiers, key):
            pair = (modifiers, key)
            if pair in registered.values():
                ctypes.set_last_error(1409)
                return False
            registered[ident] = pair
            return True

        def fake_unregister(hwnd, ident):
            registered.pop(ident, None)
            return True

        events = queue.Queue()
        with patch.object(native, "_RegisterHotKey", side_effect=fake_register), \
                patch.object(native, "_UnregisterHotKey", side_effect=fake_unregister):
            manager = native.HotkeyManager(events)
            try:
                manager.register("Ctrl+Alt+M")
                self.assertEqual(len(registered), 1)
                manager.register("Alt+Ctrl+M")
                self.assertEqual(len(registered), 1)
                self.assertTrue(native._PostThreadMessage(manager._thread_id, 0x0312, 101, 0))
                self.assertEqual(events.get(timeout=2), ("hotkey", None))
                manager.register("Ctrl+Alt+N")
                self.assertEqual(len(registered), 1)
                self.assertEqual(list(registered.values())[0][1], ord("N"))
            finally:
                manager.close()
            self.assertFalse(manager._thread.is_alive())
            self.assertEqual(registered, {})


class DpapiTests(unittest.TestCase):
    def test_roundtrip_and_empty(self):
        secret = "test-only-FormulaSnip-密钥-123"
        encrypted = native.protect_secret(secret)
        self.assertTrue(encrypted.startswith("dpapi:v1:"))
        self.assertNotIn(secret, encrypted)
        self.assertEqual(native.unprotect_secret(encrypted), secret)
        self.assertNotEqual(encrypted, native.protect_secret(secret))
        self.assertEqual(native.protect_secret(""), "")
        self.assertEqual(native.unprotect_secret(""), "")

    def test_rejects_plaintext_or_corrupt_credential(self):
        for value in ("sk-plaintext", "dpapi:v1:bad!", "dpapi:v1:YWJj"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.unprotect_secret(value)

    def test_virtual_screen_has_positive_dimensions(self):
        left, top, width, height = native.get_virtual_screen()
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)


class SingleInstanceTests(unittest.TestCase):
    def test_mutex_blocks_other_process_then_releases(self):
        if not native.acquire_single_instance():
            self.skipTest("Another FormulaSnip process is already running")
        command = [sys.executable, "-c", "import windows_native as n; print(n.acquire_single_instance())"]
        try:
            self.assertTrue(native.acquire_single_instance())  # idempotent locally
            result = subprocess.run(command, cwd=os.path.dirname(__file__), capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "False")
        finally:
            native.release_single_instance()
            native.release_single_instance()
        result = subprocess.run(command, cwd=os.path.dirname(__file__), capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stdout.strip(), "True")


@unittest.skipUnless(os.environ.get("FORMULASNIP_TEST_CLIPBOARD") == "1", "Explicit clipboard test opt-in required")
class NativeClipboardTests(unittest.TestCase):
    def test_unicode_roundtrip(self):
        sample = "FormulaSnip test: α + 中文"
        native.copy_text(sample)
        getter = native._signature(native._user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
        self.assertTrue(native._OpenClipboard(None))
        try:
            handle = getter(13)
            self.assertTrue(handle)
            pointer = native._GlobalLock(handle)
            try:
                self.assertEqual(ctypes.wstring_at(pointer), sample)
            finally:
                native._GlobalUnlock(handle)
        finally:
            native._CloseClipboard()


if __name__ == "__main__":
    unittest.main()
