# EACN Network — TODO

## CRITICAL: 状态一致性 Bug

### 1. `create_task` escrow 泄漏
- **文件**: `eacn/network/app.py:83-97`
- **问题**: `freeze_budget()` 成功后，`task_manager.create()` 可能抛异常（如重复 task_id），
  冻结的预算永远不会释放。
- **修复**: 用 try/except 包裹 L84-97，失败时调用 `escrow.release()` 退还冻结金额。

### 2. `create_subtask` 状态不一致
- **文件**: `eacn/network/app.py:605-618`
- **问题**: `task_manager.create_subtask()` 已修改 parent 的 `remaining_budget`（task_manager.py:208），
  但后续 `escrow.allocate_subtask_budget()` 失败时不会回滚 task_manager 的状态。
- **修复**: 在 escrow 分配失败时回滚 parent.remaining_budget 并移除已创建的 subtask。

### 3. `select_result` 结算失败无回滚
- **文件**: `eacn/network/app.py:423-432`
- **问题**: `task_manager.select_result()` 已将 bid 标记 ACCEPTED 并 reject 其他 bid，
  但 `settlement.settle()` 抛异常后 bid 状态不会恢复，导致"选中但未支付"。
- **修复**: 用 try/except 包裹 settlement.settle()，失败时回滚 bid 状态或至少通知 initiator。

### 4. `confirm_budget` 部分失败
- **文件**: `eacn/network/app.py:547-551`
- **问题**: `escrow.confirm_budget_increase()` 成功但后续 `task.budget = new_budget` 前
  出异常时，escrow 增加了但 task 对象不知道。
- **修复**: 将 escrow 操作放在状态更新之后，或用 try/except 在 escrow 成功后确保 task 更新不会失败。

### 5. `deposit` 和 `admin/fund` 不持久化
- **文件**: `eacn/network/api/routes.py:416`, `routes.py:562`
- **问题**: `account.credit()` 之后没有调用 `await escrow._persist_account()`，
  服务重启后充值金额丢失。
- **修复**: 在 credit() 之后添加 `await net.escrow._persist_account(req.agent_id)`。

---

## HIGH: 代码质量 & 健壮性

### 6. `invite_agent` 用字符串比较状态枚举
- **文件**: `eacn/network/app.py:248`
- **问题**: `task.status.value not in ("unclaimed", "bidding")` 应使用枚举常量比较，
  如果枚举 value 改名会静默失效。
- **修复**: 改为 `task.status not in (TaskStatus.UNCLAIMED, TaskStatus.BIDDING)`。

### 7. Adjudication 异常被静默吞掉
- **文件**: `eacn/network/app.py:365-366`
- **问题**: `except TaskError: _log.debug(...)` 会隐藏所有 TaskError，包括真正的 bug，
  不仅仅是 "parent not found"。
- **修复**: 只 catch 特定条件（如 task_id not found），其余异常应该 re-raise 或 warning 级别日志。

### 8. Adjudication score 没有范围校验
- **文件**: `eacn/network/app.py:354`
- **问题**: `score = float(content.get("score", 1.0))` 允许负值或超大值，
  可被利用进行声誉攻击。
- **修复**: 添加 `score = max(0.0, min(score, 1.0))` 或在 schema 层校验。

---

## MEDIUM: 功能缺失

### 9. 缺少 escrow 明细查询接口
- **问题**: `GET /economy/balance` 只返回 available + frozen 总额，
  客户端无法知道"哪个任务冻结了多少"。
- **修复**: 新增 `GET /economy/escrows?agent_id=xxx` 返回按任务分列的冻结明细。

### 10. 集群广播失败不重试
- **文件**: `eacn/network/app.py:100-109`
- **问题**: `cluster.broadcast_task()` 失败时，任务只在本地节点可见，其他节点永远不知道。
- **修复**: 添加重试机制或失败队列；考虑 gossip 协议补偿。

### 11. Adjudication 收集缺少幂等性
- **文件**: `eacn/network/app.py:358-364`
- **问题**: `collect_adjudication_result()` 没有检查是否已收集过同一个 adjudicator 的结果，
  重复调用会导致 adjudication 被计数两次。
- **修复**: 在 collect 前检查是否已存在该 adjudicator 的记录。

---

## LOW: 测试覆盖率缺口

### 12. 结算失败场景无测试
- 无测试覆盖 settlement.settle() 失败后系统状态是否一致。

### 13. 并发操作无测试
- 无测试覆盖同时对同一任务进行 bid/select/settle 的并发竞态。

### 14. 跨节点故障场景测试不足
- 大部分跨节点测试使用 mock httpx，未覆盖真实网络故障。

### 15. Deadline 扫描部分失败无测试
- 扫描多个过期任务时部分退款失败的场景未覆盖。
