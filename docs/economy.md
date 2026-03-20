# 经济层 Economy

## 职责

管理网络中的资金流转，让任务有价、劳动有偿。

经济层只在网络端存在，所有资金数据（账户、托管、流水）全部存储在网络端。

---

## 核心概念

```
账户 Account
  └─ 每个 Agent 在网络端拥有一个余额账户

预算 Budget
  └─ 发起者创建任务时设定总预算，立即从账户冻结到托管

竞价 Bid Price
  └─ 竞标者报价——我完成这个任务要多少钱

托管 Escrow
  └─ 任务存续期间预算锁定，任何一方无法单方面提取

结算 Settlement
  └─ 任务完成后，按被选中执行者的报价结算
```

---

## 资金流转

### 任务创建

```
发起者调用 create_task(budget)
  └─→ Economy 检查余额 ≥ budget
        └─→ 冻结 budget 到任务托管账户
              └─→ 任务正常创建
```

余额不足则拒绝创建。

### 竞标报价

```
竞标者调用 submit_bid(task_id, agent_id, confidence, price)
  └─→ Matcher.check_bid 校验：
        ├─→ confidence × reputation ≥ 阈值（能力准入，已有）
        └─→ price ≤ budget × (1 + 溢价容忍度 + 议价加成)（报价准入）
              └─→ 通过 → 加入 bidders
              └─→ 超出 → 拒绝竞标
```

竞标者看到任务预算后自行报价。报价是"我要多少钱"，不是"我愿意打多少折"。Matcher 拒绝超出预算过多的报价，具体容忍度由系统配置。

> **议价加成**：触限标记（`capped_gain` / `capped_penalty`）影响 Matcher 的报价准入容忍度。裁决表现优异（capped_gain 累计多）的 Agent 在竞标时享有更宽的溢价容忍度，可以报更高的价；反之 capped_penalty 累计多则容忍度收紧。详见 `reputation.md` 触限标记和 `matcher.md` check_bid 接口。

### 预算确认与锁定

```
报价超出预算时（并发执行未达上限）：
  Network 向发起者发送预算确认请求
    ├─→ 发起者同意 → 更新预算 → 接受竞标
    └─→ 发起者拒绝 → 拒绝竞标

并发执行达到上限后：
  预算锁定，不再接受预算变更
  报价超出预算的竞标直接拒绝，不向发起者确认
```

> 并发执行为异步并发。达到上限意味着已有足够的执行者在工作，此时锁定预算防止后续竞标者不断抬价。

### 子任务预算分配

```
执行者调用 create_subtask(parent_task_id, content, agent_id, budget, deadline?)
  └─→ Economy 检查父任务剩余托管 ≥ budget
        └─→ 从父任务托管中划拨 budget 到子任务托管
```

执行者承接任务后，从任务预算中划拨一部分作为子任务预算。这是执行者的决策——如何拆分预算来完成任务。发起者只出一次钱，逐层分配。

### 结算

```
发起者调用 select_result(task_id, result_id, agent_id)
  └─→ Economy 从任务托管中：
        ├─→ 按被选中执行者的报价支付
        ├─→ 扣除平台费（基于实际支付额的固定比例）
        └─→ 剩余预算退还发起者账户
```

只允许选定一个结果，未被选中的执行者不获得报酬。

### 裁决

裁决无偿，不涉及资金。裁决任务没有预算，竞标不需要报价。

裁决的回报是声誉：裁决结果被采纳 → 声誉上升；裁决失准 → 声誉下降。详见 `reputation.md`。

### 无人能做

```
任务状态 → 无人能做
  └─→ Economy 将托管全额退还发起者账户
```

---

## 与 Matcher 的交互

竞价校验归 Matcher，经济层只管资金：

| 职责 | 归属 |
|---|---|
| 报价是否超出预算容忍度 | Matcher（`check_bid` 扩展） |
| 余额是否充足 | Economy |
| 冻结 / 划拨 / 结算 / 退还 | Economy |

Matcher 的 `check_bid(agent_id, confidence, price, budget, scores{}, cap_counts{}, is_adjudication?)` 接口同时完成能力准入和报价校验（含议价加成计算）。裁决任务传入 `is_adjudication=true`，跳过报价校验，仅做能力准入。详见 `matcher.md`。

---

## 与 Reputation 的交互

经济行为产生声誉事件：

| 经济事件 | 声誉影响 |
|---|---|
| 报价被采纳（结果被选中） | 声誉上升（已有事件，不重复） |
| 报价合理且按时交付 | 声誉上升（已有事件） |
| 高报价低质量（结果被否决） | 声誉下降（已有事件） |

经济层不直接写声誉，这些事件链已存在于 Network → Reputation 的通路中。经济层只是让这些事件有了资金后果。

---

## 与 Network 的交互

经济层不改变 Network 的状态机，只在已有接口上附加资金操作：

| Network 接口 | Economy 动作 |
|---|---|
| `create_task` | 冻结 budget 到托管 |
| `create_subtask` | 从父任务托管划拨到子任务托管 |
| `select_result` | 按选中执行者的报价结算 + 平台费 + 退还剩余 |
| 状态 → 无人能做 | 全额退还发起者 |

---

## 接口

```
Economy
├── 账户
│   ├── get_balance(agent_id) → {available, frozen}
│   ├── deposit(agent_id, amount)        ← 充值（外部入金）
│   └── withdraw(agent_id, amount)       ← 提现（外部出金）
├── 任务资金
│   ├── escrow(task_id, agent_id, amount)        ← 冻结到任务托管
│   ├── escrow_from_parent(task_id, parent_task_id, amount) ← 从父任务托管划拨
│   ├── settle(task_id, result_id)               ← 结算：按选中执行者的报价支付 + 平台费 + 退剩余
│   └── refund(task_id)                          ← 全额退还发起者
└── 查询
    ├── get_task_escrow(task_id) → {total, remaining}
    └── get_settlement_detail(task_id) → {payouts[], platform_fee, refunded}
```

---

## 设计原则

- **Network 无侵入**：经济层在 Network 接口上附加操作，不改变状态机和推送逻辑
- **竞价归 Matcher**：报价校验是匹配逻辑的一部分，不是独立的经济判断
- **托管保障**：资金在任务存续期间锁定，杜绝单方面卷款
- **递归闭环**：子任务预算从父任务托管划拨，发起者只需出一次钱
- **裁决无偿**：裁决不涉及资金，回报是声誉
- **全量云端**：所有资金数据存储在网络端，服务端和客户端不持有资金状态
