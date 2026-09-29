"""Operational runbook knowledge base.

Runbooks are markdown files with YAML front matter. They are synced into
PostgreSQL and retrieved by cause category and full-text relevance
(tsvector/ts_rank). Retrieval results always carry source attribution (runbook
id, file, section). Vector search is deliberately not used: the corpus is
small, category metadata is exact, and FTS keeps retrieval deterministic and
dependency-free (see docs/DESIGN_DECISIONS.md).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.db.models import Runbook as RunbookRow

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Runbook:
    id: str
    title: str
    categories: list[str]
    body: str
    path: str
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256((self.title + "|".join(self.categories) + self.body).encode()).hexdigest()


def parse_runbook(path: Path, root: Path | None = None) -> Runbook:
    raw = path.read_text(encoding="utf-8")
    m = _FRONT.match(raw)
    if not m:
        raise ValueError(f"{path}: missing front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    sections: dict[str, str] = {}
    current = "Overview"
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif not line.startswith("# "):
            buf.append(line)
    sections[current] = "\n".join(buf).strip()
    rel = str(path.relative_to(root)) if root else path.name
    return Runbook(id=meta["id"], title=meta["title"], categories=list(meta.get("categories", [])), body=body,
                   path=rel.replace("\\", "/"), sections={k: v for k, v in sections.items() if v})


def load_dir(directory: Path) -> list[Runbook]:
    if not directory.exists():
        return []
    return [parse_runbook(p, directory.parent) for p in sorted(directory.glob("*.md"))]


async def sync(session: AsyncSession, runbooks: list[Runbook]) -> int:
    for rb in runbooks:
        stmt = pg_insert(RunbookRow).values(id=rb.id, title=rb.title, categories=rb.categories, body=rb.body,
                                            source_path=rb.path, content_hash=rb.content_hash)
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={
            "title": rb.title, "categories": rb.categories, "body": rb.body, "source_path": rb.path,
            "content_hash": rb.content_hash}, where=RunbookRow.content_hash != rb.content_hash)
        await session.execute(stmt)
    return len(runbooks)


def best_section(body: str, preferred: tuple[str, ...] = ("Remediation", "Mitigation", "Diagnosis")) -> tuple[str, str]:
    rb = Runbook(id="", title="", categories=[], body=body, path="")
    sections: dict[str, str] = {}
    current, buf = "Overview", []
    for line in body.splitlines():
        if line.startswith("## "):
            sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        else:
            buf.append(line)
    sections[current] = "\n".join(buf).strip()
    rb.sections = sections
    for p in preferred:
        for name, content in sections.items():
            if name.lower().startswith(p.lower()) and content:
                return name, content
    return "Overview", sections.get("Overview", "")


async def search(session: AsyncSession, category: str | None, query: str, limit: int = 3) -> list[dict[str, Any]]:
    sql = text("""
        SELECT id, title, source_path, body,
               ts_rank(tsv, plainto_tsquery('english', :q)) AS rank,
               (:cat = ANY(categories)) AS category_match
        FROM runbooks
        WHERE (:cat = ANY(categories)) OR tsv @@ plainto_tsquery('english', :q)
        ORDER BY category_match DESC, rank DESC
        LIMIT :limit
    """)
    rows = (await session.execute(sql, {"q": query or "", "cat": category or "", "limit": limit})).mappings().all()
    out = []
    for r in rows:
        section, content = best_section(r["body"])
        out.append({"id": r["id"], "title": r["title"], "path": r["source_path"], "section": section,
                    "snippet": content[:900], "rank": round(float(r["rank"] or 0), 4),
                    "categoryMatch": bool(r["category_match"])})
    return out


def search_local(runbooks: list[Runbook], category: str | None, query: str, limit: int = 3) -> list[dict[str, Any]]:
    """In-memory fallback (offline replay and tests)."""
    terms = {t for t in re.findall(r"[a-z]{4,}", query.lower())}
    scored = []
    for rb in runbooks:
        text_l = (rb.title + " " + rb.body).lower()
        rank = sum(text_l.count(t) for t in terms) / (1 + len(text_l) / 1000)
        match = bool(category and category in rb.categories)
        if match or rank > 0:
            scored.append((match, rank, rb))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    out = []
    for match, rank, rb in scored[:limit]:
        section, content = best_section(rb.body)
        out.append({"id": rb.id, "title": rb.title, "path": rb.path, "section": section, "snippet": content[:900],
                    "rank": round(rank, 4), "categoryMatch": match})
    return out
