"""薄壳启动器：一键起本地服务并打开浏览器。

定位是**降低非技术用户的启动门槛**，不是完整桌面应用——不做打包签名、不做自动更新、
不引入强制依赖。托盘图标是可选增强：装了 `pystray` 就显示，没装就退化成「按 Ctrl-C 退出」。
这条取舍的理由见 `docs/01` §4.1。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def pick_free_port(host: str, preferred: int, *, tries: int = 20) -> int:
    """优先用 preferred；被占用则向后顺延找空闲端口。

    端口冲突是启动器该自己解决的事，不该丢给用户去排查。
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{host}:{preferred}~{preferred + tries - 1} 内没有可用端口")


def wait_until_ready(host: str, port: int, *, timeout: float = 20.0, interval: float = 0.2) -> bool:
    """轮询健康端点直到服务就绪或超时。

    用 HTTP 端点而不是 TCP 连接探测：TCP 通了不代表路由已挂好，
    此时打开浏览器会看到「连接被拒」，比多等 0.2 秒更糟。
    """
    url = f"http://{host}:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def tray_available() -> bool:
    """托盘图标是可选增强，不是依赖——缺了不影响启动。"""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _icon_image():
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=(37, 99, 235, 255))
    draw.line((18, size // 2, size - 18, size // 2), fill=(255, 255, 255, 255), width=7)
    return img


def run_tray(url: str) -> bool:
    """显示托盘图标并阻塞到用户选择退出；不可用时返回 False（不抛异常）。"""
    if not tray_available():
        return False
    import pystray

    icon = pystray.Icon(
        "dirtidy",
        _icon_image(),
        "dirtidy",
        pystray.Menu(
            pystray.MenuItem("打开审阅页", lambda *_: webbrowser.open(url), default=True),
            pystray.MenuItem("退出", lambda ic, _item: ic.stop()),
        ),
    )
    icon.run()
    return True


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def launch(
    root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    use_tray: bool = True,
) -> int:
    """起服务 → 等就绪 → 开浏览器 → 阻塞到退出。返回退出码。"""
    port = pick_free_port(host, port)
    cmd = [
        sys.executable,
        "-m",
        "dirtidy",
        "serve",
        "--root",
        str(root),
        "--host",
        host,
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(cmd)
    url = f"http://{host}:{port}/"
    try:
        if not wait_until_ready(host, port):
            print("启动超时：本地服务没有在预期时间内就绪。", file=sys.stderr)
            return 1
        print(f"dirtidy 已就绪：{url}")
        if open_browser:
            webbrowser.open(url)
        if use_tray and run_tray(url):
            return 0
        if use_tray:
            print("未安装 pystray，跳过托盘图标。")
        print("按 Ctrl-C 退出。")
        proc.wait()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        _stop(proc)
    return 0
