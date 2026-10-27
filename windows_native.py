"""Small, dependency-free Win32 helpers used by FormulaSnip.

Only the hotkey worker owns its thread message queue.  It communicates with the
Tk main thread through the caller's Queue and never calls Tk from its worker.
"""

from __future__ import annotations

import base64
import atexit
import ctypes
from ctypes import wintypes
import hashlib
import os
import queue
import re
import threading
import time


if os.name != "nt":
    raise OSError("FormulaSnip 的桌面功能需要 Windows。")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)


def _signature(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_RegisterHotKey = _signature(_user32, "RegisterHotKey", wintypes.BOOL,
                             wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
_UnregisterHotKey = _signature(_user32, "UnregisterHotKey", wintypes.BOOL,
                               wintypes.HWND, ctypes.c_int)
_GetMessage = _signature(_user32, "GetMessageW", wintypes.BOOL,
                         ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_PeekMessage = _signature(_user32, "PeekMessageW", wintypes.BOOL,
                          ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                          wintypes.UINT)
_PostThreadMessage = _signature(_user32, "PostThreadMessageW", wintypes.BOOL,
                                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_GetCurrentThreadId = _signature(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
_GetSystemMetrics = _signature(_user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
_OpenClipboard = _signature(_user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
_CloseClipboard = _signature(_user32, "CloseClipboard", wintypes.BOOL)
_EmptyClipboard = _signature(_user32, "EmptyClipboard", wintypes.BOOL)
_SetClipboardData = _signature(_user32, "SetClipboardData", wintypes.HANDLE,
                               wintypes.UINT, wintypes.HANDLE)
_RegisterClipboardFormat = _signature(_user32, "RegisterClipboardFormatW", wintypes.UINT,
                                      wintypes.LPCWSTR)
_GlobalAlloc = _signature(_kernel32, "GlobalAlloc", wintypes.HGLOBAL,
                          wintypes.UINT, ctypes.c_size_t)
_GlobalLock = _signature(_kernel32, "GlobalLock", ctypes.c_void_p, wintypes.HGLOBAL)
_GlobalUnlock = _signature(_kernel32, "GlobalUnlock", wintypes.BOOL, wintypes.HGLOBAL)
_GlobalFree = _signature(_kernel32, "GlobalFree", wintypes.HGLOBAL, wintypes.HGLOBAL)
_LocalFree = _signature(_kernel32, "LocalFree", wintypes.HLOCAL, wintypes.HLOCAL)
_CreateWindowEx = _signature(_user32, "CreateWindowExW", wintypes.HWND,
                             wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_DestroyWindow = _signature(_user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
_CreateMutex = _signature(_kernel32, "CreateMutexW", wintypes.HANDLE,
                          ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
_CloseHandle = _signature(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)

_instance_lock = threading.Lock()
_instance_handle = None


def _single_instance_name() -> str:
    # Local scopes the object to this interactive Windows session.  Hash the
    # Windows username so another account does not share the same mutex name.
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_username = _signature(advapi32, "GetUserNameW", wintypes.BOOL,
                              wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    buffer = ctypes.create_unicode_buffer(257)
    length = wintypes.DWORD(len(buffer))
    if not get_username(buffer, ctypes.byref(length)):
        raise RuntimeError("无法确认当前 Windows 用户，请重新启动软件。")
    identity = os.environ.get("USERDOMAIN", "") + "\\" + buffer.value
    digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
    return "Local\\FormulaSnip-" + digest


def acquire_single_instance() -> bool:
    """Hold a per-user/session named mutex; False means another copy is open."""
    global _instance_handle
    with _instance_lock:
        if _instance_handle is not None:
            return True
        name = _single_instance_name()
        ctypes.set_last_error(0)
        handle = _CreateMutex(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise RuntimeError("无法检查软件运行状态，请重新打开软件。")
        if error == 183:  # ERROR_ALREADY_EXISTS
            _CloseHandle(handle)
            return False
        _instance_handle = handle
        return True


def release_single_instance() -> None:
    global _instance_handle
    with _instance_lock:
        if _instance_handle is not None:
            _CloseHandle(_instance_handle)
            _instance_handle = None


atexit.register(release_single_instance)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


_CryptProtectData = _signature(_crypt32, "CryptProtectData", wintypes.BOOL,
                               ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                               ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.DWORD, ctypes.POINTER(_DataBlob))
_CryptUnprotectData = _signature(_crypt32, "CryptUnprotectData", wintypes.BOOL,
                                 ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                                 ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                                 wintypes.DWORD, ctypes.POINTER(_DataBlob))


def enable_dpi_awareness() -> None:
    """Call before making a Tk root so screenshot and mouse pixels agree."""
    try:
        fn = _signature(_user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, ctypes.c_void_p)
        if fn(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
        if ctypes.get_last_error() == 5:  # already fixed by executable manifest
            return
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        fn = _signature(shcore, "SetProcessDpiAwareness", ctypes.c_long, ctypes.c_int)
        if fn(2) in (0, -2147024891):
            return
    except (OSError, AttributeError):
        pass
    try:
        _signature(_user32, "SetProcessDPIAware", wintypes.BOOL)()
    except AttributeError:
        pass


def get_virtual_screen() -> tuple[int, int, int, int]:
    """Return virtual desktop physical-pixel left, top, width, height."""
    return tuple(_GetSystemMetrics(n) for n in (76, 77, 78, 79))


def parse_hotkey(hotkey: str) -> tuple[int, int, str]:
    """Return Win32 modifiers, virtual key and a normalized label."""
    if not isinstance(hotkey, str):
        raise ValueError("快捷键请使用 Ctrl+Alt+M 这样的格式。")
    parts = [p.strip().upper() for p in hotkey.split("+")]
    if not parts or any(not p for p in parts):
        raise ValueError("快捷键请使用 Ctrl+Alt+M 这样的格式。")
    names = {"CTRL": (2, "Ctrl"), "CONTROL": (2, "Ctrl"),
             "ALT": (1, "Alt"), "SHIFT": (4, "Shift")}
    modifiers = 0
    keys = []
    for part in parts:
        if part in names:
            bit, _ = names[part]
            if modifiers & bit:
                raise ValueError("快捷键中不能重复填写同一个修饰键。")
            modifiers |= bit
        else:
            keys.append(part)
    if len(keys) != 1 or not modifiers:
        raise ValueError("快捷键需要 Ctrl、Alt、Shift 中的至少一个，加一个字母、数字或 F1–F11。")
    key = keys[0]
    if re.fullmatch(r"[A-Z0-9]", key):
        vk = ord(key)
    elif re.fullmatch(r"F(?:[1-9]|10|11)", key):
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError("主键仅支持字母、数字或 F1–F11；F12 和 Win 组合由系统保留。")
    normalized = "+".join([label for bit, label in ((2, "Ctrl"), (1, "Alt"), (4, "Shift"))
                            if modifiers & bit] + [key])
    return modifiers, vk, normalized


class HotkeyManager:
    """Register global shortcuts without blocking or touching the Tk main loop."""

    _WM_COMMAND = 0x8000 + 101
    _WM_HOTKEY = 0x0312

    def __init__(self, callback_queue: queue.Queue):
        self._callback_queue = callback_queue
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._closed = False
        self._method_lock = threading.Lock()
        self._thread_id = None
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="FormulaSnipHotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("无法启动快捷键监听，请重新打开软件。")
        if self._startup_error:
            raise RuntimeError("无法启动快捷键监听，请重新打开软件。") from self._startup_error

    def register(self, hotkey: str) -> None:
        parsed = parse_hotkey(hotkey)
        with self._method_lock:
            self._request("register", parsed)

    def _request(self, action, data=None):
        if self._closed or not self._thread.is_alive():
            raise RuntimeError("快捷键监听已经关闭。")
        done = threading.Event()
        result = []
        self._commands.put((action, data, done, result))
        if not _PostThreadMessage(self._thread_id, self._WM_COMMAND, 0, 0):
            raise RuntimeError("无法更新快捷键，请重新打开软件。")
        if not done.wait(5):
            raise RuntimeError("更新快捷键超时，请重新打开软件。")
        if result and isinstance(result[0], Exception):
            raise result[0]

    def close(self) -> None:
        with self._method_lock:
            if self._closed:
                return
            try:
                if self._thread.is_alive():
                    self._request("close")
            finally:
                self._closed = True
        self._thread.join(timeout=5)

    def _run(self):
        active_id = None
        current_label = None
        next_id = 101
        try:
            msg = wintypes.MSG()
            self._thread_id = _GetCurrentThreadId()
            _PeekMessage(ctypes.byref(msg), None, 0, 0, 0)  # force thread queue creation
            self._ready.set()
            running = True
            while running:
                status = _GetMessage(ctypes.byref(msg), None, 0, 0)
                if status <= 0:
                    break
                if msg.message == self._WM_HOTKEY:
                    if active_id is not None and msg.wParam == active_id:
                        self._callback_queue.put(("hotkey", None))
                elif msg.message == self._WM_COMMAND:
                    while True:
                        try:
                            action, data, done, result = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            if action == "close":
                                running = False
                            elif action == "register":
                                modifiers, vk, label = data
                                if label != current_label:
                                    new_id = next_id
                                    next_id = 101 if next_id >= 0xBFFE else next_id + 1
                                    if not _RegisterHotKey(None, new_id, modifiers | 0x4000, vk):
                                        error = ctypes.get_last_error()
                                        if error == 1409:
                                            raise ValueError(f"快捷键 {label} 已被其他程序占用，请换一个组合。")
                                        if error == 5:
                                            raise ValueError("当前进程无法注册全局快捷键（Windows 错误 5）。请从资源管理器直接启动软件；仍可使用截图按钮。")
                                        raise ValueError(f"无法注册快捷键 {label}（Windows 错误 {error}），请换一个组合。")
                                    # Keep the old shortcut if registering the replacement fails.
                                    if active_id is not None:
                                        _UnregisterHotKey(None, active_id)
                                    active_id, current_label = new_id, label
                            else:
                                raise RuntimeError("无法处理快捷键设置。")
                        except Exception as exc:
                            result.append(exc)
                        finally:
                            done.set()
                        if not running:
                            break
        except Exception as exc:
            self._startup_error = exc
        finally:
            if active_id is not None:
                _UnregisterHotKey(None, active_id)
            self._ready.set()
            while True:
                try:
                    _, _, done, result = self._commands.get_nowait()
                    result.append(RuntimeError("快捷键监听已停止，请重新打开软件。"))
                    done.set()
                except queue.Empty:
                    break


def build_cf_html(fragment: str) -> bytes:
    """Build CF_HTML; all offsets are UTF-8 byte offsets, never character counts."""
    if not isinstance(fragment, str) or "\x00" in fragment:
        raise ValueError("HTML 公式格式无效。")
    # Accept a fragment or a full document generated by the converter.
    match = re.search(r"<body\b[^>]*>(.*?)</body\s*>", fragment, re.I | re.S)
    if match:
        fragment = match.group(1)
    if re.search(r"<(?:script|iframe|object|embed)\b|\bon\w+\s*=", fragment, re.I):
        raise ValueError("HTML 公式包含不支持的内容。")
    start_marker = b"<!--StartFragment-->"
    end_marker = b"<!--EndFragment-->"
    prefix = b'<html><head><meta charset="utf-8"></head><body>' + start_marker
    suffix = end_marker + b"</body></html>"
    payload = fragment.encode("utf-8")
    template = ("Version:1.0\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
                "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n")
    header_len = len(template.format(0, 0, 0, 0).encode("ascii"))
    start_fragment = header_len + len(prefix)
    end_fragment = start_fragment + len(payload)
    header = template.format(header_len, end_fragment + len(suffix),
                             start_fragment, end_fragment).encode("ascii")
    return header + prefix + payload + suffix


def _clipboard_write(formats: list[tuple[int, bytes]]) -> None:
    # An owner HWND is required by EmptyClipboard + SetClipboardData.
    owner = _CreateWindowEx(0, "STATIC", "FormulaSnipClipboard", 0,
                            0, 0, 0, 0, ctypes.c_void_p(-3), None, None, None)
    if not owner:
        raise RuntimeError("无法访问剪贴板，请稍后重试。")
    opened = False
    try:
        for _ in range(12):
            if _OpenClipboard(owner):
                opened = True
                break
            time.sleep(0.025)
        if not opened:
            raise RuntimeError("剪贴板正被其他程序使用，请稍后重试。")
        if not _EmptyClipboard():
            raise RuntimeError("无法更新剪贴板，请稍后重试。")
        for fmt, data in formats:
            handle = _GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            if not handle:
                raise MemoryError("内存不足，无法复制公式。")
            transferred = False
            try:
                pointer = _GlobalLock(handle)
                if not pointer:
                    raise RuntimeError("无法准备剪贴板内容。")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    _GlobalUnlock(handle)
                if not _SetClipboardData(fmt, handle):
                    raise RuntimeError("复制失败，请稍后重试。")
                transferred = True
            finally:
                if not transferred:
                    _GlobalFree(handle)
    finally:
        if opened:
            _CloseClipboard()
        _DestroyWindow(owner)


def copy_text(text: str) -> None:
    if "\x00" in text:
        raise ValueError("复制内容不能包含空字符。")
    _clipboard_write([(13, (text + "\x00").encode("utf-16-le"))])


def copy_mathml(mathml: str, html: str, fallback_latex: str) -> None:
    """Publish MathML, CF_HTML and plain LaTeX for varying Word/host versions."""
    if any("\x00" in s for s in (mathml, html, fallback_latex)):
        raise ValueError("公式格式无效。")
    math_fmt = _RegisterClipboardFormat("MathML")
    html_fmt = _RegisterClipboardFormat("HTML Format")
    if not math_fmt or not html_fmt:
        raise RuntimeError("无法注册公式剪贴板格式。")
    _clipboard_write([
        (math_fmt, (mathml + "\x00").encode("utf-8")),
        (html_fmt, build_cf_html(html) + b"\x00"),
        (13, (fallback_latex + "\x00").encode("utf-16-le")),
    ])


def _dpapi(data: bytes, decrypt: bool) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    if decrypt:
        ok = _CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1,
                                ctypes.byref(target))
    else:
        ok = _CryptProtectData(ctypes.byref(source), "FormulaSnip API credential", None,
                              None, None, 1, ctypes.byref(target))
    if not ok:
        raise ValueError("无法读取或保存密钥，请在当前 Windows 账户中重新填写。")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        _LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def protect_secret(secret: str) -> str:
    """Current-user DPAPI encryption.  Never silently fall back to plaintext."""
    if not secret:
        return ""
    return "dpapi:v1:" + base64.b64encode(_dpapi(secret.encode("utf-8"), False)).decode("ascii")


def unprotect_secret(protected: str) -> str:
    if not protected:
        return ""
    if not isinstance(protected, str) or not protected.startswith("dpapi:v1:"):
        raise ValueError("密钥保存格式不受支持，请重新填写。")
    try:
        data = base64.b64decode(protected[len("dpapi:v1:"):], validate=True)
        return _dpapi(data, True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("无法读取密钥，请在当前 Windows 账户中重新填写。") from exc

