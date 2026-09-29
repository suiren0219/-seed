## 面板配置优化完成

### 改动内容

**1. 新增两个预警配置项（现在可以在面板直接调了）**
- `warn_threshold`（低电量预警阈值）：剩余电量低于此值时发出预警，默认 20 度
- `warn_drop_per_hour`（掉电速率预警阈值）：电量下降速率超过此值时预警（可能是大功率电器或异常），默认 5 度/小时

**2. 精简了提示文案**
- `rooms` 的 hint 从 80+ 字压到 60 字，去掉了「# 开头的说明行和空行会被忽略」这种显而易见的说明
- `cron_hours` 的 hint 补了「早 8 点和晚 20 点」让用户更清楚
- 示例文本也精简了（default 里自带的说明文字）

### 面板字段一览（面板会按这个顺序展示）

| 字段 | 描述 | 默认值 | 提示 |
|---|---|---|---|
| `account` | 学号或身份证号（支持加密） | "" | 支持 `enc:v1:` 加密格式，留空则由用户各自发 /电量绑定 |
| `customercode` | 学校代码 | 1000145 | - |
| `rooms` | 默认监控的房间列表（兜底用） | 示例文本（3 行） | 填写格式：房间名\|roomverify。roomverify 获取方式：绑定账号后发 /电量房间 即可看到。普通用户不必填写这里，直接使用 /电量绑定 自助绑定。 |
| `threshold` | 预警阈值（度，已弃用建议用下面的） | 20 | - |
| `cron_hours` | 每日定时播报的小时 | "8,20" | 逗号分隔，如 8,20 表示每天早 8 点和晚 20 点各播报一次 |
| **`warn_threshold`** | **低电量预警阈值（度）** | **20** | **剩余电量低于此值时发出预警，默认 20 度** |
| **`warn_drop_per_hour`** | **掉电速率预警阈值（度/小时）** | **5** | **电量下降速率超过此值时预警（可能是大功率电器或异常），默认 5 度/小时** |
| `notify_origin` | 默认房间的定时播报目标会话（一般留空） | "" | 只有「用默认房间兜底」的管理员才需要填：普通用户各自发 /电量推送 就会写入自己的目标会话。这里填 AstrBot 的会话标识（形如 aiocqhttp:GroupMessage:123456，可从日志里 /电量推送 那一行抄），留空则默认房间不参与定时播报 |
| `encrypt_key` | 敏感信息加密密钥 | "" | 留空即可：默认读环境变量 DORM_POWER_KEY 或仓库根的 .dorm_power_key 文件 |
| ... | （搜校相关的其他配置项） | ... | ... |

### 插件代码改动

- `main.py` 的 `_threshold()` 方法：优先读面板 `warn_threshold`，回退到旧的 `threshold` 或 `config.yaml` 的 `warn.threshold`（向后兼容）
- `scheduler_job.py` 的 `report_room()`：优先读面板 `warn_threshold` / `warn_drop_per_hour`，回退到 `config.yaml` 的 `warn` 块

### 测试 & 体检

- 三套离线测试（解析 / 安全 / 冒烟）全过
- 工作区体检 9 项全绿：语法、配置解密、包与源码一致、版本号一致、无隐私残留

### GitHub 推送状态

克隆里已提交 `436ef01`（面板配置优化：暴露预警阈值配置项并精简提示文案），但推送时代理断了。
**等代理恢复后，在克隆目录里跑一条命令即可推送：**

```bash
cd "D:\shchool water and power\astrbot_plugin_dorm_power\astrbot_plugin_dorm_power"
https_proxy=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 git push origin main
```

或者你直接告诉我「代理好了」，我立刻推。

---

## 安装包

**[astrbot_plugin_dorm_power.zip](D:/shchool%20water%20and%20power/astrbot_plugin_dorm_power.zip)** — 29.8 KB，8 个文件，sha256 `7eb07f99…`

打包时间 2026-09-18T00:43:30，版本 v2.2.0，包内有 BUILD.txt 校验单（每个文件的 sha256）。

**装之前先在面板卸载旧插件**（重复上传会报「目录已存在」）。
