"""tok/s 悬浮窗：无边框、置顶的原生小窗（WebView2 渲染）。

用法：float.bat  →  uv run python float_app.py

- frameless   去掉系统标题栏，只剩玻璃卡片本身
- on_top      真置顶（替代旧的假 📌）
- easy_drag   按住窗口任意位置拖动
- transparent 窗口四角/边缘透明，玻璃直接浮在桌面上
"""
import webview

webview.create_window(
    "tok/s",
    "http://127.0.0.1:9000/float",
    width=264,
    height=216,
    frameless=True,
    easy_drag=True,
    on_top=True,
    transparent=True,
)
webview.start()
