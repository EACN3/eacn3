# EACN3 长期运行稳定性审查报告

> 审查日期: 2026-03-22

## 总结

EACN3 架构设计清晰、模块分离合理，但**核心运行时状态全部存在内存中**，数据库层虽已建好但未被利用。这意味着当前版本**不适合长期生产部署**——任何进程重启都会导致全部任务、账户余额、信誉分数丢失。此外存在连接泄漏和内存无界增长问题。

---

## 严重问题 (生产上线前必须修复)

### 1. 任务状态仅存内存，重启即丢失

**位置**: `eacn/network/task_manager.py:24-25`

```python
class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}  # 纯内存！
```

`database.py` 已有 `save_task()` / `load_task()` / `list_tasks()` 方法和 `tasks` 表，但 `TaskManager` 从未调用。`api/app.py` 的 lifespan 启动时也没有从 DB 加载任务。

**影响**: 进程崩溃/重启后，所有进行中的任务（BIDDING、AWAITING_RETRIEVAL 等）全部消失，集群节点可能尝试向不存在的任务提交结果。

**修复建议**:
- `TaskManager` 每次状态变更后调用 `db.save_task()`
- `Network.start()` 时从 DB 加载所有未完成任务
- 实现崩溃恢复：恢复挂起的截止时间

### 2. HTTP Client 每次请求创建新连接

**位置**:
- `eacn/network/cluster/router.py:74-78` — `_post()` 方法
- `eacn/network/cluster/service.py` — `broadcast_task()` 等
- `eacn/network/cluster/bootstrap.py` — `join()` / `leave()`

```python
async def _post(self, url: str, body: dict, timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:  # 每次新建！
        resp = await client.post(url, json=body)
```

**影响**: 每次集群通信都新建 TCP 连接/SSL 会话/连接池。高频任务广播下会导致端口耗尽（单 IP 最多约 65k 端口）、连接重置错误、内存泄漏。

**修复建议**: 在 `ClusterService` 启动时创建全局共享 `httpx.AsyncClient`，所有请求复用，shutdown 时关闭。

### 3. 信誉分数和账户余额仅存内存

**位置**:
- `eacn/network/reputation.py:40-45` — `self._scores`, `self._server_reputation`
- `eacn/network/economy/escrow.py:19-22` — `self._accounts`, `self._task_escrows`

数据库有 `reputation`、`accounts`、`escrow` 表但从未被运行时读写。

**影响**: 重启后所有账户余额归零、信誉分数重置为默认 0.5。无财务审计轨迹，用户可否认付款。

**修复建议**:
- 余额/信誉变更后立即持久化到 DB
- 启动时从 DB 恢复所有账户和信誉状态
- 添加定期全量 flush 作为安全网

---

## 中等问题 (长期部署前修复)

### 4. WebSocket 连接无清理机制

**位置**: `eacn/network/api/websocket.py:86-104`

```python
while True:
    data = await ws.receive_text()  # 无超时，永久阻塞
```

- 无心跳超时：客户端挂起后连接永不释放
- 无最大连接数限制：`_connections` 字典无界增长
- 死连接在 `manager._connections` 中累积

**影响**: 运行数周后，死连接累积导致内存耗尽、服务无法接受新连接。

**修复建议**: 添加 `asyncio.wait_for()` 超时、定期心跳检测、最大连接数限制。

### 5. Push 事件历史无界增长

**位置**: `eacn/network/push.py:41`

```python
self._history: list[PushEvent] = []  # 无限追加，永不清理
```

**影响**: 每天 1 万任务，一年后仅 push history 就占用 3.6+ GB 内存。

**修复建议**: 使用 `collections.deque(maxlen=N)` 或定期持久化到 DB 后清空。

### 6. 无自动过期扫描后台任务

**位置**: `eacn/network/app.py:530` — `scan_deadlines()` 存在但仅通过 API 手动调用

**影响**: 超期任务永久堆积在内存中，不会自动清理或超时。

**修复建议**: 在 `lifespan` 启动时用 `asyncio.create_task()` 创建定时扫描协程。

### 7. 结算操作非原子性

**位置**: `eacn/network/economy/settlement.py:27-63`

```python
def settle(self, ...):
    initiator_id = self.escrow.deduct_for_settlement(task_id, total_deduction)
    executor_account.credit(bid_price)      # 如果这一步失败，上一步已执行
    self.total_fees_collected += platform_fee  # 不在 DB 中
```

三步操作无事务保护。中途崩溃会导致资金凭空消失——已从托管扣款但执行者未收到。`total_fees_collected` 纯内存，重启后归零。

**修复建议**: 使用数据库事务包裹整个结算流程，确保原子性。

---

## 低优先级

### 8. Gossip 知识集 O(n²) 增长

`eacn/network/discovery/gossip.py` 的 `exchange()` 使用集合并集操作，100k 代理时每个知道 ~10k 其他代理 → 10 亿数据库行。

### 9. 数据库单连接

`aiosqlite` 只开一个连接，并发请求通过内部锁序列化。单节点可接受，但集群模式下可能成为瓶颈。

---

## 关键发现：DB 层建好但未使用

| 组件 | DB 表已建 | 运行时使用 DB |
|------|-----------|---------------|
| 任务 (tasks) | ✅ | ❌ |
| 账户 (accounts) | ✅ | ❌ |
| 托管 (escrow) | ✅ | ❌ |
| 信誉 (reputation) | ✅ | ❌ |
| Push 历史 | ✅ | ❌ |
| DHT / 注册 | ✅ | ✅ |
| 日志 (log_entries) | ✅ | ✅ |

---

## 修复优先级建议

1. **立即修复** (P0): 任务/账户/信誉状态持久化到 DB、httpx 连接复用
2. **上线前修复** (P1): 结算原子性、过期扫描后台任务、WebSocket 心跳
3. **规模化前修复** (P2): Push 历史限界、Gossip 知识集限制、DB 连接池
