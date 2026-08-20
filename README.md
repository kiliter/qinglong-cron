# 青龙定时任务订阅

这是一个供青龙面板订阅的公开定时任务仓库。首个任务用于定期备份多个 Lucky 实例的配置，将备份上传到一个 WebDAV 服务，并通过 Server酱发送执行汇总。

## 已提供任务

### Lucky 配置备份到 WebDAV

- 每天凌晨 03:00 执行；
- 支持配置多个 Lucky 实例；
- 每个实例使用独立 OpenToken；
- 所有实例共用一个 WebDAV 目标；
- 远端仅保留最近 30 天的任务备份；
- 每次执行均发送一条 Server酱中文汇总；
- 单个 Lucky 失败不会阻止其他实例继续备份；
- 仅使用 Python 3 标准库，无需安装第三方依赖。

任务脚本：[`tasks/lucky_webdav_backup.py`](tasks/lucky_webdav_backup.py)

## 青龙订阅

在青龙面板的“订阅管理”中新增订阅：

| 配置项 | 填写内容 |
| --- | --- |
| 名称 | `qinglong-cron` |
| 类型 | 公开仓库 |
| 链接 | `https://github.com/kiliter/qinglong-cron.git` |
| 定时类型 | `crontab` |
| 定时规则 | 可按需设置，例如每天更新一次订阅 |
| 白名单 | `tasks/lucky_webdav_backup.py` |
| 黑名单 | `tests/\|docs/\|examples/` |

白名单必须填写完整任务路径，不要只填写 `tasks/`，否则青龙可能把目录中的其他 Python 文件也作为候选任务处理。订阅完成后，青龙会从脚本注释读取任务名称和 Cron。任务默认规则为五段 Cron `0 3 * * *`，即每天凌晨 03:00。

## Lucky 准备

1. 登录每个 Lucky 管理后台。
2. 进入设置页面底部的开发者设置。
3. 启用 OpenToken 并复制生成的 Token。
4. 确认青龙容器能够访问 Lucky 管理地址。

脚本默认调用 Lucky 2.x 的 `GET /api/configure` 备份接口，并通过 `openToken` 请求头鉴权。如使用了接口不同的版本，可在对应实例中设置 `backup_api_path` 覆盖默认路径。

## 环境变量配置

在青龙面板的“环境变量”中新增：

- 名称：`LUCKY_BACKUP_CONFIG`
- 值：复制并修改 [`examples/lucky_backup_config.example.json`](examples/lucky_backup_config.example.json) 的完整 JSON 内容。

主要配置字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `luckies` | 是 | Lucky 实例数组，至少包含一个实例。 |
| `luckies[].name` | 是 | 实例显示名，同时用于生成安全的远端目录名。 |
| `luckies[].base_url` | 是 | Lucky 面板基础地址，例如 `http://192.168.1.1:16601`。 |
| `luckies[].open_token` | 是 | 该实例的 OpenToken。 |
| `luckies[].backup_api_path` | 否 | 备份接口，默认 `/api/configure`。 |
| `luckies[].verify_ssl` | 否 | 是否校验 HTTPS 证书，默认 `true`。 |
| `webdav.url` | 是 | WebDAV 服务地址，可包含服务自身的路径前缀。 |
| `webdav.username` | 是 | WebDAV 用户名。 |
| `webdav.password` | 是 | WebDAV 密码或应用专用密码。 |
| `webdav.remote_root` | 否 | 任务远端根目录，默认 `/qinglong/lucky-backup`。 |
| `webdav.verify_ssl` | 否 | 是否校验 WebDAV HTTPS 证书，默认 `true`。 |
| `serverchan.send_key` | 是 | Server酱 SendKey，支持 `SCT` 和 `sctp` 两类。 |
| `serverchan.verify_ssl` | 否 | 是否校验 Server酱 HTTPS 证书，默认 `true`。 |
| `retention_days` | 否 | 保留天数，默认 `30`。 |
| `timeout_seconds` | 否 | 单次网络请求超时秒数，默认 `60`。 |

`verify_ssl: false` 只适合使用自签名证书且暂时无法配置可信证书的内网服务。公网服务应保持 `true`。

## 远端目录与清理规则

备份文件路径如下：

```text
/qinglong/lucky-backup/<实例名称>/<实例名称>_YYYYMMDD_HHMMSS.zip
```

清理功能只会删除任务根目录下同时满足以下条件的文件：

1. 位于已配置 Lucky 实例对应的子目录；
2. 文件名严格符合本任务的命名格式；
3. WebDAV 返回的修改时间早于当前时间 30 天以上。

手工文件、名称不匹配的文件以及无法取得修改时间的文件不会被删除。如果本轮所有 Lucky 备份均失败，任务会跳过远端清理。

## Server酱通知

任务每次运行只合并发送一条通知，包含各实例的成功或失败状态、清理数量和总耗时。

- `SCT` 开头的 SendKey 自动使用 Server酱 Turbo；
- `sctp` 开头的 SendKey 自动使用 Server酱³；
- 通知与任务日志不会输出 OpenToken、WebDAV 密码或完整 SendKey。

## 手动测试

配置环境变量后，可在青龙面板中手动运行任务，也可以在项目目录执行：

```bash
python3 tasks/lucky_webdav_backup.py
```

运行自动化测试：

```bash
python3 -m unittest discover -s tests -v
```

测试全部使用本地模拟服务，不会访问真实 Lucky、WebDAV 或 Server酱。

## 安全提示

- 不要把真实配置保存到仓库；
- 不要在 Issue、日志截图或聊天记录中公开任何 Token 或密码；
- OpenToken 或 SendKey 泄露后应立即在对应后台重置；
- 建议为 WebDAV 使用仅能访问备份目录的独立账号。
