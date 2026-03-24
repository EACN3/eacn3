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

### 16. Subtask escrow 归属错误 — 退款打给执行者而非发起者
- **文件**: `eacn/network/economy/escrow.py:102-130`
- **问题**: `allocate_subtask_budget()` 创建子任务 escrow 时存储的是 `subtask_initiator_id`（执行者），
  而非原始付款人（父任务发起者）。`release()` 退款时调用执行者的 `unfreeze()`，但执行者从未冻结过资金。
- **修复**: escrow 元组应保留原始发起者 ID，或在 release 时追溯到父任务的真实付款人。

### 17. `unfreeze()` 金额超额时静默截断
- **文件**: `eacn/network/economy/account.py:22-26`
- **问题**: `unfreeze(amount)` 当 amount > frozen 时静默将 amount 截为 frozen，不抛异常。
  调用方以为退了全额，实际少退了。
- **修复**: 超额时应 raise ValueError 而非静默截断。

### 18. Settlement 非原子 — 崩溃导致双重支付或退款丢失
- **文件**: `eacn/network/economy/settlement.py:27-64`
- **问题**: `settle()` 先 persist 执行者付款，再 release 发起者退款。中间崩溃后重启：
  执行者已拿到钱（已持久化），escrow 仍在 DB 中，重试会再付一次。
- **修复**: 用事务包裹整个结算流程，或引入幂等结算 key 防止重复执行。

### 19. Deadline 字符串比较导致 `Z` 后缀的截止时间永不过期
- **文件**: `eacn/network/task_manager.py:268-277`
- **问题**: `scan_expired()` 用字符串 `<=` 比较 deadline 和 now。`now` 格式为 `+00:00`，
  而用户可能传 `Z` 后缀。ASCII 中 `Z`(90) > `+`(43)，导致比较永远为 False。
- **修复**: 用 `datetime.fromisoformat()` 解析后比较，或统一归一化为同一格式。

### 20. Deadline 过期不级联关闭子任务 — 子任务 escrow 永久冻结
- **文件**: `eacn/network/app.py:629-650`
- **问题**: `scan_deadlines()` 过期父任务时只退父任务 escrow，从不调用 `_terminate_children()`。
  子任务及其 escrow 永久留存。对比：手动 `close_task()` (L462) 会级联关闭。
- **修复**: 在 `handle_expired()` 后添加 `await self._terminate_children(task)` 调用。

### 21. 并发 `select_result` 可导致执行者被双重支付
- **文件**: `eacn/network/app.py:423-432`, `economy/settlement.py:27-64`
- **问题**: `select_result()` 无互斥锁。两个并发请求可同时通过验证并各调用一次 `settle()`，
  导致执行者被 credit 两次，发起者 escrow 被扣两次。
- **修复**: 在 select_result 入口加 per-task asyncio.Lock，或在 settlement 层做幂等检查。

### 22. Adjudication 结果在父任务完成竞态中丢失
- **文件**: `eacn/network/app.py:316-330, 347-366`
- **问题**: `submit_result` 先检查父任务未终止（L320），再收集 adjudication（L358）。
  两步之间父任务可被 `select_result` 关闭，adjudication 被附加到已完成的 parent 上，
  但 `collect_results()` 已执行，adjudication 永远不会被返回。
- **修复**: 在 collect_adjudication_result 前重新检查父任务状态，或用锁保护。

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

### 23. Peer 路由全部无认证 — 任意节点可伪造 bid/result/subtask
- **文件**: `eacn/network/api/peer_routes.py:132-284`
- **问题**: 所有 `/peer/*` 端点无认证。恶意节点可 `POST /peer/task/bid` 伪造任意 agent 的出价，
  `POST /peer/join` 加入集群，`DELETE /peer/dht/revoke` 破坏 DHT。
- **修复**: 添加节点间 HMAC 签名或 mTLS 认证。

### 24. Admin config 端点无认证 — 可注入恶意超参数
- **文件**: `eacn/network/api/routes.py:511-547`
- **问题**: `PUT /admin/config` 无认证，任何人可设置 `platform_fee_rate=1.0`（100% 抽成）
  或 `default_reputation=-10`，彻底破坏经济系统。
- **修复**: 添加 admin API key 或 RBAC 认证。

### 25. IDOR — 任意调用方可修改/删除其他人的 agent 和 server
- **文件**: `eacn/network/api/discovery_routes.py:142-184` (update_agent), `75-96` (unregister_server)
- **问题**: `PUT /api/discovery/agents/{id}` 不验证所有权，攻击者可劫持任意 agent 的 domains/url。
  `DELETE /api/discovery/servers/{id}` 不验证归属，可级联删除别人的整个 server 及其所有 agent。
- **修复**: 验证调用方身份与资源归属关系。

### 26. `add_result()` 不验证提交者是否为活跃竞标者
- **文件**: `eacn/network/task_manager.py:138-143`
- **问题**: `TaskManager.add_result()` 只检查任务状态，不验证 `result.agent_id` 是否有活跃 bid。
  虽然 `app.py` 层做了验证，但 TaskManager 作为核心层缺少防护，直接调用可被绕过。
- **修复**: 在 TaskManager 层也加 bidder 校验。

### 27. `get_subtree()` 无环检测 — 循环引用导致栈溢出
- **文件**: `eacn/network/task_manager.py:307-313`
- **问题**: 递归遍历子树无 visited set，若 parent-child 出现循环引用（并发修改可导致），
  会无限递归直到 RecursionError。
- **修复**: 添加 visited set 参数防止重复访问。

### 28. `select_result()` 不转移任务状态 — 状态机违约
- **文件**: `eacn/network/task_manager.py:145-162`
- **问题**: 文档说"transitions task"，但实际只更新 bid 状态，不改 task.status。
  Network 层补偿了这一点，但 TaskManager 自身违反了契约，直接调用会导致状态不一致。
- **修复**: 在 select_result 末尾添加 `task.status = TaskStatus.COMPLETED`。

### 29. 并发竞标可突破 max_concurrent_bidders 槽位限制
- **文件**: `eacn/network/task_manager.py:93-102`
- **问题**: 检查 `concurrent_slots_full` 和 `bids.append()` 之间无锁，
  两个并发请求都读到"未满"并各自获得 EXECUTING 状态，实际超出限制。
- **修复**: 用 asyncio.Lock per-task 保护 add_bid 的检查-修改序列。

### 30. Burst 检测 off-by-one — 恰好 threshold 次事件不触发
- **文件**: `eacn/network/reputation.py:235-245`
- **问题**: `same_type_count > self.BURST_THRESHOLD` 用 `>`，恰好等于阈值（默认 8）时不触发。
  攻击者可提交恰好 8 次相同事件获取声誉提升而不被检测。
- **修复**: 改为 `>=`。

### 31. Cluster 路由表仅在内存 — 节点重启后路由丢失
- **文件**: `eacn/network/cluster/router.py:41-65`
- **问题**: `ClusterRouter._routes` 字典仅内存维护，`set_route()` 不写 DB。
  节点崩溃重启后路由表为空，对端仍认为本节点拥有某任务，发来的请求找不到任务。
- **修复**: 在 set_route 时持久化到 DB，启动时恢复。

### 32. 跨节点 Push 事件丢失无重试无持久化
- **文件**: `eacn/network/cluster/router.py:144-150`
- **问题**: `forward_push()` 调用 `_broadcast_to_nodes()` 时异常被静默吞掉（L134-135）。
  与 WebSocket 层的 offline_store 不同，跨节点 push 无持久化层，网络抖动即永久丢失。
- **修复**: 添加跨节点消息持久化队列和重试机制。

### 33. 重复 result 提交未阻止 — 同一 agent 可多次提交结果
- **文件**: `eacn/network/app.py:331-343`
- **问题**: `submit_result` 验证 agent 有活跃 bid，但不检查该 agent 是否已提交过 result。
  同一 agent 可多次提交，导致 results 列表中有重复项，影响 adjudication 和选择逻辑。
- **修复**: 检查 `task.results` 中是否已有同一 agent_id 的记录。

### 34. Adjudication target result 未找到时静默跳过
- **文件**: `eacn/network/adjudication.py:81-87`
- **问题**: `collect_adjudication_result` 遍历 parent results 查找 target agent，
  未找到时循环正常结束，返回 Adjudication 对象（暗示成功），但实际从未挂载到任何 result 上。
- **修复**: 未找到时应抛异常或返回 None 告知调用方。

### 35. 配置数值字段无边界校验 — 可注入负数/极端值
- **文件**: `eacn/network/config.py:31-107`
- **问题**: `platform_fee_rate` 可为负（平台倒贴）、`ack_timeout` 可为负（立即超时）、
  `offline_ttl_seconds` 可为负（消息立即过期）、`offline_max_per_agent` 可为负（无上限）。
  均无 Pydantic Field 约束。
- **修复**: 添加 `Field(ge=0)` / `Field(ge=0, le=1)` 等约束。

### 36. 模型层关键数值字段缺少约束
- **文件**: `eacn/core/models/task.py:73,89,90,94`
- **问题**: `timeout_s`、`depth`、`max_depth`、`max_concurrent_bidders` 均无 `ge=0` 约束，
  允许负值。`max_concurrent_bidders=-1` 导致 `concurrent_slots_full` 永远为 False。
  `budget` 和 `price` 用 `ge=0` 允许零值，经济上无意义。
- **修复**: depth/timeout 加 `ge=0`；max_concurrent_bidders 加 `ge=1`；budget/price 改为 `gt=0`。

### 37. Schema 层用 str 而非 Enum — 非法枚举值延迟到运行时才报错
- **文件**: `eacn/network/api/schemas.py:29,51,98`, `eacn/network/app.py:94`
- **问题**: 请求 schema 中 `level: str` 而非 `TaskLevel`。非法值如 `"invalid"` 通过 schema 验证，
  到 `app.py:94` 的 `TaskLevel(level)` 才抛 ValueError，但此时 escrow 可能已冻结。
- **修复**: schema 中直接用 `TaskLevel | None` 类型，让 Pydantic 在入口校验。

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

### 38. Gossip 自引用 — 三角交换后节点将自己加入已知列表
- **文件**: `eacn/network/cluster/gossip.py:60-84`
- **问题**: A↔B↔C 三角 gossip 后，C 告诉 A "我认识 A"，`handle_exchange` 将 A 存入自己的已知列表。
  后续域名查询返回自己作为候选节点，任务转发尝试发给自己。
- **修复**: 在 `handle_exchange` 中过滤掉 `local_node_id`。

### 39. 跨节点状态通知不幂等 — 重试导致 agent 收到重复推送
- **文件**: `eacn/network/cluster/service.py:222-254`, `eacn/network/app.py:671-689`
- **问题**: `handle_status_notification` 每次调用都创建新 PushEvent（不同 msg_id），
  网络重试导致 agent 收到多份相同内容但不同 ID 的通知。
- **修复**: 用 task_id + status 作为幂等 key，重复请求跳过。

### 40. Adjudication 任务无清理机制 — 内存无限增长
- **文件**: `eacn/network/task_manager.py:29-46`
- **问题**: 完成的 adjudication 任务永远留在 `_tasks` 字典中。大规模运行后
  （100K 任务 × 3 adjudication = 300K 对象）内存持续增长直到 OOM。
- **修复**: 完成后定期清理已终止的 adjudication 任务，或引入 TTL 淘汰机制。

### 41. DB 连接 / HTTP 客户端在启动失败时泄漏
- **文件**: `eacn/network/api/app.py:26-32`, `eacn/network/cluster/service.py:72-92`
- **问题**: `network.start()` 失败时，已打开的 DB 连接和 httpx.AsyncClient 不会被关闭
  （lifespan 未到达 yield，不执行 shutdown 逻辑）。
- **修复**: 用 try/except 包裹 startup，失败时显式关闭已创建资源。

### 42. 全局 `_network` 引用无启动完成门控
- **文件**: `eacn/network/api/routes.py:26-37`, `eacn/network/api/app.py:24-87`
- **问题**: `set_network()` 在 lifespan 末尾才调用，但 FastAPI 可能在此之前已接受请求。
  请求处理器调用 `get_network()` 返回 None 或部分初始化的 Network 对象。
- **修复**: 添加 startup_complete 事件，路由中间件在完成前返回 503。

### 43. `cluster/status` 端点泄露集群拓扑信息
- **文件**: `eacn/network/api/routes.py:476-508`
- **问题**: 无认证即可获取所有节点 ID、端点地址、agent 数量、seed 节点列表。
  攻击者可据此定向攻击高价值节点或 seed 节点。
- **修复**: 添加认证或限制返回字段。

### 44. Reputation 事件端点无速率限制
- **文件**: `eacn/network/api/routes.py:380-385`
- **问题**: `POST /reputation/events` 无速率限制，攻击者可极速提交大量正面事件
  将声誉刷到 1.0。burst 检测的 off-by-one（#30）进一步降低了检测效果。
- **修复**: 添加 per-agent 速率限制。

### 45. Depth 校验 off-by-one — 实际允许 max_depth+1 层
- **文件**: `eacn/network/task_manager.py:178-183`
- **问题**: `new_depth > parent.max_depth` 应为 `>=`。max_depth=10 时允许 depth 0-10 共 11 层。
- **修复**: 改为 `new_depth >= parent.max_depth`。

### 46. Domains 列表允许空字符串元素
- **文件**: `eacn/core/models/task.py:85`
- **问题**: `domains: list[str] = Field(min_length=1)` 只校验列表非空，
  不校验元素内容。`domains=["", ""]` 通过验证，破坏域名匹配逻辑。
- **修复**: 添加 `@field_validator` 确保每个元素非空。

### 47. PushEvent recipients 列表允许空字符串 ID
- **文件**: `eacn/core/models/push_event.py:32-33`
- **问题**: `recipients: list[str] = Field(min_length=1)` 同上，允许 `[""]`。
- **修复**: 同 #46。

### 48. 环境变量 `EACN3_DB_PATH` 无路径校验
- **文件**: `eacn/network/api/app.py:96-98`
- **问题**: 直接使用环境变量作为 DB 路径，无路径遍历防护。
  攻击者设 `EACN3_DB_PATH=../../sensitive.db` 可在任意位置创建文件。
- **修复**: 校验路径无 `..`，限制在预期目录内。

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

### 49. Subtask escrow 全流程无测试
- 无测试覆盖 subtask 创建 → 结算 → 退款的完整 escrow 流转，尤其是退款归属问题（#16）。

### 50. 负数/零值模型字段无测试
- 无测试验证 `budget=0`、`max_concurrent_bidders=-1`、`depth=-5` 等边界值的行为。

### 51. Deadline 时区格式混合无测试
- 无测试混用 `Z` 和 `+00:00` 后缀验证 `scan_expired()` 的正确性（#19）。

### 52. 并发 select_result 无测试
- 无测试覆盖两个并发请求同时选择同一任务结果的竞态（#21）。

### 53. Gossip 三角/环形拓扑无测试
- 无测试覆盖 3+ 节点的循环 gossip 交换，无法发现自引用问题（#38）。

### 54. 节点重启后路由恢复无测试
- 无测试验证节点崩溃重启后 cluster 路由表是否正确恢复（#31）。

### 55. 非法枚举值通过 API 提交无测试
- 无测试验证 `level="invalid"` 等非法值通过 API 后的错误处理（#37）。

### 56. 同一 agent 重复提交 result 无测试
- 无测试验证同一 agent 对同一任务多次调用 submit_result 的行为（#33）。

### 57. 启动失败资源清理无测试
- 无测试验证 `network.start()` 失败时 DB 连接和 HTTP 客户端是否正确关闭（#41）。

### 58. Adjudication 任务内存增长无测试
- 无测试监控大量 adjudication 完成后 `_tasks` 字典的大小变化（#40）。
