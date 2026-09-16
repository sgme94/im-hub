# im-hub

**纯 CLI 的本地 IM 采集、查询与证据中心。无前端，核心链路不需要 LLM。**

面向微信、KIM/OA、QQ/TIM 与企业微信：复用已授权的数据来源，将消息规范化后导入私有 ChatLab，供脚本、Agent 和分析流程查询。`im-hub` 不拥有业务项目状态，不自动写入 MyMind 事实账本。

## 当前状态

`1.0.0rc2` 新增微信当前加密库/WAL自动只读采集，取消所有采集源软件版本白名单。已有的产品能力：Windows x64 便携包、配置校验、多来源有界运行、KIM 增量／微信明文缓存、TIM 完整导出的精确前缀去重、企微显式原生复制、跨平台查询、完整证据导出／Markdown 汇编、校验、备份恢复、内置四平台合成自检。详细操作见 [产品手册](docs/PRODUCT_GUIDE.md)。

**微信/KIM已完成当前本地源三轮自动读取；四平台最终验收仍未全部通过**：TIM／企微需要真实 UI 校准与新到消息验证，长时间无人值守尚未运行。软件版本仅诊断，不设白名单；只有实际能力/身份/格式验证失败才停止。企微当前缺少语义消息控件，不是版本被禁止。见[实时采集说明](docs/LIVE_COLLECTION.md)和[交付门槛](docs/RELEASE_GATES.json)。

| 来源 | 已验证的上游路线 | 本项目输入适配 | LLM 是否必需 |
|---|---|---|---|
| 微信 | 当前加密库及已提交 WAL，只使用显式现有密钥；兼容旧明文分片 | `wechat-live` / `wechat-sqlite` → `database-json` | 否 |
| KIM / OA | 精确绑定账号目录及群的当前原生 SQLite | `kim-sqlite` → `database-json`，或旧 JSONL | 否 |
| TIM / QQ | 客户端官方 TXT 导出；严格追加序列可跨导出去重 | `tim-sequence-json` / `tim-txt` | 否 |
| 企业微信 | 旧版已验收原生载荷；新复制有进程／原生格式／序列校验；版本只记录 | `wecom-native` / `wecom-json` | 否 |
| QCE 备选 | 已验证合成导出，真实登录未验收 | `qce-json` | 否 |

**无前端不等于上游客户端无 GUI。** TIM 发起导出、企微定位和复制仍需要桌面。新增语义状态机需要准确的客户端控件校准；前序人工实验不能代替当前版本自动导航验收。常规查询永不触碰客户端。

## 安装

Windows 便携包：整体解压发行页 `im-hub-1.0.0rc2-windows-x64.zip`，保留 `_internal` 和 `backend`，直接运行下列命令。此包自带 Python 运行时、Node 与固定 ChatLab 后端；不需要另装前端或运行 npm。

```powershell
.\im-hub.exe --help
.\im-hub.exe doctor
.\im-hub.exe selftest
```

便携 EXE 未签名，使用前核对发行 SHA256，不关闭系统安全保护。以下为源码安装方式：

Python 3.11+。建议独立虚拟环境，不修改其他项目环境：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\im-hub.exe --help
```

激活虚拟环境后可直接调用 `im-hub`，也支持 `python -m im_hub`。Python 核心包无运行时第三方依赖。安装不会注册服务、创建定时任务、启动聊天客户端或安装前端。

仅源码／wheel 安装方式需要另行准备固定的 ChatLab CLI 0.37.1 和 Node。本地已有安装可以复用：

```powershell
$env:IM_HUB_CHATLAB_DIR = 'C:\path\to\node_modules\chatlab-cli'
# 仅 Node 不在 PATH 时设置
# $env:IM_HUB_NODE = 'C:\path\to\node.exe'
```

没有安装时，可在本项目目录显式安装固定后端：

```powershell
npm install --no-save --package-lock=false chatlab-cli@0.37.1
```

此依赖提供 CLI 后端，不是本项目的前端应用。**查询已入库消息不需要 Node、LLM 或客户端在线**。所有依赖/格式支持以 `doctor` 和验收为准，不承诺任意新版本自动兼容。

## 使用

```powershell
im-hub capabilities
im-hub doctor
im-hub --home .local/state init

# sources/status/coverage 列出已导入来源，不实时枚举客户端会话
im-hub --home .local/state sources
im-hub --home .local/state coverage --max-age-seconds 3600
im-hub --home .local/state query '网关' --limit 50
im-hub --home .local/state query '网络' --platform kim --limit 50
im-hub --home .local/state query --platform wecom --limit 50
```

不传 `--home` 时使用 `IM_HUB_HOME`，否则默认用户目录下 `.im-hub`。查询永远不触发新采集，不抢鼠标、键盘、焦点或剪贴板。JSON 写到 stdout，退出码用于自动化判断。

### 配置式采集

导出器先完成本地文件，再写来源 manifest（原始观察时间和文件 SHA-256）。模板在 `examples/sources.json`，只有合成示例，没有真实账号或工作群。

```powershell
python scripts/make_demo.py --output .local/demo
im-hub --home .local/state collect --config .local/demo/sources.json --source demo-tim --dry-run
im-hub --home .local/state collect --config .local/demo/sources.json --source demo-tim
im-hub --home .local/state query --include-synthetic --limit 50
```

`file` transport 只读取明确配置的已完成文件和 manifest。不会新登录或启动导出，不以文件修改时间冒充来源观察时间。合成数据默认排除在普通查询之外。

### 数据库增量采集

`kim-sqlite` 按重叠窗口读取当前本地消息，`wechat-sqlite` 重扫指定缓存窗口。来源连接只读、SQL 有执行期限，固定账号/会话/分片，超限或身份变化则失败。示例与恢复契约见 [数据库采集手册](docs/DATABASE_COLLECTION.md)。

```powershell
im-hub --home .local/database-state collect --config .local/sources.local.json --source kim-work --dry-run
im-hub --home .local/database-state collect --config .local/sources.local.json --source kim-work
im-hub --home .local/database-state collect --config .local/sources.local.json --source kim-work --reconcile
im-hub --home .local/database-state collection-status --source kim-work
```

第一次使用新的 home 要先执行 `init`；真实配置保持本地。`--reconcile` 对配置起点之后的窗口重新对账，补回普通重叠增量可能遗漏的更早迟到消息。失败保留待恢复批次，下次优先完成原批次；只在消息导入并回读通过后推进本地扫描游标。

已有数据也可通过 `ingest` 显式接入：

```powershell
im-hub --home .local/state ingest --adapter tim-txt --platform qq `
  --input .local/demo/tim.txt --account demo-account `
  --conversation demo-group --name '测试群' --source-epoch demo-generation `
  --observed-at '2024-01-02T12:00:00+08:00' --data-class synthetic
```

不同 TIM 导出缺乏已验证的全局消息身份，默认拒绝自动混入第二个快照；显式 `--allow-new-snapshot` 只表示接受可能保留重复，不能称为已解决跨导出去重。

### 分析入口

产品级入口支持完整分页导出、报告、运维与恢复：

```powershell
im-hub config-check --config .local/sources.local.json
im-hub --home .local/state run --config .local/sources.local.json
im-hub --home .local/state export-all '迁移' --max-records 10000
im-hub --home .local/state report '迁移' --max-records 10000
im-hub --home .local/state verify
im-hub --home .local/state health
im-hub --home .local/state backup --output 'D:/Backups/im-hub-snapshot.zip'
im-hub restore --input 'D:/Backups/im-hub-snapshot.zip' --destination 'D:/IMHub-Restored'
```

备份输出与恢复目标必须是新路径；备份是含敏感消息的**未加密**私有 ZIP。配置不随备份恢复。`run --cycles 3 --interval 60` 仅在当前进程运行三轮，不安装定时任务；部分失败返回退出码 4。具体恢复和桌面采集条件见产品手册。

```powershell
im-hub --home .local/state export '迁移' --limit 200 --full
im-hub --home .local/state analyze --limit 200 --full
im-hub --home .local/state validate-candidates --input '<本地候选包路径>'
```

`analyze` 目前是本地关键词初筛，LLM 调用数为零；不是语义总结，不能可靠识别否定、引用或讽刺。模型分析由外部可选消费者完成：显式导出最小证据 → 模型生成候选 → 核验证据 → 既有业务处理器。采集和查询不等待模型。

`validate-candidates` 只校验结构和原消息引用存在，不证明候选结论成立。默认不上传证据，也不直接写 MyMind。

## 关键契约

消息身份按平台、账号、会话、来源代际隔离；同秒同文但不同来源身份不合并。WeCom 原始 ID/发送者语义仍可能为 provisional；原生载荷与解析 JSON 共用同一身份通道。

`fresh/stale` 与 `complete/partial` 分开。重放旧文件不刷新观察时间；微信明文缓存上游时效未知，返回 `freshness=unknown` 与独立 `cache_observed_at`。KIM 的 fresh 仅说明刚读取本地库。样本最大消息时间及本地扫描游标均不等于完整采集水位；当前 `complete_through=null`。失败不伪装成“零消息”，采集错误另见 `collection-status`。

分页游标绑定过滤条件和已提交版本；数据改变时明确要求重开查询，避免静默漏数。结果包含证据 ID、正文解析缺口、来源批次及覆盖状态。详情见 [架构与契约](docs/ARCHITECTURE.md)。

## 开发与验证

```powershell
python -m unittest discover -s tests -v
python scripts/check_database_smoke.py
python scripts/check_legacy.py --home '<已有旧版消息库>'
```

单元测试全部使用合成数据，默认不访问真实客户端。`check_database_smoke` 需要实际 ChatLab 后端，但使用临时合成数据库验证增量、跨分片身份与重放。`check_legacy` 只读已有库、只输出脱敏计数，不复制聊天。新包支持显式查询旧版 `im-unified` 数据目录，但拒绝写入旧目录，避免影响历史证据。当前验收见 [数据库采集验收](docs/ACCEPTANCE_DATABASE.json)，旧 `ACCEPTANCE.json` 保留为 v0.2.0 的历史记录。

[施工计划](docs/ROADMAP.md) 区分已完成和待完成；[安全边界](SECURITY.md) 与 [依赖说明](THIRD_PARTY_NOTICES.md) 必须保留。仓库中不得提交聊天正文、截图、账号/群标识、凭据、SQLite 库或真实采集配置。
