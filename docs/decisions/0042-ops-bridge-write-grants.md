# ADR-0042 · 主系统与 Agent 双端确认桥接写操作

- 日期：2026-09-06
- 状态：已采纳
- 依据：只读/无人审批情况下 `awen_ops_call_tool` 仍到达写 handler 的失败回归测试

Agent 不 import awenOps 的路由、凭据或业务存储；仍通过 HTTP/SSE 调用能力目录。
不能把“没有审批通道”理解成允许写入，不能把目录查询失败理解成工具只读。

Bridge v2 的写路径为：读取真实 destructive → 本地检查 plan_mode/execute/通道 → 主系统
`/prepare` 签发绑定工具与参数的 call_id → 需审批时经 RemoteApproval 发送该 ID → 主系统确认
用户选择并授予授权 → `/call` 原子消费。完全放行也不能跳过主系统签发的本轮权限。

通用 CLI 审批机制保留；新增可选审批元数据仅用于桥接。板块“session”选项明确叫
“本轮同一工具都批准”，以主系统签发的范围为准，不复用通用 ops_tool_call 的缓存扩大授权。
成功写入置 executed_writes，收尾需经过既有自查门禁。

老主系统允许只读查询，写操作明确提示一起升级。Agent `/health` 暴露协议号 2。
主系统仓库 `test_bridge_agent_contract.py` 以两仓库真实代码覆盖批准、拒绝、中止、中文参数及审批竞态。
授权在主系统单进程内存中，结束/重启安全拒绝；跨 worker 共享状态是后续独立工程，不伪称支持。
