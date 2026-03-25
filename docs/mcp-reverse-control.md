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
