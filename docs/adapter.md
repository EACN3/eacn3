# 适配器 Adapter

## 职责

适配器是**服务端的注册基础设施**，负责将任何外部事物（MCP 工具、已有 Agent、第三方框架）转化为网络能识别的标准 Agent。它完成三件事：

1. **生成通信层**：为接入者创建 Agent Card，处理所有 A2A 协议交互（bid、竞标、结果提交、子任务、Agent 间直接通信）
2. **注入协作能力**：通过 MCP 工具接口，让接入者能使用网络协作能力
3. **协议转译**：将 EACN3 的 A2A 消息转译为接入者能理解的格式，反之亦然

```
任何外部事物 → Adapter → 通信层生成 + 能力注入 → 网络中的一等公民

  MCP 工具          ┐
  已有的 Agent      ├─→ Adapter ─→ Agent Card + 通信层 + 协作能力 ─→ EACN3 网络
  LangChain Agent   │
  AutoGen Agent     │
  CrewAI Crew       │
  任何可调用的服务  ┘
```

---

## 适配目标

| 接入类型 | 适配重点 |
|----------|----------|
| MCP 工具 | 从 MCP tool schema 映射 skills，生成 `executor` Agent Card |
| 已有 Agent（自研） | 提取能力描述，生成 Agent Card，注入协作 MCP 接口 |
| LangChain / LangGraph | 封装 Chain/Agent 为 Agent Card，将任务推送转译为 chain 调用 |
| AutoGen | 将多 Agent 会话映射为 EACN3 任务树 |
| CrewAI | 将角色（Role）映射为 Agent，将 Crew 映射为子任务组 |
| OpenAI Agents SDK | 封装 function calling 为 skills |
| Semantic Kernel | 将 Plugin 映射为 MCPTool 或 Skill |

---

## Adapter 的三项核心工作

### 1. 生成通信层

Adapter 从接入者提供的信息（name、description、domains、skills、url）生成完整的 Agent Card 和通信层。通信层处理所有 A2A 协议交互，接入者无需了解 A2A 协议。

```
Adapter 生成通信层：
  接入者提供的信息 → Agent Card（网络身份）
                   → bid 评估逻辑（基于 Agent Card 中的能力描述）
                   → A2A 回调处理（POST /events → 转译 → 接入者）
```

### 2. 注入协作能力

Adapter 通过两个机制让接入者知道并能使用网络协作能力：

**协作提示（认知层面）**：通信层每次递交任务时，硬编码附带协作能力说明，告知接入者有哪些协作工具可用。不需要在注册时注入 skill 声明。

**MCP 工具接口（执行层面）**：Adapter 在注册时注入可调用的工具接口，内部路由到通信层。

```
注册时注入的 MCP 工具接口：
├── 执行者工具（处理被分配的任务）
│   ├── create_subtask(task_id, description, budget, deadline?)  → 通信层 → Network
│   ├── reject_task(task_id, reason)               → 通信层 → Network
│   ├── submit_result(task_id, result)             → 通信层 → Network
│   └── send_message(agent_id, content)            → 通信层 → A2A 直连对方 Agent
└── 发起者工具（管理自己发布的任务）
    ├── get_task_status(task_id)                    → 通信层 → Network
    ├── close_task(task_id)                         → 通信层 → Network
    ├── update_deadline(task_id, new_deadline)      → 通信层 → Network
    ├── update_discussions(task_id, message)         → 通信层 → Network
    ├── select_result(task_id, agent_id)            → 通信层 → Network
    ├── get_task_results(task_id)                   → 通信层 → Network
    └── confirm_budget(task_id, approved, new_budget?) → 通信层 → Network
```

> 注入的 MCP 工具接口中，`agent_id` 由通信层自动填充，Agent 无需传入。

### 3. 协议转译

Adapter 在 EACN3 消息格式与接入者的原生格式之间双向转译：

```
运行时：
  网络 ──A2A──→ 通信层 ──→ Adapter 转译 ──→ 接入者（原生格式）
  接入者 ──原生调用──→ Adapter 转译 ──→ 通信层 ──A2A──→ 网络
```

---

## 每个 Adapter 必须实现

```
Adapter
├── extract_capabilities(source) → {name, description, domains, skills}
│     ← 从接入者提取能力信息，用于生成 Agent Card
├── handle_task(task) → void
│     ← 将已分配的任务转译为接入者能理解的格式并执行
└── translate_output(output) → Result
      ← 将接入者的输出转译为 EACN3 结果格式
```

> 通信层生成、能力注入、bid 评估等逻辑由 Adapter 基类统一提供，各框架的 Adapter 只需实现上述三个转译方法。

---

## 接入流程

```
注册阶段：
  1. 接入者提供原始信息（代码、配置、MCP schema 等）
  2. Adapter.extract_capabilities() 提取能力描述
  3. Adapter 生成 Agent Card + 通信层
  4. Adapter 注入协作能力（MCP 工具接口）
  5. Adapter 将 Agent Card 提交给 Registry.register()
  6. Registry 校验 → 持久化 → DHT 公告

运行阶段：
  1. 通信层收到任务推送 → bid 评估
  2. 竞标成功 → Adapter.handle_task() 转译给接入者执行
  3. 接入者产出结果 → Adapter.translate_output() → 通信层提交
  4. 接入者调用注入的 MCP 接口（如 create_subtask）→ 通信层处理
```

---

## 设计原则

- **对 EACN3 透明**：网络侧看到的永远是标准 Agent，感知不到底层是什么
- **对接入者透明**：接入者不需要了解 A2A 协议，只看到标准的 skill 和 MCP 工具
- **各框架独立实现**：每个 Adapter 的转译逻辑单独维护，互不干扰
- **基类统一**：通信层生成、能力注入、bid 评估由 Adapter 基类提供，子类只做转译
- **最小侵入**：不修改底层框架代码，只在外层做协议转换和能力注入
