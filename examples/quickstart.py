"""Minimal example: create a network, register an agent, publish a task."""

import asyncio

from eacn.core.models import (
    Task, AgentCard, AgentType, Skill, ServerCard,
)
from eacn.network.app import Network
from eacn.server.app import Server


async def main():
    # 1. Start network
    network = Network()
    await network.start()

    # 2. Start server
    server_card = ServerCard(
        server_id="s1", version="0.1.0",
        endpoint="http://localhost:9000", owner="demo",
    )
    server = Server(server_card)
    await server.start()

    # 3. Register an agent
    card = AgentCard(
        agent_id="agent-python",
        name="Python Coder",
        agent_type=AgentType.EXECUTOR,
        domains=["coding", "python"],
        skills=[Skill(name="write_python", description="Write Python code")],
        url="http://localhost:8001",
        server_id="s1",
    )
    await server.registry.register(card)
    print(f"Registered: {card.name}")

    # 4. Create a task
    task = Task(
        id="task-001",
        initiator_id="user-1",
        domains=["coding", "python"],
        budget=50.0,
        content={"description": "Write a hello world script"},
    )
    network.task_manager.create(task)
    print(f"Task created: {task.id} ({task.status})")

    # 5. Match agents
    candidates = server.matcher.match_agents(
        task, server.registry.list_agents(),
        scores={"agent-python": 0.8},
    )
    print(f"Matched {len(candidates)} agent(s): {[a.name for a in candidates]}")


if __name__ == "__main__":
    asyncio.run(main())
