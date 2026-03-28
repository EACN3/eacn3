"""Async SQLite database layer for EACN network persistence.

Uses aiosqlite with `:memory:` default (or file path).
Provides typed stores for tasks, escrow, reputation, and log entries.
All public methods are async and concurrency-safe via aiosqlite's internal lock.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiosqlite

class Database:
    """Thin async wrapper around aiosqlite with typed stores."""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        # Serialize all write operations to prevent concurrent access errors
        self._write_lock = asyncio.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._create_tables()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    async def _exec_write(self, sql: str, params: tuple | list = ()) -> None:
        """Execute a single write statement under the write lock."""
        async with self._write_lock:
            await self.db.execute(sql, params)
            await self.db.commit()

    async def _exec_write_many(self, sql: str, params_list: list) -> None:
        """Execute many write statements atomically under the write lock."""
        async with self._write_lock:
            await self.db.executemany(sql, params_list)
            await self.db.commit()

    async def _exec_script_write(self, sql: str) -> None:
        """Execute a multi-statement script under the write lock."""
        async with self._write_lock:
            await self.db.executescript(sql)
            await self.db.commit()

    # ── Schema ───────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id             TEXT PRIMARY KEY,
                data           TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'unclaimed',
                initiator_id   TEXT NOT NULL,
                parent_id      TEXT,
                type           TEXT NOT NULL DEFAULT 'normal',
                deadline       TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
            CREATE INDEX IF NOT EXISTS idx_tasks_initiator ON tasks(initiator_id);

            CREATE TABLE IF NOT EXISTS escrow (
                task_id        TEXT PRIMARY KEY,
                initiator_id   TEXT NOT NULL,
                amount         REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS accounts (
                agent_id       TEXT PRIMARY KEY,
                available      REAL NOT NULL DEFAULT 0.0,
                frozen         REAL NOT NULL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS reputation (
                agent_id       TEXT PRIMARY KEY,
                score          REAL NOT NULL DEFAULT 0.5,
                cap_counts     TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS server_reputation (
                server_id      TEXT PRIMARY KEY,
                score          REAL NOT NULL DEFAULT 0.5,
                event_count    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS log_entries (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                fn_name        TEXT NOT NULL,
                args           TEXT NOT NULL DEFAULT '{}',
                result         TEXT,
                timestamp      TEXT NOT NULL,
                error          TEXT,
                task_id        TEXT,
                agent_id       TEXT,
                server_id      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_log_task ON log_entries(task_id);
            CREATE INDEX IF NOT EXISTS idx_log_agent ON log_entries(agent_id);

            CREATE TABLE IF NOT EXISTS dht (
                domain         TEXT NOT NULL,
                agent_id       TEXT NOT NULL,
                PRIMARY KEY (domain, agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_dht_domain ON dht(domain);

            CREATE TABLE IF NOT EXISTS push_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                type           TEXT NOT NULL,
                task_id        TEXT NOT NULL,
                recipients     TEXT NOT NULL,
                payload        TEXT NOT NULL DEFAULT '{}',
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_push_task ON push_history(task_id);

            CREATE TABLE IF NOT EXISTS agent_cards (
                agent_id       TEXT PRIMARY KEY,
                server_id      TEXT NOT NULL,
                network_id     TEXT NOT NULL DEFAULT '',
                name           TEXT NOT NULL,
                tier           TEXT NOT NULL DEFAULT 'general',
                domains        TEXT NOT NULL,
                skills         TEXT NOT NULL,
                url            TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'online',
                last_fetch_at  TEXT NOT NULL DEFAULT (datetime('now')),
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_agent_cards_server ON agent_cards(server_id);

            CREATE TABLE IF NOT EXISTS server_cards (
                server_id      TEXT PRIMARY KEY,
                version        TEXT NOT NULL,
                endpoint       TEXT NOT NULL,
                owner          TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'online',
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS gossip_known (
                agent_id       TEXT NOT NULL,
                known_agent_id TEXT NOT NULL,
                PRIMARY KEY (agent_id, known_agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_gossip_agent ON gossip_known(agent_id);

            -- ═══════════════════════════════════════════════════════
            -- Cluster layer tables (node federation)
            -- ═══════════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS cluster_nodes (
                node_id     TEXT PRIMARY KEY,
                endpoint    TEXT NOT NULL,
                domains     TEXT NOT NULL DEFAULT '[]',
                status      TEXT NOT NULL DEFAULT 'online',
                version     TEXT NOT NULL,
                joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS cluster_task_routes (
                task_id     TEXT PRIMARY KEY,
                origin_node TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cluster_task_participants (
                task_id     TEXT NOT NULL,
                node_id     TEXT NOT NULL,
                PRIMARY KEY (task_id, node_id)
            );

            CREATE TABLE IF NOT EXISTS cluster_dht (
                domain      TEXT NOT NULL,
                node_id     TEXT NOT NULL,
                PRIMARY KEY (domain, node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cluster_dht_domain ON cluster_dht(domain);

            CREATE TABLE IF NOT EXISTS cluster_gossip (
                node_id       TEXT NOT NULL,
                known_node_id TEXT NOT NULL,
                PRIMARY KEY (node_id, known_node_id)
            );

            -- ═══════════════════════════════════════════════════════
            -- Offline message cache (reliable delivery)
            -- ═══════════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS offline_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id      TEXT NOT NULL UNIQUE,
                agent_id    TEXT NOT NULL,
                type        TEXT NOT NULL,
                task_id     TEXT NOT NULL DEFAULT '',
                payload     TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_offline_agent ON offline_messages(agent_id);
            CREATE INDEX IF NOT EXISTS idx_offline_expires ON offline_messages(expires_at);
        """)
        await self.db.commit()
        # Migration: add status + last_fetch_at to agent_cards if missing
        try:
            await self.db.execute(
                "ALTER TABLE agent_cards ADD COLUMN status TEXT NOT NULL DEFAULT 'online'"
            )
            await self.db.commit()
        except Exception:
            pass  # column already exists
        try:
            await self.db.execute(
                "ALTER TABLE agent_cards ADD COLUMN last_fetch_at TEXT NOT NULL DEFAULT (datetime('now'))"
            )
            await self.db.commit()
        except Exception:
            pass  # column already exists

    async def save_task(self, task_id: str, data: dict[str, Any]) -> None:
        """Insert or replace a full task JSON blob."""
        await self._exec_write(
            """INSERT INTO tasks (id, data, status, initiator_id, parent_id, type, deadline)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 data=excluded.data, status=excluded.status, deadline=excluded.deadline""",
            (
                task_id,
                json.dumps(data, ensure_ascii=False),
                data.get("status", "unclaimed"),
                data.get("initiator_id", ""),
                data.get("parent_id"),
                data.get("type", "normal"),
                data.get("deadline"),
            ),
        )

    async def get_task_created_at(self, task_id: str) -> str | None:
        async with self.db.execute(
            "SELECT created_at FROM tasks WHERE id = ?", (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def load_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT data FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return json.loads(row[0]) if row else None

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Update task status atomically.

        Uses a single UPDATE that sets both the indexed column and the JSON
        blob field in one statement, avoiding the race between json_set and
        a concurrent save_task UPSERT (#61).
        """
        await self._exec_write(
            "UPDATE tasks SET status = ?, data = json_set(data, '$.status', ?) WHERE id = ?",
            (status, status, task_id),
        )

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        initiator_id: str | None = None,
        parent_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if initiator_id:
            conditions.append("initiator_id = ?")
            params.append(initiator_id)
        if parent_id is not None:
            conditions.append("parent_id = ?")
            params.append(parent_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        async with self.db.execute(
            f"SELECT data FROM tasks {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [json.loads(row[0]) for row in rows]

    async def find_expired_tasks(self, now: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT data FROM tasks
               WHERE deadline IS NOT NULL AND deadline <= ?
                 AND status NOT IN ('completed', 'no_one_able')""",
            (now,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [json.loads(row[0]) for row in rows]

    async def delete_task(self, task_id: str) -> None:
        await self._exec_write("DELETE FROM tasks WHERE id = ?", (task_id,))

    async def list_all_accounts(self) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT agent_id, available, frozen FROM accounts"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"agent_id": r[0], "available": r[1], "frozen": r[2]}
                for r in rows
            ]

    async def get_account(self, agent_id: str) -> dict[str, float] | None:
        async with self.db.execute(
            "SELECT available, frozen FROM accounts WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return {"available": row[0], "frozen": row[1]} if row else None

    async def upsert_account(
        self, agent_id: str, available: float, frozen: float
    ) -> None:
        await self._exec_write(
            """INSERT INTO accounts (agent_id, available, frozen)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 available=excluded.available, frozen=excluded.frozen""",
            (agent_id, available, frozen),
        )

    async def list_all_escrows(self) -> list[tuple[str, str, float]]:
        async with self.db.execute(
            "SELECT task_id, initiator_id, amount FROM escrow"
        ) as cursor:
            rows = await cursor.fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    async def save_escrow(
        self, task_id: str, initiator_id: str, amount: float
    ) -> None:
        await self._exec_write(
            """INSERT INTO escrow (task_id, initiator_id, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 initiator_id=excluded.initiator_id, amount=excluded.amount""",
            (task_id, initiator_id, amount),
        )

    async def get_escrow(self, task_id: str) -> tuple[str, float] | None:
        async with self.db.execute(
            "SELECT initiator_id, amount FROM escrow WHERE task_id = ?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None

    async def delete_escrow(self, task_id: str) -> None:
        await self._exec_write("DELETE FROM escrow WHERE task_id = ?", (task_id,))

    async def list_all_reputations(self) -> list[tuple[str, float, dict]]:
        async with self.db.execute(
            "SELECT agent_id, score, cap_counts FROM reputation"
        ) as cursor:
            rows = await cursor.fetchall()
            return [(r[0], r[1], json.loads(r[2])) for r in rows]

    async def list_all_server_reputations(self) -> list[tuple[str, float, int]]:
        async with self.db.execute(
            "SELECT server_id, score, event_count FROM server_reputation"
        ) as cursor:
            rows = await cursor.fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    async def get_reputation(self, agent_id: str) -> tuple[float, dict] | None:
        async with self.db.execute(
            "SELECT score, cap_counts FROM reputation WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return (row[0], json.loads(row[1]))

    async def upsert_reputation(
        self, agent_id: str, score: float, cap_counts: dict
    ) -> None:
        await self._exec_write(
            """INSERT INTO reputation (agent_id, score, cap_counts)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 score=excluded.score, cap_counts=excluded.cap_counts""",
            (agent_id, score, json.dumps(cap_counts)),
        )

    async def get_server_reputation(self, server_id: str) -> tuple[float, int] | None:
        async with self.db.execute(
            "SELECT score, event_count FROM server_reputation WHERE server_id = ?",
            (server_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None

    async def upsert_server_reputation(
        self, server_id: str, score: float, event_count: int
    ) -> None:
        await self._exec_write(
            """INSERT INTO server_reputation (server_id, score, event_count)
               VALUES (?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 score=excluded.score, event_count=excluded.event_count""",
            (server_id, score, event_count),
        )

    async def insert_log(
        self,
        fn_name: str,
        timestamp: str,
        *,
        args: dict | None = None,
        result: Any = None,
        error: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        server_id: str | None = None,
    ) -> None:
        await self._exec_write(
            """INSERT INTO log_entries
               (fn_name, args, result, timestamp, error, task_id, agent_id, server_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fn_name,
                json.dumps(args or {}),
                json.dumps(result) if result is not None else None,
                timestamp,
                error,
                task_id,
                agent_id,
                server_id,
            ),
        )

    async def query_logs(
        self,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        fn_name: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if fn_name:
            conditions.append("fn_name = ?")
            params.append(fn_name)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with self.db.execute(
            f"SELECT fn_name, args, result, timestamp, error, task_id, agent_id, server_id "
            f"FROM log_entries {where} ORDER BY id DESC LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "fn_name": r[0],
                    "args": json.loads(r[1]),
                    "result": json.loads(r[2]) if r[2] else None,
                    "timestamp": r[3],
                    "error": r[4],
                    "task_id": r[5],
                    "agent_id": r[6],
                    "server_id": r[7],
                }
                for r in rows
            ]

    async def dht_announce(self, domain: str, agent_id: str) -> None:
        await self._exec_write(
            "INSERT OR IGNORE INTO dht (domain, agent_id) VALUES (?, ?)",
            (domain, agent_id),
        )

    async def dht_revoke(self, domain: str, agent_id: str) -> None:
        await self._exec_write(
            "DELETE FROM dht WHERE domain = ? AND agent_id = ?",
            (domain, agent_id),
        )

    async def dht_lookup(self, domain: str) -> list[str]:
        async with self.db.execute(
            "SELECT agent_id FROM dht WHERE domain = ?", (domain,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def insert_push(
        self,
        event_type: str,
        task_id: str,
        recipients: list[str],
        payload: dict[str, Any],
    ) -> None:
        await self._exec_write(
            "INSERT INTO push_history (type, task_id, recipients, payload) VALUES (?, ?, ?, ?)",
            (event_type, task_id, json.dumps(recipients), json.dumps(payload)),
        )

    async def get_push_history(
        self, task_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if task_id:
            sql = "SELECT type, task_id, recipients, payload FROM push_history WHERE task_id = ? ORDER BY id DESC LIMIT ?"
            params: tuple = (task_id, limit)
        else:
            sql = "SELECT type, task_id, recipients, payload FROM push_history ORDER BY id DESC LIMIT ?"
            params = (limit,)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "type": r[0],
                    "task_id": r[1],
                    "recipients": json.loads(r[2]),
                    "payload": json.loads(r[3]),
                }
                for r in rows
            ]

    async def save_agent_card(self, card: dict[str, Any]) -> None:
        await self._exec_write(
            """INSERT INTO agent_cards
               (agent_id, server_id, network_id, name, tier, domains, skills, url, description, status, last_fetch_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', datetime('now'))
               ON CONFLICT(agent_id) DO UPDATE SET
                 server_id=excluded.server_id, network_id=excluded.network_id,
                 name=excluded.name, tier=excluded.tier,
                 domains=excluded.domains, skills=excluded.skills,
                 url=excluded.url, description=excluded.description,
                 status='online', last_fetch_at=datetime('now')""",
            (
                card["agent_id"],
                card["server_id"],
                card.get("network_id", ""),
                card["name"],
                card.get("tier", "general"),
                json.dumps(card["domains"]),
                json.dumps(card["skills"]),
                card["url"],
                card.get("description", ""),
            ),
        )

    async def get_agent_card(self, agent_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT agent_id, server_id, network_id, name, tier, domains, skills, url, description, status FROM agent_cards WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "agent_id": row[0],
                "server_id": row[1],
                "network_id": row[2],
                "name": row[3],
                "tier": row[4],
                "domains": json.loads(row[5]),
                "skills": json.loads(row[6]),
                "url": row[7],
                "description": row[8],
                "status": row[9],
            }

    async def touch_agent_fetch(self, agent_id: str) -> None:
        """Update last_fetch_at and mark agent online (called on every event poll)."""
        await self._exec_write(
            "UPDATE agent_cards SET last_fetch_at = datetime('now'), status = 'online' WHERE agent_id = ?",
            (agent_id,),
        )

    async def touch_agents_by_server(self, server_id: str) -> int:
        """Refresh last_fetch_at for ALL agents belonging to a server.

        Called on server heartbeat — if the server is alive, its agents are
        reachable. Prevents the liveness scanner from killing agents just
        because the host LLM hasn't polled events recently (e.g. busy
        executing a long-running task).

        Returns the number of agents refreshed.
        """
        async with self._write_lock:
            await self.db.execute(
                "UPDATE agent_cards SET last_fetch_at = datetime('now'), status = 'online' WHERE server_id = ? AND status IN ('online', 'offline')",
                (server_id,),
            )
            changed = self.db.total_changes
            await self.db.commit()
            return changed

    async def set_agent_status(self, agent_id: str, status: str) -> None:
        await self._exec_write(
            "UPDATE agent_cards SET status = ? WHERE agent_id = ?",
            (status, agent_id),
        )

    async def mark_agent_offline_if_still_stale(self, agent_id: str, timeout_seconds: int) -> bool:
        """Atomically mark agent offline ONLY if last_fetch_at is still stale.
        Prevents race where agent polls between scan and update."""
        async with self._write_lock:
            await self.db.execute(
                """UPDATE agent_cards SET status = 'offline'
                   WHERE agent_id = ? AND status = 'online'
                     AND last_fetch_at < datetime('now', ? || ' seconds')""",
                (agent_id, f"-{timeout_seconds}"),
            )
            changed = self.db.total_changes
            await self.db.commit()
            return changed > 0

    async def scan_stale_agents(self, timeout_seconds: int) -> list[dict[str, str]]:
        """Find agents whose last_fetch_at is older than timeout_seconds.
        Returns list of {agent_id, server_id, status} for agents that should go offline."""
        async with self.db.execute(
            """SELECT agent_id, server_id, status FROM agent_cards
               WHERE status = 'online'
                 AND last_fetch_at < datetime('now', ? || ' seconds')""",
            (f"-{timeout_seconds}",),
        ) as cursor:
            return [{"agent_id": r[0], "server_id": r[1], "status": r[2]} async for r in cursor]

    async def count_online_agents_by_server(self, server_id: str) -> int:
        """Count how many agents on a server are still online."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM agent_cards WHERE server_id = ? AND status = 'online'",
            (server_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def filter_online_agents(self, agent_ids: list[str]) -> list[str]:
        """Return only agent_ids that have status='online'."""
        if not agent_ids:
            return []
        placeholders = ",".join("?" for _ in agent_ids)
        async with self.db.execute(
            f"SELECT agent_id FROM agent_cards WHERE agent_id IN ({placeholders}) AND status = 'online'",
            agent_ids,
        ) as cursor:
            return [r[0] async for r in cursor]

    async def delete_agent_card(self, agent_id: str) -> None:
        await self._exec_write(
            "DELETE FROM agent_cards WHERE agent_id = ?", (agent_id,),
        )

    async def query_agent_cards_by_domain(self, domain: str) -> list[dict[str, Any]]:
        # Use json_each for exact matching instead of LIKE to prevent wildcard injection (#63)
        async with self.db.execute(
            """SELECT agent_id, server_id, network_id, name, tier, domains, skills, url, description
               FROM agent_cards
               WHERE EXISTS (
                   SELECT 1 FROM json_each(agent_cards.domains) WHERE json_each.value = ?
               )""",
            (domain,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "agent_id": r[0], "server_id": r[1], "network_id": r[2],
                    "name": r[3], "tier": r[4],
                    "domains": json.loads(r[5]), "skills": json.loads(r[6]),
                    "url": r[7], "description": r[8],
                }
                for r in rows
            ]

    async def get_agent_ids_by_server(self, server_id: str) -> list[str]:
        async with self.db.execute(
            "SELECT agent_id FROM agent_cards WHERE server_id = ?",
            (server_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def save_server_card(
        self, server_id: str, version: str, endpoint: str, owner: str, status: str = "online",
    ) -> None:
        await self._exec_write(
            """INSERT INTO server_cards (server_id, version, endpoint, owner, status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 version=excluded.version, endpoint=excluded.endpoint,
                 owner=excluded.owner, status=excluded.status""",
            (server_id, version, endpoint, owner, status),
        )

    async def get_server_card(self, server_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT server_id, version, endpoint, owner, status FROM server_cards WHERE server_id = ?",
            (server_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "server_id": row[0], "version": row[1], "endpoint": row[2],
                "owner": row[3], "status": row[4],
            }

    async def update_server_status(self, server_id: str, status: str) -> None:
        await self._exec_write(
            "UPDATE server_cards SET status = ? WHERE server_id = ?",
            (status, server_id),
        )

    async def delete_server_card(self, server_id: str) -> None:
        await self._exec_write(
            "DELETE FROM server_cards WHERE server_id = ?", (server_id,),
        )

    async def dht_revoke_all(self, agent_id: str) -> None:
        await self._exec_write(
            "DELETE FROM dht WHERE agent_id = ?", (agent_id,),
        )

    async def dht_revoke_by_server(self, server_id: str) -> None:
        await self._exec_write(
            """DELETE FROM dht WHERE agent_id IN
               (SELECT agent_id FROM agent_cards WHERE server_id = ?)""",
            (server_id,),
        )

    async def gossip_add(self, agent_id: str, known_agent_id: str) -> None:
        await self._exec_write(
            "INSERT OR IGNORE INTO gossip_known (agent_id, known_agent_id) VALUES (?, ?)",
            (agent_id, known_agent_id),
        )

    async def gossip_add_many(self, agent_id: str, known_ids: set[str]) -> None:
        if not known_ids:
            return
        await self._exec_write_many(
            "INSERT OR IGNORE INTO gossip_known (agent_id, known_agent_id) VALUES (?, ?)",
            [(agent_id, kid) for kid in known_ids],
        )

    async def gossip_get_known(self, agent_id: str) -> set[str]:
        async with self.db.execute(
            "SELECT known_agent_id FROM gossip_known WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}

    async def gossip_remove(self, agent_id: str) -> None:
        await self._exec_write(
            "DELETE FROM gossip_known WHERE agent_id = ? OR known_agent_id = ?",
            (agent_id, agent_id),
        )

    async def cluster_save_node(self, node: dict[str, Any]) -> None:
        await self._exec_write(
            """INSERT INTO cluster_nodes
               (node_id, endpoint, domains, status, version, joined_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                 endpoint=excluded.endpoint, domains=excluded.domains,
                 status=excluded.status, version=excluded.version,
                 last_seen=excluded.last_seen""",
            (
                node["node_id"],
                node["endpoint"],
                json.dumps(node.get("domains", [])),
                node.get("status", "online"),
                node.get("version", "0.1.0"),
                node.get("joined_at", ""),
                node.get("last_seen", ""),
            ),
        )

    async def cluster_get_node(self, node_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT node_id, endpoint, domains, status, version, joined_at, last_seen "
            "FROM cluster_nodes WHERE node_id = ?",
            (node_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "node_id": row[0], "endpoint": row[1],
                "domains": json.loads(row[2]), "status": row[3],
                "version": row[4], "joined_at": row[5], "last_seen": row[6],
            }

    async def cluster_get_all_nodes(self) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT node_id, endpoint, domains, status, version, joined_at, last_seen "
            "FROM cluster_nodes"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "node_id": r[0], "endpoint": r[1],
                    "domains": json.loads(r[2]), "status": r[3],
                    "version": r[4], "joined_at": r[5], "last_seen": r[6],
                }
                for r in rows
            ]

    async def cluster_remove_node(self, node_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_nodes WHERE node_id = ?", (node_id,),
        )

    async def cluster_update_node_status(self, node_id: str, status: str) -> None:
        await self._exec_write(
            "UPDATE cluster_nodes SET status = ? WHERE node_id = ?",
            (status, node_id),
        )

    async def cluster_set_route(self, task_id: str, origin_node: str) -> None:
        await self._exec_write(
            """INSERT INTO cluster_task_routes (task_id, origin_node)
               VALUES (?, ?)
               ON CONFLICT(task_id) DO UPDATE SET origin_node=excluded.origin_node""",
            (task_id, origin_node),
        )

    async def cluster_get_route(self, task_id: str) -> str | None:
        async with self.db.execute(
            "SELECT origin_node FROM cluster_task_routes WHERE task_id = ?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def cluster_remove_route(self, task_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_task_routes WHERE task_id = ?", (task_id,),
        )

    async def cluster_add_participant(self, task_id: str, node_id: str) -> None:
        await self._exec_write(
            "INSERT OR IGNORE INTO cluster_task_participants (task_id, node_id) VALUES (?, ?)",
            (task_id, node_id),
        )

    async def cluster_get_participants(self, task_id: str) -> set[str]:
        async with self.db.execute(
            "SELECT node_id FROM cluster_task_participants WHERE task_id = ?",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}

    async def cluster_remove_participants(self, task_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_task_participants WHERE task_id = ?",
            (task_id,),
        )

    async def cluster_dht_store(self, domain: str, node_id: str) -> None:
        await self._exec_write(
            "INSERT OR IGNORE INTO cluster_dht (domain, node_id) VALUES (?, ?)",
            (domain, node_id),
        )

    async def cluster_dht_revoke(self, domain: str, node_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_dht WHERE domain = ? AND node_id = ?",
            (domain, node_id),
        )

    async def cluster_dht_revoke_all(self, node_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_dht WHERE node_id = ?", (node_id,),
        )

    async def cluster_dht_lookup(self, domain: str) -> list[str]:
        async with self.db.execute(
            "SELECT node_id FROM cluster_dht WHERE domain = ?", (domain,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def cluster_gossip_add(self, node_id: str, known_node_id: str) -> None:
        await self._exec_write(
            "INSERT OR IGNORE INTO cluster_gossip (node_id, known_node_id) VALUES (?, ?)",
            (node_id, known_node_id),
        )

    async def cluster_gossip_add_many(self, node_id: str, known_ids: set[str]) -> None:
        if not known_ids:
            return
        await self._exec_write_many(
            "INSERT OR IGNORE INTO cluster_gossip (node_id, known_node_id) VALUES (?, ?)",
            [(node_id, kid) for kid in known_ids],
        )

    async def cluster_gossip_get_known(self, node_id: str) -> set[str]:
        async with self.db.execute(
            "SELECT known_node_id FROM cluster_gossip WHERE node_id = ?",
            (node_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}

    async def cluster_gossip_remove(self, node_id: str) -> None:
        await self._exec_write(
            "DELETE FROM cluster_gossip WHERE node_id = ? OR known_node_id = ?",
            (node_id, node_id),
        )

    # ── Offline message cache ────────────────────────────────────────

    async def offline_store(
        self,
        msg_id: str,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
        expires_at: str | None = None,
    ) -> None:
        """Store a message for an offline agent."""
        await self._exec_write(
            """INSERT OR IGNORE INTO offline_messages
               (msg_id, agent_id, type, task_id, payload, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (msg_id, agent_id, event_type, task_id,
             json.dumps(payload, ensure_ascii=False), expires_at),
        )

    async def offline_drain(self, agent_id: str) -> list[dict[str, Any]]:
        """Retrieve and delete all pending offline messages for an agent.

        Returns messages ordered oldest-first. Expired messages are pruned.
        Uses specific IDs to avoid deleting messages inserted between SELECT and DELETE (#59).
        """
        async with self._write_lock:
            # Prune expired
            await self.db.execute(
                "DELETE FROM offline_messages WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')",
            )
            # Fetch with IDs for precise deletion
            async with self.db.execute(
                """SELECT id, msg_id, type, task_id, payload, created_at
                   FROM offline_messages
                   WHERE agent_id = ?
                   ORDER BY id ASC""",
                (agent_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                await self.db.commit()
                return []
            # Delete only the rows we fetched (raw execute, already inside lock)
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" for _ in ids)
            await self.db.execute(
                f"DELETE FROM offline_messages WHERE id IN ({placeholders})", ids,
            )
            await self.db.commit()

        return [
            {
                "msg_id": r[1],
                "type": r[2],
                "task_id": r[3],
                "payload": json.loads(r[4]),
                "created_at": r[5],
            }
            for r in rows
        ]

    async def offline_count(self, agent_id: str) -> int:
        """Count pending offline messages for an agent."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM offline_messages WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def offline_count_all(self) -> dict[str, int]:
        """Count pending offline messages grouped by agent."""
        async with self.db.execute(
            "SELECT agent_id, COUNT(*) FROM offline_messages GROUP BY agent_id",
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}

    async def offline_delete_by_task(self, task_id: str) -> int:
        """Delete offline messages for a specific task (#80). Atomic."""
        async with self._write_lock:
            async with self.db.execute(
                "SELECT COUNT(*) FROM offline_messages WHERE task_id = ?", (task_id,),
            ) as cursor:
                row = await cursor.fetchone()
                count = row[0] if row else 0
            if count:
                await self.db.execute(
                    "DELETE FROM offline_messages WHERE task_id = ?", (task_id,),
                )
                await self.db.commit()
            return count

    async def offline_prune_overflow(self, agent_id: str, max_per_agent: int) -> int:
        """Delete oldest messages exceeding the per-agent cap. Returns count deleted.

        Atomic: count + delete under the same lock to prevent race with concurrent stores.
        """
        async with self._write_lock:
            async with self.db.execute(
                "SELECT COUNT(*) FROM offline_messages WHERE agent_id = ?",
                (agent_id,),
            ) as cursor:
                row = await cursor.fetchone()
                total = row[0] if row else 0
            if total <= max_per_agent:
                return 0
            overflow = total - max_per_agent
            await self.db.execute(
                """DELETE FROM offline_messages WHERE id IN (
                    SELECT id FROM offline_messages
                    WHERE agent_id = ? ORDER BY id ASC LIMIT ?
                )""",
                (agent_id, overflow),
            )
            await self.db.commit()
            return overflow
