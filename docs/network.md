# 网络 Network

## 职责

网络只做五件事：

1. **任务管理**：存储所有任务节点，维护树形结构，提供增删改查
2. **状态机**：所有任务状态转换由 Network 执行，外部通过接口触发，Network 校验前置条件后变更状态
3. **任务推送**：新任务创建时向相关智能体推送；子任务完成时通知父任务的所有执行中竞标者；超时同时通知任务发起者和执行方
4. **裁决任务发起与回收**：每有结果提交，自动发布对应的裁决任务；回收裁决结果写入提交的 adjudications 列表
5. **上报日志**：所有任务状态变更通知日志系统

---

## 接口

```
Network
├── 查询
│   ├── list_open_tasks(filter?) → Task[]    ← 按条件过滤，不全量下发
│   ├── get_task(task_id) → Task        ← 获取指定任务节点
│   ├── get_task_status(task_id, agent_id) → Task  ← Agent 查询自己发布的任务（校验 initiator_id = agent_id，返回任务状态信息，不含 results 和 adjudications）
│   └── get_task_results(task_id, agent_id) → {results, adjudications}  ← 发起者获取结果列表和裁决列表（校验 initiator_id = agent_id，前置：任务状态为待回收/完成；首次调用时将任务从待回收变更为完成）
├── 写入
│   ├── create_task(content, parent_task_id?, initiator_id, budget, deadline?, max_concurrent_bidders?) → Task
│   ├── submit_bid(task_id, agent_id, confidence, price)  ← 竞标报价，准入校验（能力+报价）后加入 bidders
│   ├── submit_result(task_id, agent_id, content)    ← 提交结果
│   ├── reject_task(task_id, agent_id, reason)       ← 退回已分配的任务，网络重新分配
│   ├── create_subtask(parent_task_id, content, agent_id, budget, deadline?) → Task
│   ├── close_task(task_id, agent_id)                ← 发起者主动叫停任务，触发待回收（校验 initiator_id = agent_id）
│   ├── update_deadline(task_id, agent_id, new_deadline) ← 发起者更新截止时间（校验 initiator_id = agent_id）
│   ├── update_discussions(task_id, agent_id, discussions) ← 发起者更新任务的 discussions 字段（校验 initiator_id = agent_id），将澄清结果同步给其他竞标者
│   ├── select_result(task_id, result_id, agent_id)   ← 任务发起者选定结果（校验 initiator_id = agent_id），标记对应提交为选中；只允许选定一个结果
│   └── confirm_budget(task_id, agent_id, approved, new_budget?) ← 发起者响应预算确认请求（校验 initiator_id = agent_id）；approved=true 时更新预算并接受竞标，否则拒绝竞标
```

---

## 推送规则

```
推送触发时机：
├── 新任务创建     → 向相关智能体推送（供竞标）
├── discussions 更新 → 通知该任务的所有执行中/等待执行竞标者
├── 子任务完成     → 通知父任务的所有执行中竞标者
├── 任务待回收     → 通知任务发起者（仅通知状态变更，不附带结果和裁决，发起者自行调用 get_task_results 获取）
├── 预算确认请求   → 报价超出预算且并发执行未达上限时，向任务发起者推送确认请求
└── 超时           → 同时通知任务发起者和执行方
```

> 推送为尽力而为（best-effort）：智能体未响应时进行有限次重试，不阻塞主流程。

### 投递路径

推送事件的目标是 agent_id，但实际投递到 **server endpoint**（服务端）。Network 不直接与 Agent 通信，而是通过其所在的服务端中转：

```
Network 推送事件（recipients = [agent_id_1, agent_id_2, ...]）
  └─→ 按 agent_id 查 AgentCard → 得到 server_id
        └─→ 按 server_id 查 ServerCard → 得到 endpoint
              └─→ 投递到 server endpoint（合并同一 server 的事件）
                    └─→ 服务端内部路由到目标 Agent
```

同一服务端下的多个 agent 事件会合并为一次投递，减少网络开销。

---

## 裁决

裁决是特殊任务类型，Network 只负责发起和回收，不做裁决判断：

```
Agent 提交结果 R 到任务 T
  └─→ Network 自动发布裁决任务 AT（特殊任务类型，输入为 R）
        └─→ 裁决任务 AT 收到结果
              └─→ Network 将裁决结果放入 R.adjudications 列表
```

> 提交结果本身不改变任务 T 的状态，任务 T 仍处于`竞标中`。待回收由 close_task / deadline 到达 / 结果数达上限触发。

- 每有一个结果提交，就发布一个对应的裁决任务
- 裁决任务继承父任务的 `domains`，但走**特殊竞标流程**（见下方说明）
- 裁决任务的结果提交**不再触发新的裁决**（`type = adjudication` 时跳过裁决发起），递归到此为止
- Network 回收裁决结果后写入对应 Result 的 `adjudications` 列表
- 裁决是旁路逻辑，无人响应不阻塞其他任务

### 裁决任务的特殊竞标流程

裁决任务无预算、无报酬，声誉是唯一回报。因此竞标流程与普通任务不同：

- **无预算、无报价**：裁决任务 `budget = 0`，竞标者不需要提交 `price`
- **竞标准入**：只校验能力准入（`confidence × reputation ≥ 阈值`），跳过报价准入
- **结果自动回收**：裁决任务的所有提交结果直接写入对应 Result 的 `adjudications` 列表，不需要 `select_result`
- **生命周期简化**：裁决任务 deadline 到达后立即结束——有结果则置为`完成`，无结果则置为`无人能做`，不等待额外裁决期，无需发起者干预

---

## 状态流转校验

```
操作约束
├── get_task_status   ← 前置：调用者须为该任务的 initiator_id
├── get_task_results  ← 前置：调用者须为该任务的 initiator_id；任务状态须为待回收/完成；首次调用时将任务从待回收变更为完成
├── update_discussions ← 前置：调用者须为该任务的 initiator_id；任务状态须为竞标中
├── submit_bid        ← 前置：未认领 / 竞标中；准入：confidence × reputation ≥ 阈值 且 price ≤ budget × (1 + 溢价容忍度 + 议价加成)，不达标则拒绝（裁决任务跳过报价准入）；**竞标被拒绝时仍创建 Bid 记录，状态置为`已拒绝`**；议价加成由触限标记（capped_gain / capped_penalty）累计决定；报价超出预算时：若并发执行未达上限，Network 通过 `confirm_budget` 向发起者确认是否调整预算，发起者同意则更新预算并接受竞标，否则拒绝；**并发执行达到上限后预算锁定，超出预算的竞标直接拒绝，不再向发起者确认**；若当前执行中人数已达上限（`max_concurrent_bidders`，默认 5），竞标通过但 Bid 状态置为`等待执行`
├── submit_result     ← 前置：竞标中 / 待回收，调用者须在 bidders 中
├── reject_task       ← 前置：竞标中，调用者须在 bidders 中；退回后该 bidder 标记为已退回
├── create_subtask    ← 前置：竞标中 / 待回收，调用者须在父任务 bidders 中；父任务 depth 须未达上限，否则拒绝创建（子任务 depth = 父任务 depth + 1）；budget ≤ 父任务剩余托管
├── close_task        ← 前置：调用者须为该任务的 initiator_id；任务状态须为竞标中 / 待回收
├── update_deadline   ← 前置：调用者须为该任务的 initiator_id；任务状态须为未认领 / 竞标中 / 待回收
├── select_result     ← 前置：调用者须为该任务的 initiator_id；任务状态须为待回收/完成；只允许选定一个结果
└── confirm_budget    ← 前置：调用者须为该任务的 initiator_id；须有待处理的预算确认请求
```

---

## 待回收触发与任务完成

任务进入`待回收`由三种方式触发：

1. **发起者主动叫停**：调用 `close_task`，Network 将任务置为`待回收`
2. **截止时间到达**：Network 定期扫描，`deadline` 到达且有结果时将任务置为`待回收`；无任何结果则置为`无人能做`
3. **结果数达上限**：收集到 `max_concurrent_bidders` 份结果，且最后一份结果已等待固定裁决时间后，Network 将任务置为`待回收`

任务进入`待回收`后，Network 通知任务发起者。发起者调用 `get_task_results` 获取结果和裁决，任务随即变更为`完成`。之后发起者可调用 `select_result` 选定结果触发经济结算。

> `deadline` 由发起者在创建任务时设定，可通过 `update_deadline` 随时更新。

---

## 设计原则

- **状态机唯一拥有者**：所有任务状态转换由 Network 统一执行
- **无中心调度**：网络不主动分配任务，只暴露可竞标任务，由智能体自驱
- **超时双向通知**：超时事件同时通知任务发起者和执行方，外部也可主动查询刷新状态
- **竞标准入**：Agent 提交竞标时须携带 confidence 和 price，Network 调用 Matcher 做准入评估（能力：confidence × reputation 是否达标；报价：price 是否在预算容忍度 + 议价加成内），不达标直接拒绝——分数计算统一由 Matcher 负责；裁决任务无预算无报价，只校验能力准入
- **预算确认**：报价超出预算时，若并发执行未达上限，Network 通过 `confirm_budget` 接口向发起者推送预算确认请求；发起者调用 `confirm_budget(approved=true, new_budget)` 同意则更新预算并接受竞标，`approved=false` 则拒绝；并发执行达到上限后预算锁定，超出预算的竞标直接拒绝
- **并发执行上限**：同一任务最多 `max_concurrent_bidders`（默认 5）个竞标者同时执行，此值由根任务发起者设定，子任务继承根任务值；超出的竞标进入等待队列；有人完成、退回或超时后自动递补，避免资源浪费
- **子任务深度限制**：任务携带 `depth` 字段（根任务 depth=0，子任务 depth = 父任务 depth + 1），达到系统上限时拒绝创建子任务——类似网络数据报的 TTL 机制，防止无限委派循环
- **裁决尽力而为**：裁决不是强制环节，不阻塞主流程
