# 插件实现方案 Plugin Implementation

## 概述

将 `plugin.md` 中描述的插件（服务端 + 客户端合体）实现为独立的 TypeScript 项目。采用与 Evo-anything 相同的插件架构模式（OpenClaw plugin + MCP server + Skills），但与 Evo-anything 没有代码依赖。

**相关文档**：
- MCP Tools 完整列表（32 个）→ `plugin-impl-tools.md`
- Skills 完整列表（14 个）→ `plugin-impl-skills.md`

---

## 核心设计：宿主驱动，插件无脑

插件本身**不做任何主动决策**。所有"智能行为"——bid 评估、任务规划、执行编排——都由宿主 LLM 在 Skill 工作流中完成。插件只提供两样东西：

1. **MCP Tools**：状态管理 + 网络端接口的薄封装（32 个，覆盖网络端接口 + 经济接口 + A2A 直连）
2. **状态**：本地持久化（已注册 Agent、任务、声誉缓存）

这和 Evo-anything 的模式完全一致：`evo_step` 不做进化决策，只管状态转换；所有策略由 Skill markdown 引导 Claude 完成。

### 三层架构在插件中的映射

文档中的 Agent 三层架构不是三个运行时模块，而是三个角色：

| 文档概念 | 插件中的实现 | 谁在运行 |
|---------|------------|---------|
| 通信层（A2A） | MCP tools（`eacn3_submit_bid`、`eacn3_submit_result` 等） | 插件进程（被动响应） |
| 规划层 | Skill markdown（`/eacn3-bounty`、`/eacn3-bid`、`/eacn3-execute`）引导的推理流程 | 宿主 LLM（Claude） |
| 执行层 | 宿主已有的工具 + 插件注入的 MCP tools | 宿主 LLM 调用 |

**不存在独立的 `planning.ts` 或 `execution.ts`**。规划就是 Claude 读 Skill 后的推理，执行就是 Claude 调工具。

### 推送机制

网络端提供 WebSocket 推送（`WS /ws/{agent_id}`），不是 HTTP 轮询。

- 插件进程在 `eacn3_connect` 时为每个已注册 Agent 建立 WS 连接
- 事件缓冲在插件进程内存中
- 宿主通过 `eacn3_get_events()` 获取缓冲事件（对宿主像"轮询"，但底层是 push）
- 控制权始终在宿主侧，插件永远不需要反向"叫醒" Claude

### 心跳

服务端需要定期向网络端发心跳。两种方式并用：

1. **Skill 循环内顺带发**：`/eacn3-bounty` 每轮顺带调 `eacn3_heartbeat()`
2. **MCP server 进程内 setInterval**：兜底，用户长时间不操作时保持在线

### bid 评估

不是确定性代码，也不是插件内嵌 LLM。就是 Claude 自己在 `/eacn3-bid` Skill 中判断：

```
1. 调 eacn3_get_task(task_id) 获取任务详情
2. 调 eacn3_list_my_agents() 获取自身能力
3. 调 eacn3_get_reputation(agent_id) 获取当前声誉
4. Claude 判断：能做吗？confidence 多少？报价多少？
5. 调 eacn3_submit_bid(task_id, confidence, price)
```

---

## 目录结构

```
eacn-dev/
├── eacn3/                          # 网络端（Python，已有，不动）
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
│   │   ├── state.ts               # 本地状态持久化（~/.eacn3/state.json）
│   │   ├── network-client.ts      # HTTP 客户端，封装 29 个网络端接口
│   │   └── ws-manager.ts          # WebSocket 连接管理 + 事件缓冲
│   │
│   ├── skills/                    # 14 个 Skills
│   │   ├── eacn3-join/SKILL.md          # /eacn3-join — 连接网络
│   │   ├── eacn3-leave/SKILL.md         # /eacn3-leave — 断开连接
│   │   ├── eacn3-register/SKILL.md      # /eacn3-register — 注册 Agent
│   │   ├── eacn3-task/SKILL.md          # /eacn3-task — 发布任务、跟踪
│   │   ├── eacn3-collect/SKILL.md       # /eacn3-collect — 回收结果、选定、结算
│   │   ├── eacn3-budget/SKILL.md        # /eacn3-budget — 预算确认
│   │   ├── eacn3-delegate/SKILL.md      # /eacn3-delegate — 委托任务
│   │   ├── eacn3-bounty/SKILL.md        # /eacn3-bounty — 接活主循环（感知+分发）
│   │   ├── eacn3-bid/SKILL.md           # /eacn3-bid — 评估并竞标
│   │   ├── eacn3-execute/SKILL.md       # /eacn3-execute — 执行已中标任务
│   │   ├── eacn3-clarify/SKILL.md       # /eacn3-clarify — 澄清请求
│   │   ├── eacn3-adjudicate/SKILL.md    # /eacn3-adjudicate — 裁决任务
│   │   ├── eacn3-browse/SKILL.md        # /eacn3-browse — 浏览网络
│   │   └── eacn3-dashboard/SKILL.md     # /eacn3-dashboard — 状态概览
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

持久化到 `~/.eacn3/state.json`：

```typescript
interface EacnState {
  server_card: ServerCard | null;
  network_endpoint: string;
  agents: Record<string, AgentCard>;
  local_tasks: Record<string, LocalTaskInfo>;
  reputation_cache: Record<string, number>;
  pending_events: PushEvent[];
}
```

状态管理模式与 Evo-anything 的 `state.ts` 一致：`getState()` / `save()` / `setState()`，首次访问时从磁盘加载，每次变更后写回。

---

## 被砍掉的模块

以下模块在早期设计中存在，已被内联到 MCP tool handler 中，不再作为独立文件：

| 模块 | 砍掉原因 |
|------|---------|
| `adapter.ts` | AgentCard 组装由用户/LLM 填参数，MCP tool 内联组装即可 |
| `registry.ts` | 校验 + 持久化 + 调网络，三行代码内联到 `eacn3_register_agent` |
| `matcher.ts` | 本地匹配几行代码，内联到 `eacn3_create_task` |
| `logger.ts` | 上报事件一行代码，内联到 `eacn3_submit_result` / `eacn3_reject_task` |

---

## 与网络端的接口对齐

`network-client.ts` 封装 29 个网络端接口。详见 `network-api.md`。

| 接口分组 | 数量 |
|---------|------|
| Discovery - Server | 4 |
| Discovery - Agent | 6 |
| Tasks - 查询 | 4 |
| Tasks - 发起者写入 | 7 |
| Tasks - 执行者写入 | 4 |
| Reputation | 2 |
| Economy | 2 |
| **合计** | **29** |

---

## 实现顺序

1. **骨架**：`package.json` / `tsconfig.json` / `openclaw.plugin.json` / `.mcp.json`
2. **models.ts** + **state.ts**：类型定义 + 本地状态读写
3. **network-client.ts** + **ws-manager.ts**：HTTP 客户端 + WebSocket 连接管理
4. **index.ts** + **server.ts**：注册全部 32 个 MCP tools（注册/匹配/日志逻辑内联）
5. **Skills**（14 个，按角色顺序）：
   - 服务端：join → leave
   - Agent：register
   - 发起者：task → collect → budget → delegate
   - 执行者：bounty → bid → execute → clarify
   - 裁决者：adjudicate
   - 通用：browse → dashboard
6. **agents/worker.md**：worker 子会话人设（可选）
