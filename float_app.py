"""tok/s 悬浮窗：无边框、置顶的原生小窗（WebView2 渲染）。

用法：float.bat  →  uv run python float_app.py

- frameless   去掉系统标题栏，玻璃卡片即整个窗口
- on_top      默认真置顶，卡片右上角 📌 可实时切换（Win32 SetWindowPos）
- easy_drag   按住窗口任意位置拖动
- transparent 圆角外透出桌面，玻璃直接浮在桌面上
"""
import ctypes

import webview

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002


class Api:
    """暴露给页面 JS 的方法：window.pywebview.api.xxx()"""

    def close_window(self):
        for w in webview.windows:
            w.destroy()

    def toggle_top(self, top: bool):
        hwnd = self._hwnd()
        if hwnd:
            ctypes.windll.user32.SetWindowPos(
                int(hwnd),
                HWND_TOPMOST if top else HWND_NOTOPMOST,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE,
            )

    @staticmethod
    def _hwnd():
        try:
            handle = webview.windows[0].native_handle
            if handle:
                return int(handle)
        except Exception:
            pass
        # 兜底：按窗口标题找
        return ctypes.windll.user32.FindWindowW(None, "tok/s") or None


webview.create_window(
    "tok/s",
    "http://127.0.0.1:9000/float",
    width=264,
    height=216,
    frameless=True,
    easy_drag=True,
    on_top=True,
    transparent=True,
    js_api=Api(),
)
webview.start()

