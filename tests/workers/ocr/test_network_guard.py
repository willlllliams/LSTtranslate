"""网络拦截的判定逻辑（需求 PRV-01）。

审计钩子装上以后无法卸载，所以这里直接调用钩子函数，不在测试进程里 install()；
装上之后的效果由 test_ocr_process.py 在子进程里检查。
"""

import logging
import socket

import pytest

from lst.workers.ocr_worker import network_guard


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("socket.getaddrinfo", ("huggingface.co", 443, 0, 0, 0)),
        ("socket.getaddrinfo", (b"paddle-model-ecology.bj.bcebos.com", 443, 0, 0, 0)),
        ("socket.gethostbyname", ("aistudio.baidu.com",)),
        ("socket.connect", (socket.socket, ("93.184.216.34", 443))),
        ("socket.connect", (socket.socket, ("2606:2800:220:1::", 443, 0, 0))),
        ("http.client.connect", (object(), "modelscope.cn", 443)),
        ("urllib.Request", ("https://huggingface.co/PaddlePaddle/x", None, dict[str, str](), "GET")),
    ],
)
def test_remote_targets_are_blocked(
    event: str, args: tuple[object, ...], caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="lst.workers.ocr_worker.network_guard"),
        pytest.raises(PermissionError),
    ):
        network_guard._hook(event, args)
    assert any("已拦截网络访问" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("socket.getaddrinfo", ("localhost", 80, 0, 0, 0)),
        ("socket.getaddrinfo", (None, 0, 0, 0, 0)),
        ("socket.connect", (socket.socket, ("127.0.0.1", 5000))),
        ("socket.connect", (socket.socket, ("::1", 5000, 0, 0))),
        ("socket.connect", (socket.socket, r"\\.\pipe\some-pipe")),  # 不是网络地址
        ("urllib.Request", ("http://127.0.0.1:8080/x", None, dict[str, str](), "GET")),
        ("open", ("C:/file.txt", "r", 0)),  # 无关的审计事件
    ],
)
def test_local_and_unrelated_events_pass(event: str, args: tuple[object, ...]) -> None:
    network_guard._hook(event, args)


@pytest.mark.parametrize(
    ("host", "loopback"), [("LOCALHOST", True), ("127.8.8.8", True), ("[::1]", True), ("10.0.0.1", False)]
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert network_guard.is_loopback(host) is loopback
