"""File-backed permanent AI learning data.

Learning data is grouped by function under three subdirectories:

``data/ai_learning/ad_detection/``
    AI advertisement-detection verdicts.
``data/ai_learning/assistant/``
    AI management-assistant answers.
``data/ai_learning/chat/``
    AI casual-chat answers.

Each function's data uses one JSONL file per group, an in-memory index built
lazily from those files, exact normalized text matching (whitespace/case
insensitive), and no automatic expiry.  AI assistant and chat answers are
written automatically, just like advertisement verdicts, so repeated questions
are answered locally without spending provider tokens.

The storage root can be changed from the panel.  Legacy ``*.jsonl`` files that
live directly in the root (from before the per-function layout) are split by
their ``kind`` value and migrated into the matching subdirectory automatically.
A failed migration never deletes the source file, and deleting the learning
directory only removes learned shortcuts; it never breaks moderation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import get_config

LEARNING_KINDS: tuple[str, ...] = ("ad_detection", "assistant", "chat")

# The historical internal kind for advertisement detection is ``ad``.  Keep
# that value so existing cache keys remain valid; only its on-disk directory is
# renamed to the clearer ``ad_detection``.
_KIND_ALIASES = {
    "ad": "ad_detection",
    "moderation": "ad_detection",
    "ad_detection": "ad_detection",
    "assistant": "assistant",
    "chat": "chat",
}


def kind_directory(kind: str) -> str:
    """Return the on-disk subdirectory for a learning kind."""
    return _KIND_ALIASES.get((kind or "").strip().lower(), "ad_detection")


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:4000]


def cache_key(kind: str, group_id: int, text: str, extra: str = "") -> str:
    payload = "\x00".join(
        [kind, str(int(group_id or 0)), normalize_text(text), str(extra or "")]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
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
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


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
        # Create the layout and split any legacy root-level JSONL files early so
        # the first write does not mix the old and new structures.
        try:
            self._migrate_legacy_layout()
        except OSError:
            pass

    # -------------------------------------------------------------- layout
    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        """Best-effort inter-process lock for file moves/migrations."""
        try:
            import fcntl  # type: ignore
        except ImportError:  # pragma: no cover - non-POSIX platforms
            yield
            return
        try:
            handle = path.open("a+", encoding="utf-8")
        except OSError:
            yield
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _ensure_layout(self, root: Path | None = None) -> Path:
        root = root or self.directory
        root.mkdir(parents=True, exist_ok=True)
        for name in LEARNING_KINDS:
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def _merge_entries(self, path: Path, items: list[dict[str, Any]]) -> bool:
        """Merge entries into a JSONL file without duplicating cache keys."""
        if not items:
            return True
        merged: dict[str, dict[str, Any]] = {}
        if path.exists():
            for item in _read_jsonl(path):
                key = str(item.get("key") or "")
                if key:
                    merged[key] = item
        for item in items:
            key = str(item.get("key") or "")
            if not key:
                key = cache_key(
                    str(item.get("kind") or "ad"),
                    int(item.get("group_id") or 0),
                    str(item.get("question") or ""),
                    str(item.get("extra") or ""),
                )
                item = {**item, "key": key}
            merged[key] = item
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                for item in merged.values():
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            tmp.replace(path)
            return True
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _migrate_legacy_layout(self, root: Path | None = None) -> None:
        """Split root-level legacy JSONL files into per-function subdirectories."""
        root = self._ensure_layout(root)
        legacy_files = sorted(root.glob("*.jsonl"))
        if not legacy_files:
            return
        with self._file_lock(root / ".migration.lock"):
            for path in sorted(root.glob("*.jsonl")):
                buckets: dict[str, list[dict[str, Any]]] = {
                    name: [] for name in LEARNING_KINDS
                }
                for item in _read_jsonl(path):
                    buckets[kind_directory(str(item.get("kind") or "ad"))].append(item)
                moved = True
                for name, items in buckets.items():
                    if items and not self._merge_entries(root / name / path.name, items):
                        moved = False
                # Keep the source if any destination write failed, so data is
                # never silently discarded.
                if moved:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def _move_data_to(self, target: Path) -> None:
        """Move all current learning files into ``target`` with merging."""
        self._migrate_legacy_layout()
        for name in LEARNING_KINDS:
            src_dir = self.directory / name
            dst_dir = target / name
            for path in sorted(src_dir.glob("*.jsonl")):
                items = list(_read_jsonl(path))
                if not self._merge_entries(dst_dir / path.name, items):
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass

    async def set_directory(self, directory: Path) -> None:
        """Switch the storage root and move existing learning files into it."""
        target = directory.expanduser()
        if not target.is_absolute():
            target = get_config().project_dir / target
        target = target.resolve()
        async with self._lock:
            if target == self.directory.resolve():
                self.directory = target
                self._ensure_layout()
                return
            target.mkdir(parents=True, exist_ok=True)
            for name in LEARNING_KINDS:
                (target / name).mkdir(parents=True, exist_ok=True)
            # Load the old files before moving them so the in-memory index stays
            # complete even if one file cannot be moved.
            self._load()
            if self.directory.is_dir():
                self._move_data_to(target)
            self.directory = target
            self._ensure_layout()
            self._migrate_legacy_layout()
            self._entries = None

    # ------------------------------------------------------------------ load
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict[str, Any]] = {}
        try:
            self._migrate_legacy_layout()
        except OSError:
            pass
        paths: list[Path] = []
        for name in LEARNING_KINDS:
            paths.extend(sorted((self.directory / name).glob("*.jsonl")))
        # Fallback for a failed legacy migration: still load root-level files.
        paths.extend(sorted(self.directory.glob("*.jsonl")))
        for path in paths:
            for item in _read_jsonl(path):
                key = str(item.get("key") or "")
                if key:
                    entries[key] = item
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
                target = self.directory / kind_directory(kind)
                target.mkdir(parents=True, exist_ok=True)
                filename = f"group_{int(group_id)}.jsonl" if group_id else "global.jsonl"
                with (target / filename).open("a", encoding="utf-8") as fh:
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
            kind = kind_directory(str(entry.get("kind") or "ad"))
            by_kind[kind] = by_kind.get(kind, 0) + 1
            total_hits += int(entry.get("hits") or 0)
        return {
            "directory": str(self.directory),
            "entries": len(entries),
            "hits": total_hits,
            "by_kind": by_kind,
            "directories": {
                name: str(self.directory / name) for name in LEARNING_KINDS
            },
        }


learning_store = LearningStore()
