# 持续运行发现的剪贴板失败与修复

## 原始事实

RC3 的首个持续运行段于 2026-09-16T15:46:40Z 启动，在已有多轮完整成功后，一次企微单消息复制的 `OpenClipboard` 返回错误5。原生 `pywintypes.error` 不是 `OSError` 子类，因此旧 CLI 没有将其转成 JSON；父进程看到空 stdout，记录 `SOURCE_CHILD_INVALID_JSON`，暂停企微来源，其他三路继续。操作者随后通过本 home 的 STOP 请求受控停止了整段，于16:06:01Z结束。不能将这一段宣称为零故障持续运行。

错误5不证明是哪个程序占用了剪贴板，也不证明版本不兼容。Windows 官方文档指出另一个窗口已打开剪贴板会导致 `OpenClipboard` 失败；另一个调用者用 NULL 窗口打开时，`GetOpenClipboardWindow` 也可能为 NULL。原始失败没有记录足够信息来认定具体竞争者。

## 修复边界

`clipboard_snapshot.open_snapshot` 在现有原生 API 上进行最多1.5秒的短时等待，每次尝试前仍核对原剪贴板所有者、序列、截止时间与输入/停止保护。来源改变立即拒绝，不接受错误群、别的进程或旧复制。错误不是5时不作为等待条件。持续不可用返回 `CLIPBOARD_OPEN_UNAVAILABLE`，不会更改权限、提升进程权限、清空剪贴板、调用别的读取接口或发送新的按键来绕过失败。

同一来源在复制过程中序列确实发生变化时返回专门的可重试快照错误；整个采集仍使用既有最多三次后续轮次重试上限，不无限重跑。捕获成功后仍需原生载荷、消息数量、群绑定和入库物理回读校验。

CLI 的最外层捕获普通 Exception 并只输出固定错误码和异常类型，不输出异常中可能包含的正文/路径；`KeyboardInterrupt` 保持独立停止语义。未知错误依旧返回非零，不被吞成成功或0条消息。

## 验证方式

合成测试覆盖等待后恢复、超时、源所有者/序列改变、其他原生错误、截止与用户输入中断、异常 JSON 格式。另有实际无害 Windows 竞争测试：本工具创建一个没有可见界面的自有消息窗口，短暂持有正常 OpenClipboard；在父进程确实遇到争用后才释放，验证同一 API 正常恢复。测试只 Open/Close，不读取、清空或写入剪贴板内容，也不访问真实 IM 客户端消息。

修复后的完整回归、独立 EXE 当前客户端复测和后续持续运行结果分别记录。失败段、维护停止和恢复段都应保留；不中途把授权截止向后移动，也不拿重新启动的时间冒充从最初一直未中断。

## 原始 API 依据

- Microsoft OpenClipboard： https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openclipboard
- Microsoft GetOpenClipboardWindow： https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getopenclipboardwindow

本说明不包含真实聊天、截图、账号、群标识或密钥。个人原始失败日志只保留在受限运行目录。
