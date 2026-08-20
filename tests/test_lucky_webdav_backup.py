"""Lucky WebDAV 备份任务的本地端到端测试。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import unittest
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

# 测试目录不再使用 __init__.py；显式加入项目根目录后，Python 会把 tasks 识别为命名空间包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tasks.lucky_webdav_backup as backup_module
from tasks.lucky_webdav_backup import (
    ConfigError,
    CONFIG_ENV_NAME,
    main,
    NotificationError,
    parse_config,
    run_task,
    send_qinglong_notification,
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
    received_tokens: list[str | None] = field(default_factory=list)


@dataclass
class FakeQLAPI:
    """模拟青龙注入的 QLAPI，只记录统一通知调用。"""

    should_fail: bool = False
    notifications: list[tuple[str, str]] = field(default_factory=list)

    def notify(self, title: str, content: str) -> None:
        """记录通知；按测试需要模拟青龙通知调用异常。"""

        if self.should_fail:
            raise RuntimeError("模拟青龙通知失败")
        self.notifications.append((title, content))


class MockHandler(BaseHTTPRequestHandler):
    """同时模拟 Lucky 和 WebDAV 端点。"""

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
            ql_api = FakeQLAPI()
            state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip())
            directory = "/dav/qinglong/lucky-backup/主路由"
            state.remote_files[directory] = [
                ("主路由_20260601_030000.zip", format_datetime(datetime(2026, 6, 1, tzinfo=timezone.utc))),
                ("主路由_20260810_030000.zip", format_datetime(datetime(2026, 8, 10, tzinfo=timezone.utc))),
                ("手工备份.zip", format_datetime(datetime(2026, 1, 1, tzinfo=timezone.utc))),
                ("主路由_20260501_030000.zip", None),
            ]

            config = parse_config(make_config(services.base_url))
            exit_code = run_task(
                config,
                now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc),
                notification_sender=ql_api.notify,
            )

            self.assertEqual(0, exit_code)
            uploaded_paths = list(state.uploads)
            self.assertEqual(1, len(uploaded_paths))
            self.assertTrue(uploaded_paths[0].endswith("主路由_20260820_030000.zip"))
            self.assertEqual(
                ["/dav/qinglong/lucky-backup/主路由/主路由_20260601_030000.zip"],
                state.deleted_paths,
            )
            self.assertEqual(["TOKEN_MAIN_SECRET"], state.received_tokens)
            self.assertEqual(1, len(ql_api.notifications))
            self.assertIn("Lucky 配置备份成功", ql_api.notifications[0][1])

    def test_one_lucky_failure_does_not_stop_other_instances(self) -> None:
        """一个 Lucky 返回 HTML 时，成功实例仍上传且最终返回失败状态。"""

        with MockServices() as services:
            state = services.state
            ql_api = FakeQLAPI()
            state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip("主路由"))
            state.lucky_responses["/lucky-side/api/configure"] = (200, b"<html>login</html>")
            config = parse_config(make_config(services.base_url, include_failed_lucky=True))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = run_task(
                    config,
                    now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc),
                    notification_sender=ql_api.notify,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(1, len(state.uploads))
            self.assertEqual(1, len(ql_api.notifications))
            notice = ql_api.notifications[0][1]
            self.assertIn("旁路由", notice)
            self.assertIn("内容不是 ZIP", notice)

            # 日志与通知都不得暴露三类真实凭据。
            combined_text = output.getvalue() + notice
            for secret in (
                "TOKEN_MAIN_SECRET",
                "TOKEN_SIDE_SECRET",
                "DAV_PASSWORD_SECRET",
            ):
                self.assertNotIn(secret, combined_text)

    def test_all_lucky_failures_skip_remote_cleanup(self) -> None:
        """全部下载失败时不得执行 PROPFIND 或 DELETE 清理。"""

        with MockServices() as services:
            services.state.lucky_responses["/lucky-main/api/configure"] = (500, b"error")
            config = parse_config(make_config(services.base_url))
            ql_api = FakeQLAPI()
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(
                    config,
                    now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc),
                    notification_sender=ql_api.notify,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual([], services.state.deleted_paths)
            self.assertEqual(1, len(ql_api.notifications))

    def test_qinglong_notification_failure_marks_task_failed(self) -> None:
        """青龙通知调用异常时，即使备份成功也必须返回失败状态。"""

        with MockServices() as services:
            services.state.lucky_responses["/lucky-main/api/configure"] = (200, make_zip())
            config = parse_config(make_config(services.base_url))
            ql_api = FakeQLAPI(should_fail=True)
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(
                    config,
                    now=datetime(2026, 8, 20, 3, tzinfo=timezone.utc),
                    notification_sender=ql_api.notify,
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(1, len(services.state.uploads))
            self.assertEqual(0, len(ql_api.notifications))

    def test_duplicate_safe_names_are_rejected(self) -> None:
        """不同显示名若映射为同一目录名，应在网络操作前拒绝。"""

        raw = json.loads(make_config("http://127.0.0.1:1"))
        raw["luckies"] = [
            {"name": "A/B", "base_url": "http://127.0.0.1:1", "open_token": "one"},
            {"name": "A B", "base_url": "http://127.0.0.1:2", "open_token": "two"},
        ]
        with self.assertRaises(ConfigError):
            parse_config(json.dumps(raw))

    def test_invalid_main_config_still_sends_qinglong_notification(self) -> None:
        """主配置不合法时，仍应通过青龙 QLAPI 发送一次配置错误通知。"""

        with MockServices() as services:
            raw = json.loads(make_config(services.base_url))
            raw["luckies"] = []
            ql_api = FakeQLAPI()
            with mock.patch.object(backup_module, "QLAPI", ql_api, create=True):
                with mock.patch.dict(os.environ, {CONFIG_ENV_NAME: json.dumps(raw)}, clear=False):
                    with contextlib.redirect_stdout(io.StringIO()):
                        exit_code = main()

            self.assertEqual(1, exit_code)
            self.assertEqual({}, services.state.uploads)
            self.assertEqual(1, len(ql_api.notifications))
            self.assertIn("配置备份失败", ql_api.notifications[0][1])

    def test_qinglong_notification_uses_injected_qlapi(self) -> None:
        """默认通知适配器必须调用青龙注入的 QLAPI.notify。"""

        ql_api = FakeQLAPI()
        with mock.patch.object(backup_module, "QLAPI", ql_api, create=True):
            send_qinglong_notification("测试标题", "测试正文")
        self.assertEqual([("测试标题", "测试正文")], ql_api.notifications)

    def test_missing_qinglong_qlapi_is_reported(self) -> None:
        """脱离青龙运行时必须明确报告 QLAPI 不可用。"""

        with mock.patch.object(backup_module, "QLAPI", None, create=True):
            with self.assertRaises(NotificationError):
                send_qinglong_notification("测试标题", "测试正文")


if __name__ == "__main__":
    unittest.main()
