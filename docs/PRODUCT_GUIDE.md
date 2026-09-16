# im-hub 产品使用与交付手册

版本：1.0.0rc2。形态：Windows x64 命令行便携包，同时提供 Python wheel 和源码。

## 1. 本次交付的边界

可运行的产品闭环是：配置 → 本地数据库／已完成导出／原生载荷 → 规范化 → 单写者导入 → 只读查询 → 证据汇编 → 校验 → 备份恢复。核心不调用 LLM，没有 Web、Electron 或桌面前端，没有消息发送入口。

这次是 **RC 候选交付，不是四个平台已全部无人值守的最终验收**。KIM 原生库、微信已有明文缓存、TIM 官方文件与企微旧版原生样本都有真实数据依据；TIM／企微新增语义 UI 驱动仍需要准确控件配置和客户端实机验收。源应用版本现在只作诊断，不再有新版本放行门槛；只有实际进程、会话、控件或载荷验证失败才停止。企微当前缺少可访问消息控件；自动导航仍需空闲桌面实测。

微信已增加 `wechat-live`：使用已配置的现有密钥读取当前加密库和已提交 WAL；读取旧缓存仍不会伪装成读取当前源。见 [实时读取手册](LIVE_COLLECTION.md)。QCE 真实登录、24–72 小时无人值守、所有附件正文、撤回／编辑语义及 MyMind 正式处理器接入不在本次已通过清单。

## 2. 解压即用

将 `im-hub-1.0.0rc2-windows-x64.zip` 整体解压，不要只拿走 EXE。目录中的 `_internal` 和 `backend` 是运行依赖；不需要预先安装 Python、Node、npm 或前端，也不会在运行时下载依赖。

```powershell
Set-Location 'C:\Tools\im-hub'
.\im-hub.exe --version
.\im-hub.exe --help
.\im-hub.exe doctor
.\im-hub.exe selftest
```

`selftest` 在临时私有目录生成四平台合成数据，使用真正的 ChatLab 后端验证增量、重复导入、查询、完整导出、备份与恢复，然后清理自建数据。它不打开真实客户端，也不证明当前客户端已兼容。

便携 EXE 未做代码签名。核对发行页 SHA256；不要为运行它关闭系统安全保护。发行包使用固定 ChatLab 0.37.1 与 Node 22.22.2，附依赖许可证及 ChatLab 对应源码。依赖未被修改为一个新的业务事实库。

## 3. 初始化自己的数据目录

```powershell
$state = Join-Path $env:USERPROFILE '.im-hub'
.\im-hub.exe --home $state init
Copy-Item .\examples\product-sources.json (Join-Path $state 'sources.local.json')
$config = Join-Path $state 'sources.local.json'
```

模板全部默认禁用。只为明确授权的工作群填写真实账号命名空间、会话身份、路径；版本元数据可选，再启用对应项。不自动扫描所有账号、个人群或家庭群。真实配置保存在数据目录，不放进 Git。

`init` 默认拒绝已经存在、没有 im-hub 所有权标记的目录。新目录 ACL 限制为当前 Windows 用户和 SYSTEM。SQLite、原生载荷、候选包、运行日志及备份可能包含敏感聊天，不能公开发布。

## 4. 一次采集与可重复运行

```powershell
.\im-hub.exe config-check --config $config
.\im-hub.exe --home $state run --config $config --dry-run
.\im-hub.exe --home $state run --config $config
.\im-hub.exe --home $state collect --config $config --source kim-work
.\im-hub.exe --home $state collect --config $config --source kim-work --reconcile
.\im-hub.exe --home $state collection-status
.\im-hub.exe --home $state desktop-status
```

`run` 是有界前台执行，默认一轮。`--cycles 3 --interval 60` 表示当前进程执行三轮、轮间等待 60 秒；不会注册服务、创建计划任务或长期后台驻留。多个来源按固定顺序处理，一个来源失败不会掩盖其他来源结果；有失败、需要人工复制或需要 UI 授权时退出码为 4，并保留逐来源状态。不得仅看顶层 JSON 解析成功而忽略 `data.success` 和退出码。

查询命令永不调用 `collect`，不会因为查询结果不足而抢桌面。文件采集 `--dry-run` 可做规范化预检；数据库 `--dry-run` 可只读源库但不写自有状态；桌面 `--dry-run` 仅校验配置，不操作界面或剪贴板。

### KIM

`transport=kim-sqlite` 使用原生 SQLite 只读事务，明确绑定账号目录、群 id 和 sessionID。支持已提交 WAL、本地重叠窗口、失败不前移、待恢复批次重放。`--reconcile` 从配置起点重新检查较早迟到消息。它读取当前本地数据，不触发服务端同步。

### 微信

`transport=wechat-sqlite` 仅读取已存在的明文分片。所有分片必须明确列出并位于绑定账号目录；每个分片失败都显式报错。同一个 local_id 在不同分片不合并。采集会重新检查配置窗口，但上游缓存刷新时间仍为 unknown。工具不读取解密密钥，不初始化可能自动扫描进程内存的旧第三方读取器。

详细数据库字段见 [DATABASE_COLLECTION.md](DATABASE_COLLECTION.md)。

### TIM 官方导出文件

`transport=tim-export`、`adapter=tim-sequence-json`、`export_mode=full-history-append-only` 是新版受控序列入口。先用客户端正常“消息管理器 → 导出消息记录 → TXT”导出同一群完整历史，核对群名、默认文件名与 TXT 的“消息对象”。导出器完成文件后写来源 manifest：

```json
{"source_id":"tim-history","observed_at":"2026-09-15T21:00:00+08:00","sha256":"替换为实际文件SHA256"}
```

同一导出重放不新增；新导出仅在旧记录逐条构成新记录的完整前缀时接受后续追加。相同秒、同发送者、同正文的两条真实消息仍有两个序号。若旧记录被修改、截断、删除或移位，返回 `TIM_EXPORT_NOT_EXACT_APPEND`，不做模糊合并。此身份只在同一个已绑定导出序列内有效，不是 QQ 官方 msgid，也不能用于任意重新导出的窗口。

原 `tim-txt` 单快照入口继续兼容；不要混用两种身份通道并宣称自动跨通道去重。`since` 只过滤导入范围，整个导出仍参与前缀校验。

### 企微受控原生复制

支持 `transport=wecom-clipboard` 的显式新复制入口：

```powershell
.\im-hub.exe --home $state capture --config $config --source wecom-copy --wait-seconds 60
```

启动后在企微正常界面选中一条完整消息并复制。程序只等待本次剪贴板序列变化，检查所属进程、原生格式和会话绑定，版本只记录；不会读剪贴板历史，不以普通纯文本兜底。误复制其他程序内容立即停止。也可先 `clipboard-status` 取得 baseline，再正常复制，然后 `collect ... --after-sequence BASELINE`。

**不再按源软件版本拒绝运行。** 新版本和缺少版本信息均进入实际能力验证；错误进程、错误群绑定或不兼容原生格式仍会拒绝。离线 `wecom-native/wecom-json` 同样按实际载荷验证。

### 可选 UI 驱动

`tim-ui` / `wecom-ui` 代码已实现有界状态机：实际进程身份 → 桌面租约 → 精确会话 → 导出／选中复制 → 文件／载荷校验 → 来源批次。必须提供已校准的语义控件配置并显式 `--allow-ui`。模板不含未经验证的通用坐标。

真实 UI 页面、选择框、版本与分页仍需逐机验证。用户输入检测和前台检查是协作式防误操作，不保证同时使用鼠标键盘绝不发生竞争。未知控件、格式不兼容、焦点丢失、长时间等待、重复页面立即停止。不要运行在锁屏／无交互桌面，也不要把合成驱动测试当作已完成的真实自动翻页验收。

## 5. 查询与完整导出

```powershell
.\im-hub.exe --home $state query '网关' --limit 50
.\im-hub.exe --home $state query '迁移' --platform kim --full
.\im-hub.exe --home $state coverage --max-age-seconds 3600
.\im-hub.exe --home $state export-all '迁移' --max-records 10000
.\im-hub.exe --home $state report '迁移' --max-records 10000
```

普通 query 每页最多 200 条，以 opaque cursor 翻页。`export-all` 与 `report` 自动翻完当前过滤范围，达到上限时报错而不是悄悄只导出第一页；默认最多一万条，可调到两万条。导出目录包含 `messages.jsonl`、`manifest.json`；报告增加 `report.md`，每条都有证据引用。

`query_complete=true` 只表示这次过滤查询已完整导出，不表示源客户端全量完整。`report` 是确定性消息汇编，没有 LLM 推理。分析候选可用 `analyze` 初筛并通过 `validate-candidates` 检查引用，但引用存在不代表结论成立，任何业务状态都需要外部授权流程确认。

## 6. 校验、备份、恢复、升级

```powershell
.\im-hub.exe --home $state verify
.\im-hub.exe --home $state health --config $config --max-age-seconds 3600
.\im-hub.exe --home $state backup --output 'D:\Backups\im-hub-20260916.zip'
.\im-hub.exe restore --input 'D:\Backups\im-hub-20260916.zip' --destination 'D:\IMHub-Restored'
```

backup 的输出父目录必须存在，ZIP 必须是新文件且位于数据 home 外。快照持有写锁，数据库走 SQLite backup API，并逐文件记录大小和 hash。备份**未加密**，虽本地限制 ACL，复制到其他介质后仍需自行保护。

restore 只允许新目标目录，先在私有临时目录核对 ZIP 路径白名单、大小、哈希、大小写别名、Windows 特殊文件名和数据库一致性，再原子改名。路径穿越、重复条目、损坏数据都会停止。不恢复设备相关配置、运行日志、已导出报告；配置应单独受限保存、恢复后重新核验来源位置与实际能力。

升级前备份，新 EXE 解压到新程序目录，继续显式指向旧数据 home。0.2/0.3 已准备但未提交的相同消息批次可按原始输入恢复，生成器版本变化不篡改不可变消息数据。更早 `im-unified` home 继续只读兼容，不在旧试验目录写新记录。

## 7. 自动化返回约定

除 `--help/--version` 外，stdout 是 UTF-8 JSON。退出码：0 成功；2 输入／身份／格式／执行错误；3 可重试锁或查询版本变化；4 多来源轮次不完整；130 用户中断。每次新轮询读取状态，不凭旧成功标志判断今天已成功。

`fresh` 与 `partial` 可以同时出现：前者是来源观察时效，后者是历史覆盖。每条记录有原始事件时间、来源观察时间与证据 ID；复制／导入时间不是事件时间。未解析附件、未证实发送者与未知消息类型均保持缺口。

## 8. 构建与发布

Python 源码安装：`python -m pip install -e .[windows]`。运行测试：`python -m unittest discover -s tests -v`。构建工具固定在 `.[build]`；便携构建使用 `scripts/build_windows.py`，显式传入已经校验的 Node、ChatLab node_modules、对应源码和许可证。无用户聊天的数据包才允许上传发行页。

正式稳定版尚需关闭：企微真实批量 UI、TIM 导出 UI 的端到端校准、四路自然新到消息观察、长时间运行与恢复实机验收。微信加密源当前已完成三轮真实读取；源软件版本不再作为拦截项。当前完整度按 [RELEASE_GATES.json](RELEASE_GATES.json) 逐项判断，不以“已经打包”替代这些验收。
