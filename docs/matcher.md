# 匹配器 Matcher

## 定位

**共享工具包（Middleware）**——不是独立服务，而是可被各层按需调用的无状态匹配库。服务端和网络端各自实例化，各自从本地 Logger 获取声誉数据。

---

## 两侧各用各的

| 部署位置 | 调用方 | 用途 | 声誉数据来源 |
|----------|--------|------|-------------|
| **网络端** | Network（跨服务端任务推送） | 推荐：从全网 Agent 中筛选推送目标 | 网络端 Logger（全局声誉事件） |
| **服务端** | 服务端（本服务端内任务） | 直连匹配：优先找达标的直连 Agent；无达标时升级为跨服务端任务 | 服务端 Logger（本地声誉事件） |

本服务端内任务不经过网络端 Network，由服务端直接调用本地 Matcher 匹配并委派给目标 Agent；跨服务端任务由网络端 Network 调用网络端 Matcher。Matcher 本身不存储声誉数据，也不跨层拉取——谁调用谁负责喂数据。

---

## 接口

```
Matcher
├── match_agents(task, agents[], scores{}, prefer_type?) → Agent[]
│     ← 给定任务和声誉分表，从 Agent 列表中找出匹配的候选 Agent
│       scores：调用方从本地 Logger 预取的声誉分 { agent_id → float }
│       prefer_type 可选，目前仅用于指定 executor（调用工具时）
└── check_bid(agent_id, confidence, price, budget, scores{}, cap_counts{}, is_adjudication?) → bool
      ← 竞标准入校验：
        1. 能力准入：confidence × reputation ≥ 阈值
        2. 报价准入：price ≤ budget × (1 + 溢价容忍度 + 议价加成)
        cap_counts：调用方从 Reputation.get_cap_counts 预取 { agent_id → {capped_gain, capped_penalty} }
        议价加成 = f(capped_gain - capped_penalty)，capped_gain 多则容忍度放宽，capped_penalty 多则收紧
        两项均通过才允许竞标，Network 在 submit_bid 时调用
        **裁决任务**：is_adjudication=true 时跳过报价准入（步骤 2），仅做能力准入
```

> MCP 工具注册后已成为 Agent，不再需要单独的 `match_tools` 接口——统一用 `match_agents`。
>
> `prefer_type` 唯一用途：Agent 想在本地调工具时传 `executor`，只匹配工具类 Agent。子任务走网络竞标，不传此字段，谁都能接。
>
> 匹配方向约束：
> - `prefer_type=executor` 的请求 → 所有 Agent 都能竞标（只要能力匹配）
> - MCP 转写的 Agent（`agent_type=executor`）→ 只能被 `prefer_type=executor` 的请求匹配到（它只是工具，没有规划能力）

---

## 服务端匹配逻辑

```
1. 检查是否指定了直连 Agent
   ├─ 有指定 → 校验该 Agent 是否达标（域匹配 + 声誉阈值）
   │   ├─ 达标 → 返回 [指定 Agent]
   │   └─ 不达标 → 降级为候选列表
   └─ 无指定 → 进入候选匹配
2. 候选匹配：域过滤 → 声誉加权 → 排序
3. 有达标候选 → 返回排序后的候选列表
4. 无达标候选 → 返回空列表（触发网络端推荐）
5. 网络端推荐仍无达标候选 → 直接返回失败，不创建任务
```

---

## 演进路径

### 阶段一：静态标签匹配（当前）

纯函数，无状态，无副作用。

```
1. agent_type 过滤：若指定了 prefer_type=executor，只匹配工具类 Agent
2. 域标签交集：task.domains ∩ target.domains，交集越大优先级越高
3. 描述关键词比对：task.description 与 target.description 的词面匹配
4. 排序返回
```

### 阶段二：语义嵌入匹配

将任务描述和 Agent/工具描述向量化，用语义相似度替代关键词比对。能匹配到从未见过的 Agent，只要能力语义相符。

```
1. 域标签交集过滤（粗筛）
2. 向量相似度排序（细排）：embed(task.description) · embed(target.description)
3. 排序返回
```

### 阶段三：语义能力路由

任务不再只匹配单个 Agent，而是路由到最优的 **Agent 组合**，并引入声誉权重和 agent_type 感知。

```
1. 语义相似度候选（同阶段二）
2. 声誉加权：candidate.score × reputation_weight
3. agent_type 过滤：若指定了 prefer_type=executor，只匹配工具类 Agent
4. 能力组合：若单 Agent 不能覆盖全部需求，返回互补的 Agent 组合
5. 排序返回
```

---

## 任务描述格式的自然演化

EACN 不预设任务描述的标准格式（类比互联网中的 HTML）。格式标准将由系统自然涌现：

- Agent 的 `skill_growth` 记录哪类任务描述格式使自身 `bid()` 判断更准确
- `task_history` 记录哪些格式的任务最终被成功执行、结果被选中
- 竞标成功率高的任务格式会成为发起方的参考模板

最终，**被网络普遍接受的描述格式本身就是事实标准**，无需顶层设计。

---

## 设计原则

- **接口稳定，实现演进**：调用方无感知，Matcher 内部逐阶段升级
- **阶段一兜底**：语义匹配失败时自动降级到标签匹配
- **无状态纯函数**：不存储声誉，不跨层拉取，调用方预取数据后传入
- **两侧独立**：服务端和网络端各自实例化，数据来源不同，互不依赖
- **格式标准涌现**：任务描述规范无需预定义，由 Agent 记忆系统自然筛选产生
