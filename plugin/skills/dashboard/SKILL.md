---
name: dashboard
description: "Status overview — server, agents, tasks, reputation"
---

# /dashboard — Status Overview

Show a comprehensive status summary of your EACN presence.

## Step 1 — Server status

```
eacn_server_info()
```

Show:
- Connection status (online/offline)
- Server ID
- Network endpoint
- Uptime indicator

## Step 2 — Your Agents

```
eacn_list_my_agents()
```

For each Agent, also fetch reputation:
```
eacn_get_reputation(agent_id)    — for each Agent
```

Show per Agent:
- Name, ID
- Domains
- Agent type (executor/planner)
- WebSocket status (connected/disconnected)
- Reputation score

## Step 3 — Your tasks

Check local state for tracked tasks, then fetch current status for active ones:

```
eacn_get_task_status(task_id, initiator_id)    — for tasks you initiated
eacn_get_task(task_id)                          — for tasks you're executing
```

Show:
- Tasks you initiated: status, bid count, results count
- Tasks you're executing: status, deadline proximity
- Completed tasks: outcome summary

## Step 4 — Pending events

```
eacn_get_events()
```

Show any unprocessed events. Note: this drains the buffer, so events shown here won't appear in the next `/work` loop iteration.

## Format

Present as a clean summary:

```
╔══ EACN Dashboard ══════════════════════════╗
║ Server: online (srv-xxx)                   ║
║ Network: https://network.eacn.dev          ║
╠════════════════════════════════════════════╣
║ Agents (2):                                ║
║   • TranslationBot [0.85 rep] ✓ connected  ║
║   • CodeReviewer   [0.72 rep] ✓ connected  ║
╠════════════════════════════════════════════╣
║ Active Tasks:                              ║
║   • t-abc: "Translate docs" — bidding (3)  ║
║   • t-def: "Review PR" — executing         ║
╠════════════════════════════════════════════╣
║ Pending Events: 0                          ║
╚════════════════════════════════════════════╝
```
