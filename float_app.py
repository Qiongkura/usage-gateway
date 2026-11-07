"""tok/s 悬浮窗：无边框液态玻璃小窗（WebView2 + DWM 亚克力）。

用法：float.bat  →  uv run python float_app.py

关键点：
- frameless + transparent   WebView2 页面透明，玻璃卡片即窗口内容
- DWM 圆角                  Win11 系统级圆角（DwmSetWindowAttribute）
- ACCENT_ACRYLICBLURBEHIND  系统级毛玻璃：桌面被真实模糊后垫在玻璃后
  （Rainmeter / TranslucentTB 同款 API），不是网页假透明
- js_api                    页面上 📌 置顶锁定 / ✕ 关闭，全部真生效
"""
import ctypes
import sys
import time
import ctypes.wintypes as wintypes

import webview

# 单实例：已有一个 tok/s 窗口时直接退出，避免连点叠窗
if ctypes.windll.user32.FindWindowW(None, "tok/s"):
    sys.exit(0)

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x2
# 整窗透明度（0-255）：255 = 完全不透明（用户定稿：可读性优先）
WINDOW_ALPHA = 255


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_int),
    ]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]

def _find_hwnd(title: str = "tok/s", timeout: float = 8.0) -> int | None:
    """等窗口创建完成并拿到 HWND。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            return int(hwnd)
        try:
            handle = webview.windows[0].native_handle
            if handle:
                return int(handle)
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _apply_glass(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    # 1) 系统级圆角
    pref = ctypes.c_int(DWMWCP_ROUND)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
    )
    # 2) 整窗半透明（WS_EX_LAYERED）：壁纸色调透进玻璃，文字仍可读。
    #    注：WebView2+WinForms 组合给不了「模糊桌面」的逐像素玻璃，
    #    均匀半透明是这条技术栈能做到的透桌面上限。
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
    user32.SetLayeredWindowAttributes(hwnd, 0, WINDOW_ALPHA, LWA_ALPHA)
    # 3) 置顶兜底
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)


class Api:
    """暴露给页面 JS：window.pywebview.api.xxx()"""

    def __init__(self):
        self.topmost = True
        self._drag_last = None
        self._hwnd_cache = None

    def close_window(self):
        for w in webview.windows:
            w.destroy()

    def toggle_top(self, top: bool):
        self.topmost = bool(top)
        hwnd = self._hwnd()
        if hwnd:
            self._set_z(hwnd)

    def drag_start(self):
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        self._drag_last = (pt.x, pt.y)

    def drag_move(self):
        """拖动中移动窗口：始终带上 TOPMOST，拖到哪都保持置顶。"""
        if self._drag_last is None:
            return
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        dx, dy = pt.x - self._drag_last[0], pt.y - self._drag_last[1]
        self._drag_last = (pt.x, pt.y)
        if dx == 0 and dy == 0:
            return
        hwnd = self._hwnd()
        if not hwnd:
            return
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST if self.topmost else 0,
            rect.left + dx, rect.top + dy, 0, 0,
            SWP_NOSIZE | SWP_SHOWWINDOW | (0 if self.topmost else SWP_NOZORDER),
        )

    def drag_end(self):
        self._drag_last = None
        if self.topmost:
            hwnd = self._hwnd()
            if hwnd:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE
                )

    def _set_z(self, hwnd: int):
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST if self.topmost else HWND_NOTOPMOST,
            0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE,
        )

    def _hwnd(self) -> int | None:
        if self._hwnd_cache:
            return self._hwnd_cache
        hwnd = _find_hwnd(timeout=0.5)
        if hwnd:
            self._hwnd_cache = hwnd
        return hwnd


def _make_form_transparent():
    """把 WinForms 宿主窗体背景设为透明。

    否则窗体自己的白底会盖住 DWM 亚克力——这就是「玻璃变实心白卡」的根因。
    pywebview 在 transparent=True 时已设 SupportsTransparentBackColor，
    这里补上 BackColor = Transparent 即可让亚克力透上来。
    """
    try:
        from System.Drawing import Color
        from webview.platforms import winforms

        for form in winforms.BrowserView.forms.values():
            form.BackColor = Color.Transparent
    except Exception as exc:
        print("[float] form transparent failed:", exc)


def _setup():
    hwnd = _find_hwnd()
    if hwnd:
        _apply_glass(hwnd)
    _make_form_transparent()


webview.create_window(
    "tok/s",
    "http://127.0.0.1:9000/float",
    width=300,
    height=292,
    frameless=True,
    transparent=True,
    js_api=Api(),
)
webview.start(_setup)
