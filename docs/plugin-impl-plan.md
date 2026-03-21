# EACN3 Plugin 实现方案（修订版）

> 基于 Evo-anything 插件架构，结合讨论中的简化决策。

## 核心原则

1. **宿主驱动，插件无脑** — 所有智能行为（bid 评估、任务规划、执行编排）由宿主 LLM 在 Skill 工作流中完成。插件只提供 MCP 工具（手脚）和状态管理。
2. **砍掉 adapter.ts / registry.ts** — 在 plugin 实现中，client 和 server 合体，中间抽象层没有存在的必要。AgentCard 组装、校验、网络注册直接内联到 MCP tool handler 里。
3. **Skill 是大脑** — 12 个 SKILL.md 不是 API 调用清单，而是完整的认知引导程序，把 agent.md / task.md 里的决策逻辑、权衡框架全部灌进去。

## 文件结构

```
plugin/
├── openclaw.plugin.json          # OpenClaw 插件清单
├── package.json                  # Node 项目 + MCP SDK 依赖
├── .mcp.json                     # MCP server 配置（stdio transport）
├── tsconfig.json                 # TypeScript 编译配置
│
├── index.ts                      # OpenClaw 原生入口（api.registerTool）
├── server.ts                     # MCP server 入口（stdio transport）
│
├── src/
│   ├── models.ts                 # TypeScript 接口：AgentCard, Task, Bid, Result, Event, EacnState
│   ├── state.ts                  # 本地状态持久化（~/.eacn3/state.json）
│   ├── network-client.ts         # HTTP 客户端，封装 29 个网络端接口
│   └── ws-manager.ts             # WebSocket 管理：每个 Agent 一条连接，事件缓冲
│
└── skills/                       # 14 个 Skill（厚认知引导）
    ├── eacn3-join/SKILL.md             # /eacn3-join — 连接网络
    ├── eacn3-leave/SKILL.md            # /eacn3-leave — 断开网络
    ├── eacn3-register/SKILL.md         # /eacn3-register — 注册 Agent
    ├── eacn3-task/SKILL.md             # /eacn3-task — 发布任务
    ├── eacn3-collect/SKILL.md          # /eacn3-collect — 收取结果
    ├── eacn3-budget/SKILL.md           # /eacn3-budget — 预算确认
    ├── eacn3-delegate/SKILL.md         # /eacn3-delegate — 委托任务
    ├── eacn3-bounty/SKILL.md           # /eacn3-bounty — 工作循环
    ├── eacn3-bid/SKILL.md              # /eacn3-bid — 评估竞标
    ├── eacn3-execute/SKILL.md          # /eacn3-execute — 执行任务
    ├── eacn3-clarify/SKILL.md          # /eacn3-clarify — 请求澄清
    ├── eacn3-adjudicate/SKILL.md       # /eacn3-adjudicate — 裁决任务
    ├── eacn3-browse/SKILL.md           # /eacn3-browse — 浏览网络
    └── eacn3-dashboard/SKILL.md        # /eacn3-dashboard — 状态总览
```

## 被砍掉的模块及原因

| 模块 | 原因 |
|------|------|
| `adapter.ts` | GenericAdapter 只是赋值（name→AgentCard.name）。用户/LLM 自己填，MCP tool 里内联组装 AgentCard 即可。未来接 LangChain 等框架时再抽出。 |
| `registry.ts` | 做三件事：校验（几个 if）、持久化（state.addAgent）、调网络（networkClient.register）。三行代码不值得单独一个模块，内联到 eacn3_register_agent handler。 |
| `matcher.ts` | 文档里 matcher 做"本地匹配"。plugin 里就是 eacn3_create_task 时先查 state 里有没有匹配的本地 Agent，几行代码内联。 |
| `logger.ts` | 文档里 logger 做"状态变更记录"。plugin 里就是在 submit_result/reject_task 等 handler 里顺便调 eacn3_report_event，一行代码。 |

## 四个核心模块

### models.ts
所有 TypeScript 接口，对应 network-api.md 的数据结构：
- `ServerCard` — 服务端身份
- `AgentCard` — Agent 身份 + 能力描述
- `TaskContent`, `Task` — 任务完整结构
- `Bid` — 竞标
- `Result` — 执行结果
- `PushEvent` — WebSocket 推送事件
- `EacnState` — 本地持久化状态

### state.ts
本地状态持久化到 `~/.eacn3/state.json`：
- `server_card` — 当前服务端信息（connect 后填充）
- `network_endpoint` — 网络端 URL
- `agents` — 已注册的 Agent（Record<agent_id, AgentCard>）
- `local_tasks` — 本地发起/参与的任务摘要
- `reputation_cache` — 声誉缓存
- `pending_events` — 未处理的事件缓冲

提供：`load()`, `save()`, `getState()`, `setState()`, 以及便捷方法 `addAgent()`, `removeAgent()`, `updateTask()` 等。

### network-client.ts
HTTP 客户端，封装 29 个网络端接口。按 network-api.md 的分组：
- Discovery — Server（4）：registerServer, getServer, heartbeat, unregisterServer
- Discovery — Agent（6）：registerAgent, getAgent, updateAgent, unregisterAgent, discoverAgents, listAgents
- Tasks — 查询（5）：createTask, getOpenTasks, getTask, getTaskStatus, listTasks
- Tasks — 发起者（5）：getTaskResults, selectResult, closeTask, updateDeadline, updateDiscussions, confirmBudget
- Tasks — 执行者（4）：submitBid, submitResult, rejectTask, createSubtask
- Reputation（2）：reportEvent, getReputation

每个方法：构造请求 → fetch → 解析响应 → 返回类型化结果。server_id 自动注入。

### ws-manager.ts
WebSocket 管理器：
- `connect(agentId)` — 建立 WS 连接到 `/ws/{agent_id}`
- `disconnect(agentId)` — 断开连接
- `disconnectAll()` — 断开所有连接
- 事件缓冲：收到推送 → 存入内存 buffer
- `drainEvents()` — 返回并清空 buffer（供 eacn3_get_events 调用）
- 自动 ping 保活
- 连接断开自动重连

## 32 个 MCP 工具

按 plugin-impl-tools.md 完整实现。每个工具是网络端 HTTP 接口的薄封装：

1. 接收宿主传入的参数
2. 从 state 注入 server_id / agent_id（用户不需要手动传）
3. 调用 network-client 对应方法
4. 更新 local state（如果需要）
5. 返回 MCP 标准格式 `{ content: [{ type: "text", text: JSON.stringify(result) }] }`

特殊逻辑（内联，不单独模块）：
- `eacn3_register_agent`：组装 AgentCard（原 adapter）+ 校验（原 registry）+ 调网络注册 + 存 state + 建 WS 连接
- `eacn3_create_task`：先查 state 里本地 Agent 有没有匹配的（原 matcher），没有则走网络
- `eacn3_submit_result` / `eacn3_reject_task`：执行后自动调 reportEvent（原 logger）
- `eacn3_get_events`：drain ws-manager 的 buffer

## 双入口（同 Evo-anything 模式）

### index.ts — OpenClaw 原生
```ts
export default function(api: any) {
  api.registerTool({ name: "eacn3_connect", ... });
  // ... 32 tools
}
```

### server.ts — MCP stdio
```ts
const server = new McpServer({ name: "eacn3", version: "0.3.0" });
server.tool("eacn3_connect", "Connect to EACN3 network", { ... }, async (params) => { ... });
// ... 32 tools
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
```

两个入口共享 `src/` 下的模块，工具实现逻辑相同。

## 14 个 Skill（厚认知引导）

每个 SKILL.md 包含：
1. **决策框架** — 不是"做不做"，而是怎么判断、权衡什么
2. **上下文感知** — 查记忆、查声誉、查历史，用什么信息做决策
3. **协作提示** — 你有哪些协作工具可用，什么场景该用哪个
4. **风险权衡** — 退回 vs 硬做、竞标 vs 放弃、澄清 vs 猜测的 tradeoff
5. **数据模型感知** — Task 完整字段、Bid 状态机、Result 裁决结构

## 实现顺序

1. 骨架文件（package.json, tsconfig, openclaw.plugin.json, .mcp.json）
2. models.ts
3. state.ts
4. network-client.ts
5. ws-manager.ts
6. server.ts + index.ts（32 个 MCP 工具）
7. 14 个 Skills
