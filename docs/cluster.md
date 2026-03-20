# 节点联邦 Cluster

## 概述

多个 Network 节点组成联邦网络。每个节点独立运行，通过 Bootstrap / DHT / Gossip 三种策略互相发现，通过节点间协议完成跨节点任务流转。

Server 只看到一个统一的 Network API，不感知节点拓扑。

```
Server A ──┐                    ┌── Server C
Server B ──┼── Node 1 ←─peer─→ Node 2 ──┼── Server D
           │     ↕                ↕      │
           │   Node 3 ←─peer─→ Node 4   │
           └── Server E          Server F ┘
```

---

## 节点模型

```python
NodeCard:
    node_id:    str         # 稳定 ID，重启不变（UUID，首次生成后持久化）
    endpoint:   str         # "http://node-1:8000"，节点间通信地址
    domains:    set[str]    # 该节点当前覆盖的所有域（由本地 Agent 注册汇总）
    status:     str         # "online" | "suspect" | "offline"
    version:    str         # 协议版本号
    joined_at:  str         # ISO 8601
    last_seen:  str         # ISO 8601，最近一次心跳时间
```

`domains` 是动态的：本地有 Agent 注册到 "翻译" 域就加入，最后一个翻译 Agent 注销就移除。

---

## 三个发现模块

三个模块功能一致：**给定 domain，返回一组 node_id**。区别是发现策略。

### Bootstrap（冷启动）

解决"第一个朋友"问题。种子节点地址列表由配置文件提供。

```
新节点 N 启动：
  1. N 联系种子节点 S：POST /peer/join {node_card}
  2. S 返回当前成员列表：{nodes: [NodeCard...]}
  3. N 拿到邻居列表，可以开始工作
  4. S 广播给所有已知节点："N 加入了"

查找：
  Bootstrap.lookup("翻译")
    → 问种子节点：谁有翻译域？
    → 种子节点查自己的全量成员表，按 domains 过滤
    → 返回 [node_B, node_C]
    （最慢但最全，兜底方案）
```

- **数据**：全量 NodeCard 列表（种子节点维护，权威源）
- **定位**：网络不成熟时的保底，成熟后可不依赖

### DHT（域路由）

结构化查找。使用一致性哈希（consistent hashing）将域名映射到负责节点。

**哈希机制**：
- 哈希函数：`SHA256(domain)` 取前 8 字节转为整数
- 哈希环：所有在线节点按 `SHA256(node_id)` 排列在 `[0, 2^64)` 环上
- 负责节点 R：`hash(domain)` 在环上顺时针方向遇到的第一个节点
- 容错：如果 R 不可达，继续顺时针找下一个节点作为备选（最多尝试 3 个）
- 节点加入/离开时，只影响相邻区间的映射，其余不变

```
Agent 注册时（Node A 收到翻译 Agent 注册）：
  1. Node A 本地存 AgentCard
  2. Node A 更新自己的 domains（加入 "翻译"）
  3. DHT.announce("翻译", node_A_id)
     → hash("翻译") → 算出负责节点 R
     → POST /peer/dht/store 到 R：{domain: "翻译", node_id: node_A}
     → R 存储这个映射

查找：
  DHT.lookup("翻译")
    → hash("翻译") → 算出负责节点 R
    → GET /peer/dht/lookup?domain=翻译 到 R
    → R 返回 {node_ids: [node_A, node_C]}
    （精确，O(1) 路由到负责节点）
```

- **数据**：每个节点存自己负责的哈希范围内的 `domain → {node_id}` 映射
- **定位**：最常用的发现路径，精确高效

### Gossip（协作扩散）

节点间完成任务协作后，交换已知节点列表。越用越快。

```
Node A 和 Node B 上的 Agent 完成了一次协作：
  gossip.exchange(node_A, node_B)
    → A 知道的节点 ∪ B 知道的节点 → 双方都知道
    → 包括每个节点覆盖哪些域

查找：
  Gossip.lookup("翻译")
    → 自己的已知列表里，哪些节点的 domains 包含 "翻译"？
    → 返回 [node_B]
    （最快，零网络开销，纯本地查）
```

- **数据**：每个节点维护 `{node_id → NodeCard}` 的已知节点列表
- **定位**：本地优先，减少 DHT 查询

### DiscoveryService 编排

不是独立模块，按优先级串联三种策略：

```
discover(domain) → [node_id...]（不含本节点，本节点的 Agent 由 discovery 层处理）

  1. Gossip.lookup(domain)        ← 本地已知列表，排除 self
     → 命中 → 返回（最快，零开销）

  2. DHT.lookup(domain)           ← 结构化路由，排除 self
     → 命中 → 返回（精确，一次网络请求）

  3. Bootstrap.lookup(domain)     ← 问种子节点，排除 self
     → 返回（最慢但最全，兜底）
```

---

## 任务跨节点生命周期

**核心规则：任务属于创建它的节点（owner）。所有状态机变更在 owner 上执行。**

### 创建 & 广播

```
Server S1 → Node A: POST /api/tasks (create_task, domain="翻译")
Node A:
  1. 创建任务，存本地，status=UNCLAIMED
  2. 初始化参与节点集合 participating_nodes = {}
  3. 本地查翻译域 Agent → 推给本地 Agent（如果有）
  4. discover("翻译") → [Node B, Node C]（不含自身）
  5. → Node B: POST /peer/task/broadcast {task_summary, origin=A, initiator_id=agent-init}
     → Node C: POST /peer/task/broadcast {task_summary, origin=A, initiator_id=agent-init}
Node B:
  1. 收到广播，记录路由 {task_id → origin=A}（轻量，不存完整任务）
  2. 本地查翻译域 Agent → 推给本地 Agent
Node C: 同上
```

### 竞标

```
Agent X（在 Server S2，连接 Node B）要竞标：
  Server S2 → Node B: POST /api/tasks/{task_id}/bid
  Node B:
    1. 查路由表：task_id → origin=A
    2. → Node A: POST /peer/task/bid {task_id, agent_id, confidence, price, from_node=B}
  Node A:
    1. 执行竞标校验（准入、状态机、预算）
    2. 记录 participating_nodes.add(B)
    3. 返回 {status: accepted/rejected, bid}
  Node B:
    1. 推给 Agent X 的 Server
```

### 退回任务

```
Agent X（在 Node B）退回任务：
  Server S2 → Node B: POST /api/tasks/{task_id}/reject
  Node B:
    1. 查路由表：task_id → origin=A
    2. → Node A: POST /peer/task/reject {task_id, agent_id, from_node=B}
  Node A:
    1. 执行退回逻辑（复用现有 Network.reject_task）
    2. 返回 {ok}
```

### 结果提交

```
Agent X 提交结果：
  Server S2 → Node B: POST /api/tasks/{task_id}/result
  Node B → Node A: POST /peer/task/result {task_id, agent_id, content, from_node=B}
  Node A:
    1. 存结果
    2. 创建裁决任务（裁决任务也是 Node A 的本地任务，也走广播流程）
    3. 返回 {ok}
```

### 创建子任务

```
Agent X（在 Node B）创建子任务：
  Server S2 → Node B: POST /api/tasks/{parent_task_id}/subtask
  Node B:
    1. 查路由表：parent_task_id → origin=A
    2. → Node A: POST /peer/task/subtask {parent_task_id, subtask_data, from_node=B}
  Node A:
    1. 创建子任务（复用现有 Network.create_subtask），子任务 owner 也是 Node A
    2. 返回 {subtask_id, ...}
    3. 广播子任务给相关节点
```

子任务与父任务在同一节点，保持任务树完整。

### 关闭 & 收集

```
Server S1 → Node A: POST /api/tasks/{task_id}/close
Node A:
  1. 关闭任务，status → AWAITING_RETRIEVAL
  2. 通知 participating_nodes 中的所有节点：POST /peer/task/status {task_id, status}
  3. 参与节点通知各自本地的相关 Agent

Server S1 → Node A: GET /api/tasks/{task_id}/results
Node A: 直接返回（所有 bid/result 已通过 forward 集中到 owner）
```

### Gossip 触发时机

```
任务完成（select_result 结算后）：
  Node A 记录了 Node B 和 Node C 参与了此次协作
  gossip.exchange(A, B)  → 双方交换已知节点列表
  gossip.exchange(A, C)  → 同上
  → 下次 A 找翻译域，Gossip 直接命中 B/C，不走 DHT
```

---

## 节点间接口

所有节点间接口使用 `/peer/` 前缀，不对 Server 暴露。

### 成员管理

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|------|------|--------|------|------|
| `POST` | `/peer/join` | `{node_card}` | `{nodes: [NodeCard...]}` | 新节点加入，返回成员列表 |
| `POST` | `/peer/leave` | `{node_id}` | `{ok}` | 优雅退出 |
| `POST` | `/peer/heartbeat` | `{node_id, domains, timestamp}` | `{ok}` | 心跳保活 |

### DHT 操作

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|------|------|--------|------|------|
| `POST` | `/peer/dht/store` | `{domain, node_id}` | `{ok}` | 宣告域映射到负责节点 |
| `DELETE` | `/peer/dht/revoke` | `{domain, node_id}` | `{ok}` | 撤销域映射 |
| `GET` | `/peer/dht/lookup` | `?domain=X` | `{node_ids: [...]}` | 查询域的节点列表 |

### Gossip 操作

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|------|------|--------|------|------|
| `POST` | `/peer/gossip/exchange` | `{from_node: NodeCard, known: [NodeCard...]}` | `{known: [NodeCard...]}` | 交换已知节点列表 |

### 任务流转

| 方法 | 路径 | 请求体 | 响应 | 说明 |
|------|------|--------|------|------|
| `POST` | `/peer/task/broadcast` | `{task_id, domain, budget, deadline, content, origin, initiator_id}` | `{ok}` | 广播任务 |
| `POST` | `/peer/task/bid` | `{task_id, agent_id, confidence, price, from_node}` | `{status, bid}` | 转发竞标 |
| `POST` | `/peer/task/reject` | `{task_id, agent_id, from_node}` | `{ok}` | 转发退回 |
| `POST` | `/peer/task/result` | `{task_id, agent_id, content, from_node}` | `{ok}` | 转发结果 |
| `POST` | `/peer/task/subtask` | `{parent_task_id, subtask_data, from_node}` | `{subtask_id, ...}` | 转发子任务创建 |
| `POST` | `/peer/task/status` | `{task_id, status, payload?}` | `{ok}` | 状态变更通知 |
| `POST` | `/peer/push` | `{type, task_id, recipients, payload}` | `{ok}` | 推送事件中转 |

共 14 个节点间接口。

### 接口详细 Schema

#### POST /peer/join

```
请求：
{
    "node_card": {
        "node_id": "node-xxxx",
        "endpoint": "http://10.0.1.2:8000",
        "domains": ["翻译", "写作"],
        "version": "0.1.0"
    }
}

响应 200：
{
    "nodes": [                          ← 当前所有在线节点
        {"node_id": "...", "endpoint": "...", "domains": [...], "status": "online", ...},
        ...
    ]
}

错误：
  409 — node_id 已存在且 endpoint 不同（可能是旧节点未清理）
  422 — 缺少必填字段
```

种子节点收到 join 后，向所有已知在线节点广播同一请求（通知新节点加入）。
非种子节点收到 join 后，只更新本地成员列表，不再广播。

#### POST /peer/leave

```
请求：{"node_id": "node-xxxx"}
响应 200：{"ok": true}
错误：404 — node_id 不存在
```

收到后：从本地成员列表移除，从 Gossip 已知列表移除，DHT 中该节点的映射标记失效。

#### POST /peer/heartbeat

```
请求：
{
    "node_id": "node-xxxx",
    "domains": ["翻译", "写作"],      ← 当前域列表（可能变化）
    "timestamp": "2026-03-20T10:00:00Z"
}

响应 200：{"ok": true}
错误：404 — node_id 不存在
```

收到后：更新 `last_seen`，如果 domains 变化则同步更新成员列表和 Gossip 已知列表。

#### POST /peer/dht/store

```
请求：{"domain": "翻译", "node_id": "node-xxxx"}
响应 200：{"ok": true}
```

存储 domain → node_id 映射。幂等，重复存储不报错。

#### DELETE /peer/dht/revoke

```
请求：{"domain": "翻译", "node_id": "node-xxxx"}
响应 200：{"ok": true}
```

移除映射。不存在时静默成功。

#### GET /peer/dht/lookup

```
请求：?domain=翻译
响应 200：{"domain": "翻译", "node_ids": ["node-A", "node-C"]}
```

返回该域下所有已注册的 node_id。空域返回空列表。

#### POST /peer/gossip/exchange

```
请求：
{
    "from_node": {"node_id": "...", "endpoint": "...", "domains": [...]},
    "known": [
        {"node_id": "...", "endpoint": "...", "domains": [...]},
        ...
    ]
}

响应 200：
{
    "known": [                          ← 本节点的已知列表，回传给对方
        {"node_id": "...", "endpoint": "...", "domains": [...]},
        ...
    ]
}
```

双方已知列表取并集。请求方发出自己的已知列表，响应方返回自己的已知列表，双方各自合并。

#### POST /peer/task/broadcast

```
请求：
{
    "task_id": "t-xxxx",
    "origin": "node-A",                ← 任务 owner 节点
    "initiator_id": "agent-init",       ← 发起任务的 Agent ID
    "domains": ["翻译"],
    "type": "normal",                   ← normal / adjudication
    "budget": 100.0,
    "deadline": "2026-03-21T00:00:00Z",
    "content": {"description": "...", "expected_output": "..."},
    "max_concurrent_bidders": 5
}

响应 200：{"ok": true}
错误：409 — task_id 路由已存在（重复广播，幂等处理）
```

收到后：
1. 存路由 `{task_id → origin}`
2. 用本地 discovery 查 Agent → 通过 Push 推给本地 Agent

#### POST /peer/task/bid

```
请求：
{
    "task_id": "t-xxxx",
    "agent_id": "agent-1",
    "server_id": "srv-xxx",             ← Agent 所在 Server
    "confidence": 0.85,
    "price": 80.0,
    "from_node": "node-B"               ← 转发来源节点
}

响应 200：
{
    "status": "accepted",               ← accepted / rejected / waiting / pending_confirmation
    "bid": {"agent_id": "...", "status": "executing", ...}
}

错误：
  404 — task_id 不存在（本节点不是 owner）
  400 — 竞标校验失败（准入不达标等）
```

owner 节点本地执行完整的竞标校验逻辑（复用现有 Network.submit_bid）。

#### POST /peer/task/reject

```
请求：
{
    "task_id": "t-xxxx",
    "agent_id": "agent-1",
    "from_node": "node-B"
}

响应 200：{"ok": true}
错误：
  404 — task_id 不存在
  400 — 前置条件不满足（Agent 未在执行该任务）
```

owner 节点本地执行退回逻辑（复用现有 Network.reject_task）。

#### POST /peer/task/result

```
请求：
{
    "task_id": "t-xxxx",
    "agent_id": "agent-1",
    "content": {"answer": "..."},
    "from_node": "node-B"
}

响应 200：{"ok": true}
错误：
  404 — task_id 不存在
  400 — 前置条件不满足
```

owner 节点本地执行结果提交（复用现有 Network.submit_result）。

#### POST /peer/task/subtask

```
请求：
{
    "parent_task_id": "t-xxxx",
    "subtask_data": {
        "domains": ["校对"],
        "content": {"description": "..."},
        "budget": 50.0,
        "deadline": "2026-03-21T00:00:00Z"
    },
    "from_node": "node-B"
}

响应 200：
{
    "subtask_id": "t-yyyy",
    "status": "unclaimed"
}

错误：
  404 — parent_task_id 不存在
  400 — 前置条件不满足
```

owner 节点本地创建子任务（复用现有 Network.create_subtask）。子任务 owner 与父任务相同，保持任务树在同一节点。创建后自动广播子任务。

#### POST /peer/task/status

```
请求：
{
    "task_id": "t-xxxx",
    "status": "awaiting_retrieval",
    "payload": {}                       ← 可选附加信息
}

响应 200：{"ok": true}
```

收到后：通知本地参与该任务的 Agent（通过 Push）。

#### POST /peer/push

```
请求：
{
    "type": "TASK_TIMEOUT",
    "task_id": "t-xxxx",
    "recipients": ["agent-1", "agent-2"],
    "payload": {"status": "awaiting_retrieval"}
}

响应 200：{"ok": true, "delivered": 2}
```

收到后：筛选本节点上注册的 recipients，通过本地 Push 投递。不在本节点的 recipients 忽略。

---

## 单节点模式（Standalone）

配置中无种子节点地址时，ClusterService 进入单节点模式：

- `discover(domain)` → 返回空列表（无其他节点）
- `broadcast(task)` → 无操作
- `forward_bid/result/reject/subtask(...)` → 不可能发生（无路由表条目）
- `gossip.exchange(...)` → 无操作
- `dht.announce/revoke(...)` → 无操作

所有任务操作完全在本地完成，行为与现有单节点实现完全一致。现有 288 个测试无需任何修改。

---

## 数据归属

| 数据 | 存哪 | 说明 |
|------|------|------|
| NodeCard | 每个节点存全量（种子节点为权威源） | 小数据，全量复制 |
| AgentCard | 注册所在的本地节点 | Server 连哪个节点就存那 |
| ServerCard | 注册所在的本地节点 | 同上 |
| 任务完整状态（bids, results, adjudications） | owner 节点 | 谁创建谁拥有 |
| 任务参与节点集合 `task_id → {node_id}` | owner 节点 | 从 bid 的 from_node 汇总，用于状态通知和 Gossip 触发 |
| 任务路由表 `task_id → origin_node` | 收到广播的节点各存一份 | 轻量映射 |
| DHT 映射 `domain → {node_id}` | 按 `hash(domain)` 分片到负责节点 | 分布式，一致性哈希 |
| Gossip 已知列表 `{node_id → NodeCard}` | 每个节点本地 | 协作后交换 |
| 声誉、经济 | owner 节点（跟任务走） | 结算在 owner 上 |

---

## 节点生命周期

### 加入

```
1. 新节点 N 生成 node_id（首次启动，持久化到本地）
2. N 读配置，拿到种子节点列表 [S1, S2]
3. N 依次尝试种子节点：S1 不可达则尝试 S2，全部不可达则进入单节点模式等待重试
4. N → S1: POST /peer/join {node_card}
5. S1 返回 {nodes: [所有在线节点]}
6. S1 → 所有已知节点: POST /peer/join {node_card}（广播 N 加入）
7. N 拿到成员列表 → 初始化 Gossip 已知列表
8. N 重新 announce 本地所有域到 DHT（覆盖重启场景）
9. N 的 Server 开始注册 Agent → 触发 DHT announce
```

### 心跳

```
每个节点每 T 秒向随机 K 个已知节点发心跳：
  POST /peer/heartbeat {node_id, domains, timestamp}

默认参数：
  T = 10 秒（心跳间隔）
  K = 3（每轮随机选择的目标节点数，不超过已知节点总数）
  M = 3（超时轮数）

检测逻辑：
  连续 M 次（30 秒）未收到某节点心跳 → 标记 suspect
  再过 M 次（60 秒）→ 标记 offline
  offline 节点的 DHT 映射由其他节点补偿查询种子节点
```

### 离开

```
优雅离开：
  1. N → 所有已知节点: POST /peer/leave {node_id}
  2. 各节点从成员列表和 Gossip 中移除 N
  3. DHT 中 N 负责的映射：其他节点下次查询时自动从种子节点补全

故障离开：
  1. 心跳超时 → 标记 offline
  2. 同上，其他节点通过 Bootstrap 兜底补全
```

---

## 请求路由（Server 视角不变）

Server 的请求始终打到自己连接的 Network 节点，路由对 Server 透明：

```
Server → Node X: POST /api/tasks/{task_id}/{action}
（action = bid | reject | result | subtask）

Node X 判断：
  if task_id 在本地 → 本地处理
  if task_id 在路由表中 → 转发到 origin 节点的 /peer/task/{action}
  if task_id 未知 → 返回 404
```

查询类操作（list_open_tasks）只返回本节点存储的任务。跨节点查询不在首期范围内。

---

## 模块结构

集群层以新增模块为主，现有代码最小改动（`api/routes.py` 增加转发判断，`app.py` 组合 ClusterService）。

```
eacn/network/
├── discovery/              ← 现有，不动。单节点内的 Agent 管理
│   ├── bootstrap.py           AgentCard 权威存储、种子 Agent 列表
│   ├── dht.py                 domain → {agent_id}（本节点内）
│   ├── gossip.py              Agent 级已知列表交换（本节点内）
│   └── query.py               三层 fallback 编排（本节点内）
│
├── cluster/                ← 全新。节点间通信
│   ├── __init__.py
│   ├── node.py                NodeCard 模型、本节点身份、成员列表管理
│   ├── bootstrap.py           节点冷启动：联系种子节点，获取成员列表
│   ├── dht.py                 domain → {node_id}：跨节点域路由
│   ├── gossip.py              节点级已知列表交换
│   ├── discovery.py           编排三层（Gossip → DHT → Bootstrap）
│   ├── router.py              请求路由：本地 or 转发到 owner 节点
│   └── config.py              种子节点地址、心跳间隔、超时阈值等配置
│
├── api/
│   ├── routes.py           ← 路由层增加跨节点转发判断
│   ├── discovery_routes.py ← 不动。Agent/Server 注册不变
│   ├── peer_routes.py      ← 全新。14 个 /peer/ 节点间端点
│   └── ...
```

### 两层协作

集群层和现有 discovery 层各管各的，通过 Network 类串联：

```
create_task(domain="翻译"):

  ┌─ 集群层（新）──────────────────────────────────────┐
  │  cluster.discover("翻译") → [Node B, Node C]       │
  │  广播任务给 B、C                                    │
  └────────────────────────────────────────────────────┘
                          ↓ 每个节点各自
  ┌─ discovery 层（现有）─────────────────────────────┐
  │  discovery.discover("翻译") → [agent_id_1, ...]   │
  │  Push 给本地 Agent                                 │
  └────────────────────────────────────────────────────┘
```

### 对现有代码的影响

| 现有模块 | 改动 |
|----------|------|
| `discovery/*` | **不动** |
| `api/discovery_routes.py` | **不动** |
| `api/schemas.py` | **不动** |
| `api/websocket.py` | **不动** |
| `task_manager.py` | **不动** |
| `push.py` | **不动** |
| `matcher.py` | **不动** |
| `reputation.py` | **不动** |
| `economy/*` | **不动** |
| `api/routes.py` | 路由层增加跨节点转发判断：bid / result / reject / subtask 操作先查路由表，本地任务走现有逻辑，远端任务转发到 owner 的 `/peer/task/*` |
| `app.py (Network)` | 初始化时创建 ClusterService；`create_task` 后调 `cluster.broadcast`；`select_result` 后调 `cluster.gossip.exchange`；Agent 注册/注销时触发 DHT announce/revoke。通过组合注入，不改现有方法签名 |

现有 288 个测试必须全部通过，不受集群层影响。单节点模式下 ClusterService 为空实现（standalone mode），不发起任何节点间通信。

---

## 数据库变更

集群层使用独立的表，不修改现有表。

```sql
-- ═══════════════════════════════════════════════════════
-- 集群层专用表（新增，不影响现有表）
-- ═══════════════════════════════════════════════════════

-- 节点注册表（每个节点本地存全量）
CREATE TABLE cluster_nodes (
    node_id     TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    domains     TEXT NOT NULL DEFAULT '[]',   -- JSON array
    status      TEXT NOT NULL DEFAULT 'online',
    version     TEXT NOT NULL,
    joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 任务路由表（收到广播的节点存映射）
CREATE TABLE cluster_task_routes (
    task_id     TEXT PRIMARY KEY,
    origin_node TEXT NOT NULL                 -- 任务 owner 的 node_id
);

-- 任务参与节点（owner 节点存，记录哪些远端节点参与了此任务）
CREATE TABLE cluster_task_participants (
    task_id     TEXT NOT NULL,
    node_id     TEXT NOT NULL,                -- 参与节点的 node_id（从 bid 的 from_node 汇总）
    PRIMARY KEY (task_id, node_id)
);

-- 集群 DHT：domain → node_id（与现有 dht 表并存，互不干扰）
CREATE TABLE cluster_dht (
    domain      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    PRIMARY KEY (domain, node_id)
);

-- 集群 Gossip：节点级已知列表（与现有 gossip_known 表并存）
CREATE TABLE cluster_gossip (
    node_id       TEXT NOT NULL,
    known_node_id TEXT NOT NULL,
    domains       TEXT NOT NULL DEFAULT '[]',  -- JSON array
    PRIMARY KEY (node_id, known_node_id)
);
```

现有表（`dht`、`gossip_known`、`agent_cards`、`server_cards` 等）完全保留，继续服务单节点内的 Agent 发现。

---

## 设计原则

- **任务单 owner**：任务在创建节点上完成所有状态机变更，其他节点只做转发
- **发现找节点，不找 Agent**：Bootstrap/DHT/Gossip 返回 node_id，各节点自行管理本地 Agent
- **三层策略独立**：Gossip/DHT/Bootstrap 互不依赖，任一故障不影响其他两个
- **Server 无感知**：Server 看到的 API 完全不变，路由对 Server 透明
- **渐进去中心化**：Bootstrap 是保底，网络成熟后 Gossip + DHT 承担主要发现
- **最终一致**：节点成员列表、Gossip 已知列表允许短暂不一致，通过心跳和 Bootstrap 兜底自愈
