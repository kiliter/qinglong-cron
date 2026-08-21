"""WebDAV 备份多目标同步任务的本地端到端测试。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import unittest
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tasks.webdav_backup_sync as sync_module
from tasks.webdav_backup_sync import (
    ConfigError,
    NotificationError,
    parse_config,
    run_task,
    send_qinglong_notification,
)


def make_zip(content: str) -> bytes:
    """创建一个最小有效 ZIP，模拟 MoviePilot 备份。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("user.db", content)
    return buffer.getvalue()


@dataclass
class MockState:
    """保存模拟 WebDAV 的文件和调用事件。"""

    files: dict[str, dict[str, bytes]] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    failed_put_roots: set[str] = field(default_factory=set)


class MockHandler(BaseHTTPRequestHandler):
    """实现测试所需的最小 WebDAV 方法。"""

    server: "MockServer"

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭 HTTP 访问日志，保持测试输出简洁。"""

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        """发送固定长度响应。"""

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_MKCOL(self) -> None:  # noqa: N802 - WebDAV 方法名由协议规定。
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip("/")
        self.server.state.events.append(f"MKCOL {path}")
        self.server.state.files.setdefault(path, {})
        self._send(201)

    def do_PROPFIND(self) -> None:  # noqa: N802 - WebDAV 方法名由协议规定。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip("/")
        state.events.append(f"PROPFIND {path}")
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

        for filename, data in state.files.get(path, {}).items():
            response = ElementTree.SubElement(root, "{DAV:}response")
            ElementTree.SubElement(response, "{DAV:}href").text = (
                path + "/" + urllib.parse.quote(filename)
            )
            propstat = ElementTree.SubElement(response, "{DAV:}propstat")
            prop = ElementTree.SubElement(propstat, "{DAV:}prop")
            ElementTree.SubElement(prop, "{DAV:}resourcetype")
            ElementTree.SubElement(prop, "{DAV:}getcontentlength").text = str(len(data))
        self._send(207, ElementTree.tostring(root), "application/xml")

    def do_GET(self) -> None:  # noqa: N802 - HTTP 方法名由协议规定。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        state.events.append(f"GET {path}")
        directory, _, filename = path.rpartition("/")
        data = state.files.get(directory, {}).get(filename)
        if data is None:
            self._send(404)
            return
        self._send(200, data, "application/zip")

    def do_PUT(self) -> None:  # noqa: N802 - WebDAV 方法名由协议规定。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        state.events.append(f"PUT {path}")
        size = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(size)
        if any(path.startswith(root + "/") for root in state.failed_put_roots):
            self._send(500)
            return
        directory, _, filename = path.rpartition("/")
        state.files.setdefault(directory, {})[filename] = data
        self._send(201)

    def do_DELETE(self) -> None:  # noqa: N802 - WebDAV 方法名由协议规定。
        state = self.server.state
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        state.events.append(f"DELETE {path}")
        directory, _, filename = path.rpartition("/")
        state.files.get(directory, {}).pop(filename, None)
        self._send(204)


class MockServer(ThreadingHTTPServer):
    """携带测试状态的 HTTP 服务。"""

    def __init__(self, address: tuple[str, int], state: MockState):
        super().__init__(address, MockHandler)
        self.state = state


class MockServices:
    """自动启动和关闭本地模拟 WebDAV。"""

    def __init__(self):
        self.state = MockState()
        self.server = MockServer(("127.0.0.1", 0), self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        """返回模拟服务地址。"""

        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "MockServices":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass
class FakeQLAPI:
    """模拟青龙系统通知接口。"""

    code: int = 200
    notifications: list[tuple[str, str]] = field(default_factory=list)

    def systemNotify(self, payload: dict[str, str]) -> dict[str, object]:  # noqa: N802
        """记录通知并返回指定状态。"""

        self.notifications.append((payload["title"], payload["content"]))
        return {"code": self.code, "message": "通知发送成功" if self.code == 200 else "发送失败"}


def endpoint(name: str, base_url: str, remote_root: str) -> dict[str, object]:
    """生成不含真实凭据的端点配置。"""

    return {
        "name": name,
        "url": base_url,
        "username": f"{name}_USER_SECRET",
        "password": f"{name}_PASSWORD_SECRET",
        "remote_root": remote_root,
        "verify_ssl": True,
    }


def make_config(base_url: str) -> str:
    """生成两组 source 到 targets 的配置。"""

    return json.dumps(
        {
            "groups": [
                {
                    "name": "主实例",
                    "source": endpoint("源端一", base_url, "/source-a"),
                    "targets": [
                        endpoint("目标一", base_url, "/target-a"),
                        endpoint("目标二", base_url, "/target-b"),
                    ],
                    "retention_count": 2,
                },
                {
                    "name": "副实例",
                    "source": endpoint("源端二", base_url, "/source-b"),
                    "targets": [endpoint("目标三", base_url, "/target-c")],
                    "retention_count": 3,
                },
            ],
            "timeout_seconds": 5,
            "max_backup_mb": 10,
        },
        ensure_ascii=False,
    )


class WebDAVBackupSyncTests(unittest.TestCase):
    """独立 WebDAV 同步任务行为测试。"""

    def test_all_targets_have_latest_file_without_download_or_cleanup(self) -> None:
        """全部目标已有最新文件时，不得下载、上传或清理。"""

        with MockServices() as services:
            state = services.state
            latest_name = "MoviePilot-Backup-2026-08-20_02-00-00.zip"
            state.files["/source-a"] = {latest_name: make_zip("最新备份")}
            state.files["/target-a"] = {latest_name: make_zip("已存在")}
            state.files["/target-b"] = {latest_name: make_zip("已存在")}
            raw = json.loads(make_config(services.base_url))
            raw["groups"] = [raw["groups"][0]]

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(
                    parse_config(json.dumps(raw, ensure_ascii=False)),
                    notification_sender=lambda _title, _content: None,
                )

            self.assertEqual(0, exit_code)
            self.assertFalse(any(event.startswith("GET ") for event in state.events))
            self.assertFalse(any(event.startswith("PUT ") for event in state.events))
            self.assertFalse(any(event.startswith("DELETE ") for event in state.events))

    def test_n_groups_only_sync_latest_and_skip_existing_target(self) -> None:
        """每组只同步最新一个；已存在目标直接跳过且不得清理。"""

        with MockServices() as services:
            state = services.state
            old_name = "MoviePilot-Backup-2026-08-19_02-00-00.zip"
            latest_name = "MoviePilot-Backup-2026-08-20_02-00-00.zip"
            second_latest = "MoviePilot-Backup-2026-08-20_01-00-00.zip"
            state.files["/source-a"] = {
                old_name: make_zip("旧备份"),
                latest_name: make_zip("最新备份"),
                "manual.zip": make_zip("手工文件"),
            }
            state.files["/target-a"] = {
                "MoviePilot-Backup-2026-08-18_02-00-00.zip": make_zip("更旧"),
                second_latest: make_zip("次新"),
                old_name: make_zip("旧备份"),
            }
            # 目标二已经存在最新文件；即使有超量旧文件也必须完全跳过清理。
            state.files["/target-b"] = {
                latest_name: make_zip("已存在"),
                second_latest: make_zip("次新"),
                old_name: make_zip("旧备份"),
            }
            second_group_latest = "MoviePilot-Backup-2026-08-20_03-00-00.zip"
            state.files["/source-b"] = {second_group_latest: make_zip("副实例")}
            state.files["/target-c"] = {}

            notification = FakeQLAPI()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = run_task(
                    parse_config(make_config(services.base_url)),
                    notification_sender=lambda title, content: notification.notifications.append(
                        (title, content)
                    ),
                )

            self.assertEqual(0, exit_code)
            self.assertIn(latest_name, state.files["/target-a"])
            self.assertIn(second_group_latest, state.files["/target-c"])
            self.assertEqual(2, len(state.files["/target-a"]))
            self.assertEqual(3, len(state.files["/target-b"]))
            self.assertEqual(1, sum(event == f"GET /source-a/{latest_name}" for event in state.events))
            self.assertNotIn(f"GET /source-a/{old_name}", state.events)
            self.assertNotIn(f"PUT /target-b/{latest_name}", state.events)
            self.assertFalse(any(event.startswith("DELETE /target-b/") for event in state.events))
            self.assertIn("目标已存在", output.getvalue())
            self.assertEqual(1, len(notification.notifications))

    def test_failed_target_does_not_block_other_target_or_group(self) -> None:
        """一个目标上传失败时，其他目标和后续同步组仍须继续。"""

        with MockServices() as services:
            state = services.state
            latest_a = "MoviePilot-Backup-2026-08-20_02-00-00.zip"
            latest_b = "MoviePilot-Backup-2026-08-20_03-00-00.zip"
            state.files["/source-a"] = {latest_a: make_zip("主实例")}
            state.files["/source-b"] = {latest_b: make_zip("副实例")}
            state.files["/target-a"] = {}
            state.files["/target-b"] = {}
            state.files["/target-c"] = {}
            state.failed_put_roots.add("/target-a")

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_task(
                    parse_config(make_config(services.base_url)),
                    notification_sender=lambda _title, _content: None,
                )

            self.assertEqual(1, exit_code)
            self.assertNotIn(latest_a, state.files["/target-a"])
            self.assertIn(latest_a, state.files["/target-b"])
            self.assertIn(latest_b, state.files["/target-c"])

    def test_duplicate_group_names_are_rejected(self) -> None:
        """重复同步组名称必须在网络请求前被拒绝。"""

        raw = json.loads(make_config("https://example.com"))
        raw["groups"][1]["name"] = raw["groups"][0]["name"]
        with self.assertRaises(ConfigError):
            parse_config(json.dumps(raw, ensure_ascii=False))

    def test_system_notification_result_is_checked(self) -> None:
        """青龙系统通知返回失败状态时必须抛出明确错误。"""

        ql_api = FakeQLAPI(code=500)
        with mock.patch.object(sync_module, "QLAPI", ql_api, create=True):
            with self.assertRaises(NotificationError):
                send_qinglong_notification("测试标题", "测试正文")


if __name__ == "__main__":
    unittest.main()
