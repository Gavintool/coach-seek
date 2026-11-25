"""Validation and persistent URL deduplication for the FTC digest."""

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .memory import SECRET

logger = logging.getLogger(__name__)
CATEGORIES = {"Rules", "Community", "Video", "Robot Design"}
MASS_MENTION = re.compile(r"@(?:everyone|here)", re.IGNORECASE)
TRACKING = {"fbclid", "gclid", "si"}
PRIVATE_QUERY_KEYS = {"auth", "authorization", "key", "token", "sig", "signature", "session", "session_id"}

MECHANISM = re.compile(
    r"\b(?:flywheels?|shooters?|launchers?|intakes?|rollers?|turrets?|feeders?|"
    r"indexers?|conveyors?|claws?|grippers?|arms?|slides?|drivetrains?|chassis|"
    r"wheels?|hoods?|gears?|belts?|cams?|linkages?|sensors?|servos?|elevators?)\b", re.I,
)
DESIGN_DETAIL = re.compile(
    r"\b(?:(?:counter|contra)[- ]rotating|coaxial|compliant|spring[- ]loaded|"
    r"independently driven|differential drive|over[- ]center|four[- ]bar|belt[- ]driven|"
    r"(?:dual|twin|two|three)[- ](?:stage|flywheel|wheel|roller|motor)|"
    r"(?:adjustable|hinged) hood|offset flywheel|variable[- ](?:angle|speed)|"
    r"(?:folds?|pivots?|retracts?|feeds?|routes?|mounts?|drives?|rotates?)\s+"
    r"(?:into|under|over|through|around|along|off|from|on|via)|"
    r"\d+(?:\.\d+)?\s*(?:mm|cm|rpm|degrees?|:1))\b", re.I,
)
RULE_DETAIL = re.compile(
    r"\b(?:(?:rule|q\s*&\s*a|question)\s*#?\s*[a-z]*\d+|[gr]\d{2,4})\b"
    r".*\b(?:allows?|permits?|requires?|prohibits?|limits?|counts? as|clarifies? that)\s+\w",
    re.I,
)


def direct_source(key: str) -> bool:
    parts = urlsplit(key)
    if parts.hostname == "youtu.be":
        return re.fullmatch(r"/[\w-]+", parts.path) is not None
    if parts.hostname == "youtube.com" or parts.hostname.endswith(".youtube.com"):
        return (
            parts.path == "/watch" and bool(dict(parse_qsl(parts.query)).get("v"))
            or re.fullmatch(r"/(?:shorts|live)/[\w-]+", parts.path) is not None
        )
    return parts.path != "/" or bool(parts.query)


def url_key(url: str) -> str | None:
    if not re.fullmatch(r"https://[^\s<>()\\\"\x00-\x1f\x7f]{1,600}", url, re.IGNORECASE):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (
            not host or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
            or parts.username is not None or parts.port not in (None, 443)
        ):
            return None
        parameters = parse_qsl(parts.query, keep_blank_values=True)
        if any(key.lower() in PRIVATE_QUERY_KEYS for key, _ in parameters):
            return None
        query = [
            (key, value) for key, value in parameters
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING
        ]
    except ValueError:
        return None
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


@dataclass(frozen=True)
class DigestItem:
    title: str
    summary: str
    url: str
    category: str
    key: str


def parse_digest(
    content: str, source_urls: list[str], redact: Callable[[str], str],
) -> list[DigestItem]:
    content = content.strip()
    if content.startswith("```json\n") and content.endswith("```"):
        content = content.removeprefix("```json\n").removesuffix("```")
    try:
        payload = json.loads(content)
    except ValueError:
        raise ValueError("Invalid digest JSON") from None
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Missing digest items")

    sources = {key for url in source_urls if isinstance(url, str) and (key := url_key(url))}
    items = []
    vague = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = [row.get(name) for name in ("title", "summary", "url", "category")]
        if any(not isinstance(value, str) for value in fields):
            continue
        if any(SECRET.search(unquote(value)) or redact(unquote(value)) != unquote(value)
               or redact(value) != value for value in fields):
            continue
        title, summary, url, category = fields
        title, summary = (" ".join(MASS_MENTION.sub("", field).split()) for field in (title, summary))
        url = url.strip()
        key = url_key(url)
        host = urlsplit(key).hostname or ""
        youtube = host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
        if not (
            key and key in sources and direct_source(key) and category in CATEGORIES
            and (category != "Video" or youtube)
            and not MASS_MENTION.search(unquote(url))
            and 0 < len(title) <= 120 and 0 < len(summary) <= 300
        ):
            continue
        description = f"{title}. {summary}"
        if category == "Rules":
            specific = RULE_DETAIL.search(description)
        else:
            specific = MECHANISM.search(description) and DESIGN_DETAIL.search(description)
        if specific:
            items.append(DigestItem(title, summary, url, category, key))
        else:
            vague += 1
    if vague:
        logger.info("FTC digest skipped %d finding(s) without a specific detail", vague)
    return sorted(items, key=lambda item: item.category != "Video")


class DigestStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE IF NOT EXISTS digest_seen (url TEXT PRIMARY KEY)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS digest_run ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), completed_at REAL NOT NULL)"
        )
        return db

    def last_run(self) -> float | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT completed_at FROM digest_run WHERE id = 1").fetchone()
            return row[0] if row else None

    def record_run(self, completed_at: float) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO digest_run VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET completed_at = excluded.completed_at",
                (completed_at,),
            )

    def unseen(self, items: list[DigestItem]) -> list[DigestItem]:
        """Return up to three items whose URLs have not been reserved yet."""
        if not items:
            return []
        with closing(self._connect()) as db:
            accepted = []
            reserved = set()
            for item in items:
                if item.key in reserved:
                    continue
                exists = db.execute("SELECT 1 FROM digest_seen WHERE url = ?", (item.key,)).fetchone()
                if exists:
                    continue
                accepted.append(item)
                reserved.add(item.key)
                if len(accepted) == 3:
                    break
            return accepted

    def mark_seen(self, items: list[DigestItem]) -> None:
        if not items:
            return
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO digest_seen VALUES (?)", [(item.key,) for item in items])

    def accept_new(self, items: list[DigestItem]) -> list[DigestItem]:
        """Reserve up to three new items across all guilds in a single transaction."""
        accepted = self.unseen(items)
        self.mark_seen(accepted)
        return accepted
