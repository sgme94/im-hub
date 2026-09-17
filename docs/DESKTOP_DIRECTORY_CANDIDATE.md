# TIM / 企业微信目录发现与固定窗口复用候选

后续实机核验：当前 TIM / 企微未提供本候选要求的完整原生元数据控件，不能把合成测试当作实际兼容性通过。新增只读探针和明确绑定后的逐会话获取命令见 [TIM/企微实机核验](TIM_WECOM_REAL_PROVIDER.md)；下述 pinned-capture 复用语义保持不变。

状态：未发布的 1.1.0a1 增量候选。Windows UIA 读取代码、合成控件测试与已完成采集批次的固定窗口复用已经接线；**实际 TIM / 企微目录控件尚未校准，不能称为两平台账号级实机验收通过。** 本轮不续期以前的桌面独占授权，也不改变现有真实配置。

## 两条不同的路径

`discover-week --allow-ui` 可在已审核 `desktop-directory` 账号配置下，遍历普通和折叠目录。它只保存原生会话标识、显示名、类型、绝对最后活动时间和页面计数/哈希，不读取消息预览、正文、附件、剪贴板、凭据或进程内存。它不主动激活窗口，不点击会话，也不使用 OCR、模型、键盘或坐标回退。只有原生 ExpandCollapse / Scroll 控件模式允许改变目录展示。

`desktop-backfill-plan` 和 `backfill-desktop-week` 不碰客户端。后者将**同一数据 home 下明确指定、已 committed 的 desktop run** 按清单固定的 `[since, until)` 截取，交给原有 normalize → ingest → physical readback。它不是自动拉取新消息：没有对应捕获批次时会保留 HOLD。它也不会按“最新文件”或同名会话自行选择来源。

现有 `collect` 命令仍负责 TIM 官方导出或企微原生复制。新路径不修改它的源身份、时间线、严格前缀去重策略或采集检查点；数据继续留在同一个 im-hub 私有 store，不建立第二份业务事实账本。

## 实机准入条件

目录操作需要同时具备 `--allow-ui`、尚未过期的 `IM_HUB_DESKTOP_UNTIL`、没有 STOP、已处于前台且无人操作的确切客户端，以及审核后的原生元数据控件配置。缺少任一项均拒绝；没有默认授权期限，不自动延长旧授权。

配置必须能独立读取真实的账号身份文本、原生会话 ID、类型、绝对时间和每个目录分区的总条目数。AutomationId 只用来定位控件，不能当永久会话 ID；RuntimeId 和显示名也不作消息身份。相同显示名可以指向不同会话，必须分开。

当前客户端若不暴露这些 UIA 字段或计数，现有候选就会明确失败；不能把合成控件配置当成兼容所有客户端的驱动。需要另外实证的数据入口时应另行评估，不绕过访问控制或使用名称猜测。软件版本只作诊断，不设置版本白名单。

## 分页和完整性的边界

每个审核分区从顶部开始，小步滚动；相邻页需有原生 ID 重叠，同时得到新条目。到达底部还必须满足独立总计数等于唯一条目数。停滞、跳页、账号变化、条目身份变化、总数变化、超限或用户输入都保留已观察项并标为 partial/failed，而不是当作空目录或完成。

普通和折叠分区都必须出现；折叠目录为空也需原生计数 0 和遍历终止证据。`directory_enumeration_complete` 仅代表这些明确校准的分区已遍历，不代表客户端所有隐藏存储或服务器历史已覆盖。

相对日期（如“昨天”）不能猜测成绝对时间，保留 `activity_time_unresolved`。最后活动时间晚于窗口右边界的会话仍保留为候选，因为不能排除它在窗口内也有消息。旧时间条目保留为 `outside_window_directory`，不是零历史证明。

## CLI

源码环境：

```powershell
$im = '.\.venv\Scripts\im-hub.exe'
$home = '.local\reviewed-account-state'
$accounts = '.local\accounts.local.json'

# 只读取已落盘清单与来源绑定；不会获取/续期桌面授权。
& $im --home $home desktop-backfill-plan --inventory '<inventory-id>' --config '.local\capture-bindings.local.json'

# 仅复用指定已完成批次；无 GUI、无新采集。
& $im --home $home backfill-desktop-week --inventory '<inventory-id>' --config '.local\capture-bindings.local.json' --max-conversations 100
& $im --home $home backfill-desktop-week --inventory '<inventory-id>' --config '.local\capture-bindings.local.json' --replay
& $im --home $home week-status --inventory '<inventory-id>' --verify
```

新目录发现仍使用 `discover-week`，只对明确开启 `desktop-directory` 的账号尝试 GUI。须由操作者提供新的具体授权截止时间并完成控件校准后才能加 `--allow-ui`；普通调用及旧 `not-scanned` 配置不会自动升级为桌面采集。

`backfill-desktop-week` 的 `--offset` 和 `--max-conversations` 对目录候选分页。返回 `next_offset` 时应按该值续页。失败、HOLD 或尚有后续页时退出码为 4；成功回读的当前页返回 0。这不构成所有渠道完整性声明。

## 显式捕获绑定

绑定文件格式为 `{"version":1,"sources":{...}}`；来源名必须等于原 desktop run 的 `source_id`。每项保存 enabled、platform、account_namespace、source_epoch、data_class、conversation_id、conversation_name，以及：

```json
{
  "captured_run_id": "<明确指定的32位十六进制已完成run_id>",
  "directory_binding": {
    "native_conversation_id": "<与目录一致的原生会话标识>",
    "conversation_type": "group"
  }
}
```

必须匹配账号、来源代际、群 ID 和名称、数据类别、原生绑定、run ID；还要核对 desktop SQLite 中 committed 状态、磁盘 manifest、payload 哈希和观察时间。捕获观察时间早于窗口终点时不能当作该窗口回补来源。绑定属于人工审核的显式映射，不证明原客户端暴露了全局稳定 ID。

同一会话多个绑定会返回 ambiguous，不任选一个。**TIM/企微直接联系人仍保留 `requires_typed_direct_desktop_adapter`**，不能沿用旧 group adapter 把单聊伪装成群聊。未知类别不进入正文复用。

## 回执与恢复

回执写在原清单的 `results/<key>.json`，类型为 `desktop-fixed-window/1`；它引用原 capture、固定窗口、实际已提交 batch、行语义摘要和读回数。它是本地采集证据，不是 MyMind WorkEvent。

重放保留原观察时间；已存在消息新增为 0。导入失败保留可恢复 batch，下一次从固定 source.json 重试，不重新驱动客户端。身份冲突不修饰成成功。`week-status` 对 desktop 回执核对清单、来源、批次和物理消息，不能靠一个 `status=committed` 字符串通过。

原有 `committed_messages` / `eligible_local_backfill_complete` 仍用于微信/KIM 本地可回补范围。桌面捕获复用单列 `desktop_captured_messages`、`desktop_captured_conversations`、`desktop_capture_failures`，避免混淆分母。`history_complete=false`、`complete_through=null`、`four_platform_complete=false` 持续保留。

## 验证与后续门禁

2026-09-17 最终源码全量测试 365/365 通过，0 失败、0 跳过；另用真实 ChatLab 0.37.1 后端运行 8 项隔离合成测试，8/8 通过，包括 TIM/企微固定窗口复用、重放、失败恢复、只读状态和篡改拒绝。两组测试期间 Python 源码及测试文件哈希均未变化。真实 UIA 读取代码的控件测试使用假控件，这不等于实际 TIM/企微目录兼容性验证。聚合回执见 `ACCEPTANCE_DESKTOP_DIRECTORY_CANDIDATE.json`。

恢复连接后还修复了复核发现的分页越界读取、清单/配置在多阶段读取中变化、回执数字类型和完整性标记未严格校验等问题。分页计划现在只验证当前页的捕获文件；源清单变化时保留已提交消息批次，但不向改变后的清单签发完成回执。8 项新复核回归全部通过。

原微信/KIM 固定窗口的 102 个会话、2,594 条消息重新物理读回通过；对应私有数据目录 1,406 个文件逐文件 SHA-256 前后一致。该检查没有刷新源观察时间，也没有重新采集客户端。

实机剩余门禁：新桌面授权与客户端控件校准、普通/折叠目录的独立分母核验、尚无捕获批次的新会话采集与绑定、单聊原生适配、固定窗口逐会话回补，再到连续增量/自然消息延迟验证。未过这些门禁，不宣布四平台完整，也不接管 MyMind 正式状态。
