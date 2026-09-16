# 原生实时读取与版本兼容策略

本次取消采集源软件的版本白名单。版本号只用于诊断；真实进程身份、会话绑定、控件能力、载荷格式、消息身份和数据一致性仍必须验证。不要将“允许新版本尝试”表述成“任意版本已通过实机验收”。`source_epoch` 是数据源代际，不应随客户端版本更新自动改名。

## 已增加：微信当前加密库读取

`wechat-live` 直接读取明确指定的当前微信加密消息分片及 WAL，使用操作者已配置的现有密钥缓存，不初始化旧版会自动扫描内存的读取器，不查找或提取新密钥，不修改任何客户端文件。需要 Python 安装可选 `crypto` 依赖；Windows 便携包已包含它。

读取链路是：检查源文件稳定性 → 校验 WAL 头和连续帧校验和 → 截取最后一次已提交事务 → 验证数据页 HMAC → 写入受限临时派生库 → 只读查询准确会话 → 删除临时库 → 导入 ChatLab → 回读后提交采集游标。每个分片独立取快照，不宣称跨分片全局原子。没有账号重建、断线同步或服务端全历史的隐含保证。

底层格式依据：SQLite 官方 Database File Format 第4节（https://www.sqlite.org/fileformat.html）及 SQLCipher Security Design（https://www.zetetic.net/sqlcipher/design/）。固定页结构参数是实际数据格式验证，不是软件版本门槛。数据格式真的不兼容时才返回具体错误。

### 配置与运行

模板见 `examples/wechat-live.json`。真实配置、账号标识和现有密钥文件仅放在本机私有目录。`key_policy` 必须为 `existing-only`，`key_id` 精确对应现有缓存条目；配置不包含密钥值。没有现有密钥时返回错误，不切换到内存扫描。

```powershell
im-hub --home C:\Private\im-hub init
im-hub config-check --config C:\Private\sources.local.json
im-hub --home C:\Private\im-hub collect --config C:\Private\sources.local.json --source wechat-work
im-hub --home C:\Private\im-hub run --config C:\Private\sources.local.json --cycles 3 --interval 15
im-hub --home C:\Private\im-hub query --platform wechat --limit 50
```

`wechat-live` 的观察时间表示当前本地加密源的读取时间，区别于 `wechat-sqlite` 旧缓存的未知上游刷新时间；两者都不能证明服务端已同步全量。当前微信路径每轮重扫配置窗口，以容纳较早迟到消息；窗口超限时明确停止，不悄悄漏数。正常读取使用内存保存最多256MiB/分片的原始快照，派生明文仅暂存在受限 runtime 下。

## 版本兼容验证

已测试：企微新版本字符串、未知版本、缺少版本信息均不拦截；错误进程仍拒绝；仅修改版本元数据不改变桌面来源指纹；完整旧配置可无损迁移旧指纹后再更新版本。历史旧指纹只有哈希而缺少原始配置时，不猜测其身份绑定，先用原配置完成迁移。

ChatLab/Node 的打包版本属于 im-hub 自身运行依赖，仍由构建清单校验，不是对采集源应用的版本限制。

## 当前真实验证与未完成项

三轮不占桌面的读取已完成：微信当前加密源62条，KIM原生库9条；微信后两轮均新增0，KIM后两轮窗口无新消息，来源库未被写入。共71条通过物理库和来源记录校验，查询保持只读。它们不是验收期间新产生的71条聊天。当前观察窗口没有时间戳晚于开始时刻的自然新消息，不能声称已测得端到端新消息延迟。

TIM/企微仍须在空闲交互桌面完成真实自动导航、导出/复制、后续新消息和重复运行验收。当前企微可访问树只提供窗口和两个无名组，没有语义消息节点；这是真实控件能力问题，不是版本被禁止。桌面正被使用时不抢焦点。

四平台最终通过条件：每个平台都有自动原生获取证据（不能使用旧文件或手动复制替代）、准确会话绑定、可查询的新到消息引用、后续重复运行新增0、失败/焦点中断不推进错误游标。缺少任何一项时最终标志保持 false。详见 `ACCEPTANCE_LIVE.json` 和 `RELEASE_GATES.json`。
