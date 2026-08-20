# 青龙定时任务订阅

这是一个供青龙面板订阅的公开定时任务仓库。首个任务用于定期备份多个 Lucky 实例的配置，将备份上传到一个或多个 WebDAV 服务，并通过青龙统一通知发送执行汇总。

## 已提供任务

### Lucky 配置备份到 WebDAV

- 每天凌晨 03:00 执行；
- 支持配置多个 Lucky 实例；
- 每个实例使用独立 OpenToken；
- 支持同时上传到多个 WebDAV 目标；
- 每个实例在远端仅保留最新 30 个任务备份；
- 每次执行均通过青龙统一通知发送一条中文汇总；
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
| `webdavs` | 是 | WebDAV 目标数组，至少包含一个目标；旧版单个 `webdav` 对象仍兼容。 |
| `webdavs[].name` | 否 | 目标显示名，用于日志和通知；默认按顺序生成。名称不得重复。 |
| `webdavs[].url` | 是 | WebDAV 服务地址，可包含服务自身的路径前缀。 |
| `webdavs[].username` | 是 | WebDAV 用户名。 |
| `webdavs[].password` | 是 | WebDAV 密码或应用专用密码。 |
| `webdavs[].remote_root` | 否 | 任务远端根目录，默认 `/qinglong/lucky-backup`。 |
| `webdavs[].verify_ssl` | 否 | 是否校验 WebDAV HTTPS 证书，默认 `true`。 |
| `retention_count` | 否 | 每个实例保留的最新备份数量，默认 `30`。 |
| `retention_days` | 否 | 旧版兼容字段；未设置 `retention_count` 时，其值按保留数量解释。 |
| `timeout_seconds` | 否 | 单次网络请求超时秒数，默认 `60`。 |

`verify_ssl: false` 只适合使用自签名证书且暂时无法配置可信证书的内网服务。公网服务应保持 `true`。

## 远端目录与清理规则

备份文件路径如下：

```text
/qinglong/lucky-backup/<实例名称>/lucky.<实例名称>.YYYYMMDD_HHMMSS.zip
```

每个 WebDAV 目标都会独立统计各实例目录中的文件，并删除超出 `retention_count` 数量的最旧备份。参与统计的文件必须同时满足以下条件：

1. 位于已配置 Lucky 实例对应的子目录；
2. 文件名严格符合本任务的命名格式；
3. 文件名中的日期和时间合法，可用于确定备份先后顺序。

清理程序同时兼容历史格式 `<实例名称>_YYYYMMDD_HHMMSS.zip`。手工文件、名称不匹配的文件以及日期无效的文件不会被统计或删除；不存在的实例目录会被视为空目录。任务按 Lucky 串行执行：拉取一个 Lucky 的 ZIP，上传到全部 WebDAV，再分别清理该 Lucky 在上传成功目标中的目录，然后才处理下一个 Lucky。单个上传或清理失败不会回滚已完成操作，也不会阻止其他目标和 Lucky 继续执行，但任务最终会标记为部分失败。

## 青龙统一通知

任务每次运行只调用一次青龙内置的 `QLAPI.notify`，通知包含各实例的成功或失败状态、清理数量和总耗时。

- 通知渠道和密钥统一在青龙“系统设置 → 通知设置”中维护；
- 可在青龙中选择 Server酱或其他已支持的通知渠道；
- `LUCKY_BACKUP_CONFIG` 不再需要填写 Server酱 SendKey；
- 通知与任务日志不会输出 OpenToken 或 WebDAV 密码。

## 手动测试

配置环境变量后，应在青龙面板中手动运行任务。脚本也可以在项目目录直接执行，但普通 Python 环境没有青龙注入的 `QLAPI`，通知阶段会明确报错并返回失败状态：

```bash
python3 tasks/lucky_webdav_backup.py
```

运行自动化测试：

```bash
python3 -m unittest discover -s tests -v
```

测试使用本地模拟 Lucky、WebDAV 服务和模拟 `QLAPI`，不会访问真实服务或通知渠道。

## 安全提示

- 不要把真实配置保存到仓库；
- 不要在 Issue、日志截图或聊天记录中公开任何 Token 或密码；
- OpenToken 或青龙通知密钥泄露后应立即在对应后台重置；
- 建议为 WebDAV 使用仅能访问备份目录的独立账号。
