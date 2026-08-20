"""Lucky WebDAV 备份任务的本地端到端测试。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from xml.etree import ElementTree

from tasks.lucky_webdav_backup import (
    ConfigError,
    CONFIG_ENV_NAME,
    main,
    parse_config,
    run_task,
    serverchan_api_url,
    ServerChanConfig,
)


def make_zip(content: str = "测试配置") -> bytes:
    """在内存中创建一个最小有效 ZIP，模拟 Lucky 备份。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lucky_base.lkcf", content)
    return buffer.getvalue()


@dataclass
class MockState:
    """模拟服务的可观察状态。"""

    lucky_responses: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    uploads: dict[str, bytes] = field(default_factory=dict)
    remote_files: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    collections: set[str] = field(default_factory=set)
    deleted_paths: list[str] = field(default_factory=list)
    notifications: list[dict[str, list[str]]] = field(default_factory=list)
    received_tokens: list[str | None] = field(default_factory=list)
    notification_code: int = 0


class MockHandler(BaseHTTPRequestHandler):
    """同时模拟 Lucky、WebDAV 和 Server酱端点。"""

    server: "MockHTTPServer"

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭测试服务访问日志，保持测试输出简洁。"""

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - HTTPServer 约定的方法名称。
        state = self.server.state
        path = urllib.parse.urlsplit(self.path).path
        state.received_tokens.append(self.headers.get("openToken"))
        status, body = state.lucky_responses.get(path, (404, b"not found"))
        self._send(status, body, "application/zip" if status == 200 else "text/html")

    def do_MKCOL(self) -> None:  # noqa: N802 - WebDAV 方法名称。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path in state.collections:
            self._send(405)
            return
        state.collections.add(path)
        self._send(201)

    def do_PUT(self) -> None:  # noqa: N802 - WebDAV 方法名称。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        size = int(self.headers.get("Content-Length", "0"))
        state.uploads[path] = self.rfile.read(size)
        self._send(201)

    def do_PROPFIND(self) -> None:  # noqa: N802 - WebDAV 方法名称。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip("/")
        # 读取并丢弃请求体，避免连接复用时残留数据。
        size = int(self.headers.get("Content-Length", "0"))
        if size:
            self.rfile.read(size)

        root = ElementTree.Element("{DAV:}multistatus")
        directory_response = ElementTree.SubElement(root, "{DAV:}response")
        ElementTree.SubElement(directory_response, "{DAV:}href").text = path + "/"
        directory_propstat = ElementTree.SubElement(directory_response, "{DAV:}propstat")
        directory_prop = ElementTree.SubElement(directory_propstat, "{DAV:}prop")
        directory_type = ElementTree.SubElement(directory_prop, "{DAV:}resourcetype")
        ElementTree.SubElement(directory_type, "{DAV:}collection")

        for filename, modified in state.remote_files.get(path, []):
            response = ElementTree.SubElement(root, "{DAV:}response")
            ElementTree.SubElement(response, "{DAV:}href").text = (
                path + "/" + urllib.parse.quote(filename)
            )
            propstat = ElementTree.SubElement(response, "{DAV:}propstat")
            prop = ElementTree.SubElement(propstat, "{DAV:}prop")
            ElementTree.SubElement(prop, "{DAV:}resourcetype")
            if modified is not None:
                ElementTree.SubElement(prop, "{DAV:}getlastmodified").text = modified

        self._send(207, ElementTree.tostring(root), "application/xml")

    def do_DELETE(self) -> None:  # noqa: N802 - WebDAV 方法名称。
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        self.server.state.deleted_paths.append(path)
        self._send(204)

    def do_POST(self) -> None:  # noqa: N802 - HTTPServer 约定的方法名称。
        size = int(self.headers.get("Content-Length", "0"))
        payload = urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8"))
        self.server.state.notifications.append(payload)
        response = {"code": self.server.state.notification_code}
        self._send(200, json.dumps(response).encode("utf-8"), "application/json")


class MockHTTPServer(ThreadingHTTPServer):
    """携带测试状态的线程 HTTP 服务。"""

    def __init__(self, address: tuple[str, int], state: MockState) -> None:
        super().__init__(address, MockHandler)
        self.state = state


class MockServices:
    """自动启动和关闭本地模拟服务。"""

    def __init__(self) -> None:
        self.state = MockState()
        self.server = MockHTTPServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "MockServices":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def make_config(base_url: str, *, include_failed_lucky: bool = False) -> str:
    """生成仅含虚假凭据的测试配置。"""

    luckies = [
        {
            "name": "主路由",
            "base_url": f"{base_url}/lucky-main",
            "open_token": "TOKEN_MAIN_SECRET",
        }
    ]
    if include_failed_lucky:
        luckies.append(
            {
                "name": "旁路由",
                "base_url": f"{base_url}/lucky-side",
                "open_token": "TOKEN_SIDE_SECRET",
            }
        )
    return json.dumps(
        {
            "luckies": luckies,
            "webdav": {
                "url": f"{base_url}/dav",
                "username": "DAV_USER_SECRET",
                "password": "DAV_PASSWORD_SECRET",
                "remote_root": "/qinglong/lucky-backup",
            },
            "serverchan": {
                "send_key": "SCT_TEST_SECRET",
                "api_url": f"{base_url}/notify/{{send_key}}.send",
            },
            "retention_days": 30,
            "timeout_seconds": 5,
        },
        ensure_ascii=False,
    )


class BackupTaskTests(unittest.TestCase):
    """备份任务主要行为测试。"""

    def test_success_upload_cleanup_and_notification(self) -> None:
        """成功场景应上传备份、只清理过期匹配文件并通知一次。"""

        with MockServices() as services:
            state = services.state
            state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip())
            directory = "/dav/qinglong/lucky-backup/主路由"
            state.remote_files[directory] = [
                ("主路由_20260601_030000.zip", format_datetime(datetime(2026, 6, 1, tzinfo=timezone.utc))),
                ("主路由_20260810_030000.zip", format_datetime(datetime(2026, 8, 10, tzinfo=timezone.utc))),
                ("手工备份.zip", format_datetime(datetime(2026, 1, 1, tzinfo=timezone.utc))),
                ("主路由_20260501_030000.zip", None),
            ]

            config = parse_config(make_config(services.base_url))
            exit_code = run_task(config, now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc))

            self.assertEqual(0, exit_code)
            uploaded_paths = list(state.uploads)
            self.assertEqual(1, len(uploaded_paths))
            self.assertTrue(uploaded_paths[0].endswith("主路由_20260820_030000.zip"))
            self.assertEqual(
                ["/dav/qinglong/lucky-backup/主路由/主路由_20260601_030000.zip"],
                state.deleted_paths,
            )
            self.assertEqual(["TOKEN_MAIN_SECRET"], state.received_tokens)
            self.assertEqual(1, len(state.notifications))
            self.assertIn("Lucky 配置备份成功", state.notifications[0]["desp"][0])

    def test_one_lucky_failure_does_not_stop_other_instances(self) -> None:
        """一个 Lucky 返回 HTML 时，成功实例仍上传且最终返回失败状态。"""

        with MockServices() as services:
            state = services.state
            state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip("主路由"))
            state.lucky_responses["/lucky-side/api/configure"] = (200, b"<html>login</html>")
            config = parse_config(make_config(services.base_url, include_failed_lucky=True))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = run_task(config, now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc))

            self.assertEqual(1, exit_code)
            self.assertEqual(1, len(state.uploads))
            self.assertEqual(1, len(state.notifications))
            notice = state.notifications[0]["desp"][0]
            self.assertIn("旁路由", notice)
            self.assertIn("内容不是 ZIP", notice)

            # 日志与通知都不得暴露三类真实凭据。
            combined_text = output.getvalue() + notice
            for secret in (
                "TOKEN_MAIN_SECRET",
                "TOKEN_SIDE_SECRET",
                "DAV_PASSWORD_SECRET",
                "SCT_TEST_SECRET",
            ):
                self.assertNotIn(secret, combined_text)

    def test_all_lucky_failures_skip_remote_cleanup(self) -> None:
        """全部下载失败时不得执行 PROPFIND 或 DELETE 清理。"""

        with MockServices() as services:
            services.state.lucky_responses["/lucky-main/api/configure"] = (500, b"error")
            config = parse_config(make_config(services.base_url))
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(config, now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc))

            self.assertEqual(1, exit_code)
            self.assertEqual([], services.state.deleted_paths)
            self.assertEqual(1, len(services.state.notifications))

    def test_serverchan_business_failure_marks_task_failed(self) -> None:
        """Server酱返回非零业务码时，即使备份成功也必须返回失败状态。"""

        with MockServices() as services:
            services.state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip())
            services.state.notification_code = 1
            config = parse_config(make_config(services.base_url))
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(config, now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc))

            self.assertEqual(1, exit_code)
            self.assertEqual(1, len(services.state.uploads))
            self.assertEqual(1, len(services.state.notifications))

    def test_duplicate_safe_names_are_rejected(self) -> None:
        """不同显示名若映射为同一目录名，应在网络操作前拒绝。"""

        raw = json.loads(make_config("http://127.0.0.1:1"))
        raw["luckies"] = [
            {"name": "A/B", "base_url": "http://127.0.0.1:1", "open_token": "one"},
            {"name": "A B", "base_url": "http://127.0.0.1:2", "open_token": "two"},
        ]
        with self.assertRaises(ConfigError):
            parse_config(json.dumps(raw))

    def test_invalid_main_config_still_sends_serverchan_notification(self) -> None:
        """主配置不合法但 SendKey 可读取时，仍应发送一次配置错误通知。"""

        with MockServices() as services:
            raw = json.loads(make_config(services.base_url))
            raw["luckies"] = []
            with mock.patch.dict(os.environ, {CONFIG_ENV_NAME: json.dumps(raw)}, clear=False):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = main()

            self.assertEqual(1, exit_code)
            self.assertEqual({}, services.state.uploads)
            self.assertEqual(1, len(services.state.notifications))
            self.assertIn("配置备份失败", services.state.notifications[0]["desp"][0])

    def test_serverchan_sc3_endpoint_is_selected_by_prefix(self) -> None:
        """sctp 前缀必须自动切换到 Server酱³ 用户域名。"""

        config = ServerChanConfig(send_key="sctp123tABC")
        self.assertEqual(
            "https://123.push.ft07.com/send/sctp123tABC.send",
            serverchan_api_url(config),
        )

    def test_invalid_sc3_send_key_is_rejected(self) -> None:
        """格式错误的 Server酱³ SendKey 不得发往未知地址。"""

        with self.assertRaises(ConfigError):
            serverchan_api_url(ServerChanConfig(send_key="sctp-invalid"))


if __name__ == "__main__":
    unittest.main()
