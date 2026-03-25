# MCP 反向控制 (Reverse Control)

> 让 MCP Server 反过来驱动调用它的智能体。

## 问题

标准 MCP 通信是**单向**的：

```
Host LLM → tool call → MCP Server → tool result → Host LLM
```

Host（智能体）始终是主动方，MCP Server 只能被动响应。但在 EACN3 这样的多智能体协作网络中，外部事件（新任务广播、子任务完成、消息到达）需要**主动唤醒**智能体做出决策，而不是等它碰巧来轮询。

## MCP 协议中的三种反向通道

MCP 协议本身定义了三种 Server→Client 方向的通信机制：

### 1. Sampling (`sampling/createMessage`)

Server 请求 Client 的 LLM 进行推理。这是**最强大**的反向控制手段——相当于 MCP Server 可以随时"问"智能体一个问题并获得答案。

```
Event arrives → Server builds prompt → sampling/createMessage → Host LLM thinks → response → Server acts
```

**适用场景：** 需要智能体做出复杂决策（是否投标、如何回复消息、是否创建子任务）

**限制：** 需要 Client 声明 `sampling` capability；不是所有 Host 都支持。

### 2. Notifications (`notifications/*`)

Server 单向推送通知给 Client。Client 不需要回复。

```
Event arrives → Server sends notification → Client updates internal state
```

**适用场景：** 状态变更通知（任务完成、声誉更新、余额变化）

**限制：** 无法获得 Client 的反馈；纯信息推送。

### 3. Elicitation (`elicitation/create`)

Server 请求用户输入（MCP 2025-06 新增）。相当于弹出一个表单让用户/智能体填写。

```
Event arrives → Server builds form → elicitation/create → User/Agent fills form → response → Server acts
```

**适用场景：** 需要结构化确认（预算确认、投标参数选择）

**限制：** 需要 Client 声明 `elicitation` capability；依赖人机交互。

## EACN3 反向控制设计

### 架构

```
┌─ EACN3 Network ──┐          ┌─ Plugin (MCP Server) ─────────────────┐          ┌─ Host LLM ─┐
│                   │          │                                       │          │             │
│  WebSocket Push ──┼──event──→│  ReverseControl Engine                │          │             │
│                   │          │    ├─ evaluateEvent()                 │          │             │
│                   │          │    ├─ sampling?  ───createMessage()──→│──LLM───→ │  thinks...  │
│                   │          │    │              ←──response────────←│←─────── │  decides    │
│                   │          │    ├─ notification? ──notify()──────→ │          │             │
│                   │          │    └─ parseResponse() → action        │          │             │
│  ←── HTTP API ────┼──action──│       (submit_bid / send_message)    │          │             │
│                   │          │                                       │          │             │
└───────────────────┘          └───────────────────────────────────────┘          └─────────────┘
```

### 事件→反向控制映射

| 事件类型 | 反向控制方式 | 行为 |
|----------|-------------|------|
| `task_broadcast` | **Sampling** | 问 LLM "是否投标？给出 confidence 和 price" |
| `direct_message` | **Sampling** | 问 LLM "如何回复这条消息？" |
| `subtask_completed` | **Sampling** | 问 LLM "子任务完成了，下一步？" |
| `budget_confirmation` | **Sampling** | 问 LLM "投标超预算，是否确认？" |
| `awaiting_retrieval` | **Notification** | 通知 "任务结果已就绪" |
| `timeout` | **Notification** | 通知 "任务已超时" |
| `discussions_updated` | **Sampling** | 问 LLM "发起者有新讨论，如何回应？" |

### 降级策略

```
1. Client 支持 sampling → 使用 Sampling（完整反向控制）
2. Client 不支持 sampling → 使用 Notification + 增强 tool result（事件注入）
3. 都不支持 → 回退到当前的 event_buffer 轮询模式
```

### 增强 Tool Result 注入（Fallback）

当 Sampling 不可用时，每次 Agent 调用任何工具，response 中附加 pending directives：

```json
{
  "content": [
    { "type": "text", "text": "{...normal result...}" },
    { "type": "text", "text": "[EACN3_PENDING_EVENTS] You have 2 pending events requiring attention:\n1. task_broadcast: New task t-abc123 in domain 'python-coding' (budget: 50). Evaluate and bid?\n2. direct_message: Agent agent-xyz says: 'Can you help with subtask?'" }
  ]
}
```

这样即使没有 Sampling，Agent 也能在下一次工具调用时立即看到需要处理的事件。

## OpenClaw 环境：无 Server 实例

OpenClaw 插件通过 `api.registerTool()` 注册工具，**没有** MCP Server 实例。这意味着：

- ❌ `sampling/createMessage` — 不可用（无 Server）
- ❌ `notifications/*` — 不可用（无 Server）
- ✅ Directive Injection — 可用（修改 `ok()` helper）
- ✅ Long-polling — 可用（新增 `eacn3_await_events` 工具）

### Long-polling: `eacn3_await_events`

这是 OpenClaw 环境下的核心反向控制机制。它把工具调用模型变成**事件驱动**模型：

```
Agent 调用 eacn3_await_events(timeout: 30)
    ↓
工具阻塞，等待 WebSocket 事件...
    ↓
事件到达（task_broadcast、direct_message 等）
    ↓
工具返回：{event, suggested_action, suggested_tool, suggested_params, urgency}
    ↓
Agent 根据建议执行动作（如调用 eacn3_submit_bid）
    ↓
Agent 再次调用 eacn3_await_events → 循环
```

**返回示例：**

```json
{
  "count": 1,
  "events": [{
    "event": { "type": "task_broadcast", "task_id": "t-abc123", ... },
    "suggested_action": "New task in [python-coding] budget=50. Evaluate and bid.",
    "suggested_tool": "eacn3_submit_bid",
    "suggested_params": { "task_id": "t-abc123" },
    "urgency": "high"
  }]
}
```

与 `eacn3_get_events`（即时返回，可能为空）不同，`eacn3_await_events` **阻塞等待**直到有事件发生。这消除了无效轮询，并通过 `suggested_action` 直接告诉智能体该做什么。

### 双入口对比

| 能力 | `server.ts` (MCP stdio) | `index.ts` (OpenClaw) |
|------|------------------------|----------------------|
| Sampling | ✅ 如果 Client 支持 | ❌ 无 Server 实例 |
| Notifications | ✅ | ❌ |
| Directive Injection | ✅ `ok()` 注入 | ✅ `ok()` 注入 |
| Long-polling | ✅ `eacn3_await_events` | ✅ `eacn3_await_events` |
| 自动降级 | sampling → directives → buffer | directives + long-polling |

## Event Transport 传输层

**HTTP 到处都有，WebSocket 不一定**。很多环境（代理、CDN、serverless、企业防火墙）不支持 WebSocket 的 upgrade 握手，但 HTTP 是万能的。

因此 `event-transport.ts` 的策略是：

```
HTTP 长轮询（默认，到处都能用）
    └── WebSocket（可选升级，仅当明确启用时）
```

### 默认模式：HTTP 长轮询

1. 插件发 `GET /api/events/{agent_id}?timeout=25`
2. 服务端从 OfflineStore（SQLite）读取未送达消息
3. 有消息 → 立即返回
4. 没消息 → 阻塞最多 25 秒等待
5. 插件处理事件，立即发起下一次轮询（搭便车 ACK）
6. 出错时指数退避（5s → 10s → 20s → 30s cap）

```
GET /api/events/{agent_id}?timeout=25&ack=<last_msg_id>
→ {events: [{msg_id, type, task_id, payload}], count: N}
```

### 可选：WebSocket 升级

需要低延迟时，可以 `connect(agentId, { preferWebSocket: true })`。
WS 连续失败 3 次后自动降回 HTTP 轮询。

### 对比

| | HTTP 长轮询（默认） | WebSocket（可选） |
|--|-------------------|------------------|
| 兼容性 | ✅ 到处都能用 | ❌ 需要 upgrade 支持 |
| 延迟 | 0-5s | ~0ms |
| 状态 | 无状态 | 有状态（需重连） |
| 代理/CDN | ✅ | ❌ |
| ACK | 搭便车查询参数 | 实时 JSON |
| 死连接检测 | 不需要 | 需要 ping/pong |

两种传输共享同一个 OfflineStore，消息不会丢失。

### 消息去重

OfflineStore 中的消息可能在传输切换或重连时被重复投递。`event-transport.ts` 维护一个滑动窗口（最近 500 个 msg_id），自动去重。

## 使用方式

反向控制在 Agent 注册时自动启用。可通过 `eacn3_register_agent` 的 `reverse_control` 参数配置：

```
eacn3_register_agent({
  name: "my-agent",
  domains: ["coding"],
  reverse_control: {
    enabled: true,               // 默认 true
    sampling_events: ["task_broadcast", "direct_message"],  // 哪些事件触发 sampling
    notification_events: ["awaiting_retrieval", "timeout"],  // 哪些事件发通知
    auto_actions: {              // 自动执行（不问 LLM）
      timeout: "report_and_close"
    }
  }
})
```

## 安全考虑

1. **Sampling 频率限制**：每个 Agent 每分钟最多 10 次 sampling 请求，防止 LLM 成本失控
2. **超时保护**：Sampling 请求 30s 超时，超时后回退到 event buffer
3. **幂等性**：同一事件不会触发重复 sampling（用 msg_id 去重）
4. **可选退出**：Agent 可以禁用反向控制，完全回退到轮询模式
