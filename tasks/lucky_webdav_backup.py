#!/usr/bin/env python3
"""
Lucky 配置备份到 WebDAV。

name: Lucky 配置备份到 WebDAV
cron: 0 3 * * *

配置通过青龙环境变量 ``LUCKY_BACKUP_CONFIG`` 提供，详细格式请查看仓库 README。
本脚本仅使用 Python 3 标准库，适合青龙订阅后直接运行。
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree


CONFIG_ENV_NAME = "LUCKY_BACKUP_CONFIG"
DEFAULT_BACKUP_API_PATH = "/api/configure"
DEFAULT_REMOTE_ROOT = "/qinglong/lucky-backup"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_TIMEOUT_SECONDS = 60
MAX_BACKUP_BYTES = 100 * 1024 * 1024
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class BackupError(Exception):
    """任务可预期错误的基类，错误文本不得包含任何凭据。"""


class ConfigError(BackupError):
    """配置格式或配置内容错误。"""


class RequestError(BackupError):
    """远端 HTTP 请求失败。"""


class NotificationError(BackupError):
    """青龙内置通知调用失败。"""


@dataclass(frozen=True)
class LuckyConfig:
    """单个 Lucky 实例配置。"""

    name: str
    safe_name: str
    base_url: str
    open_token: str
    backup_api_path: str = DEFAULT_BACKUP_API_PATH
    verify_ssl: bool = True


@dataclass(frozen=True)
class WebDAVConfig:
    """唯一 WebDAV 目标配置。"""

    url: str
    username: str
    password: str
    remote_root: str = DEFAULT_REMOTE_ROOT
    verify_ssl: bool = True


@dataclass(frozen=True)
class AppConfig:
    """一次任务运行所需的完整配置。"""

    luckies: tuple[LuckyConfig, ...]
    webdav: WebDAVConfig
    retention_days: int = DEFAULT_RETENTION_DAYS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class InstanceResult:
    """单个 Lucky 实例的执行结果，用于日志和通知汇总。"""

    name: str
    success: bool
    message: str
    remote_path: str | None = None


@dataclass(frozen=True)
class RemoteFile:
    """WebDAV PROPFIND 返回的远端文件信息。"""

    name: str
    modified_at: datetime | None
    is_collection: bool


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    """校验字段为 JSON 对象并返回，便于生成清晰的中文错误。"""

    if not isinstance(value, dict):
        raise ConfigError(f"配置字段 {field_name} 必须是 JSON 对象")
    return value


def _require_string(mapping: dict[str, Any], key: str, field_name: str) -> str:
    """读取必填字符串，并拒绝空白内容。"""

    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置字段 {field_name}.{key} 必须是非空字符串")
    return value.strip()


def _read_bool(mapping: dict[str, Any], key: str, default: bool, field_name: str) -> bool:
    """读取布尔配置，避免把字符串 ``false`` 错误解释为真值。"""

    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置字段 {field_name}.{key} 必须是布尔值")
    return value


def _validate_http_url(value: str, field_name: str) -> str:
    """仅允许 HTTP/HTTPS 地址，防止 urllib 打开本地文件等非预期协议。"""

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"配置字段 {field_name} 必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ConfigError(f"配置字段 {field_name} 不得在 URL 中内嵌账号或密码")
    return value.rstrip("/")


def safe_instance_name(name: str) -> str:
    """将实例名称转换为安全且可读的远端目录名。"""

    normalized = unicodedata.normalize("NFKC", name.strip())
    safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", normalized)
    safe_name = safe_name.strip("._-")[:64]
    if not safe_name or safe_name in {".", ".."}:
        raise ConfigError(f"Lucky 实例名称无法转换为安全路径：{name!r}")
    return safe_name


def _validate_remote_root(value: str) -> str:
    """校验远端根目录，明确禁止路径穿越片段。"""

    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ConfigError("配置字段 webdav.remote_root 必须是有效的远端目录")
    return "/" + "/".join(parts)


def parse_config(raw: str) -> AppConfig:
    """解析并完整校验 ``LUCKY_BACKUP_CONFIG``。"""

    if not raw.strip():
        raise ConfigError(f"未设置环境变量 {CONFIG_ENV_NAME}")
    try:
        root = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"环境变量 {CONFIG_ENV_NAME} 不是有效 JSON：第 {exc.lineno} 行") from None
    root = _require_mapping(root, CONFIG_ENV_NAME)

    lucky_items = root.get("luckies")
    if not isinstance(lucky_items, list) or not lucky_items:
        raise ConfigError("配置字段 luckies 必须是非空数组")

    luckies: list[LuckyConfig] = []
    used_safe_names: set[str] = set()
    for index, item in enumerate(lucky_items):
        field_name = f"luckies[{index}]"
        item = _require_mapping(item, field_name)
        name = _require_string(item, "name", field_name)
        safe_name = safe_instance_name(name)
        if safe_name in used_safe_names:
            raise ConfigError(f"多个 Lucky 实例生成了相同的安全目录名：{safe_name}")
        used_safe_names.add(safe_name)
        api_path = item.get("backup_api_path", DEFAULT_BACKUP_API_PATH)
        if not isinstance(api_path, str) or not api_path.strip().startswith("/"):
            raise ConfigError(f"配置字段 {field_name}.backup_api_path 必须以 / 开头")
        luckies.append(
            LuckyConfig(
                name=name,
                safe_name=safe_name,
                base_url=_validate_http_url(
                    _require_string(item, "base_url", field_name), f"{field_name}.base_url"
                ),
                open_token=_require_string(item, "open_token", field_name),
                backup_api_path=api_path.strip(),
                verify_ssl=_read_bool(item, "verify_ssl", True, field_name),
            )
        )

    webdav_obj = _require_mapping(root.get("webdav"), "webdav")
    webdav = WebDAVConfig(
        url=_validate_http_url(_require_string(webdav_obj, "url", "webdav"), "webdav.url"),
        username=_require_string(webdav_obj, "username", "webdav"),
        password=_require_string(webdav_obj, "password", "webdav"),
        remote_root=_validate_remote_root(
            str(webdav_obj.get("remote_root", DEFAULT_REMOTE_ROOT)).strip()
        ),
        verify_ssl=_read_bool(webdav_obj, "verify_ssl", True, "webdav"),
    )

    retention_days = root.get("retention_days", DEFAULT_RETENTION_DAYS)
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or not 1 <= retention_days <= 3650:
        raise ConfigError("配置字段 retention_days 必须是 1 到 3650 之间的整数")
    timeout_seconds = root.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 5 <= timeout_seconds <= 600:
        raise ConfigError("配置字段 timeout_seconds 必须是 5 到 600 之间的整数")

    return AppConfig(
        luckies=tuple(luckies),
        webdav=webdav,
        retention_days=retention_days,
        timeout_seconds=timeout_seconds,
    )


def _ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """按配置创建 TLS 上下文；HTTP 地址不使用该上下文。"""

    if verify_ssl:
        return None
    return ssl._create_unverified_context()  # noqa: SLF001 - 用户明确关闭证书校验时使用。


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    verify_ssl: bool = True,
    max_response_bytes: int = 2 * 1024 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    """发送 HTTP 请求，并把底层错误转换成不泄露 URL 凭据的中文错误。"""

    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=_ssl_context(verify_ssl),
        ) as response:
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise RequestError(f"{method} 响应超过允许大小 {max_response_bytes} 字节")
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as exc:
        # 不把 exc 或 URL 原样写入错误，避免远端地址中的敏感信息出现在日志中。
        raise RequestError(f"{method} 请求返回 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else "URLError"
        raise RequestError(f"{method} 请求连接失败（{reason_name}）") from None
    except TimeoutError:
        raise RequestError(f"{method} 请求超时") from None


def join_url(base_url: str, *parts: str) -> str:
    """将路径片段逐个 URL 编码后拼接，保留 WebDAV 基础地址已有路径。"""

    encoded_parts: list[str] = []
    for part in parts:
        for segment in part.replace("\\", "/").split("/"):
            if segment:
                encoded_parts.append(urllib.parse.quote(segment, safe=""))
    if not encoded_parts:
        return base_url.rstrip("/")
    return base_url.rstrip("/") + "/" + "/".join(encoded_parts)


def lucky_backup_url(config: LuckyConfig) -> str:
    """生成 Lucky 备份接口地址；API 路径始终相对于面板地址拼接。"""

    return join_url(config.base_url, config.backup_api_path)


def download_lucky_backup(config: LuckyConfig, target: Path, timeout: int) -> int:
    """下载并验证 Lucky ZIP 备份，成功时返回文件字节数。"""

    _, _, body = http_request(
        lucky_backup_url(config),
        headers={"openToken": config.open_token, "Accept": "application/zip"},
        timeout=timeout,
        verify_ssl=config.verify_ssl,
        max_response_bytes=MAX_BACKUP_BYTES,
    )
    if not body.startswith(ZIP_SIGNATURES):
        raise RequestError("Lucky 返回内容不是 ZIP 备份，可能是鉴权失败或接口路径不正确")
    target.write_bytes(body)
    if not zipfile.is_zipfile(target):
        raise RequestError("Lucky 返回内容具有 ZIP 标记，但文件结构无效")
    return len(body)


class WebDAVClient:
    """实现本任务所需的最小 WebDAV 客户端。"""

    def __init__(self, config: WebDAVConfig, timeout: int) -> None:
        self.config = config
        self.timeout = timeout
        credentials = f"{config.username}:{config.password}".encode("utf-8")
        self._authorization = "Basic " + base64.b64encode(credentials).decode("ascii")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """构造带 Basic Auth 的请求头，不在日志中输出其内容。"""

        headers = {"Authorization": self._authorization}
        if extra:
            headers.update(extra)
        return headers

    def url_for(self, *parts: str) -> str:
        """生成任务根目录下的远端 URL。"""

        return join_url(self.config.url, self.config.remote_root, *parts)

    def ensure_collection(self, *parts: str) -> None:
        """逐级创建任务目录；WebDAV 以 405 表示目录已经存在。"""

        root_parts = [part for part in self.config.remote_root.split("/") if part]
        extra_parts = [part for value in parts for part in value.split("/") if part]
        current_parts: list[str] = []
        for part in root_parts + extra_parts:
            current_parts.append(part)
            url = join_url(self.config.url, *current_parts)
            try:
                status, _, _ = http_request(
                    url,
                    method="MKCOL",
                    headers=self._headers(),
                    timeout=self.timeout,
                    verify_ssl=self.config.verify_ssl,
                    max_response_bytes=64 * 1024,
                )
                if status not in {200, 201, 204}:
                    raise RequestError(f"MKCOL 请求返回非预期状态 {status}")
            except RequestError as exc:
                # 大多数 WebDAV 服务使用 405 表示集合已存在，可以安全继续。
                if "HTTP 405" not in str(exc):
                    raise

    def upload(self, instance_name: str, filename: str, data: bytes) -> str:
        """上传一个备份文件，返回用于通知展示的远端相对路径。"""

        self.ensure_collection(instance_name)
        status, _, _ = http_request(
            self.url_for(instance_name, filename),
            method="PUT",
            headers=self._headers({"Content-Type": "application/zip"}),
            data=data,
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=256 * 1024,
        )
        if status not in {200, 201, 204}:
            raise RequestError(f"PUT 请求返回非预期状态 {status}")
        return f"{self.config.remote_root}/{instance_name}/{filename}"

    def list_files(self, instance_name: str) -> list[RemoteFile]:
        """使用 Depth=1 的 PROPFIND 列出实例目录中的文件。"""

        request_body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getlastmodified/></d:prop></d:propfind>"""
        _, _, body = http_request(
            self.url_for(instance_name),
            method="PROPFIND",
            headers=self._headers({"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}),
            data=request_body,
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=10 * 1024 * 1024,
        )
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise RequestError("WebDAV PROPFIND 返回的 XML 无法解析") from None

        files: list[RemoteFile] = []
        for response in root.findall("{DAV:}response"):
            href_node = response.find("{DAV:}href")
            if href_node is None or not href_node.text:
                continue
            decoded_path = urllib.parse.unquote(urllib.parse.urlsplit(href_node.text).path)
            is_collection = response.find(".//{DAV:}collection") is not None
            name = decoded_path.rstrip("/").rsplit("/", 1)[-1]
            if is_collection or not name:
                continue
            modified_node = response.find(".//{DAV:}getlastmodified")
            modified_at: datetime | None = None
            if modified_node is not None and modified_node.text:
                try:
                    modified_at = parsedate_to_datetime(modified_node.text)
                    if modified_at.tzinfo is None:
                        modified_at = modified_at.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    modified_at = None
            files.append(RemoteFile(name=name, modified_at=modified_at, is_collection=False))
        return files

    def delete(self, instance_name: str, filename: str) -> None:
        """删除一个已由调用方严格筛选的过期备份文件。"""

        status, _, _ = http_request(
            self.url_for(instance_name, filename),
            method="DELETE",
            headers=self._headers(),
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=256 * 1024,
        )
        if status not in {200, 202, 204}:
            raise RequestError(f"DELETE 请求返回非预期状态 {status}")


def cleanup_expired_backups(
    client: WebDAVClient,
    luckies: Iterable[LuckyConfig],
    retention_days: int,
    now: datetime,
) -> int:
    """只删除本任务固定命名规则且早于保留边界的 ZIP 文件。"""

    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    deleted_count = 0
    for lucky in luckies:
        filename_pattern = re.compile(
            rf"^{re.escape(lucky.safe_name)}_\d{{8}}_\d{{6}}\.zip$"
        )
        for remote_file in client.list_files(lucky.safe_name):
            if not filename_pattern.fullmatch(remote_file.name):
                continue
            if remote_file.modified_at is None:
                continue
            if remote_file.modified_at.astimezone(timezone.utc) >= cutoff:
                continue
            client.delete(lucky.safe_name, remote_file.name)
            deleted_count += 1
    return deleted_count


def send_qinglong_notification(title: str, description: str) -> None:
    """调用青龙任务运行环境注入的 ``QLAPI.notify`` 发送汇总通知。"""

    # 青龙会为 Python 任务注入 QLAPI；同时检查 builtins 以兼容不同运行器实现。
    ql_api = globals().get("QLAPI")
    if ql_api is None:
        import builtins

        ql_api = getattr(builtins, "QLAPI", None)
    notify_method = getattr(ql_api, "notify", None)
    if not callable(notify_method):
        raise NotificationError("当前运行环境未提供青龙 QLAPI.notify")
    try:
        notify_method(title, description)
    except Exception as exc:  # noqa: BLE001 - 第三方运行器可能抛出任意异常类型。
        raise NotificationError(f"青龙通知调用失败（{type(exc).__name__}）") from None


def build_notification(
    results: list[InstanceResult],
    deleted_count: int,
    cleanup_error: str | None,
    elapsed_seconds: float,
) -> tuple[str, str]:
    """生成不含凭据的中文通知标题和 Markdown 正文。"""

    success_count = sum(result.success for result in results)
    failed_count = len(results) - success_count
    status_text = "成功" if failed_count == 0 and cleanup_error is None else "部分失败"
    title = f"Lucky 备份{status_text}：{success_count}/{len(results)}"
    lines = [
        f"## Lucky 配置备份{status_text}",
        "",
        f"- 成功实例：{success_count}",
        f"- 失败实例：{failed_count}",
        f"- 清理过期文件：{deleted_count}",
        f"- 总耗时：{elapsed_seconds:.1f} 秒",
        "",
        "### 实例结果",
        "",
    ]
    for result in results:
        icon = "✅" if result.success else "❌"
        lines.append(f"- {icon} **{result.name}**：{result.message}")
    if cleanup_error:
        lines.extend(["", f"- ❌ **过期清理**：{cleanup_error}"])
    return title, "\n".join(lines)


def run_task(
    config: AppConfig,
    *,
    now: datetime | None = None,
    notification_sender: Callable[[str, str], None] | None = None,
) -> int:
    """执行完整备份流程，返回供青龙识别的进程退出码。"""

    started = time.monotonic()
    task_now = now or datetime.now().astimezone()
    timestamp = task_now.strftime("%Y%m%d_%H%M%S")
    client = WebDAVClient(config.webdav, config.timeout_seconds)
    results: list[InstanceResult] = []
    successful_uploads = 0

    with tempfile.TemporaryDirectory(prefix="lucky-backup-") as temp_dir:
        for lucky in config.luckies:
            target = Path(temp_dir) / f"{lucky.safe_name}_{timestamp}.zip"
            try:
                print(f"[开始] 正在备份 Lucky 实例：{lucky.name}")
                size = download_lucky_backup(lucky, target, config.timeout_seconds)
                remote_path = client.upload(lucky.safe_name, target.name, target.read_bytes())
                successful_uploads += 1
                message = f"已上传 {size} 字节到 {remote_path}"
                results.append(InstanceResult(lucky.name, True, message, remote_path))
                print(f"[成功] {lucky.name}：{message}")
            except BackupError as exc:
                message = str(exc)
                results.append(InstanceResult(lucky.name, False, message))
                print(f"[失败] {lucky.name}：{message}")

    deleted_count = 0
    cleanup_error: str | None = None
    if successful_uploads > 0:
        try:
            deleted_count = cleanup_expired_backups(
                client,
                config.luckies,
                config.retention_days,
                task_now,
            )
            print(f"[清理] 已删除 {deleted_count} 个超过 {config.retention_days} 天的备份")
        except BackupError as exc:
            cleanup_error = str(exc)
            print(f"[失败] 过期备份清理失败：{cleanup_error}")
    else:
        cleanup_error = "本轮没有成功上传的备份，已跳过远端清理"
        print(f"[跳过] {cleanup_error}")

    elapsed = time.monotonic() - started
    title, description = build_notification(results, deleted_count, cleanup_error, elapsed)
    notification_error: str | None = None
    try:
        sender = notification_sender or send_qinglong_notification
        sender(title, description)
        print("[通知] 青龙汇总通知调用成功")
    except BackupError as exc:
        notification_error = str(exc)
        print(f"[失败] 青龙通知调用失败：{notification_error}")
    except Exception as exc:  # noqa: BLE001 - 兼容测试注入及自定义通知适配器。
        notification_error = f"青龙通知调用失败（{type(exc).__name__}）"
        print(f"[失败] 青龙通知调用失败：{notification_error}")

    has_instance_failure = any(not result.success for result in results)
    return 1 if has_instance_failure or cleanup_error or notification_error else 0


def main() -> int:
    """青龙任务入口。"""

    raw_config = os.environ.get(CONFIG_ENV_NAME, "")
    try:
        config = parse_config(raw_config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        # 主配置失败时不进行备份或清理，但仍尽最大努力调用青龙的统一通知。
        try:
            send_qinglong_notification(
                "Lucky 备份配置错误",
                f"## Lucky 配置备份失败\n\n- 配置错误：{exc}",
            )
            print("[通知] 青龙配置错误通知调用成功")
        except BackupError as notify_exc:
            print(f"[失败] 青龙配置错误通知调用失败：{notify_exc}")
        return 1
    return run_task(config)


if __name__ == "__main__":
    sys.exit(main())
