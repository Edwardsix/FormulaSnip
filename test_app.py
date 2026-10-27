"""Application integration checks with synthetic credentials and an offline OCR mock.

The real Tk widgets and DPAPI config store are exercised. Screenshot capture,
global shortcuts, clipboard mutation, file dialogs and external OCR are mocked.
"""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import app as ui
from recognition import RecognitionError


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="config-test-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.store = ui.ConfigStore(Path(self.temporary.name) / "config")

    def settings(self, **kwargs):
        return {**ui.DEFAULTS, "api_key": "synthetic-test-key-never-sent", **kwargs}

    def test_encryption_round_trip_without_plaintext_on_disk(self):
        original = self.settings()
        self.store.save(original)
        disk_text = self.store.path.read_text("utf-8")
        self.assertNotIn(original["api_key"], disk_text)
        disk = json.loads(disk_text)
        self.assertNotIn("api_key", disk)
        self.assertTrue(disk["encrypted_api_key"].startswith("dpapi:v1:"))
        self.assertEqual(self.store.load()["api_key"], original["api_key"])

    def test_not_remembering_removes_previously_saved_key(self):
        self.store.save(self.settings())
        self.store.save(self.settings(remember_key=False))
        disk_text = self.store.path.read_text("utf-8")
        self.assertNotIn("encrypted_api_key", disk_text)
        self.assertNotIn("synthetic-test-key", disk_text)
        loaded = self.store.load()
        self.assertEqual(loaded["api_key"], "")
        self.assertFalse(loaded["remember_key"])

    def test_corrupt_or_nonobject_json_falls_back(self):
        self.store.folder.mkdir(parents=True)
        for bad in ("{broken", "[]", "null", '"a string"'):
            with self.subTest(bad=bad):
                self.store.path.write_text(bad, "utf-8")
                loaded = self.store.load()
                self.assertEqual(loaded, ui.DEFAULTS)
                self.assertTrue(self.store.warning)

    def test_invalid_types_do_not_reach_widgets_or_hotkeys(self):
        self.store.folder.mkdir(parents=True)
        self.store.path.write_text(json.dumps({
            "provider": [], "api_base": 42, "model": None,
            "mathpix_app_id": {}, "hotkey": 7,
            "auto_copy": "false", "remember_key": "true",
            "api_key": "must-not-load-plaintext",
        }), "utf-8")
        loaded = self.store.load()
        for key in ui.DEFAULTS:
            self.assertIs(type(loaded[key]), type(ui.DEFAULTS[key]), key)
        self.assertIn(loaded["provider"], ("openai", "mathpix"))
        self.assertEqual(loaded["api_key"], "")

    def test_unreadable_encrypted_key_has_clear_fallback(self):
        self.store.folder.mkdir(parents=True)
        self.store.path.write_text(json.dumps({"encrypted_api_key": "broken", "model": "custom-model"}), "utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded["api_key"], "")
        self.assertTrue(self.store.warning)

    def test_encryption_failure_leaves_previous_configuration_intact(self):
        self.store.save(self.settings())
        before = self.store.path.read_bytes()
        with patch.object(ui, "protect_secret", side_effect=ValueError("synthetic encryption failure")):
            with self.assertRaises(ValueError):
                self.store.save(self.settings(api_key="replacement-test-key"))
        self.assertEqual(self.store.path.read_bytes(), before)


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.hotkeys = self.stack.enter_context(patch.object(ui, "HotkeyManager")).return_value
        self.clipboard = self.stack.enter_context(patch.object(ui, "copy_text"))
        self.error_dialog = self.stack.enter_context(patch.object(ui.messagebox, "showerror"))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="app-test-", dir=Path(__file__).parent))
        self.root = tk.Tk()
        self.root.withdraw()
        self.application = ui.FormulaApp(self.root, ui.ConfigStore(Path(self.temp)))
        self.application.settings.update(api_key="synthetic-ui-key-never-sent", auto_copy=True)
        self.addCleanup(self.close_app)

    def close_app(self):
        if not self.application.closed:
            # Tcl shares its event queue across test roots. Cancel timers from
            # this root before destroying their registered Python callbacks.
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.application.close()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.application.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.application.busy, "Mock OCR result was not consumed")

    def put_image(self):
        self.application.set_image(Image.new("RGB", (80, 30), "white"), "synthetic image")

    def text(self):
        return self.application.editor.get("1.0", "end-1c")

    def settings_dialog(self):
        # Exercise the dialog's actual save/update logic without displaying a
        # modal window or waiting for a user-visible Toplevel to become mapped.
        dialog = ui.SettingsDialog.__new__(ui.SettingsDialog)
        dialog.app = self.application
        dialog.window = Mock()
        dialog.vars = {
            name: tk.StringVar(master=self.root, value=self.application.settings[name])
            for name in ("api_base", "model", "api_key", "mathpix_app_id", "hotkey")
        }
        dialog.provider = tk.StringVar(master=self.root, value="视觉模型（OpenAI 兼容接口）")
        dialog.previous_provider = dialog.provider.get()
        dialog.remember = tk.BooleanVar(master=self.root, value=True)
        dialog.auto_copy = tk.BooleanVar(master=self.root, value=True)
        dialog.fields = {name: Mock() for name in dialog.vars}
        dialog.hint = Mock()
        self.application.settings_dialog = dialog
        return dialog

    def test_mock_backend_updates_editor_history_and_auto_copy(self):
        self.put_image()
        formula = r"\frac{x^2}{2}"
        with patch.object(ui, "recognize", return_value=formula) as backend:
            self.application.recognize_image()
            self.assertTrue(self.application.busy)
            self.wait_idle()
        self.assertEqual(self.text(), formula)
        self.assertEqual(self.application.history, [formula])
        self.clipboard.assert_called_once_with(formula)
        self.assertIn("已复制", self.application.status.get())
        image, settings = backend.call_args.args
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(settings["api_key"], "synthetic-ui-key-never-sent")
        self.assertEqual(str(self.application.capture_button.cget("state")), "normal")
        self.error_dialog.assert_not_called()

    def test_backend_error_preserves_existing_formula(self):
        self.application.set_result("old-result")
        self.put_image()
        with patch.object(ui, "recognize", side_effect=RecognitionError("synthetic OCR failure")):
            self.application.recognize_image()
            self.wait_idle()
        self.assertEqual(self.text(), "old-result")
        self.assertEqual(self.application.history, ["old-result"])
        self.clipboard.assert_not_called()
        self.error_dialog.assert_called_once()
        self.assertEqual(self.error_dialog.call_args.args[1], "synthetic OCR failure")

    def test_clipboard_failure_does_not_lose_successful_ocr(self):
        self.put_image()
        self.clipboard.side_effect = RuntimeError("clipboard in use")
        with patch.object(ui, "recognize", return_value="a=b"):
            self.application.recognize_image()
            self.wait_idle()
        self.assertEqual(self.text(), "a=b")
        self.assertIn("剪贴板", self.application.status.get())
        self.error_dialog.assert_not_called()

    def test_auto_copy_can_be_disabled(self):
        self.put_image()
        self.application.settings["auto_copy"] = False
        with patch.object(ui, "recognize", return_value="x=y"):
            self.application.recognize_image()
            self.wait_idle()
        self.assertEqual(self.text(), "x=y")
        self.clipboard.assert_not_called()

    def test_duplicate_request_is_ignored_while_busy(self):
        self.put_image()
        with patch.object(ui, "recognize", return_value="a") as backend:
            self.application.recognize_image()
            self.application.recognize_image()
            self.wait_idle()
        backend.assert_called_once()

    def test_stale_async_result_cannot_replace_current_job(self):
        self.application.set_result("keep-this")
        self.application.job = 7
        self.application.set_busy(True)
        self.application.events.put(("result", (6, "stale")))
        self.application.poll()
        self.assertEqual(self.text(), "keep-this")
        self.assertTrue(self.application.busy)
        self.clipboard.assert_not_called()
        self.application.events.put(("result", (7, "current")))
        self.application.poll()
        self.assertEqual(self.text(), "current")
        self.assertFalse(self.application.busy)

    def test_history_is_deduplicated_and_bounded(self):
        for index in range(15):
            self.application.set_result(f"x={index}")
        self.application.set_result("x=12")
        self.assertEqual(len(self.application.history), 12)
        self.assertEqual(self.application.history[0], "x=12")
        self.assertEqual(self.application.history.count("x=12"), 1)
        self.application.history_list.selection_set(2)
        self.application.select_history(None)
        self.assertEqual(self.text(), self.application.history[2])

    def test_word_copy_uses_edited_latex(self):
        self.application.set_result("original")
        self.application.editor.delete("1.0", "end")
        self.application.editor.insert("1.0", r"\sqrt{edited}")
        with patch.object(ui, "latex_to_mathml", return_value="<math/>") as converter, \
                patch.object(ui, "build_clipboard_html", return_value="<html/>") as html, \
                patch.object(ui, "copy_mathml") as clipboard:
            self.application.copy_word()
        converter.assert_called_once_with(r"\sqrt{edited}")
        html.assert_called_once_with(r"\sqrt{edited}")
        clipboard.assert_called_once_with("<math/>", "<html/>", r"\sqrt{edited}")

    def test_docx_export_uses_edited_latex_and_selected_path(self):
        self.application.set_result("x^2")
        output = str(Path(self.temp) / "test.docx")
        with patch.object(ui, "latex_to_mathml", return_value="<math/>") as converter, \
                patch.object(ui.filedialog, "asksaveasfilename", return_value=output), \
                patch.object(ui, "save_docx") as save:
            self.application.export_word()
        converter.assert_called_once_with("x^2")
        save.assert_called_once_with("x^2", output)

    def test_transparent_image_is_flattened_onto_white(self):
        self.application.set_image(Image.new("RGBA", (10, 10), (0, 0, 0, 0)), "transparent")
        self.assertEqual(self.application.image.mode, "RGB")
        self.assertEqual(self.application.image.getpixel((0, 0)), (255, 255, 255))

    def test_settings_reject_malformed_urls_before_any_save(self):
        for value in ("https://[broken", "https://example.com:99999/v1", "https://example.com/v1?", "https://example.com\\bad", "http://example.com/v1"):
            with self.subTest(value=value):
                dialog = self.settings_dialog()
                dialog.vars["api_base"].set(value)
                self.error_dialog.reset_mock()
                with patch.object(self.application.store, "save") as save:
                    dialog.save()
                    save.assert_not_called()
                self.error_dialog.assert_called_once()
                self.assertEqual(self.error_dialog.call_args.args[0], "接口地址无效")
                dialog.window.destroy.assert_not_called()

    def test_unavailable_unchanged_hotkey_does_not_block_api_save(self):
        dialog = self.settings_dialog()
        dialog.vars["api_key"].set("replacement-synthetic-key-never-sent")
        self.hotkeys.register.reset_mock()
        self.hotkeys.register.side_effect = RuntimeError("desktop shortcut unavailable")
        with patch.object(self.application.store, "save") as save:
            dialog.save()
        self.hotkeys.register.assert_not_called()
        save.assert_called_once()
        self.assertEqual(self.application.settings["api_key"], "replacement-synthetic-key-never-sent")
        self.assertIsNone(self.application.settings_dialog)
        dialog.window.destroy.assert_called_once()
        self.error_dialog.assert_not_called()

    def test_provider_switch_clears_key_only_when_selection_changes(self):
        dialog = self.settings_dialog()
        dialog.vars["api_key"].set("first-provider-test-key")
        dialog.update_fields()
        self.assertEqual(dialog.vars["api_key"].get(), "first-provider-test-key")
        dialog.provider.set("Mathpix")
        dialog.update_fields()
        self.assertEqual(dialog.vars["api_key"].get(), "")
        dialog.vars["api_key"].set("second-provider-test-key")
        dialog.update_fields()
        self.assertEqual(dialog.vars["api_key"].get(), "second-provider-test-key")
        dialog.provider.set("视觉模型（OpenAI 兼容接口）")
        dialog.update_fields()
        self.assertEqual(dialog.vars["api_key"].get(), "")

    def test_hotkey_cleanup_failure_still_destroys_application_window(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.hotkeys.close.side_effect = RuntimeError("synthetic hotkey cleanup failure")
        with patch.object(self.root, "destroy", wraps=self.root.destroy) as destroy:
            self.application.close()
        destroy.assert_called_once()
        self.assertTrue(self.application.closed)


if __name__ == "__main__":
    unittest.main()
