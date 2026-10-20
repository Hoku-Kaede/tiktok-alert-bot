from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class WatchTarget:
    username: str
    channel_id: int
    guild_id: int
    notify_posts: bool = True
    notify_live: bool = True
    last_post_id: Optional[str] = None
    live_room_id: Optional[str] = None
    consecutive_errors: int = 0

    @property
    def key(self) -> str:
        return f"{self.guild_id}:{self.username.lower()}"


@dataclass
class Watchlist:
    path: Path
    targets: dict[str, WatchTarget] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: Path) -> "Watchlist":
        targets: dict[str, WatchTarget] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = []
            for item in raw:
                try:
                    target = WatchTarget(**item)
                except TypeError:
                    continue
                targets[target.key] = target
        return cls(path=path, targets=targets)

    def save(self) -> None:
        with self._lock:
            data = [asdict(t) for t in self.targets.values()]
            self.path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )

    def add(self, target: WatchTarget) -> None:
        with self._lock:
            self.targets[target.key] = target
        self.save()

    def remove(self, guild_id: int, username: str) -> Optional[WatchTarget]:
        key = f"{guild_id}:{username.lower()}"
        with self._lock:
            target = self.targets.pop(key, None)
        self.save()
        return target

    def get(self, guild_id: int, username: str) -> Optional[WatchTarget]:
        return self.targets.get(f"{guild_id}:{username.lower()}")

    def for_guild(self, guild_id: int) -> list[WatchTarget]:
        return [t for t in self.targets.values() if t.guild_id == guild_id]

    def all(self) -> list[WatchTarget]:
        return list(self.targets.values())
