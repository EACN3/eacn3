# 任务节点 Task

## 结构

```
Task
├── id
├── content (TaskContent)
│   ├── description        ← 任务描述
│   ├── attachments        ← [{type, content}]
│   ├── expected_output    ← {type, description} | null
│   └── discussions        ← [{initiator_id, messages: [{role, message}]}]，澄清结束时由任务发起者决定是否将沟通结果更新到此处，供其他竞标者参考
├── type                   ← normal / adjudication
├── initiator_id           ← 任务发起者（根任务为外部发起方，子任务为创建该子任务的 agent，裁决任务继承被裁决结果所属任务的 initiator_id）
├── server_id              ← 发起者所在服务端（创建任务时由网络端根据 initiator_id 自动填入，推送时直接用此字段定位 endpoint，无需反查 AgentCard）
├── target_result_id       ← 裁决任务专用：被裁决的 Result ID
├── domains                ← string[]，任务所属域标签；任务发起者提供，未提供时由服务端/通信层根据任务内容自动推断
├── status                 ← 未认领/竞标中/待回收/完成/无人能做
├── parent_id
├── child_ids
├── depth                 ← 当前任务在委派链中的深度；根任务 depth=0，每创建子任务 depth+1；达到上限时 Network 拒绝创建子任务
├── human_contact
│   ├── allowed            ← bool，默认 false
│   ├── contact_id         ← 允许时指定可联系的 Human
│   └── timeout_s          ← Human 响应等待超时秒数
├── budget               ← 任务预算，创建时由发起者设定并冻结到托管
├── max_concurrent_bidders ← 最大同时执行竞标者数量，根任务由发起者设定（默认 5），子任务继承根任务值
├── deadline
└── created_at
```

---

## 关联结构

竞标和结果从 Task 中拆出，独立存储：

```
Bid
├── id
├── task_id
├── agent_id
├── server_id              ← 竞标者所在服务端（提交竞标时由网络端自动填入）
├── confidence   ← float, 0.0 ~ 1.0，竞标时提交的置信度
├── price        ← 竞标报价：执行者完成该任务要求的金额
├── status       ← 等待执行/执行中/等待子任务/已提交/已退回/已超时/已拒绝
│                  等待执行：竞标通过但执行中人数已达上限，排队等待
│                  执行中：正在执行任务
│                  等待子任务：竞标者调用 create_subtask 后进入，子任务完成通知到达后回到执行中
│                  已提交：竞标者调用 submit_result 后进入
│                  已退回：竞标者调用 reject_task 后进入
│                  已超时：deadline 到达时仍在执行中/等待子任务的竞标者
│                  已拒绝：竞标准入校验未通过
└── started_at

Result
├── id
├── task_id
├── submitter_id
├── content
├── selected        ← bool
├── adjudications   ← [裁决任务返回的结果列表]
└── submitted_at
```

---

## 竞标模型

**多竞标者、各自独立执行、结果择优。**

- 任意数量的智能体可以对同一任务提交竞标，写入竞标表
- 每个竞标者独立执行，均可提交结果到结果表
- **并发执行上限**：同一任务最多允许 `max_concurrent_bidders` 个竞标者同时处于`执行中`状态（默认 5），此值由根任务发起者设定，子任务继承根任务值；达到上限后，新竞标仍可提交但进入等待队列，有人提交结果、退回或超时后，队列中的下一位进入执行；**达到上限后任务预算锁定，不再接受预算变更，报价超出预算的竞标直接拒绝**
- 任务进入`待回收`由三种方式触发：发起者主动叫停（close_task）、截止时间到达（且有结果）、或收集到 `max_concurrent_bidders` 份结果且最后一份已等待固定裁决时间
- 待回收后 Network 通知任务发起者，发起者调用 `get_task_results` 获取结果和裁决，任务随即变更为`完成`

---

## 生命周期

```
未认领
  ├─→ 竞标中（有智能体提交竞标，可继续接收结果）
  │     ├─→ 待回收（发起者主动叫停 / deadline 到达且有结果 / 结果数达上限且裁决等待期结束）
  │     │     ├─→ 完成（发起者获取结果）
  │     │     └─→ 无人能做（无任何结果 / 所有结果被否决）
  │     └─→ 无人能做（deadline 到达且无任何结果）
  └─→ 无人能做（deadline 到达且无人竞标）
```

---

## 树形结构

- 任务通过 `parent_id` / `child_ids` 组成树
- 子任务由**正在执行父任务的智能体**按需创建并挂载
- 子任务`完成`后，网络向父任务所有`执行中`的竞标者推送变更
- 根任务无父节点，其结果即为整个网络的最终输出
