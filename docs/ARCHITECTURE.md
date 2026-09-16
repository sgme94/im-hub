# 架构与接口契约

## 核心原则

采集层、规范化层、查询层全为确定性代码，运行时 LLM 调用为零。`im-hub` 是 CLI 项目，没有 Web/Electron/桌面前端；TIM/企微的上游获取仍可依赖其原生 GUI。

```text
上游取数（明确授权、部分待实现）
  微信 DB / KIM SQLite / TIM 官方导出 / 企微原生复制
       ↓ 不可变文件 + 原始观察 manifest
collect（文件入口 / KIM 原生 SQLite / 微信已有明文分片） / ingest
       ↓ 数据库取数先登记不可变待恢复批次，导入回读后推进本地扫描游标
       ↓ 身份绑定、规范化、原文件哈希
批次 staged → ChatLab validate/import → 精确回读 → provenance revision
       ↓
sources / coverage / query / evidence（只读、无网络、无客户端操作）
       ↓
export / analyze（规则初筛）
       ↓ 可选，尚未内置
LLM 语义分析 → 候选证据检查 → 外部业务处理器
```

## 消息

`platform/account_namespace/conversation_id/source_epoch` 隔离来源；`message_key/evidence_ref` 是本系统键，不冒充官方 msgid。`native_message_id/identity_quality` 描述原生或降级身份。

时间分为原消息 `event_time/event_ms`、来源 `source_observed_at`、批次导入时间。微信缓存另有 `cache_observed_at`，其来源观察时间为 null、freshness 为 unknown；不以读缓存时间冒充客户端刷新。窗口是 `[since, until)`，必须显式时区。ChatLab 时间为秒，源毫秒精度保留在 sidecar。

`sender_verified` 不随显示姓名自动变真。`body_state/content_flags` 保留媒体占位、空正文和未知类型。`reply_to_source_id` 保留原始引用，但当前不承诺完整跨消息引用图。

## 存储与事务

ChatLab 保存消息正文；`index.sqlite3` 保存来源/批次/消息映射，不保存正式业务状态。不可变批次保留重建所需数据。一个 home 只有一个写者。

ChatLab 与 sidecar 是两个数据库，没有跨库原子事务。失败批次不会提交新记录到统一查询；保留 staged/failed，修复后使用同输入重试对账。相同身份不同内容会冲突停止，不擅自处理编辑/撤回。

旧库兼容只读。新数据目录使用 `im-hub.json` 标记，默认拒绝在无所有权标记的已有目录初始化。

## CLI

| 命令 | 副作用 | LLM |
|---|---|---|
| `capabilities`, `doctor` | 只读能力/依赖观察 | 不调用 |
| `sources`, `status`, `coverage`, `query`, `evidence` | 只读消息库 | 不调用 |
| `init` | 创建私有数据目录 | 不调用 |
| `collect` | 显式文件或配置数据库读取与入库，不操作客户端界面 | 不调用 |
| `collection-status` | 只读查询采集成功/失败、待恢复批次和本地扫描游标 | 不调用 |
| `ingest` | 显式载荷规范化与入库 | 不调用 |
| `export`, `analyze` | 生成本地证据/初筛包 | 不调用 |
| `validate-candidates` | 只读核验引用存在 | 不调用 |

普通命令 stdout 为一个 JSON 对象。`--help/--version` 是常规文本。退出码：0 当前操作成功（仍可 partial），2 输入/身份/格式/本地操作错误，3 写锁占用或查询版本变化，130 中断需对账。

无任意 SQL、shell、路径查询代理、自动发送或云上传。输入路径仅用于操作者显式采集/校验，不要把全参数 CLI 当作远程权限网关。

## 桌面采集下一阶段

已落地数据库取数及恢复协议见 [数据库采集手册](DATABASE_COLLECTION.md)。KIM 原生读取已接通；微信明文缓存读取不意味着加密客户端实时刷新。采集状态库与消息导入并非跨库原子事务，断点通过同一不可变批次重放完成对账。

计划将 UI 导航固化为有限状态机：校验进程版本 → 获取桌面租约 → 确认准确目标 → 导出/原生复制 → 核对文件/载荷来源 → 记录覆盖 → 恢复界面。用户输入、焦点变化、身份不符、版本未知即暂停/失败。

TIM 至少核对当前会话、导出默认文件名、TXT 消息对象，不能盲信过期 UIA 坐标。企微需要复制前后序列号、所属进程/版本、显式来源绑定和逐页覆盖。可选 LLM 视觉定位不是默认路线，更不是跳过这些保护的授权。
