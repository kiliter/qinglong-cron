# WebDAV 备份多目标同步任务

## 1. 使用场景

MoviePilot WebDAV 备份插件在 MoviePilot 容器内生成备份，并上传到一个源 WebDAV。青龙不需要访问 MoviePilot 数据目录，只负责把源 WebDAV 中最新的备份文件同步到一个或多个目标 WebDAV。

本任务位于 `tasks/webdav_backup_sync.py`，与 Lucky 配置备份任务互不依赖，使用独立环境变量和执行计划。

## 2. 执行规则

每个同步组严格按照以下流程执行：

1. 列出 `source` 目录，仅识别符合命名格式的 ZIP 文件。
2. 按文件名时间排序，只选择最新一个源文件。
3. 依次检查每个 `target` 是否存在同名文件。
4. 目标已存在同名文件时直接跳过，不上传，也不执行清理。
5. 至少一个目标缺少文件时，只从源端下载一次 ZIP，并校验 ZIP 有效性。
6. 将同一份 ZIP 上传到所有缺失目标。
7. 每个目标上传成功后，删除该目标中超出 `retention_count` 数量的最旧备份。
8. 单个目标或同步组失败不会阻止其他目标和同步组继续执行，不进行回滚。

默认识别以下 MoviePilot 备份文件名：

```text
MoviePilot-Backup-YYYY-MM-DD_HH-MM-SS.zip
```

手工文件、名称不匹配文件和日期无效文件不会被同步或删除。

## 3. 青龙配置

在青龙“环境变量”中新增：

- 名称：`WEBDAV_BACKUP_SYNC_CONFIG`
- 值：复制并修改 `examples/webdav_backup_sync_config.example.json` 的完整 JSON 内容。

配置支持任意数量的同步组，每组包含一个源端和一个或多个目标端：

```json
{
  "groups": [
    {
      "name": "MoviePilot 主实例",
      "source": {
        "name": "源存储",
        "url": "https://source.example.com/dav/user",
        "username": "源端用户名",
        "password": "源端密码",
        "remote_root": "/moviepilot-backup",
        "verify_ssl": true
      },
      "targets": [
        {
          "name": "异地存储 A",
          "url": "https://target-a.example.com/dav/user",
          "username": "目标端用户名",
          "password": "目标端密码",
          "remote_root": "/moviepilot-backup",
          "verify_ssl": true
        }
      ],
      "retention_count": 10,
      "filename_prefix": "MoviePilot-Backup-"
    }
  ],
  "timeout_seconds": 60,
  "max_backup_mb": 512
}
```

## 4. 配置字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `groups` | 是 | 同步组数组，至少包含一组。 |
| `groups[].name` | 是 | 同步组名称，不得重复。 |
| `groups[].source` | 是 | 本组唯一源 WebDAV。 |
| `groups[].targets` | 是 | 目标 WebDAV 数组，至少包含一个目标。 |
| `groups[].retention_count` | 否 | 每个目标保留的备份数量，默认 `10`。 |
| `groups[].filename_prefix` | 否 | 备份文件名前缀，默认 `MoviePilot-Backup-`。 |
| `source/targets[].name` | 否 | 日志和通知中显示的名称；同组目标名称不得重复。 |
| `source/targets[].url` | 是 | WebDAV 服务地址，可包含服务本身的路径前缀。 |
| `source/targets[].username` | 是 | WebDAV 用户名。 |
| `source/targets[].password` | 是 | WebDAV 密码或应用专用密码。 |
| `source/targets[].remote_root` | 否 | 备份所在目录，默认 `/`。 |
| `source/targets[].verify_ssl` | 否 | 是否验证 HTTPS 证书，默认 `true`。 |
| `timeout_seconds` | 否 | 单次网络请求超时秒数，默认 `60`。 |
| `max_backup_mb` | 否 | 单个源备份允许的最大体积，默认 `512` MiB。 |

当前 WebDAV 客户端使用 Basic Auth。如果服务只允许 Digest Auth，需要先在服务端启用 Basic Auth，或后续扩展客户端认证方式。

## 5. 调度建议

应确保 MoviePilot 插件先完成源端上传，再运行青龙同步任务。例如：

```text
MoviePilot WebDAV 备份：0 2 * * *
青龙 WebDAV 同步：     10 2 * * *
```

任务脚本默认 Cron 为 `10 2 * * *`。首次配置后建议在青龙中手动执行一次，确认各目标的上传、跳过和清理日志符合预期。

## 6. 通知与安全

任务使用青龙 `QLAPI.systemNotify` 发送一次汇总通知，并校验返回状态。日志和通知不会输出 WebDAV 密码或认证请求头。

- 不要把真实配置提交到 Git 仓库。
- 建议为源端账号配置只读权限；但部分 WebDAV 服务无法细分权限时，应至少限制到备份目录。
- 建议为每个目标使用仅能访问对应备份目录的独立账号。
- `verify_ssl: false` 只适合暂时使用自签名证书的可信内网。
