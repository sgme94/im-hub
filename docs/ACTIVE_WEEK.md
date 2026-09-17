# 活跃群聊与单聊：发现、固定窗口回补、覆盖验收

版本：1.1.0a1。新增命令 `discover-week`、`backfill-week`、`week-status`；查询增加 `--conversation-type group|direct`。本版是账号范围采集预览，不代表四个平台的全部会话已经覆盖。旧 RC4 的四个平台各一个样本群与本页的账号级发现是不同范围。

本机最终回补在 2026-09-16T23:05:13Z 已完成；恢复连接后重新只读核验通过。固定窗口为 `[2026-09-09T22:39:50Z, 2026-09-16T22:39:50Z)`。微信 54 个群聊、35 个单聊，共 2,502 条；KIM/OA 5 个群聊、8 个单聊，共 92 条。102 个会话、2,594 条记录全部通过物理回读，历史重放新增 0，完整导出哈希一致。TIM/企微账号目录未扫描，不能用 0 表示其真实活跃会话数量。

最终数据已解码 1,178 条微信独立 Zstandard 原生正文，微信记录中空正文为 0；其中 1,006 条仍为非文本/富媒体内容缺口，不代表附件实体已取得。KIM 空正文 32 条、正文或元素缺口 34 条另列。最终清单中微信还单列 143 个业务类别、45 个未知类别及 187 个目录有活动但主分片缺消息的候选；这些与首轮中间清单不同，不合并两次快照的统计。

源码安装使用 `python -m pip install -e ".[activity]"`。已有本机环境已安装必要依赖；不要拿旧的独立 RC4 EXE 调用新增命令。

## 已实现的范围

微信：使用同一已授权账号的原生 session/contact 数据库，以及明确列出的主消息分片；使用已有密钥验证加密数据页和已提交 WAL。对所有实际消息表扫描固定窗口，而不是从旧的四个样本群反推会话目录。通过原生会话 ID 和表名映射识别群聊、联系人、业务账号和未知类别。会话是否隐藏、是否免打扰、是否已读，都不用于排除。`is_hidden` 仅作为原始标记保留，不等同于已经独立证明界面的“折叠”含义。

KIM/OA：联合读取当前账号的 session、group、user 与 message 表，按固定窗口识别活动。群与单聊分别校验。单聊的 `typeID` 可能是对方，也可能是本账号；必须结合 `creater`、明确的 `self_user_id` 以及收发双方核验，不直接把 `typeID` 当联系人。单聊的原生会话身份使用 `dm-session:<sessionID>`，避免不同会话或相同显示名被错误合并。

TIM/QQ 与企微：本版账号发现配置用 `not-scanned` 明确记录。旧的指定群导出/复制驱动仍存在，但它们不是全账号目录发现，不会被当作这两个渠道已经扫描完成。后续需要会话清单（包括折叠入口）发现、逐会话绑定、固定时间边界回补与新的桌面授权。本次不触发 GUI，也不续期已结束的专用桌面窗口。

公众号、系统/业务通知单列，不混入群聊和直接联系人。未知类别、目录显示活跃但主消息表中没有对应正文的会话，保留为缺口；不猜测、不从分母中隐去，不按零条宣称完成。

## 配置

使用 `examples/active-accounts.json` 的结构，只填已经授权的当前账号。此配置不是原先逐群的 `sources.local.json`。每个账号包含平台、命名空间、来源代际、原生文件清单和明确的群聊/单聊范围。密钥参数只引用既有文件与条目名，配置和输出都不复制密钥值。

模板内 `enabled=false`，操作者完成本机范围核对后显式改为 true。所有真实配置、会话名、联系人、派生消息包及报告只存入受限的本地数据 home，不提交版本库。

```powershell
$im = 'C:\path\to\im-hub\.venv\Scripts\im-hub.exe'
$state = 'C:\Private\im-hub-active'
$config = 'C:\Private\accounts.local.json'

# 新的独立数据目录。旧样本验收库不用改写。
& $im --home $state init

# 不传 since/until 时，以本次调用时刻冻结向前七天的窗口。
$discovery = (& $im --home $state discover-week --config $config --days 7 |
    ConvertFrom-Json)
if (-not $discovery.ok) { throw $discovery.error.code }
$inventory = $discovery.data.inventory_id

# 一次最多处理 200 个待回补会话；重复调用从未完成项继续。
& $im --home $state backfill-week --inventory $inventory --max-conversations 200

# 逐会话查看状态并实际回读已提交数据。
& $im --home $state week-status --inventory $inventory --verify --details --limit 100

# 新的类型筛选，不会读取客户端或触发采集。
& $im --home $state query '进度' --conversation-type direct --limit 50
& $im --home $state query --platform wechat --conversation-type group --limit 50
```

首次导入仍需要固定的本机 ChatLab 0.37.1 后端与 Node，可用既有 RC4 安装中的 `backend/node_modules/chatlab-cli` 和 `backend/node.exe`，通过 `IM_HUB_CHATLAB_DIR` 与 `IM_HUB_NODE` 指定。1.1.0a1 的新命令通过源码安装的 console entry 运行；旧的 RC4 独立 EXE 不包含它们。

也可显式提供两个带时区的时间，例如 `--since 2026-09-10T06:00:00+08:00 --until 2026-09-17T06:00:00+08:00`。区间为 `[since,until)`；原清单与每个消息包都保存同一个窗口，耗时不会把窗口越推越晚。

## 回补的恢复与证据

目录结构：

```text
<home>/activity/<inventory-id>/
  inventory.json          不可变的会话清单、账号范围、固定窗口与本地索引证据
  inventory.sha256        清单内容哈希
  packets/<key>.json      已完成本地读取的不可变群聊/单聊消息包
  packets/<key>.sha256
  results/<key>.json      该会话的导入/失败/回读结果
  coverage.md             逐平台、逐会话的覆盖报告，包含真实会话名，保持私有
  last-backfill.json
```

发现阶段只读取目录和消息 ID、时间、类型索引，不读取或保存正文。回补阶段才读取清单中选定的群聊和单聊正文。每次账号读取只做一次派生快照，不为每个会话反复解密同一源库。所有源文件只读；派生明文临时库在受限 runtime 下创建并清理。

回补先核对来源身份与会话索引，再生成不可变消息包；调用既有 ChatLab 导入并逐条回读成功后，才把会话标为 committed。索引变化时返回 `DISCOVERED_MESSAGE_INDEX_CHANGED_REDISCOVER`，不沿旧清单静默遗漏新增或删除。应重新发现生成新清单；旧清单保留实际未完成原因。

导入失败后优先复用已保存的消息包，不再次操作客户端。重新调用 `backfill-week` 会跳过已完成项；`--replay` 则对包括已完成项在内的数据重复入库回读，用于验证新增为 0。窗口和原观察时间不会因重放变新。

新的 `activity-json` 包使用单独身份通道。消息键包含平台、账号、会话类型、原生会话、来源代际和消息 ID；相同显示名不合并。群聊进入 ChatLab 的 group 类型，单聊进入 private 类型，不再把所有消息都伪装成群聊。旧 `database-json`、TIM、企微样本库和已有消息键维持不变。

## 如何解释覆盖指标

`confirmed_groups` / `confirmed_direct`：确实在本地消息表中找到窗口内消息的会话数。

`directory_only_candidates`：原生目录最后消息时间落在窗口内，但配置主消息分片里没有可回补记录。可能是业务存储、未同步、未解析等情况，原因未知时不下结论。

`eligible_conversations`：类型和原生身份可确认、存在本地消息、没有超过本次边界的群聊/单聊。

`committed_conversations/messages`：消息已导入且回读通过的数量。`week-status --verify` 会再次核对已保存消息包及物理消息表；只读，不重新拉取来源。

`discover-week` 和 `backfill-week` 会写入本地清单、回补数据或回执，返回 `query_only=false`；只有 `week-status` 返回 `query_only=true`。核验还检查回执的 stream、记录数量、原始清单索引、来源身份和正文缺口数；不一致时返回失败，不按损坏的回执计数宣称完整。

`eligible_local_backfill_complete` 只表示已经发现、可读取的本地范围已补齐，不代表客户端所有会话或服务端全历史完整。未扫描渠道显示 not_scanned，界面汇编不把其数字显示为“零活跃群”。`four_platform_complete` 与 `server_history_complete` 保持 false，直到有真正充分的四渠道全范围验收。

微信本地已观察到的独立 Zstandard 正文帧在有界解压后按 UTF-8 校验，原始压缩字节的哈希保留。超限、截断、需要未知字典或非法帧不静默生成文本；保持错误或缺口。正文为空、其他压缩体未解析、附件/引用未完全解析等另计 `body_gap_records`；消息数量完整不代表所有附件内容或语义完整。账号的隐藏/免打扰标记保留，尚未独立确认其界面含义时不声称“折叠群已全部覆盖”。

可再次运行新的 `discover-week` 和 `backfill-week`，同一身份通道内的旧消息会去重。但本版尚未把新会话发现接入后台持续调度，也未完成 TIM/企微全目录发现；不应将三条新 CLI 命令宣称为四渠道最终成品。

## 验收

`tests/test_activity.py` 使用合成数据库覆盖：群聊/单聊/业务/未知分类、隐藏与免打扰、单聊收发双方核验、固定半开区间、同名会话隔离、ChatLab private 类型、分批恢复、导入失败重放、源索引变化、清单/消息包篡改、只读查询与覆盖校验，以及 Zstandard 帧、未知输出大小、截断/尾随数据及解压上限。

恢复后的完整回归为 294 项通过、0 失败、0 跳过。真实后端合成验证与本机真实账号数据验证分开记录。真实验收只发布聚合数量与代码哈希；原始会话清单、消息包和报告不进入 Git。最终本轮结果见 `ACCEPTANCE_ACTIVE_WEEK.json`；它证明本地已发现范围的回补、去重和核验，不证明四渠道完整覆盖、服务端同步或自然新消息延迟。
