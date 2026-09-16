# v0.3.0：显式数据库采集

## 已实现与未实现

`collect` 现在支持三个独立 transport：`file`、`kim-sqlite`、`wechat-sqlite`。新增数据库驱动没有使用 LLM、OCR、GUI、客户端自动登录或网络服务。查询仍只访问已入库消息，绝不触发采集。

| transport | 实际读取对象 | 是否刷新客户端 | 来源时效 |
|---|---|---|---|
| `kim-sqlite` | 当前明确绑定账号目录中的 KIM `user.db`，只取一个配置群 | 否；直接读取原生本地 SQLite | 观察时间是该本地读事务开始时刻，不是服务端同步证明 |
| `wechat-sqlite` | 外部已取得、明确列出的微信明文 SQLite 分片 | 否；不初始化旧读取器，不访问密钥、不解密 | 缓存读取时间单列；上游观察时间未知，freshness 始终 unknown |
| `file` | 已完成的导出文件及原始观察 manifest | 否 | 保持原有文件观察口径，重放不刷新 |

本页说明 `kim-sqlite` 和旧 `wechat-sqlite`。**1.0.0rc2 新增 `wechat-live` 当前加密库/WAL读取，参见 [实时读取说明](LIVE_COLLECTION.md)。** 新实现仅使用显式现有密钥配置，不调用会提取密钥的旧读取器构造函数。TIM/企微的真实自动桌面验收仍未完成。

## 配置与调用

模板为 `examples/database-sources.json`，里面只有合成示例。真实配置必须保存在受限本地目录，不提交到 GitHub。

一个 source 只绑定一个平台、一个本地账号目录、一个会话和一个来源代际。账号目录绑定不等于证明当前登录账号；群显示名必须核对，KIM 同时核对原生群 ID 与 sessionID。微信表名由群 ID 计算，分片列表必须显式提供，不遍历客户端所有库。

```powershell
# 已按 README 安装 im-hub，并配置固定 ChatLab 后端后
im-hub --home .local/database-state init

# 先审查实际将读取的范围：读取数据库，但不写入任何 im-hub 批次/游标
im-hub --home .local/database-state collect `
  --config .local/sources.local.json --source kim-work --dry-run

# 一次有界采集，不注册服务或定时任务
im-hub --home .local/database-state collect `
  --config .local/sources.local.json --source kim-work

# 对配置起点之后的整个窗口重新对账，补回重叠窗口之外的迟到消息
im-hub --home .local/database-state collect `
  --config .local/sources.local.json --source kim-work --reconcile

# 查采集健康状态，与来源消息查询分开
im-hub --home .local/database-state collection-status --source kim-work
im-hub --home .local/database-state coverage --max-age-seconds 3600
im-hub --home .local/database-state query '网络' --platform kim
```

`--until` 可固定一次历史验证的右边界，必须带时区且不能在未来；省略时使用当前 UTC 秒作为排他右边界。下一次不能把已经提交的扫描时间向后退；历史补查使用 `--reconcile`，而不是回退游标。

初始化要求全新的专用目录；实际安装已经初始化过时不需要重建。新版本仍拒绝写入旧版 `im-unified` 目录，只保留旧库只读查询兼容。

### 主要配置字段

| 字段 | 约束 |
|---|---|
| `enabled` | 必须显式为 true |
| `adapter` | 两种数据库 transport 都使用 `database-json` |
| `account_namespace / account_directory` | 本地账号标签与明确的账号目录；不会扫描其他账号 |
| `conversation_id / conversation_name` | 明确且经核对的原生群标识与显示名 |
| `source_epoch` | 此次已核对的数据库/账号代际 |
| `data_class` | real 或 synthetic；操作者声明，不是自动真实性检测 |
| `database / expected_session_id` | 仅 KIM；文件名须为 `user.db`，父目录必须等于绑定账号目录 |
| `shards` | 仅微信；1–16 个 `{id,path}`，ID 不是文件路径且不得重复，文件路径必须在绑定账号目录下 |
| `initial_since` | 必须带时区的初始回补起点 |
| `overlap_seconds` | 1–604800，默认 3600；KIM 重叠回读窗口 |
| `max_records` | 1–20000，默认 10000；超限失败，不截断后推进游标 |
| `timeout_seconds` | 1–30，默认 8；每个库 SQL 执行期限，另有 SQLite 锁等待上限 |

数据库主文件身份（路径、文件系统标识）与已读取表 schema 的摘要固定后，文件被替换或 schema 变化即拒绝继续，不会悄悄继承游标。配置变化也要求复核：修复原配置继续，或在确认账号、代际和范围后使用新的 source 名称；不要用改名称绕过损坏或身份冲突。

## 快照与正文边界

来源连接使用 SQLite `mode=ro`、`query_only`、显式读事务，并设置查询执行期限。不修改源库索引、journal 模式，也不主动 checkpoint。测试覆盖“仅存在于 WAL 的已提交消息可读”和“未提交消息不可见”。各分片独立快照，**不宣称跨库原子一致**。SQLite 自身可能维护共享内存协调信息，不能把只读 SQL 宣称成客户端运行期间所有文件字节永不变化。

KIM 文本元素与已观察的 @成员元素可解析；引用消息把当前回复放入 `text`，旧引用放入 `extras.database.quoted_text`，不把旧配置当新消息再次搜索。文件、系统或未知元素无法完整解释时保留记录、原始类型、正文摘要和缺口。

微信只对可严格解码的正文取文本；二进制容器、压缩内容、图片与复杂卡片保留缺口和原始摘要。本版没有安装解压/附件分析扩展，也没有通过“可打印字节”猜正文。未知发送者保持未核实，原分片 ID 参与身份，不将不同分片相同 local_id 合并。

`database-json` 有独立身份通道，避免让新解析口径覆盖旧 `normalized-v2` 的消息。迁移旧库前应专门对账，不应把同一来源同时经两种通道大量导入一个正式数据集后假定自动去重。

## 游标、迟到消息与恢复

KIM 普通扫描范围：`[max(initial_since, last_scan_until - overlap_seconds), until)`。同秒不同消息依赖原生 ID 保留；在重叠范围内迟到的记录可被后续扫描取得。**更早的迟到记录不保证被普通增量发现**，应按来源同步情况执行显式 `--reconcile`；本版不自动新建调度任务。

微信缓存每次都扫描 `initial_since` 之后的配置窗口，不能凭“上次已经看过缓存”推断上游完整同步。缓存全窗口变大时可能达到记录/时间上限，需要拆分已授权采集范围或后续实现分页回补，不能静默截断。

```text
取得 .collection.lock
  → 校验固定来源配置
  → 若有 pending：恢复原不可变批次，不先重新读源
  → 否则读取指定本地数据库窗口并关闭源连接
  → 写入 acquisitions/<run>/source.json
  → 登记 staged
  → 既有 ChatLab 导入、逐条回读、消息来源映射提交
  → 提交 collection.sqlite3 中的 scan_until 与成功记录
```

采集进程不在等待 ChatLab 时长期持有源库读事务。导入失败留下待恢复批次；下次同一 source 先重放它。即使 ChatLab/消息来源映射已提交，而采集游标还未写入，重试也用原批次对账，避免重复新增。恢复返回 `resumed_pending=true` 与 `database_read_performed=false`，不会谎称重新访问客户端；要取得恢复批次之后的变化，再调用一次 collect。

`--dry-run` 不推进游标、不创建采集数据库或批次。已有 pending 时 dry-run 明确拒绝新读，要求先恢复；遇到新配置、身份不符、数据库不可读、格式不支持、超限时均返回失败，不写成“窗口零消息”。完整扫描已绑定群且确实零条则可以成功提交该本地窗口。

## 状态语义

`collection-status` 提供 `last_success_at`、`local_scan_until`、`last_error`、各阶段数量和 `pending_runs`。这里的 scan_until **只是成功检查过的本地时间窗口边界**，不是聊天服务端或客户端同步完整水位；`client_complete_through` 固定为 null。

`coverage` 与 `query` 中仍为 `history_completeness=partial`、`complete_through=null`。读取失败时应同时查看 `collection-status`，不能只凭旧数据的 freshness 忽略最新采集错误。

微信缓存消息的 `source_observed_at=null`，另有 `cache_observed_at` 和 `source_time_basis=cache_read_not_client_sync`。KIM 的 source_observed_at 指本地数据库读事务观察时间，仍带 `client_sync_verified=false`。这些时效均不能作为业务事项完成状态。

## 验证

```powershell
python -m unittest discover -s tests -v
# 后者使用真实 ChatLab 后端，但输入数据库和聊天全部为临时合成数据
python scripts/check_database_smoke.py
```

第二个测试独立运行命令行进程，验证 KIM 首次/重放/新增同秒消息/再重放，以及微信两个分片的去重与时效。退出后清理临时合成数据库，不访问真实客户端。

实机验收与合成测试分开计数。脱敏结果见 `ACCEPTANCE_DATABASE.json`；个人路径、原始群标识、原文和详细查询输出只留本机受限 `.local` 数据目录。连续无人值守、客户端重启与升级、附件完整解析、自动桌面采集仍有待验收。
