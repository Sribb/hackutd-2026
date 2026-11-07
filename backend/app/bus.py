"""Realtime hub: every screen (web console, phones, field devices) connects to /ws.

Pattern adapted from CauldronWatch's WebSocketManager (backend/api/websocket.py):
one manager, typed broadcast messages. Added client roles and presence so the
console can show which phones are connected, and so a phone can drive the big screen.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

ROLES = {"web", "phone", "device", "other"}


@dataclass
class Client:
    id: str
    ws: WebSocket
    role: str = "web"
    name: str = ""
    connected_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {"id": self.id, "role": self.role, "name": self.name, "connected_at": self.connected_at}


class Hub:
    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self._ids = itertools.count(1)
        self.seq = 0

    async def connect(self, ws: WebSocket, role: str, name: str) -> Client:
        await ws.accept()
        role = role if role in ROLES else "other"
        c = Client(id=f"c{next(self._ids)}", ws=ws, role=role, name=name or role)
        self.clients[c.id] = c
        return c  # caller sends hello first, then announces presence

    async def disconnect(self, c: Client) -> None:
        if self.clients.pop(c.id, None) is not None:
            await self.broadcast("presence", self.presence())

    def presence(self) -> dict:
        return {"clients": [c.public() for c in self.clients.values()]}

    def envelope(self, typ: str, data: dict, meta: dict | None = None) -> dict:
        self.seq += 1
        msg = {"type": typ, "seq": self.seq, "data": data}
        if meta:
            msg.update(meta)
        return msg

    async def send(self, c: Client, typ: str, data: dict, meta: dict | None = None) -> None:
        try:
            await c.ws.send_text(json.dumps(self.envelope(typ, data, meta), default=str))
        except Exception:
            self.clients.pop(c.id, None)

    async def broadcast(self, typ: str, data: dict, meta: dict | None = None,
                        roles: set[str] | None = None) -> None:
        if not self.clients:
            return
        text = json.dumps(self.envelope(typ, data, meta), default=str)
        targets = [c for c in list(self.clients.values()) if roles is None or c.role in roles]
        results = await asyncio.gather(*(c.ws.send_text(text) for c in targets), return_exceptions=True)
        dead = [c for c, r in zip(targets, results) if isinstance(r, Exception)]
        for c in dead:
            self.clients.pop(c.id, None)
        if dead:
            await self.broadcast("presence", self.presence())
