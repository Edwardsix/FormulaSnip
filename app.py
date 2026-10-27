"""FormulaSnip — Windows screenshot-to-formula utility."""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from PIL import Image, ImageEnhance, ImageGrab, ImageOps, ImageTk, UnidentifiedImageError

from recognition import DEFAULT_MODEL, RecognitionError, clean_latex, endpoint_url, recognize
from windows_native import (HotkeyManager, copy_mathml, copy_text, enable_dpi_awareness,
                            get_virtual_screen, protect_secret, unprotect_secret,
                            acquire_single_instance, release_single_instance)
from word_export import build_clipboard_html, latex_to_mathml, save_docx

APP_NAME = "FormulaSnip 公式快贴"
VERSION = "1.0.0"
BG = "#f3f6fb"
PANEL = "#ffffff"
INK = "#14243b"
MUTED = "#63758d"
ACCENT = "#2864eb"
LINE = "#dce4ee"
DEMO = r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"
DEFAULTS = {
    "provider": "openai", "api_base": "https://api.openai.com/v1",
    "model": DEFAULT_MODEL, "api_key": "", "mathpix_app_id": "",
    "hotkey": "Ctrl+Alt+M", "auto_copy": True, "remember_key": True,
}


class ConfigStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self.path = folder / "settings.json"
        self.warning = ""

    def load(self):
        settings = dict(DEFAULTS)
        try:
            if not self.path.exists():
                return settings
            data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置内容无效")
            for key, default in DEFAULTS.items():
                if key != "api_key" and key in data and type(data[key]) is type(default):
                    settings[key] = data[key]
            if settings["provider"] not in ("openai", "mathpix"):
                settings["provider"] = DEFAULTS["provider"]
            if data.get("encrypted_api_key"):
                settings["api_key"] = unprotect_secret(data["encrypted_api_key"])
        except Exception:
            self.warning = "未能读取已保存设置。请重新填写识别服务；密钥可能属于另一个 Windows 用户。"
        return settings

    def save(self, settings):
        data = {k: v for k, v in settings.items() if k in DEFAULTS and k != "api_key"}
        if settings.get("remember_key") and settings.get("api_key"):
            data["encrypted_api_key"] = protect_secret(settings["api_key"])
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        temporary.replace(self.path)


def button(parent, text, command, primary=False, **kwargs):
    return tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                     bg=ACCENT if primary else "#eaf0fb", fg="white" if primary else INK,
                     activebackground="#1c4fc1" if primary else "#dce7fa",
                     activeforeground="white" if primary else INK,
                     font=("Microsoft YaHei UI", 10), padx=15, pady=10,
                     cursor="hand2", highlightthickness=0, **kwargs)


class CaptureOverlay:
    """Frozen virtual desktop; all drag coordinates refer to the captured bitmap."""
    def __init__(self, root, completed):
        self.root, self.completed = root, completed
        self.left, self.top, self.width, self.height = get_virtual_screen()
        self.screen = ImageGrab.grab(all_screens=True).convert("RGB")
        if self.screen.size != (self.width, self.height):
            raise RuntimeError("显示器尺寸发生变化，请重新截图。")
        self.top_level = tk.Toplevel(root)
        self.top_level.withdraw()
        self.top_level.overrideredirect(True)
        self.top_level.attributes("-topmost", True)
        self.top_level.geometry(f"{self.width}x{self.height}+0+0")
        self.canvas = tk.Canvas(self.top_level, width=self.width, height=self.height,
                                highlightthickness=0, cursor="crosshair", bg="black")
        self.canvas.pack(fill="both", expand=True)
        self.dark = ImageTk.PhotoImage(ImageEnhance.Brightness(self.screen).enhance(.40))
        self.canvas.create_image(0, 0, image=self.dark, anchor="nw")
        self.canvas.create_text(max(32, -self.left + 32), max(32, -self.top + 32),
                                anchor="nw", fill="white", font=("Microsoft YaHei UI", 14),
                                text="拖动框选公式  ·  松开开始识别  ·  Esc / 右键取消")
        self.start = None
        self.crop_ref = None
        self.rect = None
        self.crop_id = None
        self.top_level.bind("<Escape>", lambda _e: self.finish(None))
        self.canvas.bind("<Button-3>", lambda _e: self.finish(None))
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        try:
            self.top_level.deiconify()
            self.top_level.update_idletasks()
            # Tk's winfo_id identifies its client window.  Position the native
            # outer window and preserve HWND width on 64-bit Windows.
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_ancestor = user32.GetAncestor
            get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
            get_ancestor.restype = wintypes.HWND
            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = [wintypes.HWND, wintypes.HWND,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.UINT]
            set_window_pos.restype = wintypes.BOOL
            hwnd = get_ancestor(self.top_level.winfo_id(), 2)  # GA_ROOT
            if not hwnd or not set_window_pos(hwnd, ctypes.c_void_p(-1), self.left, self.top,
                                              self.width, self.height, 0x0040):
                raise RuntimeError("无法覆盖所有显示器，请重新截图。")
            self.top_level.update_idletasks()
            self.top_level.lift()
            self.top_level.focus_force()
            self.top_level.grab_set()
        except Exception:
            # A half-created topmost overlay must never block the restored app.
            self.top_level.destroy()
            raise

    def point(self, event):
        # Root screen coordinates stay correct even when dragging outside a
        # display or over a differently scaled monitor with a negative origin.
        x = event.x_root - self.left
        y = event.y_root - self.top
        return max(0, min(self.width, x)), max(0, min(self.height, y))

    def press(self, event):
        self.start = self.point(event)

    def bounds(self, event):
        x, y = self.point(event)
        x0, y0 = self.start
        return min(x, x0), min(y, y0), max(x, x0), max(y, y0)

    def move(self, event):
        if self.start is None:
            return
        bounds = self.bounds(event)
        x1, y1, x2, y2 = bounds
        if self.crop_id:
            self.canvas.delete(self.crop_id)
        if self.rect:
            self.canvas.delete(self.rect)
        if x2 > x1 and y2 > y1:
            self.crop_ref = ImageTk.PhotoImage(self.screen.crop(bounds))
            self.crop_id = self.canvas.create_image(x1, y1, image=self.crop_ref, anchor="nw")
        self.rect = self.canvas.create_rectangle(*bounds, outline="#60a5ff", width=2)

    def release(self, event):
        if self.start is None:
            return
        bounds = self.bounds(event)
        if bounds[2] - bounds[0] < 8 or bounds[3] - bounds[1] < 8:
            self.finish(None)
        else:
            self.finish(self.screen.crop(bounds))

    def finish(self, image):
        if not self.top_level.winfo_exists():
            return
        self.top_level.grab_release()
        self.top_level.destroy()
        self.completed(image)


class SettingsDialog:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        w = self.window
        w.title("识别服务与快捷键")
        w.configure(bg=PANEL)
        w.resizable(False, False)
        w.transient(app.root)
        w.grab_set()
        w.geometry("650x700")
        w.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(w, text="识别服务与快捷键", bg=PANEL, fg=INK,
                 font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", padx=28, pady=(24, 4))
        tk.Label(w, text="截图只在你点击识别或完成框选后，发送至下面选择的服务。",
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=28, pady=(0, 18))
        form = tk.Frame(w, bg=PANEL)
        form.pack(fill="x", padx=28)
        self.vars = {key: tk.StringVar(value=str(app.settings.get(key, "")))
                     for key in ["api_base", "model", "api_key", "mathpix_app_id", "hotkey"]}
        self.provider = tk.StringVar(value="Mathpix" if app.settings["provider"] == "mathpix"
                                     else "视觉模型（OpenAI 兼容接口）")
        self.previous_provider = self.provider.get()
        self.remember = tk.BooleanVar(value=app.settings["remember_key"])
        self.auto_copy = tk.BooleanVar(value=app.settings["auto_copy"])
        tk.Label(form, text="识别方式", bg=PANEL, fg=INK).grid(row=0, column=0, sticky="w", pady=8)
        combo = ttk.Combobox(form, textvariable=self.provider, state="readonly", width=38,
                             values=["视觉模型（OpenAI 兼容接口）", "Mathpix"])
        combo.grid(row=0, column=1, sticky="ew", pady=8)
        self.fields = {}
        rows = [("api_base", "接口地址"), ("model", "视觉模型名称"),
                ("mathpix_app_id", "Mathpix App ID"), ("api_key", "API Key"), ("hotkey", "全局截图快捷键")]
        for row, (key, label) in enumerate(rows, 1):
            tk.Label(form, text=label, bg=PANEL, fg=INK).grid(row=row, column=0, sticky="w", pady=8, padx=(0, 18))
            entry = ttk.Entry(form, textvariable=self.vars[key], width=43,
                              show="●" if key == "api_key" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            self.fields[key] = entry
        form.columnconfigure(1, weight=1)
        self.show_key = tk.BooleanVar(value=False)
        tk.Checkbutton(w, text="显示密钥", variable=self.show_key, bg=PANEL,
                       command=lambda: self.fields["api_key"].configure(show="" if self.show_key.get() else "●")) .pack(anchor="w", padx=178)
        tk.Checkbutton(w, text="保存密钥（使用 Windows 当前用户加密）", variable=self.remember,
                       bg=PANEL, fg=INK).pack(anchor="w", padx=28, pady=(14, 4))
        tk.Checkbutton(w, text="识别完成后自动复制 LaTeX", variable=self.auto_copy,
                       bg=PANEL, fg=INK).pack(anchor="w", padx=28, pady=4)
        self.hint = tk.Label(w, justify="left", wraplength=580, bg="#f1f5fd", fg=MUTED,
                             padx=14, pady=13, font=("Microsoft YaHei UI", 10))
        self.hint.pack(fill="x", padx=28, pady=16)
        actions = tk.Frame(w, bg=PANEL)
        actions.pack(side="bottom", fill="x", padx=28, pady=22)
        button(actions, "保存设置", self.save, primary=True).pack(side="right")
        button(actions, "取消", self.close).pack(side="right", padx=10)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.update_fields())
        self.update_fields()
        w.wait_visibility()
        w.focus_force()

    def update_fields(self):
        mathpix = self.provider.get() == "Mathpix"
        if self.provider.get() != self.previous_provider:
            self.vars["api_key"].set("")
            self.previous_provider = self.provider.get()
        for key in ["api_base", "model"]:
            self.fields[key].configure(state="disabled" if mathpix else "normal")
        self.fields["mathpix_app_id"].configure(state="normal" if mathpix else "disabled")
        self.hint.configure(text=(
            "Mathpix：填写 App ID 与 App Key。此处的 API Key 即 Mathpix App Key。\n"
            "快捷键示例：Ctrl+Alt+M、Ctrl+Shift+F9。修改后立即生效。" if mathpix else
            "官方接口地址：https://api.openai.com/v1\n"
            "也可填写兼容接口及其支持图片输入的模型名称。Key 必须属于该接口服务。\n"
            "API 服务可能单独计费；ChatGPT 订阅不等于 API 额度。"))

    def save(self):
        settings = dict(self.app.settings)
        settings.update({key: var.get().strip() for key, var in self.vars.items()})
        settings.update(provider="mathpix" if self.provider.get() == "Mathpix" else "openai",
                        remember_key=self.remember.get(), auto_copy=self.auto_copy.get())
        if settings["provider"] == "mathpix" and not settings["mathpix_app_id"]:
            messagebox.showerror("还需要一项设置", "请输入 Mathpix App ID。", parent=self.window)
            return
        if settings["provider"] == "openai":
            try:
                endpoint_url(settings["api_base"])
            except RecognitionError as exc:
                messagebox.showerror("接口地址无效", str(exc), parent=self.window)
                return
            if not settings["model"]:
                messagebox.showerror("还需要一项设置", "请输入支持图片输入的模型名称。", parent=self.window)
                return
        previous = self.app.settings["hotkey"]
        try:
            # An unavailable desktop hotkey must not block unrelated API settings.
            if settings["hotkey"] != previous:
                self.app.hotkeys.register(settings["hotkey"])
            self.app.store.save(settings)
        except Exception as exc:
            try:
                self.app.hotkeys.register(previous)
            except Exception:
                pass
            messagebox.showerror("设置未保存", str(exc), parent=self.window)
            return
        self.app.settings = settings
        self.app.update_service()
        self.app.status.set("设置已保存。现在可以截图或导入图片。")
        self.close()

    def close(self):
        self.app.settings_dialog = None
        self.window.destroy()


class FormulaApp:
    def __init__(self, root, store):
        self.root, self.store = root, store
        self.settings = store.load()
        self.events = queue.Queue()
        self.hotkeys = HotkeyManager(self.events)
        self.image = None
        self.preview_ref = None
        self.history = []
        self.busy = False
        self.capturing = False
        self.settings_dialog = None
        self.preview_files = []
        self.last_result = ""
        self.job = 0
        self.closed = False
        root.title(f"{APP_NAME}  |  {VERSION}")
        root.geometry("1080x780")
        root.minsize(980, 740)
        root.configure(bg=BG)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.status = tk.StringVar(value="按 Ctrl+Alt+M 开始，或先载入示例体验复制与导出。")
        self.build_ui()
        try:
            self.hotkeys.register(self.settings["hotkey"])
        except Exception as exc:
            self.status.set(f"快捷键不可用：{exc}。仍可点击“截图识别”。")
        self.update_service()
        if store.warning:
            self.status.set(store.warning)
        root.after(80, self.poll)

    def build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=30, pady=(24, 16))
        tk.Label(header, text="ƒ", font=("Georgia", 32, "bold"), fg=ACCENT, bg=BG).pack(side="left", padx=(0, 13))
        name = tk.Frame(header, bg=BG)
        name.pack(side="left")
        tk.Label(name, text="FormulaSnip 公式快贴", font=("Microsoft YaHei UI", 22, "bold"),
                 fg=INK, bg=BG).pack(anchor="w")
        tk.Label(name, text="截取公式，把灵感留在文档里。", font=("Microsoft YaHei UI", 10),
                 fg=MUTED, bg=BG).pack(anchor="w", pady=(4, 0))
        button(header, "设置", self.open_settings).pack(side="right")
        button(header, "帮助", self.show_help).pack(side="right", padx=(0, 8))
        self.service_label = tk.Label(header, text="", bg=BG, fg=MUTED, justify="right")
        self.service_label.pack(side="right", padx=18)
        toolbar = tk.Frame(self.root, bg=BG)
        toolbar.pack(fill="x", padx=30, pady=(0, 18))
        self.capture_button = button(toolbar, "＋  截图识别", self.capture, primary=True)
        self.capture_button.pack(side="left")
        self.hotkey_label = tk.Label(toolbar, text="Ctrl + Alt + M", bg=BG, fg=MUTED)
        self.hotkey_label.pack(side="left", padx=14)
        self.import_button = button(toolbar, "导入图片", self.import_image)
        self.import_button.pack(side="left", padx=(10, 8))
        self.paste_button = button(toolbar, "粘贴图片", self.paste_image)
        self.paste_button.pack(side="left")
        button(toolbar, "载入示例", self.load_demo).pack(side="right")
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=30)
        body.columnconfigure(0, weight=4, uniform="panels")
        body.columnconfigure(1, weight=6, uniform="panels")
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        tk.Label(left, text="01  图片", font=("Microsoft YaHei UI", 12, "bold"),
                 bg=PANEL, fg=INK).pack(anchor="w", padx=20, pady=(18, 12))
        self.image_canvas = tk.Canvas(left, bg="#f8faff", highlightthickness=0, height=240)
        self.image_canvas.pack(fill="both", expand=True, padx=20)
        self.image_canvas.bind("<Configure>", lambda _e: self.draw_preview())
        self.image_info = tk.Label(left, text="框选时尽量只保留公式，避免大段正文。", bg=PANEL, fg=MUTED,
                                   wraplength=330, justify="left")
        self.image_info.pack(anchor="w", padx=20, pady=(12, 10))
        self.recognize_button = button(left, "识别当前图片", self.recognize_image, primary=True)
        self.recognize_button.pack(fill="x", padx=20, pady=(0, 18))
        tk.Frame(left, bg=LINE, height=1).pack(fill="x", padx=20)
        tk.Label(left, text="本次会话 · 最近公式", bg=PANEL, fg=MUTED).pack(anchor="w", padx=20, pady=(13, 7))
        self.history_list = tk.Listbox(left, height=4, bg=PANEL, fg=INK, bd=0,
                                      highlightthickness=0, selectbackground="#e8efff",
                                      selectforeground=INK, activestyle="none", exportselection=False,
                                      font=("Consolas", 10))
        self.history_list.pack(fill="x", padx=20, pady=(0, 18))
        self.history_list.bind("<<ListboxSelect>>", self.select_history)
        right = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew")
        tk.Label(right, text="02  LaTeX 结果", font=("Microsoft YaHei UI", 12, "bold"),
                 bg=PANEL, fg=INK).pack(anchor="w", padx=20, pady=(18, 8))
        tk.Label(right, text="识别后可在这里修改，也可以直接输入公式。", bg=PANEL, fg=MUTED).pack(anchor="w", padx=20, pady=(0, 12))
        edit_frame = tk.Frame(right, bg=LINE)
        edit_frame.pack(fill="both", expand=True, padx=20)
        self.editor = tk.Text(edit_frame, wrap="word", font=("Consolas", 13), bg="#fbfcff", fg=INK,
                              insertbackground=ACCENT, relief="flat", padx=14, pady=14,
                              undo=True, height=10, width=30, highlightthickness=1, highlightbackground=LINE)
        scrollbar = ttk.Scrollbar(edit_frame, command=self.editor.yview)
        self.editor.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)
        tk.Label(right, text="03  复制或导出", font=("Microsoft YaHei UI", 12, "bold"),
                 bg=PANEL, fg=INK).pack(anchor="w", padx=20, pady=(18, 10))
        actions = tk.Frame(right, bg=PANEL)
        actions.pack(fill="x", padx=20)
        button(actions, "复制 LaTeX", self.copy_latex, primary=True).pack(side="left", padx=(0, 8))
        button(actions, "复制 Word 公式", self.copy_word).pack(side="left")
        actions2 = tk.Frame(right, bg=PANEL)
        actions2.pack(fill="x", padx=20, pady=(10, 12))
        button(actions2, "导出 Word (.docx)", self.export_word).pack(side="left", padx=(0, 8))
        button(actions2, "预览公式", self.preview_formula).pack(side="left")
        tk.Label(right, text="Word 粘贴兼容性因版本而异；导出 .docx 可保留可编辑公式。",
                 bg=PANEL, fg=MUTED, wraplength=500, justify="left", font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=20, pady=(0, 18))
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=30, pady=18)
        tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED, anchor="w", wraplength=1000,
                 justify="left").pack(fill="x")
        self.root.bind("<Control-Return>", lambda _e: self.recognize_image())

    def update_service(self):
        provider = "Mathpix" if self.settings["provider"] == "mathpix" else "视觉模型"
        configured = bool(self.settings.get("api_key"))
        self.service_label.configure(text=f"{provider} · {'已配置' if configured else '请先设置 Key'}",
                                     fg="#21815b" if configured else MUTED)
        self.hotkey_label.configure(text=self.settings["hotkey"].replace("+", " + "))

    def open_settings(self):
        if self.busy or self.capturing:
            self.status.set("请等待当前操作完成后再修改设置。")
            return
        if self.settings_dialog:
            self.settings_dialog.window.lift()
            return
        self.settings_dialog = SettingsDialog(self)

    def show_help(self):
        folder = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
        guide = folder / "使用说明.html"
        if guide.is_file():
            webbrowser.open(guide.as_uri())
        else:
            messagebox.showinfo("快速开始", "1. 在设置中填写服务和 API Key。\n2. 按 Ctrl+Alt+M，拖动框选公式。\n3. 复制 LaTeX、复制 Word 公式，或导出 .docx。\n\n按 Esc 取消截图。最小化后快捷键仍可用，关闭窗口即退出。", parent=self.root)

    def poll(self):
        if self.closed:
            return
        for _ in range(20):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "hotkey":
                self.capture()
            elif kind == "result":
                job, result = value
                if job == self.job:
                    self.set_busy(False)
                    self.set_result(result)
                    self.status.set("识别完成。请核对上下标、符号及括号。")
                    if self.settings.get("auto_copy"):
                        try:
                            copy_text(result)
                            self.status.set("识别完成，LaTeX 已复制。也可复制 Word 公式或导出 .docx。")
                        except Exception:
                            self.status.set("识别完成。剪贴板正被占用，请点击“复制 LaTeX”。")
            elif kind == "error":
                job, message = value
                if job == self.job:
                    self.set_busy(False)
                    self.status.set("识别未完成，当前编辑内容已保留。")
                    messagebox.showerror("识别未完成", message, parent=self.root)
        self.root.after(80, self.poll)

    def set_busy(self, value):
        self.busy = value
        for widget in [self.capture_button, self.import_button, self.paste_button, self.recognize_button]:
            widget.configure(state="disabled" if value else "normal")
        self.recognize_button.configure(text="正在识别，请稍候…" if value else "识别当前图片")

    def capture(self):
        if self.busy or self.capturing or self.settings_dialog:
            return
        self.capturing = True
        self.root.withdraw()
        self.root.after(250, self.begin_capture)

    def begin_capture(self):
        try:
            self.overlay = CaptureOverlay(self.root, self.capture_complete)
        except Exception as exc:
            self.capture_complete(None)
            messagebox.showerror("截图失败", str(exc), parent=self.root)

    def capture_complete(self, image):
        self.capturing = False
        self.root.deiconify()
        self.root.lift()
        if image is None:
            self.status.set("已取消截图。")
            return
        self.set_image(image, "屏幕截图")
        self.recognize_image()

    def set_image(self, image, source):
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            background = Image.new("RGBA", image.size, "white")
            image = Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")
        else:
            image = image.convert("RGB")
        # Limit upload size while keeping normal formula crops at original resolution.
        image.thumbnail((3200, 3200), Image.Resampling.LANCZOS)
        self.image = image
        self.image_info.configure(text=f"{source}  ·  {image.width} × {image.height} 像素")
        self.draw_preview()
        self.status.set("图片已就绪，点击“识别当前图片”。")

    def draw_preview(self):
        canvas = self.image_canvas
        canvas.delete("all")
        width, height = max(100, canvas.winfo_width()), max(100, canvas.winfo_height())
        if self.image is None:
            canvas.create_text(width / 2, height / 2 - 15, text="框选一条公式", fill=INK,
                               font=("Microsoft YaHei UI", 17, "bold"))
            canvas.create_text(width / 2, height / 2 + 22, text="截图 · 导入 · 粘贴", fill=MUTED,
                               font=("Microsoft YaHei UI", 10))
            return
        image = self.image.copy()
        image.thumbnail((max(1, width - 28), max(1, height - 28)), Image.Resampling.LANCZOS)
        self.preview_ref = ImageTk.PhotoImage(image)
        canvas.create_image(width / 2, height / 2, image=self.preview_ref)

    def import_image(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.root, title="选择公式图片",
                                          filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("图片过大，请裁剪至公式区域后再导入。")
                self.set_image(image.copy(), Path(path).name)
        except Exception:
            messagebox.showerror("无法导入", "图片无法读取或尺寸过大。请使用较小的 PNG、JPG 或 BMP 图片。", parent=self.root)

    def paste_image(self):
        if self.busy:
            return
        try:
            image = ImageGrab.grabclipboard()
            if isinstance(image, Image.Image):
                if image.width * image.height > 40_000_000:
                    raise ValueError("图片过大，请先裁剪。")
                self.set_image(image, "剪贴板图片")
            elif isinstance(image, list) and image:
                with Image.open(image[0]) as file_image:
                    if file_image.width * file_image.height > 40_000_000:
                        raise ValueError("图片过大，请先裁剪。")
                    self.set_image(file_image.copy(), "剪贴板文件")
            else:
                raise ValueError("剪贴板里没有图片。请先复制图片或截图。")
        except Exception as exc:
            messagebox.showerror("无法粘贴", str(exc), parent=self.root)

    def recognize_image(self):
        if self.busy or self.capturing:
            return
        if self.image is None:
            self.status.set("请先截图、导入图片或粘贴图片。")
            return
        if not self.settings.get("api_key"):
            self.status.set("图片已保留。请先填写识别服务和 API Key，再点击识别。")
            self.open_settings()
            return
        if self.settings["provider"] == "mathpix" and not self.settings.get("mathpix_app_id"):
            self.open_settings()
            return
        buffer = io.BytesIO()
        self.image.save(buffer, format="PNG")
        png = buffer.getvalue()
        self.job += 1
        job = self.job
        settings = dict(self.settings)
        self.set_busy(True)
        self.status.set("正在向所选服务识别公式；通常需要数秒，请稍候。")
        def worker():
            try:
                result = recognize(png, settings)
                self.events.put(("result", (job, result)))
            except RecognitionError as exc:
                self.events.put(("error", (job, str(exc))))
            except Exception:
                self.events.put(("error", (job, "识别时遇到异常。请检查网络和服务设置后重试。")))
        threading.Thread(target=worker, daemon=True, name="formula-recognition").start()

    def set_result(self, result):
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", result)
        self.last_result = result
        self.history = [result] + [entry for entry in self.history if entry != result]
        self.history = self.history[:12]
        self.history_list.delete(0, "end")
        for entry in self.history:
            self.history_list.insert("end", entry.replace("\n", " ")[:65])

    def select_history(self, _event):
        selection = self.history_list.curselection()
        if selection:
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", self.history[selection[0]])
            self.status.set("已恢复这条公式，可继续编辑或导出。")

    def load_demo(self):
        if self.busy:
            return
        self.set_result(DEMO)
        self.status.set("已载入内置示例（未调用识别服务）。可体验复制、预览与 Word 导出。")

    def get_latex(self):
        source = self.editor.get("1.0", "end-1c").strip()
        if not source:
            raise ValueError("请先识别图片或输入 LaTeX，也可点击“载入示例”。")
        return clean_latex(source)

    def copy_latex(self):
        try:
            copy_text(self.get_latex())
            self.status.set("LaTeX 已复制。Word 可按 Alt+= 插入公式，切换为 LaTeX 输入后粘贴。")
        except Exception as exc:
            messagebox.showerror("无法复制", str(exc), parent=self.root)

    def copy_word(self):
        try:
            latex = self.get_latex()
            copy_mathml(latex_to_mathml(latex), build_clipboard_html(latex), latex)
            self.status.set("Word 公式已复制。在 Word 正文按 Ctrl+V；若显示源码，请改用“导出 Word”。")
        except Exception as exc:
            messagebox.showerror("公式转换未完成", str(exc), parent=self.root)

    def export_word(self):
        try:
            latex = self.get_latex()
            # Validate conversion before opening the save dialog.
            latex_to_mathml(latex)
        except Exception as exc:
            messagebox.showerror("无法导出", str(exc), parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出可编辑 Word 公式",
                                          defaultextension=".docx", initialfile="公式.docx",
                                          filetypes=[("Word 文档", "*.docx")])
        if not path:
            return
        try:
            save_docx(latex, path)
            self.status.set(f"已导出：{path}。打开文档即可编辑公式，或复制到其他 Word 文档。")
        except Exception as exc:
            messagebox.showerror("导出未完成", str(exc), parent=self.root)

    def preview_formula(self):
        try:
            mathml = latex_to_mathml(self.get_latex())
            html = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>FormulaSnip 公式预览</title>"
                    "<style>body{margin:60px;font-family:Segoe UI,Microsoft YaHei,sans-serif;background:#f3f6fb;color:#14243b}"
                    "main{padding:40px;background:white;border:1px solid #dce4ee;border-radius:16px;overflow:auto}"
                    "math{font-size:32px}p{color:#63758d}</style><h2>公式预览</h2><main>" + mathml +
                    "</main><p>离线 MathML 预览；建议使用 Microsoft Edge 或 Chrome。Word 导出为可编辑公式。</p></html>")
            self.store.folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".html", prefix="preview-", dir=self.store.folder,
                                             encoding="utf-8", delete=False) as file:
                file.write(html)
                path = Path(file.name)
            self.preview_files.append(path)
            webbrowser.open(path.as_uri())
            self.status.set("已在默认浏览器打开离线预览。")
        except Exception as exc:
            messagebox.showerror("无法预览", str(exc), parent=self.root)

    def close(self):
        self.closed = True
        try:
            self.hotkeys.close()
        except Exception:
            pass
        finally:
            for file in self.preview_files:
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    pass
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--self-check-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    enable_dpi_awareness()
    root = tk.Tk()
    if args.self_check_output:
        root.withdraw()
        report = {"version": VERSION, "tk": root.tk.call("info", "patchlevel")}
        from windows_native import parse_hotkey
        report["hotkey_parser"] = parse_hotkey("Ctrl+Alt+M")[2]
        report["screen_size"] = list(get_virtual_screen())
        report["screenshot_size"] = list(ImageGrab.grab(all_screens=True).size)
        report["mathml"] = "<mfrac>" in latex_to_mathml(DEMO)
        sample = args.self_check_output.with_suffix(".docx")
        save_docx(DEMO, str(sample))
        report["docx_bytes"] = sample.stat().st_size
        sample.unlink()
        protected = protect_secret("self-check-synthetic-key")
        report["encrypted_config"] = unprotect_secret(protected) == "self-check-synthetic-key"
        manager = HotkeyManager(queue.Queue())
        try:
            manager.register("Ctrl+Alt+Shift+F8")
            report["global_hotkey"] = True
        finally:
            manager.close()
        args.self_check_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        root.destroy()
        return
    if not acquire_single_instance():
        root.withdraw()
        messagebox.showinfo("工具已经打开", "FormulaSnip 已在运行，请从任务栏打开现有窗口。", parent=root)
        root.destroy()
        return
    folder = args.config_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "FormulaSnip"
    app = FormulaApp(root, ConfigStore(folder))
    def report_callback_exception(_type, _value, _traceback):
        if not app.closed:
            messagebox.showerror("操作未完成", "遇到意外错误，请重试。若持续发生，请重新启动工具。", parent=root)
    root.report_callback_exception = report_callback_exception
    try:
        root.mainloop()
    finally:
        release_single_instance()


if __name__ == "__main__":
    main()
