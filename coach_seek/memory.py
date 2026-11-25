"""SQLite-backed team facts scoped to a Discord guild."""

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

MAX_FACT = 400
MAX_MEMORIES = 6
CREDENTIAL = re.compile(
    r"\b(?:sk|ghp|github_pat)[_-][\w-]{12,}|"
    r"[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)|"
    r"\b(?:password|passwd|api[ _-]?key|(?:bot|access|discord)[ _-]?token|secret)"
    r"""\s*[:=]\s*(?:"[^"]+"|'[^']+'|\S+)""",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"\b(?:pass(?:word|phrase|wd)|api[ _-]?key|access[ _-]?token|bot[ _-]?token|"
    r"discord[ _-]?token|deepseek[ _-]?api[ _-]?key|secrets?|credentials?|bearer)\b",
    re.IGNORECASE,
)
WORD = re.compile(r"[^\W_]+")
STOP_WORDS = set("about and are can could does for from have how our that the their them this was were what when where which who will with would you your".split())


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.execute("""
                    CREATE TABLE memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id TEXT NOT NULL,
                        fact TEXT NOT NULL CHECK(length(fact) BETWEEN 1 AND 400),
                        normalized TEXT NOT NULL,
                        UNIQUE(guild_id, normalized)
                    )
                """)
                db.execute("""
                    CREATE TABLE memory_revisions (
                        guild_id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL
                    )
                """)
                db.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise ValueError("Unsupported Coach Seek memory database version.")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def revision(self, guild_id: int) -> int:
        with self.connection() as db:
            return self._revision(db, guild_id)

    @staticmethod
    def _revision(db: sqlite3.Connection, guild_id: int) -> int:
        row = db.execute(
            "SELECT revision FROM memory_revisions WHERE guild_id = ?", (str(guild_id),)
        ).fetchone()
        return row[0] if row else 0

    def remember(self, guild_id: int, facts: Sequence[str], revision: int | None = None) -> list[int]:
        ids = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if revision is not None and revision != self._revision(db, guild_id):
                return []
            for raw in facts:
                fact = " ".join(raw.split())
                if not 1 <= len(fact) <= MAX_FACT or SECRET.search(fact) or CREDENTIAL.search(fact):
                    continue
                key = (str(guild_id), fact.casefold())
                db.execute(
                    "INSERT INTO memories(guild_id, fact, normalized) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, normalized) DO NOTHING",
                    (key[0], fact, key[1]),
                )
                row = db.execute(
                    "SELECT id FROM memories WHERE guild_id = ? AND normalized = ?", key
                ).fetchone()
                ids.append(row[0])
        return ids

    def forget(self, guild_id: int, query: str) -> list[int]:
        query = query.strip()
        if not query:
            return []
        if re.fullmatch(r"#?[0-9]+", query):
            predicate = "CAST(id AS TEXT) = ?"
            value = query.removeprefix("#").lstrip("0")
        else:
            predicate = "instr(normalized, ?) > 0"
            value = query.casefold()
        with self.connection() as db:
            rows = db.execute(
                f"DELETE FROM memories WHERE guild_id = ? AND {predicate} RETURNING id",
                (str(guild_id), value),
            ).fetchall()
            db.execute("""
                INSERT INTO memory_revisions(guild_id, revision) VALUES (?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET revision = revision + 1
            """, (str(guild_id),))
        return sorted(row[0] for row in rows)

    def recent(self, guild_id: int, limit: int = MAX_MEMORIES) -> list[tuple[int, str]]:
        with self.connection() as db:
            return db.execute(
                "SELECT id, fact FROM memories WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (str(guild_id), limit),
            ).fetchall()

    def relevant(self, guild_id: int, question: str) -> list[str]:
        terms = set(WORD.findall(question.casefold())) - STOP_WORDS
        # Bound the candidate window as well as the amount sent to DeepSeek.
        scored = [
            (len(terms.intersection(WORD.findall(fact.casefold()))), memory_id, fact)
            for memory_id, fact in self.recent(guild_id, 250)
        ]
        return [fact for score, _, fact in sorted(scored, reverse=True)[:MAX_MEMORIES] if score]
