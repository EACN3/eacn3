# 插件实现方案 Plugin Implementation

## 概述

将 `plugin.md` 中描述的插件（服务端 + 客户端合体）实现为独立的 TypeScript 项目。采用与 Evo-anything 相同的插件架构模式（OpenClaw plugin + MCP server + Skills），但与 Evo-anything 没有代码依赖。

**相关文档**：
- MCP Tools 完整列表（29 个）→ `plugin-impl-tools.md`
- Skills 完整列表（12 个）→ `plugin-impl-skills.md`

---

## 核心设计：宿主驱动，插件无脑

插件本身**不做任何主动决策**。所有"智能行为"——bid 评估、任务规划、执行编排——都由宿主 LLM 在 Skill 工作流中完成。插件只提供两样东西：

1. **MCP Tools**：状态管理 + 网络端接口的薄封装（29 个，覆盖网络端全部 28 个接口 + A2A 直连）
2. **状态**：本地持久化（已注册 Agent、任务、声誉缓存）

这和 Evo-anything 的模式完全一致：`evo_step` 不做进化决策，只管状态转换；所有策略由 Skill markdown 引导 Claude 完成。

### 三层架构在插件中的映射

文档中的 Agent 三层架构不是三个运行时模块，而是三个角色：

| 文档概念 | 插件中的实现 | 谁在运行 |
|---------|------------|---------|
| 通信层（A2A） | MCP tools（`eacn_submit_bid`、`eacn_submit_result` 等） | 插件进程（被动响应） |
| 规划层 | Skill markdown（`/eacn-work`、`/eacn-bid`、`/eacn-execute`）引导的推理流程 | 宿主 LLM（Claude） |
| 执行层 | 宿主已有的工具 + 插件注入的 MCP tools | 宿主 LLM 调用 |

**不存在独立的 `planning.ts` 或 `execution.ts`**。规划就是 Claude 读 Skill 后的推理，执行就是 Claude 调工具。

### 推送机制

网络端提供 WebSocket 推送（`WS /ws/{agent_id}`），不是 HTTP 轮询。

- 插件进程在 `eacn_connect` 时为每个已注册 Agent 建立 WS 连接
- 事件缓冲在插件进程内存中
- 宿主通过 `eacn_get_events()` 获取缓冲事件（对宿主像"轮询"，但底层是 push）
- 控制权始终在宿主侧，插件永远不需要反向"叫醒" Claude

### 心跳

服务端需要定期向网络端发心跳。两种方式并用：

1. **Skill 循环内顺带发**：`/eacn-work` 每轮顺带调 `eacn_heartbeat()`
2. **MCP server 进程内 setInterval**：兜底，用户长时间不操作时保持在线

### bid 评估

不是确定性代码，也不是插件内嵌 LLM。就是 Claude 自己在 `/eacn-bid` Skill 中判断：

```
1. 调 eacn_get_task(task_id) 获取任务详情
2. 调 eacn_list_my_agents() 获取自身能力
3. 调 eacn_get_reputation(agent_id) 获取当前声誉
4. Claude 判断：能做吗？confidence 多少？报价多少？
5. 调 eacn_submit_bid(task_id, confidence, price)
```

---

## 目录结构

```
eacn-dev/
├── eacn/                          # 网络端（Python，已有，不动）
│
├── plugin/                        # 服务端+客户端插件（TypeScript，新建）
│   ├── openclaw.plugin.json
│   ├── package.json
│   ├── .mcp.json
│   ├── tsconfig.json
│   │
│   ├── index.ts                   # OpenClaw 入口（api.registerTool）
│   ├── server.ts                  # MCP server 独立入口（stdio transport）
│   │
│   ├── src/
│   │   ├── models.ts              # 数据类型（对齐 docs/ 中的定义）
│   │   ├── state.ts               # 本地状态持久化（~/.eacn/state.json）
│   │   ├── network-client.ts      # HTTP + WS 客户端，调网络端接口
│   │   ├── ws-manager.ts          # WebSocket 连接管理 + 事件缓冲
│   │   ├── matcher.ts             # 本地任务→Agent 匹配
│   │   ├── adapter.ts             # Adapter 基类（Agent Card 包装）
│   │   ├── registry.ts            # Agent 注册、校验、向网络端 DHT 公告
│   │   └── logger.ts              # 本地事件日志 + 上报网络端
│   │
│   ├── skills/                    # 12 个 Skills
│   │   ├── eacn-join/SKILL.md          # /eacn-join — 连接网络
│   │   ├── eacn-leave/SKILL.md         # /eacn-leave — 断开连接
│   │   ├── eacn-register/SKILL.md      # /eacn-register — 注册 Agent
│   │   ├── eacn-task/SKILL.md          # /eacn-task — 发布任务、跟踪
│   │   ├── eacn-collect/SKILL.md       # /eacn-collect — 回收结果、选定、结算
│   │   ├── eacn-work/SKILL.md          # /eacn-work — 接活主循环（感知+分发）
│   │   ├── eacn-bid/SKILL.md           # /eacn-bid — 评估并竞标
│   │   ├── eacn-execute/SKILL.md       # /eacn-execute — 执行已中标任务
│   │   ├── eacn-clarify/SKILL.md       # /eacn-clarify — 澄清请求
│   │   ├── eacn-adjudicate/SKILL.md    # /eacn-adjudicate — 裁决任务
│   │   ├── eacn-browse/SKILL.md        # /eacn-browse — 浏览网络
│   │   └── eacn-dashboard/SKILL.md     # /eacn-dashboard — 状态概览
│   │
│   └── agents/
│       └── worker.md              # worker 子会话人设（可选）
│
├── docs/
├── tests/
└── ...
```

---

## 本地状态（state.ts）

持久化到 `~/.eacn/state.json`：

```typescript
interface EacnState {
  server_card: ServerCard | null;
  network_endpoint: string | null;
  agents: Record<string, AgentCard>;
  local_tasks: Record<string, Task>;
  reputation_cache: Record<string, number>;
  pending_events: LogEntry[];
}
```

状态管理模式与 Evo-anything 的 `state.ts` 一致：`getState()` / `save()` / `setState()`，首次访问时从磁盘加载，每次变更后写回。

---

## Adapter（adapter.ts）

Adapter 在插件中的角色比文档描述的更轻：

- **不生成运行时通信层**——通信层就是 MCP tools，已经存在
- **只做注册时的包装**：提取能力 → 生成 AgentCard → 提交 Registry

```typescript
abstract class Adapter {
  abstract extractCapabilities(source: unknown): {
    name: string;
    description: string;
    domains: string[];
    skills: Skill[];
  };

  register(source: unknown): AgentCard {
    const caps = this.extractCapabilities(source);
    const card = buildAgentCard(caps);
    registry.register(card);
    networkClient.announceAgent(card);
    return card;
  }
}
```

首期只实现 `GenericAdapter`（用户手动提供 name/description/domains）。MCP 工具自动注册、框架适配等后续再加。

---

## 与网络端的接口对齐

`network-client.ts` 完整覆盖网络端全部 28 个接口。详见 `plugin-impl-tools.md` 的覆盖校验表。

| 接口分组 | 数量 | 文档 |
|---------|------|------|
| Discovery - Server | 4 | `discovery.md` |
| Discovery - Agent | 6 | `discovery.md` |
| Tasks - 查询 | 5 | `network.md` |
| Tasks - 发起者写入 | 7 | `network.md` |
| Tasks - 执行者写入 | 4 | `network.md` |
| Reputation | 2 | `reputation.md` |
| WebSocket 推送 | 1 | `network.md` |
| **合计** | **28** | |

Economy 接口（get_balance、deposit 等）是网络端内部模块，未暴露 HTTP API。如需在 /eacn-dashboard 显示余额，需网络端补充。

---

## 实现顺序

1. **骨架**：`package.json` / `tsconfig.json` / `openclaw.plugin.json` / `.mcp.json`
2. **models.ts** + **state.ts**：类型定义 + 本地状态读写
3. **network-client.ts** + **ws-manager.ts**：HTTP 客户端 + WebSocket 连接管理
4. **adapter.ts** + **registry.ts**：注册链路
5. **matcher.ts** + **logger.ts**：本地匹配 + 日志
6. **index.ts** + **server.ts**：注册全部 29 个 MCP tools
7. **Skills**（按角色顺序）：
   - 服务端：join → leave
   - Agent：register
   - 发起者：task → collect
   - 执行者：work → bid → execute → clarify
   - 裁决者：adjudicate
   - 通用：browse → dashboard
8. **agents/worker.md**：worker 子会话人设（可选）
