"""Atomic SQLite disk index for adjacent-edition mapping tables."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from .archive import load_archive
from .errors import (
    EditionNotFoundError,
    IndexBuildError,
    IndexLoadError,
    MissingTableError,
    PageNotFoundError,
)
from .model import Archive, EditionInfo, MappingRecord, PageInfo

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE editions (
    edition_index INTEGER PRIMARY KEY,
    edition_id TEXT NOT NULL UNIQUE
);
CREATE TABLE pages (
    edition_index INTEGER NOT NULL REFERENCES editions(edition_index),
    page_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    length INTEGER NOT NULL CHECK (length >= 0),
    PRIMARY KEY (edition_index, page_id)
) WITHOUT ROWID;
CREATE TABLE edges (
    edge_index INTEGER PRIMARY KEY,
    from_edition_id TEXT NOT NULL,
    to_edition_id TEXT NOT NULL,
    table_present INTEGER NOT NULL CHECK (table_present IN (0, 1)),
    mapping_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE mappings (
    record_id TEXT NOT NULL PRIMARY KEY,
    edge_index INTEGER NOT NULL REFERENCES edges(edge_index),
    from_page TEXT NOT NULL,
    from_start INTEGER NOT NULL,
    from_end INTEGER NOT NULL,
    to_page TEXT NOT NULL,
    to_start INTEGER NOT NULL,
    to_end INTEGER NOT NULL,
    CHECK (from_start >= 0 AND from_end >= from_start),
    CHECK (to_start >= 0 AND to_end >= to_start),
    CHECK ((from_end - from_start) = (to_end - to_start))
);
/* Composite index supports the exact edge/page + half-open overlap lookup. */
CREATE INDEX idx_mappings_overlap
    ON mappings (edge_index, from_page, from_start, from_end);
"""


class IndexStore:
    """Read-only wrapper around a prebuilt SQLite citation index."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.conn = connection
        self.path = path

    @classmethod
    def open(cls, path: Union[str, Path]) -> "IndexStore":
        path = Path(path)
        if not path.exists():
            raise IndexLoadError(f"index file does not exist: {path}")
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            store = cls(conn, path)
            store._verify_schema()
            return store
        except IndexLoadError:
            try:
                conn.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            raise IndexLoadError(f"cannot open index {path}: {exc}") from exc

    @classmethod
    def in_memory(cls, archive: Archive) -> "IndexStore":
        """Build a non-durable index directly in memory (useful for tests)."""

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        populate_connection(conn, archive)
        conn.commit()
        store = cls(conn, Path(":memory:"))
        store._verify_schema()
        return store

    def _verify_schema(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndexLoadError(f"not a readable citation index: {exc}") from exc
        if row is None or row["value"] != SCHEMA_VERSION:
            raise IndexLoadError(
                f"unsupported or missing citation index schema in {self.path}"
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def resolve_edition(self, edition: Union[str, int]) -> EditionInfo:
        if isinstance(edition, bool):
            raise EditionNotFoundError("boolean is not an edition reference")
        if isinstance(edition, int):
            return self.edition_by_index(edition)
        return self.edition_by_id(str(edition))

    def edition_by_id(self, edition_id: str) -> EditionInfo:
        try:
            row = self.conn.execute(
                "SELECT edition_index FROM editions WHERE edition_id = ?",
                (str(edition_id),),
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndexLoadError(f"index query failed: {exc}") from exc
        if row is None:
            raise EditionNotFoundError(f"unknown edition id {edition_id!r}")
        return self.edition_by_index(int(row["edition_index"]))

    def edition_by_index(self, edition_index: int) -> EditionInfo:
        try:
            edition_row = self.conn.execute(
                "SELECT edition_id FROM editions WHERE edition_index = ?",
                (edition_index,),
            ).fetchone()
            if edition_row is None:
                raise EditionNotFoundError(
                    f"edition index {edition_index} is outside this volume"
                )
            page_rows = self.conn.execute(
                """
                SELECT page_id, page_index, length
                FROM pages
                WHERE edition_index = ?
                ORDER BY page_index
                """,
                (edition_index,),
            ).fetchall()
        except EditionNotFoundError:
            raise
        except sqlite3.Error as exc:
            raise IndexLoadError(f"index query failed: {exc}") from exc

        pages = tuple(
            PageInfo(
                page_id=r["page_id"],
                page_index=int(r["page_index"]),
                length=int(r["length"]),
            )
            for r in page_rows
        )
        return EditionInfo(
            edition_index=edition_index,
            edition_id=edition_row["edition_id"],
            pages=pages,
        )

    def require_page(self, edition: EditionInfo, page_id: str) -> PageInfo:
        try:
            return edition.page(str(page_id))
        except KeyError:
            raise PageNotFoundError(
                f"page {page_id!r} does not exist in edition "
                f"{edition.edition_id!r}; page ids are edition-local"
            ) from None

    def table_present(self, edge_index: int) -> bool:
        try:
            row = self.conn.execute(
                "SELECT table_present FROM edges WHERE edge_index = ?",
                (edge_index,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise IndexLoadError(f"index query failed: {exc}") from exc
        if row is None:
            raise IndexLoadError(f"index has no edge metadata for edge {edge_index}")
        return bool(row["table_present"])

    def missing_table_edges(
        self,
        first_edge: int,
        end_edge_exclusive: int,
    ) -> list[tuple[int, str, str]]:
        """Return missing-table edges in the selected adjacent-edge range."""

        try:
            rows = self.conn.execute(
                """
                SELECT edge_index, from_edition_id, to_edition_id
                FROM edges
                WHERE edge_index >= ? AND edge_index < ?
                  AND table_present = 0
                ORDER BY edge_index
                """,
                (first_edge, end_edge_exclusive),
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexLoadError(f"index query failed: {exc}") from exc
        return [
            (
                int(row["edge_index"]),
                row["from_edition_id"],
                row["to_edition_id"],
            )
            for row in rows
        ]

    def fetch_overlaps(
        self,
        edge_index: int,
        from_page: str,
        start: int,
        end: int,
    ) -> tuple[MappingRecord, ...]:
        """Return indexed mappings intersecting half-open interval [start, end).

        The SQL predicate is sargable on ``(edge_index, from_page,
        from_start)``; it never asks SQLite to scan the whole mappings table
        for a citation query.
        """

        if start >= end:
            return ()
        try:
            rows = self.conn.execute(
                """
                SELECT record_id, edge_index, from_page, from_start, from_end,
                       to_page, to_start, to_end
                FROM mappings
                WHERE edge_index = ?
                  AND from_page = ?
                  AND from_start < ?
                  AND from_end > ?
                  AND from_start < from_end
                ORDER BY from_start, from_end, record_id
                """,
                (edge_index, str(from_page), end, start),
            ).fetchall()
        except sqlite3.Error as exc:
            raise IndexLoadError(f"index overlap query failed: {exc}") from exc

        return tuple(
            MappingRecord(
                record_id=r["record_id"],
                edge_index=int(r["edge_index"]),
                from_page=r["from_page"],
                from_start=int(r["from_start"]),
                from_end=int(r["from_end"]),
                to_page=r["to_page"],
                to_start=int(r["to_start"]),
                to_end=int(r["to_end"]),
            )
            for r in rows
        )


def populate_connection(conn: sqlite3.Connection, archive: Archive) -> None:
    """Create schema and insert a validated archive into ``conn``."""

    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )

    conn.executemany(
        "INSERT INTO editions(edition_index, edition_id) VALUES (?, ?)",
        [(e.edition_index, e.edition_id) for e in archive.editions],
    )
    page_rows = [
        (e.edition_index, p.page_id, p.page_index, p.length)
        for e in archive.editions
        for p in e.pages
    ]
    conn.executemany(
        """
        INSERT INTO pages(edition_index, page_id, page_index, length)
        VALUES (?, ?, ?, ?)
        """,
        page_rows,
    )

    edge_rows = []
    mapping_rows = []
    for edge_index in range(archive.edge_count()):
        table = archive.tables[edge_index]
        from_e = archive.editions[edge_index]
        to_e = archive.editions[edge_index + 1]
        records = list(table or ())
        edge_rows.append(
            (
                edge_index,
                from_e.edition_id,
                to_e.edition_id,
                1 if table is not None else 0,
                len(records),
            )
        )
        for r in records:
            mapping_rows.append(
                (
                    r.record_id,
                    r.edge_index,
                    r.from_page,
                    r.from_start,
                    r.from_end,
                    r.to_page,
                    r.to_start,
                    r.to_end,
                )
            )

    conn.executemany(
        """
        INSERT INTO edges(
            edge_index, from_edition_id, to_edition_id,
            table_present, mapping_count
        ) VALUES (?, ?, ?, ?, ?)
        """,
        edge_rows,
    )
    conn.executemany(
        """
        INSERT INTO mappings(
            record_id, edge_index, from_page, from_start, from_end,
            to_page, to_start, to_end
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        mapping_rows,
    )


def build_index(
    source: Union[str, Path, Archive],
    index_path: Union[str, Path],
    *,
    overwrite: bool = False,
    require_all_tables: bool = False,
) -> IndexStore:
    """Build a new index in a temporary file and atomically rename it.

    A failed build closes and removes only its temporary file.  If
    ``index_path`` already exists and ``overwrite`` is true, the previous file
    remains untouched until ``os.replace`` succeeds.  Thus a build/load failure
    cannot leave a previously queryable index replaced by a half-written file.
    """

    archive = load_archive(source) if not isinstance(source, Archive) else source
    index_path = Path(index_path)
    if index_path.exists() and not overwrite:
        raise IndexBuildError(
            f"index already exists: {index_path}; pass overwrite=True/--force to rebuild"
        )

    missing = [
        i
        for i, table in enumerate(archive.tables)
        if table is None and i < len(archive.editions) - 1
    ]
    if require_all_tables and missing:
        details = ", ".join(
            f"{archive.editions[i].edition_id}->{archive.editions[i + 1].edition_id}"
            for i in missing
        )
        raise MissingTableError(f"missing comparison table(s): {details}")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.",
        suffix=".building",
        dir=str(index_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(tmp_path)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        populate_connection(conn, archive)

        check = conn.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise IndexBuildError("SQLite integrity check failed before atomic replace")
        conn.commit()
        conn.close()
        conn = None

        # Atomic on same filesystem/directory.
        os.replace(tmp_path, index_path)
        _fsync_directory(index_path.parent)
        return IndexStore.open(index_path)
    except (MissingTableError, IndexBuildError):
        _cleanup_temp(conn, tmp_path)
        raise
    except (sqlite3.Error, OSError) as exc:
        _cleanup_temp(conn, tmp_path)
        raise IndexBuildError(f"failed to build index {index_path}: {exc}") from exc
    except Exception:
        _cleanup_temp(conn, tmp_path)
        raise


def _cleanup_temp(conn: Optional[sqlite3.Connection], path: Path) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
