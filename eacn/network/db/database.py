"""Async SQLite database layer for EACN network persistence.

Uses aiosqlite with `:memory:` default (or file path).
Provides typed stores for tasks, escrow, reputation, and log entries.
All public methods are async and concurrency-safe via aiosqlite's internal lock.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite


class Database:
    """Thin async wrapper around aiosqlite with typed stores."""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

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
                agent_type     TEXT NOT NULL,
                domains        TEXT NOT NULL,
                skills         TEXT NOT NULL,
                url            TEXT NOT NULL,
                description    TEXT NOT NULL DEFAULT '',
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
        """)
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Task store
    # ══════════════════════════════════════════════════════════════════

    async def save_task(self, task_id: str, data: dict[str, Any]) -> None:
        """Insert or replace a full task JSON blob."""
        await self.db.execute(
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
        await self.db.commit()

    async def load_task(self, task_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT data FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return json.loads(row[0]) if row else None

    async def update_task_status(self, task_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE tasks SET status = ?, data = json_set(data, '$.status', ?) WHERE id = ?",
            (status, status, task_id),
        )
        await self.db.commit()

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
        await self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Account store
    # ══════════════════════════════════════════════════════════════════

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
        await self.db.execute(
            """INSERT INTO accounts (agent_id, available, frozen)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 available=excluded.available, frozen=excluded.frozen""",
            (agent_id, available, frozen),
        )
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Escrow store
    # ══════════════════════════════════════════════════════════════════

    async def save_escrow(
        self, task_id: str, initiator_id: str, amount: float
    ) -> None:
        await self.db.execute(
            """INSERT INTO escrow (task_id, initiator_id, amount)
               VALUES (?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                 initiator_id=excluded.initiator_id, amount=excluded.amount""",
            (task_id, initiator_id, amount),
        )
        await self.db.commit()

    async def get_escrow(self, task_id: str) -> tuple[str, float] | None:
        async with self.db.execute(
            "SELECT initiator_id, amount FROM escrow WHERE task_id = ?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None

    async def delete_escrow(self, task_id: str) -> None:
        await self.db.execute("DELETE FROM escrow WHERE task_id = ?", (task_id,))
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Reputation store
    # ══════════════════════════════════════════════════════════════════

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
        await self.db.execute(
            """INSERT INTO reputation (agent_id, score, cap_counts)
               VALUES (?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 score=excluded.score, cap_counts=excluded.cap_counts""",
            (agent_id, score, json.dumps(cap_counts)),
        )
        await self.db.commit()

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
        await self.db.execute(
            """INSERT INTO server_reputation (server_id, score, event_count)
               VALUES (?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 score=excluded.score, event_count=excluded.event_count""",
            (server_id, score, event_count),
        )
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Log store
    # ══════════════════════════════════════════════════════════════════

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
        await self.db.execute(
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
        await self.db.commit()

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

    # ══════════════════════════════════════════════════════════════════
    # DHT store
    # ══════════════════════════════════════════════════════════════════

    async def dht_announce(self, domain: str, agent_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO dht (domain, agent_id) VALUES (?, ?)",
            (domain, agent_id),
        )
        await self.db.commit()

    async def dht_revoke(self, domain: str, agent_id: str) -> None:
        await self.db.execute(
            "DELETE FROM dht WHERE domain = ? AND agent_id = ?",
            (domain, agent_id),
        )
        await self.db.commit()

    async def dht_lookup(self, domain: str) -> list[str]:
        async with self.db.execute(
            "SELECT agent_id FROM dht WHERE domain = ?", (domain,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    # ══════════════════════════════════════════════════════════════════
    # Push history store
    # ══════════════════════════════════════════════════════════════════

    async def insert_push(
        self,
        event_type: str,
        task_id: str,
        recipients: list[str],
        payload: dict[str, Any],
    ) -> None:
        await self.db.execute(
            "INSERT INTO push_history (type, task_id, recipients, payload) VALUES (?, ?, ?, ?)",
            (event_type, task_id, json.dumps(recipients), json.dumps(payload)),
        )
        await self.db.commit()

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

    # ══════════════════════════════════════════════════════════════════
    # AgentCard store
    # ══════════════════════════════════════════════════════════════════

    async def save_agent_card(self, card: dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO agent_cards
               (agent_id, server_id, network_id, name, agent_type, domains, skills, url, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 server_id=excluded.server_id, network_id=excluded.network_id,
                 name=excluded.name, agent_type=excluded.agent_type,
                 domains=excluded.domains, skills=excluded.skills,
                 url=excluded.url, description=excluded.description""",
            (
                card["agent_id"],
                card["server_id"],
                card.get("network_id", ""),
                card["name"],
                card["agent_type"],
                json.dumps(card["domains"]),
                json.dumps(card["skills"]),
                card["url"],
                card.get("description", ""),
            ),
        )
        await self.db.commit()

    async def get_agent_card(self, agent_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT agent_id, server_id, network_id, name, agent_type, domains, skills, url, description FROM agent_cards WHERE agent_id = ?",
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
                "agent_type": row[4],
                "domains": json.loads(row[5]),
                "skills": json.loads(row[6]),
                "url": row[7],
                "description": row[8],
            }

    async def delete_agent_card(self, agent_id: str) -> None:
        await self.db.execute(
            "DELETE FROM agent_cards WHERE agent_id = ?", (agent_id,),
        )
        await self.db.commit()

    async def query_agent_cards_by_domain(self, domain: str) -> list[dict[str, Any]]:
        async with self.db.execute(
            """SELECT agent_id, server_id, network_id, name, agent_type, domains, skills, url, description
               FROM agent_cards
               WHERE domains LIKE ?""",
            (f'%"{domain}"%',),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "agent_id": r[0], "server_id": r[1], "network_id": r[2],
                    "name": r[3], "agent_type": r[4],
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

    # ══════════════════════════════════════════════════════════════════
    # ServerCard store
    # ══════════════════════════════════════════════════════════════════

    async def save_server_card(
        self, server_id: str, version: str, endpoint: str, owner: str, status: str = "online",
    ) -> None:
        await self.db.execute(
            """INSERT INTO server_cards (server_id, version, endpoint, owner, status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(server_id) DO UPDATE SET
                 version=excluded.version, endpoint=excluded.endpoint,
                 owner=excluded.owner, status=excluded.status""",
            (server_id, version, endpoint, owner, status),
        )
        await self.db.commit()

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
        await self.db.execute(
            "UPDATE server_cards SET status = ? WHERE server_id = ?",
            (status, server_id),
        )
        await self.db.commit()

    async def delete_server_card(self, server_id: str) -> None:
        await self.db.execute(
            "DELETE FROM server_cards WHERE server_id = ?", (server_id,),
        )
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # DHT store (extended)
    # ══════════════════════════════════════════════════════════════════

    async def dht_revoke_all(self, agent_id: str) -> None:
        await self.db.execute(
            "DELETE FROM dht WHERE agent_id = ?", (agent_id,),
        )
        await self.db.commit()

    async def dht_revoke_by_server(self, server_id: str) -> None:
        await self.db.execute(
            """DELETE FROM dht WHERE agent_id IN
               (SELECT agent_id FROM agent_cards WHERE server_id = ?)""",
            (server_id,),
        )
        await self.db.commit()

    # ══════════════════════════════════════════════════════════════════
    # Gossip store
    # ══════════════════════════════════════════════════════════════════

    async def gossip_add(self, agent_id: str, known_agent_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO gossip_known (agent_id, known_agent_id) VALUES (?, ?)",
            (agent_id, known_agent_id),
        )
        await self.db.commit()

    async def gossip_add_many(self, agent_id: str, known_ids: set[str]) -> None:
        for kid in known_ids:
            await self.db.execute(
                "INSERT OR IGNORE INTO gossip_known (agent_id, known_agent_id) VALUES (?, ?)",
                (agent_id, kid),
            )
        await self.db.commit()

    async def gossip_get_known(self, agent_id: str) -> set[str]:
        async with self.db.execute(
            "SELECT known_agent_id FROM gossip_known WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {r[0] for r in rows}

    async def gossip_remove(self, agent_id: str) -> None:
        await self.db.execute(
            "DELETE FROM gossip_known WHERE agent_id = ? OR known_agent_id = ?",
            (agent_id, agent_id),
        )
        await self.db.commit()
