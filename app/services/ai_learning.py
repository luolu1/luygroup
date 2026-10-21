"""File-backed permanent AI learning data.

Successful AI answers and ad-detection verdicts are stored as JSONL files under
``data/ai_learning/``. Repeating the same question/ad in the same group is then
answered from this local learning data, which saves provider tokens and reduces
latency.

The data is intentionally simple and dependency-free:

* one JSONL file per group;
* an in-memory index built lazily from those files;
* exact normalized text matching (whitespace/case insensitive);
* no automatic expiry: learned entries remain long-term training data;
* the storage directory can be changed from the panel, and existing files are
  moved into the new directory automatically.

It is a learning store, not a source of truth for moderation rules. Deleting the
directory only removes learned shortcuts and never breaks the bot.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ..config import get_config


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:4000]


def cache_key(kind: str, group_id: int, text: str, extra: str = "") -> str:
    payload = "\x00".join(
        [kind, str(int(group_id or 0)), normalize_text(text), str(extra or "")]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LearningStore:
    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            cfg = get_config()
            configured = (cfg.ai.learning_dir or "").strip()
            if configured:
                directory = Path(configured).expanduser()
                if not directory.is_absolute():
                    directory = cfg.project_dir / directory
                directory = directory.resolve()
            else:
                directory = cfg.project_dir / "data" / "ai_learning"
        self.directory = directory
        self._entries: dict[str, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def set_directory(self, directory: Path) -> None:
        """Switch the storage directory and move existing JSONL files into it."""
        target = directory.expanduser()
        if not target.is_absolute():
            target = get_config().project_dir / target
        target = target.resolve()
        async with self._lock:
            if target == self.directory.resolve():
                self.directory = target
                return
            target.mkdir(parents=True, exist_ok=True)
            # Load the old files before moving them so the in-memory index stays
            # complete even if one file cannot be moved.
            self._load()
            if self.directory.is_dir():
                for path in sorted(self.directory.glob("*.jsonl")):
                    dest = target / path.name
                    try:
                        if dest.exists():
                            with path.open("r", encoding="utf-8") as src, dest.open(
                                "a", encoding="utf-8"
                            ) as out:
                                shutil.copyfileobj(src, out)
                            path.unlink()
                        else:
                            shutil.move(str(path), str(dest))
                    except OSError:
                        continue
            self.directory = target
            self._entries = None

    # ------------------------------------------------------------------ load
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._entries = entries
            return entries
        for path in sorted(self.directory.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        key = str(item.get("key") or "")
                        if key:
                            entries[key] = item
            except OSError:
                continue
        self._entries = entries
        return entries

    # ------------------------------------------------------------------- get
    def get(
        self,
        kind: str,
        group_id: int,
        text: str,
        *,
        extra: str = "",
    ) -> dict[str, Any] | None:
        entries = self._load()
        key = cache_key(kind, group_id, text, extra)
        entry = entries.get(key)
        if not entry:
            return None
        entry["hits"] = int(entry.get("hits") or 0) + 1
        return entry

    # ------------------------------------------------------------------- put
    async def put(
        self,
        kind: str,
        group_id: int,
        text: str,
        answer: str,
        *,
        extra: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not (answer or "").strip():
            return
        async with self._lock:
            entries = self._load()
            key = cache_key(kind, group_id, text, extra)
            previous = entries.get(key) or {}
            entry: dict[str, Any] = {
                "key": key,
                "kind": kind,
                "group_id": int(group_id or 0),
                "question": (text or "")[:3000],
                "answer": (answer or "")[:6000],
                "extra": str(extra or "")[:500],
                "provider": str((meta or {}).get("provider") or ""),
                "created_at": time.time(),
                "hits": int(previous.get("hits") or 0),
            }
            if meta:
                for name, value in meta.items():
                    if name not in entry:
                        entry[name] = value
            entries[key] = entry
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                filename = f"group_{int(group_id)}.jsonl" if group_id else "global.jsonl"
                with (self.directory / filename).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                # A read-only/full disk must never break moderation.
                pass

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        entries = self._load()
        by_kind: dict[str, int] = {}
        total_hits = 0
        for entry in entries.values():
            kind = str(entry.get("kind") or "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            total_hits += int(entry.get("hits") or 0)
        return {
            "directory": str(self.directory),
            "entries": len(entries),
            "hits": total_hits,
            "by_kind": by_kind,
        }

learning_store = LearningStore()
