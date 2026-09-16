# im-hub 0.4.0rc1 交付说明

## 交付结论

这是可安装的 **CLI 交付候选版**，不是空脚手架。文件/数据库接入、TIM 严格追加导入、企微原生载荷处理、跨来源查询、批量运行、完整导出、备份恢复均有代码和可重复测试。采集、导入、查询、规则初筛不调用 LLM；没有网页或桌面前端。

**当前不能认定“四路全自动无人值守成品”已经验收完成。** 本机自动桌面探测被工具安全检查阻断，受限 TIM 原生控件查询又返回无效控件标识，因此 TIM/企微新自动导航驱动只完成代码与合成编排验证，没有实机端到端验收。微信仍读取已有明文缓存，没有实现加密客户端实时刷新；连续 24–72 小时运行也没有完成。发布为 rc，保留这些阻塞，不能以打包/测试数量取代真实验收。

| 能力 | 交付接口 | 验证口径 |
|---|---|---|
| KIM 本地数据库 | `collect` / `kim-sqlite` | 前序真实 9 条及当前数据库回归；只读本地快照，不等于服务端完整 |
| 微信明文缓存 | `collect` / `wechat-sqlite` | 前序真实 52 条及分片/恢复测试；上游时效 unknown |
| TIM 完整 TXT | `collect` / `tim-export` | 文件解析 + 原生 ChatLab 导入 + 严格前缀去重；发起客户端导出仍可人工完成 |
| 企微原生载荷 | `ingest`；`wecom-clipboard` | 原生载荷解析前序已实测；新统一剪贴板入口经过合成编排测试，需正常复制和来源校验 |
| TIM/企微自动 UI | `tim-ui` / `wecom-ui` + `--allow-ui` | 可选驱动代码已实现；需要本机版本与控件配置校准，实机验收未通过，不默认启用 |
| 查询和证据 | `query` / `evidence` / `export-all` | 只查已入库消息，不调用客户端/模型/网络 |
| 一次批量采集 | `run` | 串行执行配置源，失败隔离，混合结果退出码 4；不安装周期任务 |
| 完整导出 | `export-all` | 持续分页导出全部匹配消息；source history 仍可 partial |
| 备份与恢复 | `backup` / `restore` | 数据库一致性复制、条目哈希、路径校验、只恢复到新目录 |
| 分析沉淀 | `analyze` / `validate-candidates` | 规则候选与来源引用检查；不是 LLM 语义分析，不写 MyMind 正式状态 |

## 安装与启动

解压交付 ZIP。需要已有 Python 3.11+；Windows 默认不安装前端、服务或计划任务，也不改全局 PATH。使用新版本目录，避免覆盖已有环境。

```powershell
.\install.ps1 -PythonExe 'C:\path\to\python.exe' `
  -InstallDir 'C:\Tools\im-hub\0.4.0rc1' `
  -ChatLabDirectory 'C:\path\to\node_modules\chatlab-cli'

& 'C:\Tools\im-hub\0.4.0rc1\im-hub.exe' --version
& 'C:\Tools\im-hub\0.4.0rc1\im-hub.exe' doctor
```

Python 核心 wheel 可离线安装。提供 `-Desktop` 才安装 pywin32/UIAutomation 可选依赖；提供 `-InstallBackend` 才通过已安装的 Node/npm 下载固定 ChatLab 0.37.1。`-ChatLabDirectory` 与 `-InstallBackend` 二选一。安装完成后使用根目录或 runtime 内的 `im-hub.exe`，两者都读取独立 runtime 的本地后端配置；显式 `IM_HUB_CHATLAB_DIR` 环境变量优先。不经 cmd.exe 转发用户检索字符串。

没有后端仍可查询/导出/备份已有库，但不能导入新消息。安装器不处理 IM 登录，也不会自动启用示例来源。脚本未做 Authenticode 签名，组织限制脚本执行时应按组织的正常授权流程处理，不关闭安全防护。

## 一次设置，反复 CLI 调用

```powershell
$im = 'C:\Tools\im-hub\0.4.0rc1\im-hub.exe'
$state = 'C:\IMHubData'
$config = 'C:\IMHubConfig\sources.local.json'

& $im --home $state init
& $im config-check --config $config
& $im --home $state run --config $config --dry-run
& $im --home $state run --config $config
& $im --home $state query '网关' --limit 100 --full
& $im --home $state coverage --max-age-seconds 3600
& $im --home $state collection-status
& $im --home $state desktop-status
```

`run` 是一次有界调用，不是在后台常驻。多个 Agent 可以独立查询同一私有库；写入通过锁串行化。不能把平台过滤当成权限系统，不要向不受信任的公网调用者暴露全参数 CLI。

`--dry-run` 的数据库路径会执行只读取数；桌面路径只检查配置而不点击；返回内容明确区分。没有 `--allow-ui` 的批量运行会将桌面源列为 needs_input，退出码 4，不悄悄抢界面。手动剪贴板源不能靠批量运行凭空取得复制基线。

## TIM：可靠的有限跨导出重复处理

普通 `tim-txt` 导入仍保持旧的快照身份策略。本版另提供 `tim-export` 源：要求每次都是同一个账号/群、同一起点的完整历史 TXT，配置 `export_mode=full-history-append-only`。记录签名包括时间、发送者、完整正文和缺口，不只正文 hash；新快照必须逐条精确匹配旧快照的完整前缀，才接受尾部新增。

同秒同人同文的两次真实发言保留两个序号。撤回导致旧行消失、重新导出改变旧正文、补同步在前面插入旧消息、只导出一个滑动时间窗，都会停止并要求审阅，不强行模糊合并。该身份是“当前完整导出代际内的稳定序号”，不是 QQ 官方消息 ID。

完成 TXT 后，由导出步骤创建来源 manifest：

```json
{"source_id":"tim-work","sha256":"完整文件的SHA256","observed_at":"2026-01-02T12:00:00+08:00"}
```

`observed_at` 是导出者实际观察时间，不用文件 mtime 或本次导入时间冒充。对旧验收文件要保留旧观察时间。

```powershell
& $im --home $state collect --config $config --source tim-work
```

源码入口 `desktop_sources.reconcile_tim`；导入成功回读之前不更新已接受序列。进程在入库后中断，下一次恢复原不可变批次，不先重新操作客户端。

## 企业微信：正常复制的可调用入口

安装 Desktop 可选依赖后：

```powershell
$baseline = (& $im clipboard-status | ConvertFrom-Json).data.clipboard_sequence
# 在正确群中正常选中消息并复制；不是只复制正文。
& $im --home $state collect --config $config --source wecom-copy --after-sequence $baseline
```

读取器只读 `WeWork Message` 注册格式，校验新的剪贴板序号、剪贴板 owner、WXWork.exe 版本、复制前后稳定性及 profile 中显式 field_6 绑定。读取可执行文件身份仅需受限进程查询，不访问进程内存、密钥或登录令牌。原始 ID/发送者语义仍是 provisional，不能据此宣称官方接口稳定。

一次命令不监听全局所有剪贴板内容；查询也不会访问剪贴板。窗口操作和剪贴板会影响桌面，不能称为纯静默采集。

## 可选自动桌面驱动

`windows_desktop.py` 使用精确语义控件选择器，不提供任意键盘脚本、任意点击坐标或聊天里的命令执行。配置要求已审阅的版本、唯一窗口、准确会话标题和操作控件；默认不启用。TIM 限定导出相关菜单、正确默认文件名、TXT 类型和保存按钮；企微限定正常消息选择/复制，禁止对输入框执行发送操作。

运行前显式 `--allow-ui`，使用单一桌面互斥锁、用户输入/焦点检查和时间/页数上限。用户输入检测属于协作式暂停，不能证明桌面完全独占，不能承诺检测到与程序同时发生的每一次输入。为了避免打断当前用户操作，不在失败后强制夺回焦点。退出时可能保留客户端对话框或选择状态，需在本机验收中核对。

**当前没有可直接套用到任意机器的通用 UI profile。** 未校准、控件不唯一、版本变化、锁屏或焦点丢失会失败，不降级为盲目坐标点击。`profile_reviewed=true` 只是本地操作员声明，不是自动验收证明。不能把仓库中的合成 profile 当作真实工作群配置。

## 完整导出、备份与恢复

```powershell
& $im --home $state export-all '迁移' --platform kim --max-records 100000
& $im --home $state verify
& $im --home $state backup --output 'C:\IMHubBackups\backup-001.zip'
& $im restore --input 'C:\IMHubBackups\backup-001.zip' --destination 'C:\IMHubRestored'
& $im --home 'C:\IMHubRestored' verify
```

`export-all` 会跨页取齐当前库匹配的消息，输出 JSONL、hash、数量与覆盖；不能将 query_complete=true 解释成源客户端历史完整。超过限制失败并清理未完成导出，不留一个假装完成的包。

备份含消息正文与原生载荷，是敏感数据，**不加密**。Windows 创建文件时先限制当前用户和 SYSTEM 权限再写内容；在有保护的本地目录保存，不自动上传。备份排除本机配置、凭据文件、运行日志、导出和分析候选。数据库通过只读备份 API 复制；原库不 checkpoint、不改 journal 设置。

恢复验证所有成员路径、大小、哈希、数据库结构及消息来源一致性，只写新目录，不覆盖已有库。恢复库可立即查询；源配置不恢复，跨机器继续采集仍需重新核对账号、路径和源代际。保留旧目录直到新库核验完成。

## 错误、更新与回退

所有普通命令 stdout 为 UTF-8 JSON。退出码 0=本次命令成功（仍可能 partial）；2=参数/身份/来源错误；3=写锁/查询版本变化需重试；4=批量运行部分源失败或等待输入；130=中断需对账。

新版本安装到独立目录；不自动迁移旧事实。旧版 `im-unified` 数据通过 `--home` 只读访问；本版不写旧目录。失败批次保留原始依据再恢复，不删除重跑来掩盖问题。升级前先备份并验证恢复到新目录。

## 发布边界和完成门槛

本次为了保护原开发目录中的并发写入，在从 v0.3.0 建立的隔离工作树/交付分支完成集成。没有重置、覆盖或强推原 main 的并行改动。候选包不能自动成为主线已经完成所有功能的证明。

正式完整生产验收仍要求：TIM 和企微在准确测试工作群中的真实新导出/新复制、跨会话与分页完整性、用户打断和锁屏恢复；微信安全上游刷新或明确支持的官方新导出入口；实际长时间运行、版本变更重新绑定、附件与撤回语义的明确支持范围。未验收部分继续显示为未完成。

技术参考：Microsoft 的 Clipboard/Sequence Number/GetLastInputInfo 官方文档；SQLite Online Backup API；Python-UIAutomation-for-Windows 项目文档。底层 API 存在并不证明特定腾讯客户端控件或未公开载荷永久兼容。
