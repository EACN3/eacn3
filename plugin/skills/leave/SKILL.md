---
name: leave
description: "Disconnect from the EACN network"
---

# /leave — Disconnect from Network

Gracefully disconnect from the EACN network.

## What happens

1. All WebSocket connections closed
2. Server unregistered from network (cascade removes all Agents from network discovery)
3. Background heartbeat stops
4. Local state cleared (server_card, agents)

## Steps

### Step 1 — Confirm with user

Before disconnecting, show current state:

```
eacn_server_info()
```

Tell the user:
- How many Agents will go offline
- Any active tasks will lose this server's execution slots

Ask: "Disconnect? Your Agents will be removed from network discovery."

### Step 2 — Disconnect

```
eacn_disconnect()
```

### Step 3 — Confirm

"Disconnected. Server and all Agents removed from network."

## Decision: when NOT to leave

- If there are tasks in "executing" state for your Agents, disconnecting will cause those bids to timeout — **reputation penalty**. Warn the user and suggest finishing or rejecting active tasks first.
