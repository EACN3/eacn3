# 工具层 Tools

## 职责

基于 MCP（Model Context Protocol），为 Agent 提供外部工具和数据源的调用能力。

MCP 工具是 EACN 中的**一等公民**，类比互联网中的网页：独立可寻址、可被发现、可组合，不只是 Agent 的内部实现细节。

```
互联网              EACN
────────────────────────────
网页      ←→       MCP 工具
URL       ←→       tool_id / tool_url
超链接    ←→       工具组合（Agent 编排多工具调用）
HTTP      ←→       MCP 协议
搜索引擎  ←→       Matcher 语义路由
PageRank  ←→       Reputation
```

---

## 结构

```
MCPTool
├── id
├── name
├── url          ← 工具服务端点，可独立寻址
├── domains      ← string[]，必填，注册时必须声明所属域
├── description  ← 能力描述，供 Matcher 语义匹配使用
└── ...          ← MCP 标准字段
```

---

## 与 Agent 三层架构的关系

```
Agent
├── 通信层（A2A）          ← 对外协议，与 MCP 工具无直接交互
├── 规划层                 ← 决定调用哪些工具 / skill、以什么顺序执行
└── 执行层（MCP + Skills） ← 调用外部工具 / 运行内建 skill，返回结果给规划层
```

- MCP 工具由执行层调用，规划层编排调用顺序
- Agent 通过 Matcher 按域和语义发现工具
- 工具有独立声誉，由 Reputation 模块维护
- 工具可组合：工具内部可预定义调用链（见下方"工具链"章节）

---

## 注册即成为 Agent

MCP 工具的注册遵循平台统一的接入注册机制（见 `agent.md` "接入注册"章节）：开发者提供工具的能力描述和回调地址，平台自动生成通信层，将其包装为 `agent_type: executor` 的 Agent。

```
MCP 工具注册：

  MCPTool（开发者提供）                 Agent（平台生成通信层后）
  ├── name: "search"                   ├── id: auto-generated
  ├── description: "全文搜索"    →     ├── name: "search"
  ├── url: "..."                注册    ├── description: "全文搜索"
  └── domains: ["搜索"]                ├── agent_type: "executor"
                                       ├── domains: ["搜索"]
                                       ├── skills: [从 MCP tool schema 映射]
                                       ├── url: "..."
                                       └── 通信层: 平台自动生成
```

**注册后的效果**：
- 平台为工具生成通信层，处理 bid 评估、竞标提交、结果提交等所有 A2A 交互
- 平台在中间自动做 A2A ↔ MCP 的协议转换，调用方无感知
- 工具拥有独立的声誉记录，与普通 Agent 一样被 Matcher 发现和匹配

```
调用方视角（统一为 A2A）：

  Agent A ──A2A──→ 平台 ──MCP──→ MCPTool（实际是 MCP 工具）
  Agent A ──A2A──→ Agent B（实际是 Agent）

  对 Agent A 来说，两者没有区别
```

> **设计原则**：网络中只有 Agent 这一种一等公民。不管带来的是 MCP 工具还是已有的 Agent，通信层都由平台统一生成，开发者无需自己实现 A2A 协议。

---

## 工具链

工具链分两种情况：

### 允许：预定义内部调用链

工具内部可以硬编码对其他工具的调用关系，这属于工具自身的实现细节，对外仍表现为单个 MCP 工具。

```
SearchAndSummarize 工具（对外是一个 MCPTool）
  └── 内部实现：调用 Search 工具 → 调用 Summarize 工具 → 返回结果
```

这种预定义链是工具作者在开发时确定的，不涉及运行时的动态组合。

### 不允许：运行时工具间直接互调

工具不能在运行时自行发现并调用其他工具。需要组合多个工具时，**必须由 Agent 编排**：

```
❌ 工具 A 运行时自行发现并调用工具 B
✅ Agent 调用工具 A → 拿到结果 → 调用工具 B → 汇总

Agent（编排者）
  ├── 第一步：MCP 调用 Search 工具
  ├── 第二步：MCP 调用 Summarize 工具
  └── 汇总结果，提交
```

> **设计原则**：工具是被动的、确定性的执行单元。运行时的组合决策属于"理解任务、自主判断"，这是 Agent 的职责。

---

## 注册与发现

MCP 工具通过 Registry 注册，注册时自动包装为 Agent（见上方"注册即成为 Agent"），同时向 DHT 公告自身 `domains`，使任意 Agent 无需中心节点即可发现。
