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
import time

import webview

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
WCA_ACCENT_POLICY = 19
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


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
    # 2) 亚克力模糊背后：把桌面真实模糊后垫在玻璃下
    accent = ACCENT_POLICY()
    accent.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
    accent.AccentFlags = 2
    accent.GradientColor = 0x59FFFFFF  # ABGR：约 35% 白色底
    data = WINDOWCOMPOSITIONATTRIBDATA()
    data.Attribute = WCA_ACCENT_POLICY
    data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
    data.SizeOfData = ctypes.sizeof(accent)
    user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    # 3) 置顶兜底
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)


class Api:
    """暴露给页面 JS：window.pywebview.api.xxx()"""

    def close_window(self):
        for w in webview.windows:
            w.destroy()

    def toggle_top(self, top: bool):
        hwnd = _find_hwnd(timeout=0.5)
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST if top else HWND_NOTOPMOST,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE,
            )


def _setup():
    hwnd = _find_hwnd()
    if hwnd:
        _apply_glass(hwnd)


webview.create_window(
    "tok/s",
    "http://127.0.0.1:9000/float",
    width=264,
    height=216,
    frameless=True,
    easy_drag=True,
    transparent=True,
    js_api=Api(),
)
webview.start(_setup)
