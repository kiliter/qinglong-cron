#!/usr/bin/env python3
"""
WebDAV 备份多目标同步。

name: WebDAV 备份多目标同步
cron: 10 2 * * *

配置通过青龙环境变量 ``WEBDAV_BACKUP_SYNC_CONFIG`` 提供。任务支持 N 组
``source -> targets``，默认同步 MoviePilot WebDAV 备份插件生成的 ZIP 文件。
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from xml.etree import ElementTree


CONFIG_ENV_NAME = "WEBDAV_BACKUP_SYNC_CONFIG"
DEFAULT_FILENAME_PREFIX = "MoviePilot-Backup-"
DEFAULT_RETENTION_COUNT = 10
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_BACKUP_MB = 512


class SyncError(Exception):
    """同步任务可预期错误的基类；错误文本不得包含账号、密码或完整 URL。"""


class ConfigError(SyncError):
    """配置内容不合法。"""


class RequestError(SyncError):
    """WebDAV 请求失败。"""


class NotificationError(SyncError):
    """青龙通知发送失败。"""


@dataclass(frozen=True)
class WebDAVEndpoint:
    """一个源端或目标端 WebDAV 配置。"""

    name: str
    url: str
    username: str
    password: str
    remote_root: str = "/"
    verify_ssl: bool = True


@dataclass(frozen=True)
class SyncGroupConfig:
    """一组相互独立的 WebDAV 源端到多目标同步配置。"""

    name: str
    source: WebDAVEndpoint
    targets: tuple[WebDAVEndpoint, ...]
    retention_count: int = DEFAULT_RETENTION_COUNT
    filename_prefix: str = DEFAULT_FILENAME_PREFIX


@dataclass(frozen=True)
class AppConfig:
    """一次任务运行所需的完整配置。"""

    groups: tuple[SyncGroupConfig, ...]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_backup_bytes: int = DEFAULT_MAX_BACKUP_MB * 1024 * 1024


@dataclass(frozen=True)
class RemoteFile:
    """WebDAV 目录中的一个普通文件。"""

    name: str
    size: int | None


@dataclass
class TargetResult:
    """单个目标端的同步统计和错误信息。"""

    name: str
    uploaded: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """目标端没有错误时视为成功。"""

        return not self.errors


@dataclass
class GroupResult:
    """单个同步组的结果，用于日志、退出码和通知汇总。"""

    name: str
    source_files: int = 0
    targets: list[TargetResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """源端及所有目标端均无错误时视为成功。"""

        return not self.errors and all(target.success for target in self.targets)


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    """校验字段为 JSON 对象。"""

    if not isinstance(value, dict):
        raise ConfigError(f"配置字段 {field_name} 必须是 JSON 对象")
    return value


def _require_string(mapping: dict[str, Any], key: str, field_name: str) -> str:
    """读取必填非空字符串。"""

    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"配置字段 {field_name}.{key} 必须是非空字符串")
    return value.strip()


def _read_bool(mapping: dict[str, Any], key: str, default: bool, field_name: str) -> bool:
    """读取布尔配置，拒绝字符串形式的真假值。"""

    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置字段 {field_name}.{key} 必须是布尔值")
    return value


def _validate_url(value: str, field_name: str) -> str:
    """只允许不含内嵌凭据的 HTTP 或 HTTPS URL。"""

    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"配置字段 {field_name} 必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ConfigError(f"配置字段 {field_name} 不得内嵌账号或密码")
    return value.rstrip("/")


def _validate_remote_root(value: str, field_name: str) -> str:
    """规范化远端目录，并禁止路径穿越片段。"""

    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ConfigError(f"配置字段 {field_name} 不得包含路径穿越片段")
    return "/" + "/".join(parts) if parts else "/"


def _parse_endpoint(value: Any, field_name: str, default_name: str) -> WebDAVEndpoint:
    """解析一个 WebDAV 端点。"""

    endpoint = _require_mapping(value, field_name)
    raw_name = endpoint.get("name", default_name)
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConfigError(f"配置字段 {field_name}.name 必须是非空字符串")
    return WebDAVEndpoint(
        name=raw_name.strip(),
        url=_validate_url(_require_string(endpoint, "url", field_name), f"{field_name}.url"),
        username=_require_string(endpoint, "username", field_name),
        password=_require_string(endpoint, "password", field_name),
        remote_root=_validate_remote_root(
            str(endpoint.get("remote_root", "/")).strip(), f"{field_name}.remote_root"
        ),
        verify_ssl=_read_bool(endpoint, "verify_ssl", True, field_name),
    )


def parse_config(raw: str) -> AppConfig:
    """解析并完整校验 ``WEBDAV_BACKUP_SYNC_CONFIG``。"""

    if not raw.strip():
        raise ConfigError(f"未设置环境变量 {CONFIG_ENV_NAME}")
    try:
        root = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"环境变量 {CONFIG_ENV_NAME} 不是有效 JSON：第 {exc.lineno} 行") from None
    root = _require_mapping(root, CONFIG_ENV_NAME)

    group_items = root.get("groups")
    if not isinstance(group_items, list) or not group_items:
        raise ConfigError("配置字段 groups 必须是非空数组")

    groups: list[SyncGroupConfig] = []
    group_names: set[str] = set()
    for group_index, item in enumerate(group_items):
        field_name = f"groups[{group_index}]"
        group_obj = _require_mapping(item, field_name)
        name = _require_string(group_obj, "name", field_name)
        if name in group_names:
            raise ConfigError(f"同步组名称不得重复：{name}")
        group_names.add(name)

        source = _parse_endpoint(group_obj.get("source"), f"{field_name}.source", "源端")
        target_items = group_obj.get("targets")
        if not isinstance(target_items, list) or not target_items:
            raise ConfigError(f"配置字段 {field_name}.targets 必须是非空数组")
        targets = tuple(
            _parse_endpoint(target, f"{field_name}.targets[{index}]", f"目标 {index + 1}")
            for index, target in enumerate(target_items)
        )
        target_names = [target.name for target in targets]
        if len(target_names) != len(set(target_names)):
            raise ConfigError(f"配置字段 {field_name}.targets 中的 name 不得重复")

        retention_count = group_obj.get("retention_count", DEFAULT_RETENTION_COUNT)
        if (
            not isinstance(retention_count, int)
            or isinstance(retention_count, bool)
            or not 1 <= retention_count <= 3650
        ):
            raise ConfigError(f"配置字段 {field_name}.retention_count 必须是 1 到 3650 之间的整数")
        filename_prefix = group_obj.get("filename_prefix", DEFAULT_FILENAME_PREFIX)
        if (
            not isinstance(filename_prefix, str)
            or not filename_prefix
            or "/" in filename_prefix
            or "\\" in filename_prefix
            or len(filename_prefix) > 128
        ):
            raise ConfigError(
                f"配置字段 {field_name}.filename_prefix 必须是不含路径分隔符的非空字符串"
            )
        groups.append(
            SyncGroupConfig(
                name=name,
                source=source,
                targets=targets,
                retention_count=retention_count,
                filename_prefix=filename_prefix,
            )
        )

    timeout_seconds = root.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 5 <= timeout_seconds <= 600
    ):
        raise ConfigError("配置字段 timeout_seconds 必须是 5 到 600 之间的整数")
    max_backup_mb = root.get("max_backup_mb", DEFAULT_MAX_BACKUP_MB)
    if (
        not isinstance(max_backup_mb, int)
        or isinstance(max_backup_mb, bool)
        or not 1 <= max_backup_mb <= 4096
    ):
        raise ConfigError("配置字段 max_backup_mb 必须是 1 到 4096 之间的整数")
    return AppConfig(
        groups=tuple(groups),
        timeout_seconds=timeout_seconds,
        max_backup_bytes=max_backup_mb * 1024 * 1024,
    )


def _ssl_context(verify_ssl: bool) -> ssl.SSLContext | None:
    """按配置创建 TLS 上下文。"""

    if verify_ssl:
        return None
    return ssl._create_unverified_context()  # noqa: SLF001 - 用户明确关闭证书校验。


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
    """发送 HTTP 请求，并转换为不泄露凭据和 URL 的中文错误。"""

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
        raise RequestError(f"{method} 请求返回 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else "URLError"
        raise RequestError(f"{method} 请求连接失败（{reason_name}）") from None
    except TimeoutError:
        raise RequestError(f"{method} 请求超时") from None


def join_url(base_url: str, *parts: str) -> str:
    """逐段编码并拼接 URL，保留 WebDAV 地址已有的路径前缀。"""

    encoded_parts: list[str] = []
    for part in parts:
        for segment in part.replace("\\", "/").split("/"):
            if segment:
                encoded_parts.append(urllib.parse.quote(segment, safe=""))
    if not encoded_parts:
        return base_url.rstrip("/") + "/"
    return base_url.rstrip("/") + "/" + "/".join(encoded_parts)


class WebDAVClient:
    """只实现同步任务需要的最小 WebDAV 操作。"""

    def __init__(self, config: WebDAVEndpoint, timeout: int):
        self.config = config
        self.timeout = timeout
        token = base64.b64encode(
            f"{config.username}:{config.password}".encode("utf-8")
        ).decode("ascii")
        self._authorization = f"Basic {token}"
        self._root_ready = False

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """构造带 Basic Auth 的请求头，不在日志中输出其内容。"""

        headers = {"Authorization": self._authorization}
        if extra:
            headers.update(extra)
        return headers

    def url_for(self, *parts: str) -> str:
        """生成当前远端根目录下的 URL。"""

        return join_url(self.config.url, self.config.remote_root, *parts)

    def ensure_root(self) -> None:
        """逐级创建目标根目录；WebDAV 通常以 405 表示目录已存在。"""

        if self._root_ready:
            return
        current_parts: list[str] = []
        for part in self.config.remote_root.split("/"):
            if not part:
                continue
            current_parts.append(part)
            try:
                status, _, _ = http_request(
                    join_url(self.config.url, *current_parts),
                    method="MKCOL",
                    headers=self._headers(),
                    timeout=self.timeout,
                    verify_ssl=self.config.verify_ssl,
                    max_response_bytes=64 * 1024,
                )
                if status not in {200, 201, 204}:
                    raise RequestError(f"MKCOL 请求返回非预期状态 {status}")
            except RequestError as exc:
                if "HTTP 405" not in str(exc):
                    raise
        self._root_ready = True

    def list_files(self) -> list[RemoteFile]:
        """使用 Depth=1 的 PROPFIND 列出根目录普通文件。"""

        request_body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>"""
        try:
            _, _, body = http_request(
                self.url_for(),
                method="PROPFIND",
                headers=self._headers(
                    {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
                ),
                data=request_body,
                timeout=self.timeout,
                verify_ssl=self.config.verify_ssl,
                max_response_bytes=10 * 1024 * 1024,
            )
        except RequestError as exc:
            if str(exc) == "PROPFIND 请求返回 HTTP 404":
                return []
            raise
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise RequestError("WebDAV PROPFIND 返回的 XML 无法解析") from None

        files: list[RemoteFile] = []
        for response in root.findall("{DAV:}response"):
            href_node = response.find("{DAV:}href")
            if href_node is None or not href_node.text:
                continue
            if response.find(".//{DAV:}collection") is not None:
                continue
            decoded_path = urllib.parse.unquote(urllib.parse.urlsplit(href_node.text).path)
            name = decoded_path.rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            size_node = response.find(".//{DAV:}getcontentlength")
            try:
                size = int(size_node.text) if size_node is not None and size_node.text else None
            except (TypeError, ValueError):
                size = None
            files.append(RemoteFile(name=name, size=size))
        return files

    def download(self, filename: str, max_backup_bytes: int) -> bytes:
        """下载并校验 ZIP 备份内容。"""

        _, _, data = http_request(
            self.url_for(filename),
            method="GET",
            headers=self._headers(),
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=max_backup_bytes,
        )
        if not data or not zipfile.is_zipfile(io.BytesIO(data)):
            raise RequestError("源端返回内容不是有效 ZIP 备份")
        return data

    def upload(self, filename: str, data: bytes) -> None:
        """上传一个备份文件到目标根目录。"""

        self.ensure_root()
        status, _, _ = http_request(
            self.url_for(filename),
            method="PUT",
            headers=self._headers({"Content-Type": "application/zip"}),
            data=data,
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=256 * 1024,
        )
        if status not in {200, 201, 204}:
            raise RequestError(f"PUT 请求返回非预期状态 {status}")

    def delete(self, filename: str) -> None:
        """删除一个已经严格筛选的多余备份。"""

        status, _, _ = http_request(
            self.url_for(filename),
            method="DELETE",
            headers=self._headers(),
            timeout=self.timeout,
            verify_ssl=self.config.verify_ssl,
            max_response_bytes=256 * 1024,
        )
        if status not in {200, 202, 204}:
            raise RequestError(f"DELETE 请求返回非预期状态 {status}")


def backup_timestamp(filename: str, filename_prefix: str) -> datetime | None:
    """解析 MoviePilot 风格备份文件名；不匹配或日期无效时返回 ``None``。"""

    pattern = re.compile(
        rf"^{re.escape(filename_prefix)}(?P<timestamp>\d{{4}}-\d{{2}}-\d{{2}}_"
        rf"\d{{2}}-\d{{2}}-\d{{2}})\.zip$"
    )
    match = pattern.fullmatch(filename)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("timestamp"), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def matching_backups(files: list[RemoteFile], filename_prefix: str) -> list[RemoteFile]:
    """筛选合法备份并按文件名时间从新到旧排序。"""

    candidates = [
        (timestamp, remote_file)
        for remote_file in files
        if (timestamp := backup_timestamp(remote_file.name, filename_prefix)) is not None
    ]
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return [remote_file for _, remote_file in candidates]


def sync_group(config: SyncGroupConfig, timeout: int, max_backup_bytes: int) -> GroupResult:
    """把源端最新一个备份同步到全部目标端；目标之间互不阻塞。"""

    result = GroupResult(name=config.name)
    source_client = WebDAVClient(config.source, timeout)
    print(f"[开始] 同步组：{config.name}")
    try:
        source_backups = matching_backups(
            source_client.list_files(), config.filename_prefix
        )
    except SyncError as exc:
        result.errors.append(f"源端列表失败：{exc}")
        print(f"[失败] {config.name}：{result.errors[-1]}")
        return result

    if not source_backups:
        result.errors.append("源端没有找到符合命名规则的 ZIP 备份")
        print(f"[失败] {config.name}：{result.errors[-1]}")
        return result
    source_file = source_backups[0]
    result.source_files = 1

    clients = {
        target.name: WebDAVClient(target, timeout) for target in config.targets
    }
    target_results = {
        target.name: TargetResult(name=target.name) for target in config.targets
    }
    missing_targets: list[str] = []
    for target in config.targets:
        target_result = target_results[target.name]
        result.targets.append(target_result)
        try:
            files = clients[target.name].list_files()
            if any(remote_file.name == source_file.name for remote_file in files):
                target_result.skipped = 1
                print(
                    f"[跳过] {config.name}/{target.name}："
                    f"目标已存在 {source_file.name}"
                )
            else:
                missing_targets.append(target.name)
        except SyncError as exc:
            target_result.errors.append(f"列出目标目录失败：{exc}")
            print(f"[失败] {config.name}/{target.name}：{target_result.errors[-1]}")

    # 至少一个目标缺少最新文件时才下载；同一份源文件在全部缺失目标间复用。
    backup_data: bytes | None = None
    if missing_targets:
        try:
            backup_data = source_client.download(source_file.name, max_backup_bytes)
        except SyncError as exc:
            error = f"下载 {source_file.name} 失败：{exc}"
            result.errors.append(error)
            print(f"[失败] {config.name}：{error}")
            return result

    if backup_data is None:
        # 所有目标均已存在最新文件，按照规则直接结束本组，不触发任何清理。
        return result

    for target_name in missing_targets:
        target_result = target_results[target_name]
        try:
            # 只有新文件上传成功后才清理该目标；已存在文件的目标完全跳过。
            clients[target_name].upload(source_file.name, backup_data)
            target_result.uploaded = 1
            print(f"[上传] {config.name}/{target_name}：{source_file.name}")
            current_files = clients[target_name].list_files()
            if not any(remote_file.name == source_file.name for remote_file in current_files):
                raise RequestError("上传完成后未在目标目录检测到最新备份，已停止清理")
            current_backups = matching_backups(current_files, config.filename_prefix)
            for remote_file in current_backups[config.retention_count :]:
                clients[target_name].delete(remote_file.name)
                target_result.deleted += 1
            print(
                f"[清理] {config.name}/{target_name}：删除 {target_result.deleted} 个，"
                f"保留最新 {config.retention_count} 个"
            )
        except SyncError as exc:
            stage = "清理" if target_result.uploaded else "上传"
            target_result.errors.append(f"{stage}失败：{exc}")
            print(f"[失败] {config.name}/{target_name}：{target_result.errors[-1]}")
    return result


def send_qinglong_notification(title: str, description: str) -> None:
    """通过青龙系统通知发送汇总，并校验返回状态。"""

    ql_api = globals().get("QLAPI")
    if ql_api is None:
        import builtins

        ql_api = getattr(builtins, "QLAPI", None)
    system_notify_method = getattr(ql_api, "systemNotify", None)
    if callable(system_notify_method):
        try:
            response = system_notify_method({"title": title, "content": description})
        except Exception as exc:  # noqa: BLE001 - 青龙运行器可能抛出任意异常类型。
            raise NotificationError(f"青龙系统通知调用失败（{type(exc).__name__}）") from None
        if not isinstance(response, dict) or str(response.get("code")) != "200":
            message = response.get("message") if isinstance(response, dict) else None
            raise NotificationError(f"青龙系统通知发送失败：{message or '未返回成功状态'}")
        return

    notify_method = getattr(ql_api, "notify", None)
    if not callable(notify_method):
        raise NotificationError("当前运行环境未提供青龙通知接口")
    try:
        notify_method(title, description)
    except Exception as exc:  # noqa: BLE001 - 兼容旧版青龙通知接口。
        raise NotificationError(f"青龙兼容通知调用失败（{type(exc).__name__}）") from None


def build_notification(results: list[GroupResult], elapsed_seconds: float) -> tuple[str, str]:
    """生成中文 Markdown 汇总通知。"""

    successful_groups = sum(result.success for result in results)
    status = "成功" if successful_groups == len(results) else "部分失败"
    uploaded = sum(target.uploaded for result in results for target in result.targets)
    skipped = sum(target.skipped for result in results for target in result.targets)
    deleted = sum(target.deleted for result in results for target in result.targets)
    lines = [
        f"## WebDAV 备份同步{status}",
        "",
        f"- 成功同步组：{successful_groups}/{len(results)}",
        f"- 上传文件：{uploaded}",
        f"- 跳过已有文件：{skipped}",
        f"- 清理多余文件：{deleted}",
        f"- 总耗时：{elapsed_seconds:.1f} 秒",
        "",
        "### 同步组结果",
        "",
    ]
    for result in results:
        icon = "✅" if result.success else "❌"
        lines.append(f"- {icon} **{result.name}**：源端候选 {result.source_files} 个")
        for error in result.errors:
            lines.append(f"  - 源端错误：{error}")
        for target in result.targets:
            target_icon = "✅" if target.success else "❌"
            lines.append(
                f"  - {target_icon} {target.name}：上传 {target.uploaded}，"
                f"跳过 {target.skipped}，删除 {target.deleted}"
            )
            for error in target.errors:
                lines.append(f"    - 错误：{error}")
    return f"WebDAV 备份同步{status}：{successful_groups}/{len(results)}", "\n".join(lines)


def run_task(
    config: AppConfig,
    *,
    notification_sender: Callable[[str, str], None] | None = None,
) -> int:
    """依次执行全部同步组并返回供青龙识别的退出码。"""

    started = time.monotonic()
    results: list[GroupResult] = []
    for group in config.groups:
        try:
            results.append(
                sync_group(group, config.timeout_seconds, config.max_backup_bytes)
            )
        except Exception as exc:  # noqa: BLE001 - 单组异常不得阻塞后续同步组。
            error = f"未预期错误（{type(exc).__name__}）"
            results.append(GroupResult(name=group.name, errors=[error]))
            print(f"[失败] {group.name}：{error}")

    title, description = build_notification(results, time.monotonic() - started)
    notification_error: str | None = None
    try:
        (notification_sender or send_qinglong_notification)(title, description)
        print("[通知] 青龙汇总通知发送成功")
    except SyncError as exc:
        notification_error = str(exc)
        print(f"[失败] 青龙通知发送失败：{notification_error}")
    except Exception as exc:  # noqa: BLE001 - 兼容测试注入及自定义通知函数。
        notification_error = f"青龙通知发送失败（{type(exc).__name__}）"
        print(f"[失败] {notification_error}")
    return 1 if any(not result.success for result in results) or notification_error else 0


def main() -> int:
    """青龙任务入口。"""

    raw_config = os.environ.get(CONFIG_ENV_NAME, "")
    try:
        config = parse_config(raw_config)
    except ConfigError as exc:
        print(f"[配置错误] {exc}")
        try:
            send_qinglong_notification(
                "WebDAV 备份同步配置错误",
                f"## WebDAV 备份同步失败\n\n- 配置错误：{exc}",
            )
        except SyncError as notify_exc:
            print(f"[失败] 青龙配置错误通知发送失败：{notify_exc}")
        return 1
    return run_task(config)


if __name__ == "__main__":
    raise SystemExit(main())
