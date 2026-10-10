"""桌面壳薄壳启动器：端口顺延、就绪判定、启动流程。"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dirtidy import desktop
from dirtidy.web.app import create_app


def test_health_endpoint_exists(tmp_path: Path) -> None:
    """探活端点必须存在——桌面壳靠它判断「路由已挂好」而非「端口通了」。"""
    app = create_app(default_root=tmp_path)
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_pick_free_port_prefers_requested() -> None:
    port = desktop.pick_free_port("127.0.0.1", 18000)
    assert 18000 <= port < 18020


def test_pick_free_port_skips_occupied() -> None:
    """首选端口被占用时要顺延——端口冲突是启动器该自己解决的事。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = int(taken.getsockname()[1])
        got = desktop.pick_free_port("127.0.0.1", busy)
    assert got != busy
    assert got > busy


def test_wait_until_ready_false_without_server() -> None:
    """没有服务时应超时返回 False，而不是假装就绪。"""
    assert desktop.wait_until_ready("127.0.0.1", 1, timeout=0.5, interval=0.1) is False


def test_wait_until_ready_true_for_health_server() -> None:
    """起一个真的健康端点，应判定为就绪（而不是只看端口能不能连）。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/health":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: object) -> None:  # 静音
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert desktop.wait_until_ready("127.0.0.1", port, timeout=3) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tray_available_never_raises() -> None:
    """托盘是可选增强：无论装没装，探测都不能抛异常。"""
    assert isinstance(desktop.tray_available(), bool)


def test_run_tray_returns_false_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "tray_available", lambda: False)
    assert desktop.run_tray("http://127.0.0.1:1/") is False


class _FakeProc:
    """替身进程：记录是否被终止，不真的起服务。"""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.terminated = False
        self.killed = False
        self.exited = False

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_launch_starts_service_opens_browser_and_stops_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launch 的完整契约：起子进程 → 等就绪 → 开浏览器 → 退出时终止子进程。"""
    seen: dict[str, object] = {}

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> _FakeProc:
        proc = _FakeProc(cmd)
        seen["proc"] = proc
        return proc

    monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(desktop, "wait_until_ready", lambda *a, **kw: True)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: seen.setdefault("opened", url))

    rc = desktop.launch(tmp_path, port=18080, use_tray=False)

    assert rc == 0
    proc = seen["proc"]
    assert isinstance(proc, _FakeProc)
    assert "serve" in proc.cmd and str(tmp_path) in proc.cmd
    opened = str(seen["opened"])
    assert opened.startswith("http://127.0.0.1:") and opened.endswith("/")
    assert proc.terminated, "退出时必须终止子进程，否则会留下孤儿服务"


def test_launch_returns_error_when_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务起不来时要报错退出，不能让用户对着一个空页面等。"""

    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> _FakeProc:
        proc = _FakeProc(cmd)
        proc.exited = True
        return proc

    monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(desktop, "wait_until_ready", lambda *a, **kw: False)
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))

    assert desktop.launch(tmp_path, use_tray=False) == 1
    assert opened == [], "没就绪就不该打开浏览器"


def test_launch_no_browser_skips_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_popen(cmd: list[str], *args: object, **kwargs: object) -> _FakeProc:
        return _FakeProc(cmd)

    monkeypatch.setattr(desktop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(desktop, "wait_until_ready", lambda *a, **kw: True)
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))

    assert desktop.launch(tmp_path, use_tray=False, open_browser=False) == 0
    assert opened == []


def test_cli_registers_desktop() -> None:
    """`desktop` 子命令要真的注册进 parser，否则文档里的命令是空头支票。"""
    from dirtidy.cli import build_parser

    args = build_parser().parse_args(["desktop", "--no-tray", "--no-browser"])
    assert args.root == "."
    assert args.no_tray is True
    assert args.no_browser is True
