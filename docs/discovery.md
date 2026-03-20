# 发现机制 Discovery

## 职责

让 Agent 在不依赖中心注册的情况下自发发现彼此。三个独立模块各司其职，逐步去中心化。

---

## 模块总览

```
discovery/
├── bootstrap.py    # Bootstrap：冷启动，种子节点
├── dht.py          # DHT：domain → {agent_id} 精确查找
├── gossip.py       # Gossip：协作时自然扩散
└── query.py        # DiscoveryService：编排三层查找
```

三个模块独立运行，互不依赖。DiscoveryService 只是编排层，按优先级串联三者。

---

## 模块一：Bootstrap（冷启动）

### 定位

新 Agent / 新 Server 首次加入网络时的入口。提供种子列表，解决"第一个朋友"问题。网络成熟后可不再依赖。

### 职责

- 接收新 Agent 注册，返回同域的种子 Agent 列表
- 接收新 Server 注册，返回网络端连接信息
- 维护全量 AgentCard 存储（兜底查询源）

### 接口

```
Bootstrap
├── register_agent(agent_card) → SeedList
│     ← 校验 AgentCard，持久化，返回同域种子 Agent 列表
├── unregister_agent(agent_id) → void
├── query(domains) → [agent_id, ...]
│     ← 从全量存储中按域过滤（兜底，最慢但最全）
└── get_agent_card(agent_id) → AgentCard
      ← 查询完整 AgentCard（供投递链解析 agent_id → server_id）
```

### 数据

```
存储：全量 AgentCard
  agent_id → AgentCard（含 server_id、domains、skills 等）

这是唯一的 AgentCard 权威存储，DHT 和 Gossip 都不存 AgentCard。
```

---

## 模块二：DHT（域发现）

### 定位

以 `domain` 为键的分布式哈希表，精确查找某个能力域下有哪些 Agent。是最常用的发现路径。

### 职责

- 维护 `domain → {agent_id}` 映射
- Agent 注册/变更时公告，注销时撤销
- Server 离线时批量摘除其下所有 Agent

### 接口

```
DHT
├── announce(domain, agent_id) → void
│     ← 注册或域变更时调用
├── revoke(domain, agent_id) → void
│     ← 注销或域变更时调用
├── revoke_all(agent_id) → void
│     ← Agent 注销时，从所有 domain 中移除
├── revoke_by_server(server_id) → void
│     ← Server 离线时，批量移除该 Server 下所有 Agent
└── lookup(domain) → [agent_id, ...]
      ← 按域查找
```

### 数据

```
DHT 只存映射关系，不存 AgentCard：
  domain → {agent_id, ...}

投递时需要回 Bootstrap 查 AgentCard 才能得到 server_id。
```

### 与 Bootstrap 的配合

```
Agent 注册时（由 DiscoveryService 编排）：
  1. Bootstrap.register_agent(agent_card)    → 存 AgentCard
  2. DHT.announce(domain, agent_id)          → 每个 domain 都公告
  两步独立，任一失败不影响另一个
```

---

## 模块三：Gossip（自然扩散）

### 定位

Agent 在协作过程中互相交换已知 Agent 列表，知识在网络中自然扩散。用得越多，本地缓存越丰富，查 DHT 的次数越少。

### 职责

- 协作结束时，双方交换已知 Agent 列表
- 维护每个 Agent 的已知列表（网络端侧的镜像）
- 提供本地优先的快速查找

### 接口

```
Gossip
├── exchange(agent_a, agent_b) → void
│     ← 协作结束时自动触发，双方互换已知列表
├── get_known(agent_id) → {agent_id, ...}
│     ← 查询某 Agent 的已知列表
└── lookup(agent_id, domain) → [agent_id, ...]
      ← 从该 Agent 的已知列表中按域过滤
```

### 数据

```
每个 Agent 的已知列表：
  agent_id → {known_agent_id, ...}

这是 Agent 本地 Memory 中 agent_profiles 在网络端的镜像。
实际的 agent_profiles 存在 Agent 本地（客户端），
网络端只维护一份副本用于辅助查找。
```

### 扩散机制

```
Agent A 与 Agent B 协作完成
  → Gossip.exchange(A, B)
  → A 认识的人 ∪ B 认识的人 → 双方都知道
  → 下次 A 找 "翻译" 时，先查自己认识的人里有没有
  → 有就直接用，不查 DHT
```

---

## DiscoveryService（编排层）

不是独立模块，是串联三者的查找编排：

```
DiscoveryService
├── 依赖：Gossip、DHT、Bootstrap
├── discover(agent_id, domain) → [agent_id, ...]
│     ← 三层 fallback 查找
├── register(agent_card) → void
│     ← 编排注册流程：Bootstrap 存卡 + DHT 公告
└── unregister(agent_id) → void
      ← 编排注销流程：DHT 撤销 + Bootstrap 删卡
```

### 查找顺序

```
discover(agent_id="A", domain="翻译")

  1. Gossip.lookup("A", "翻译")      ← A 认识的人里有没有能翻译的
     → 命中 → 返回（最快，零网络开销）

  2. DHT.lookup("翻译")              ← 全网谁能翻译
     → 命中 → 返回（精确，O(log N)）

  3. Bootstrap.query(["翻译"])        ← 从全量存储中兜底查
     → 返回（最慢但最全）
```

---

## 推送投递链

发现和投递是两个独立过程。发现只返回 agent_id，投递需要逐层解析地址：

```
discover("翻译") → [agent_id]        （Discovery 负责）

投递时（Push 负责）：
  agent_id → AgentCard → server_id   （Bootstrap.get_agent_card）
           → ServerCard → endpoint   （ServerRegistry 查找）
           → HTTP POST               （投递）
```

三层数据各自独立，互不耦合：
- **DHT**：`domain → {agent_id}`（谁能干这活）
- **AgentCard**：`agent_id → server_id`（agent 属于谁）
- **ServerCard**：`server_id → endpoint`（server 在哪）

---

## 持久化

所有发现模块必须使用数据库持久化，不允许纯内存存储。网络端重启后数据必须完整恢复。

### 数据库表

```sql
-- Bootstrap: AgentCard 权威存储
CREATE TABLE agent_cards (
    agent_id       TEXT PRIMARY KEY,
    server_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    agent_type     TEXT NOT NULL,
    domains        TEXT NOT NULL,          -- JSON array
    skills         TEXT NOT NULL,          -- JSON array
    url            TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_agent_cards_server ON agent_cards(server_id);

-- Bootstrap: ServerCard 注册表
CREATE TABLE server_cards (
    server_id      TEXT PRIMARY KEY,
    version        TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    owner          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'online',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- DHT: domain → agent_id 映射（已有）
CREATE TABLE dht (
    domain         TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    PRIMARY KEY (domain, agent_id)
);

-- Gossip: Agent 已知列表
CREATE TABLE gossip_known (
    agent_id       TEXT NOT NULL,
    known_agent_id TEXT NOT NULL,
    PRIMARY KEY (agent_id, known_agent_id)
);
CREATE INDEX idx_gossip_agent ON gossip_known(agent_id);
```

### 各模块存储对应

| 模块 | 表 | 读写模式 |
|------|----|----------|
| Bootstrap | `agent_cards` + `server_cards` | 读写（权威源） |
| DHT | `dht` | 读写（映射关系） |
| Gossip | `gossip_known` | 读写（已知列表） |
| Push（投递链解析） | `agent_cards` + `server_cards` | 只读 |

### 当前实现差距

| 问题 | 现状 | 应改为 |
|------|------|--------|
| DHT 存储 | 内存 dict，重启丢失 | 使用 `dht` 表（DB 层方法已有，DHT 类未调用） |
| Gossip 存储 | 内存 dict，重启丢失 | 新建 `gossip_known` 表 |
| AgentCard 存储 | 无存储 | 新建 `agent_cards` 表 |
| ServerCard 存储 | 无存储 | 新建 `server_cards` 表 |
| 数据库路径 | 默认 `:memory:` | 生产环境必须指定文件路径 |

---

## 设计原则

- **三模块独立**：Bootstrap、DHT、Gossip 各自独立运行，互不依赖，任一模块故障不影响其他两个
- **全部持久化**：所有发现数据存数据库，不允许纯内存存储，重启后数据完整恢复
- **渐进去中心化**：Bootstrap 只是入口，网络成熟后可完全依赖 DHT + Gossip
- **域标签是索引核心**：DHT 的效率完全依赖 `domains` 的准确性，注册时强制要求非空
- **DHT 只管能力映射**：DHT 只存 domain → agent_id，不存 server 信息；投递路径的解析由推送层负责，与 DHT 解耦
- **本地优先**：Gossip 积累的本地缓存优先使用，减少 DHT 查询
- **AgentCard 单一权威源**：Bootstrap 是 AgentCard 的唯一持久化存储，DHT 和 Gossip 只存 ID 引用
