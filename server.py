"""
Memory backend for the home server.

Plain JSON API (not MCP). The Vercel app speaks MCP to Claude and forwards
tool calls here. Bind to localhost and point your tunnel at it.

Run:
    export BACKEND_SECRET="<long random string>"
    uvicorn server:app --host 127.0.0.1 --port 8787

Deps: pip install fastapi uvicorn
"""
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "memory.db")
SECRET = os.environ.get("BACKEND_SECRET", "")
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))
# Optional one-way export of every entry as a markdown file (for Obsidian).
# The database stays the source of truth; edits made to these files are NOT read back.
VAULT_DIR = os.environ.get("VAULT_DIR", "")

if len(SECRET) < 32:
    raise SystemExit("Set BACKEND_SECRET to a random string of at least 32 characters")

# Best-effort filter: reject writes that look like they contain credentials.
SECRET_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"tskey-[A-Za-z0-9-]{10,}",
    r"\bsk-[A-Za-z0-9_-]{20,}",
    r"\bghp_[A-Za-z0-9]{30,}",
    r"\bgithub_pat_[A-Za-z0-9_]{30,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i:\b(?:password|passwd|secret|api[_-]?key|token)\b)\s*[:=]\s*\S{6,}",
]
SECRET_RE = re.compile("|".join(SECRET_PATTERNS))

app = FastAPI(title="memory-backend")


# ---------- db ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                machine TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT ''
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
                title, summary, details, tags, machine,
                content='entries', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
                INSERT INTO entries_fts(rowid, title, summary, details, tags, machine)
                VALUES (new.id, new.title, new.summary, new.details, new.tags, new.machine);
            END;
            CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, title, summary, details, tags, machine)
                VALUES ('delete', old.id, old.title, old.summary, old.details, old.tags, old.machine);
            END;
            CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
                INSERT INTO entries_fts(entries_fts, rowid, title, summary, details, tags, machine)
                VALUES ('delete', old.id, old.title, old.summary, old.details, old.tags, old.machine);
                INSERT INTO entries_fts(rowid, title, summary, details, tags, machine)
                VALUES (new.id, new.title, new.summary, new.details, new.tags, new.machine);
            END;
            """
        )


init_db()


# ---------- auth + rate limit ----------
_hits: list[float] = []


def require_auth(x_backend_secret: str = Header(default="")):
    if not hmac.compare_digest(x_backend_secret.encode(), SECRET.encode()):
        raise HTTPException(status_code=404, detail="not found")
    now = time.time()
    _hits[:] = [t for t in _hits if now - t < 60]
    if len(_hits) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    _hits.append(now)


def check_no_secrets(*texts: str):
    for t in texts:
        if t and SECRET_RE.search(t):
            raise HTTPException(
                status_code=422,
                detail="Rejected: text looks like it contains a credential. "
                "Rewrite without keys, tokens or passwords.",
            )


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------- markdown export (optional) ----------
# One-way export for Obsidian. Layout:
#   <vault>/entries/<id>-<title-slug>.md   one note per saved entry
#   <vault>/hubs/<name>.md                 one note per machine / tag (created once, never overwritten)
# Each entry links to its machine hub, its tag hubs and a few related entries
# (entries sharing a tag), so Obsidian's graph view shows edges.
MAX_RELATED = 5


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:50].strip("-") or "entry"


def _yaml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)  # JSON strings are valid YAML


def _hub_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")[:40]


def _norm_tags(raw: str) -> list:
    out = []
    for t in (raw or "").split(","):
        h = _hub_name(t)
        if h and h not in out:
            out.append(h)
    return out


def _stem(row) -> str:
    return f"{row['id']:05d}-{_slug(row['title'])}"


def _alias(title: str) -> str:
    return re.sub(r"[|\[\]#^]", " ", title).strip()


def _render(row, index: dict, tagmap: dict) -> str:
    tags = tagmap[row["id"]]
    machine = _hub_name(row["machine"])
    mine = set(tags)
    scored = []
    for oid, ts in tagmap.items():
        if oid == row["id"]:
            continue
        shared = len(mine & set(ts))
        if shared:
            scored.append((shared, oid))
    scored.sort(key=lambda x: (-x[0], -x[1]))  # most shared tags, then newest
    related = [index[oid] for _, oid in scored[:MAX_RELATED]]

    tags_yaml = "[" + ", ".join(_yaml_str(t) for t in tags) + "]"
    lines = [
        "---",
        f"id: {row['id']}",
        f"title: {_yaml_str(row['title'])}",
        f"created: {row['created_at']}",
        f"updated: {row['updated_at']}",
        f"machine: {_yaml_str(row['machine'] or '')}",
        f"source: {_yaml_str(row['source'] or '')}",
        f"tags: {tags_yaml}",
        "---",
        "",
        f"# {row['title']}",
        "",
    ]
    if machine:
        lines.append(f"Machine: [[{machine}]]")
    if tags:
        lines.append("Topics: " + " ".join(f"[[{t}]]" for t in tags))
    if related:
        lines.append(
            "Related: "
            + " ".join(f"[[{_stem(r)}|{_alias(r['title'])}]]" for r in related)
        )
    lines += ["", row["summary"]]
    if row["details"]:
        lines += ["", "## Details", "", row["details"]]
    return "\n".join(lines) + "\n"


def _write_entry(row, index: dict, tagmap: dict, vault: Path) -> set:
    """Write one entry note if its content changed. Returns the hub names it needs."""
    entries = vault / "entries"
    entries.mkdir(parents=True, exist_ok=True)
    path = entries / f"{_stem(row)}.md"
    # remove stale copies: renamed titles, and the old flat layout in the vault root
    for old in list(entries.glob(f"{row['id']:05d}-*.md")) + list(vault.glob(f"{row['id']:05d}-*.md")):
        if old != path:
            old.unlink()
    text = _render(row, index, tagmap)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)  # atomic, so Syncthing never sees a half-written file
    names = set(tagmap[row["id"]])
    if _hub_name(row["machine"]):
        names.add(_hub_name(row["machine"]))
    return names


def _ensure_hubs(names: set, vault: Path) -> None:
    hubs = vault / "hubs"
    hubs.mkdir(parents=True, exist_ok=True)
    for n in names:
        p = hubs / f"{n}.md"
        if not p.exists():  # never overwrite: you may add your own notes to a hub
            p.write_text(f"# {n}\n", encoding="utf-8")


def _index(c):
    index = {
        r["id"]: r
        for r in c.execute("SELECT id, title, tags, machine FROM entries")
    }
    tagmap = {i: _norm_tags(r["tags"]) for i, r in index.items()}
    return index, tagmap


def export_affected(c, changed_id: int, old_tags_raw: str = "") -> None:
    """Re-export an entry plus every entry that shares a tag with it (their
    Related links may change). Never raises."""
    if not VAULT_DIR:
        return
    try:
        vault = Path(VAULT_DIR)
        index, tagmap = _index(c)
        touched = set(tagmap.get(changed_id, [])) | set(_norm_tags(old_tags_raw))
        ids = {changed_id} | {i for i, ts in tagmap.items() if touched & set(ts)}
        names = set()
        for i in sorted(ids):
            row = c.execute("SELECT * FROM entries WHERE id = ?", (i,)).fetchone()
            if row:
                names |= _write_entry(row, index, tagmap, vault)
        _ensure_hubs(names, vault)
    except Exception as e:  # export must never break a save
        print(f"vault export failed for entry {changed_id}: {e}")


def export_all(c) -> int:
    vault = Path(VAULT_DIR)
    index, tagmap = _index(c)
    names = set()
    rows = c.execute("SELECT * FROM entries ORDER BY id").fetchall()
    for row in rows:
        names |= _write_entry(row, index, tagmap, vault)
    _ensure_hubs(names, vault)
    return len(rows)


# ---------- models ----------
class SaveIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    details: str = Field(default="", max_length=8000)
    tags: str = Field(default="", max_length=200)
    machine: str = Field(default="", max_length=50)
    source: str = Field(default="", max_length=50)


class UpdateIn(BaseModel):
    id: int
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    details: Optional[str] = Field(default=None, max_length=8000)
    tags: Optional[str] = Field(default=None, max_length=200)
    machine: Optional[str] = Field(default=None, max_length=50)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=8, ge=1, le=25)


def row_out(r: sqlite3.Row, snippet: Optional[str] = None) -> dict:
    d = dict(r)
    if snippet is not None:
        d["snippet"] = snippet
    return d


def fts_match(query: str) -> str:
    # Turn free text into a safe FTS5 query: quoted prefix terms joined by OR.
    terms = re.findall(r"\w+", query)[:12]
    if not terms:
        raise HTTPException(status_code=422, detail="empty query")
    return " OR ".join(f'"{t}"*' for t in terms)


# ---------- routes ----------
@app.post("/save", dependencies=[Depends(require_auth)])
def save(body: SaveIn):
    check_no_secrets(body.title, body.summary, body.details, body.tags)
    ts = now_iso()
    with db() as c:
        cur = c.execute(
            "INSERT INTO entries(created_at, updated_at, title, summary, details, tags, machine, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, ts, body.title, body.summary, body.details, body.tags, body.machine, body.source),
        )
        export_affected(c, cur.lastrowid)
        return {"id": cur.lastrowid, "saved": True}


@app.post("/search", dependencies=[Depends(require_auth)])
def search(body: SearchIn):
    match = fts_match(body.query)
    with db() as c:
        rows = c.execute(
            "SELECT e.*, snippet(entries_fts, 1, '[', ']', '…', 24) AS snip "
            "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
            "WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts) LIMIT ?",
            (match, body.limit),
        ).fetchall()
        return {"results": [row_out(r, r["snip"]) for r in rows]}


@app.get("/recent", dependencies=[Depends(require_auth)])
def recent(n: int = 10, machine: str = ""):
    n = max(1, min(n, 50))
    with db() as c:
        if machine:
            rows = c.execute(
                "SELECT * FROM entries WHERE machine = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (machine, n),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM entries ORDER BY updated_at DESC, id DESC LIMIT ?", (n,)
            ).fetchall()
        return {"results": [row_out(r) for r in rows]}


@app.get("/entry/{entry_id}", dependencies=[Depends(require_auth)])
def get_entry(entry_id: int):
    with db() as c:
        r = c.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="no such entry")
        return row_out(r)


@app.post("/update", dependencies=[Depends(require_auth)])
def update(body: UpdateIn):
    fields = {
        k: v
        for k, v in body.model_dump().items()
        if k != "id" and v is not None
    }
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to update")
    check_no_secrets(*[v for v in fields.values() if isinstance(v, str)])
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    with db() as c:
        old = c.execute("SELECT tags FROM entries WHERE id = ?", (body.id,)).fetchone()
        cur = c.execute(
            f"UPDATE entries SET {sets} WHERE id = ?",
            (*fields.values(), now_iso(), body.id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="no such entry")
        export_affected(c, body.id, old["tags"] if old else "")
        return {"id": body.id, "updated": True}


@app.post("/reexport", dependencies=[Depends(require_auth)])
def reexport():
    """Rebuild the whole vault from the database. Not exposed through MCP."""
    if not VAULT_DIR:
        raise HTTPException(status_code=400, detail="VAULT_DIR is not set")
    with db() as c:
        return {"exported": export_all(c)}


@app.get("/health")
def health():
    return {"ok": True}
