"""ServerCard and ServerStatus data models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ServerStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNREGISTERED = "unregistered"


class ServerCard(BaseModel):
    server_id: str
    version: str
    endpoint: str
    owner: str
    status: ServerStatus = ServerStatus.ONLINE
