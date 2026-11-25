"""Load and validate volume/edition/mapping-table descriptions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .errors import ArchiveValidationError, IllegalCoordinateError
from .model import Archive, EditionInfo, MappingRecord, PageInfo


def _as_id(value: Any, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ArchiveValidationError(f"{what} must be a string or integer id")
    text = str(value)
    if text == "":
        raise ArchiveValidationError(f"{what} must not be empty")
    return text


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IllegalCoordinateError(f"{what} must be a non-negative integer")
    if value < 0:
        raise IllegalCoordinateError(f"{what} must be non-negative, got {value}")
    return value


def _pick(obj: dict[str, Any], keys: tuple[str, ...], what: str) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    raise ArchiveValidationError(f"missing {what}; expected one of {', '.join(keys)}")


def _interval(
    raw: dict[str, Any],
    *,
    prefix: str,
    page: PageInfo,
    context: str,
) -> tuple[int, int]:
    """Read either [start, end] or prefix_start/prefix_end coordinates."""

    interval_key = f"{prefix}_interval"
    if interval_key in raw and raw[interval_key] is not None:
        pair = raw[interval_key]
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise IllegalCoordinateError(
                f"{context}: {interval_key} must be a [start, end] pair"
            )
        start_raw, end_raw = pair
    else:
        start_raw = _pick(
            raw,
            (f"{prefix}_start", f"{prefix}_begin"),
            f"{prefix}_start",
        )
        end_raw = _pick(raw, (f"{prefix}_end",), f"{prefix}_end")

    start = _as_int(start_raw, f"{context}: {prefix}_start")
    end = _as_int(end_raw, f"{context}: {prefix}_end")
    if start > end:
        raise IllegalCoordinateError(
            f"{context}: start {start} is greater than end {end}"
        )
    if end > page.length:
        raise IllegalCoordinateError(
            f"{context}: interval [{start},{end}) exceeds page "
            f"{page.page_id!r} length {page.length}"
        )
    return start, end


def _load_editions(raw_editions: Any) -> tuple[tuple[EditionInfo, ...], dict[str, int]]:
    if not isinstance(raw_editions, list) or not raw_editions:
        raise ArchiveValidationError("'editions' must be a non-empty list")

    editions: list[EditionInfo] = []
    ids: dict[str, int] = {}

    for edition_index, raw_edition in enumerate(raw_editions):
        where = f"editions[{edition_index}]"
        if not isinstance(raw_edition, dict):
            raise ArchiveValidationError(f"{where} must be an object")

        edition_id = _as_id(
            _pick(raw_edition, ("id", "edition_id"), f"{where}.id"),
            f"{where}.id",
        )
        if edition_id in ids:
            raise ArchiveValidationError(f"duplicate edition id {edition_id!r}")

        raw_pages = _pick(raw_edition, ("pages",), f"{where}.pages")
        if not isinstance(raw_pages, list):
            raise ArchiveValidationError(f"{where}.pages must be a list")

        pages: list[PageInfo] = []
        page_ids: set[str] = set()
        for page_index, raw_page in enumerate(raw_pages):
            page_where = f"{where}.pages[{page_index}]"
            if not isinstance(raw_page, dict):
                raise ArchiveValidationError(f"{page_where} must be an object")
            page_id = _as_id(
                _pick(raw_page, ("id", "page_id"), f"{page_where}.id"),
                f"{page_where}.id",
            )
            if page_id in page_ids:
                raise ArchiveValidationError(
                    f"duplicate page id {page_id!r} in edition {edition_id!r}"
                )
            length = _as_int(
                _pick(raw_page, ("length", "page_length"), f"{page_where}.length"),
                f"{page_where}.length",
            )
            page_ids.add(page_id)
            pages.append(PageInfo(page_id=page_id, page_index=page_index, length=length))

        idx = len(editions)
        ids[edition_id] = idx
        editions.append(
            EditionInfo(
                edition_index=idx,
                edition_id=edition_id,
                pages=tuple(pages),
            )
        )

    return tuple(editions), ids


def archive_from_dict(data: Any) -> Archive:
    """Validate a plain decoded JSON object and return an :class:`Archive`."""

    if not isinstance(data, dict):
        raise ArchiveValidationError("archive root must be a JSON object")

    editions, edition_ids = _load_editions(data.get("editions"))
    edge_count = len(editions) - 1
    tables: list[Optional[tuple[MappingRecord, ...]]] = [None] * edge_count
    seen_record_ids: set[str] = set()

    raw_tables: Any = data.get("tables", [])
    if raw_tables is None:
        raw_tables = []

    # Also accept a flat "mappings" array.  It is grouped by adjacent edition.
    flat_mappings = data.get("mappings", [])
    if flat_mappings is None:
        flat_mappings = []
    if not isinstance(raw_tables, list):
        raise ArchiveValidationError("'tables' must be a list when supplied")
    if not isinstance(flat_mappings, list):
        raise ArchiveValidationError("'mappings' must be a list when supplied")

    grouped_flat: dict[int, list[dict[str, Any]]] = {}
    for i, raw_mapping in enumerate(flat_mappings):
        if not isinstance(raw_mapping, dict):
            raise ArchiveValidationError(f"mappings[{i}] must be an object")
        from_id = _as_id(
            _pick(raw_mapping, ("from_edition", "source_edition"), "from_edition"),
            f"mappings[{i}].from_edition",
        )
        to_id = _as_id(
            _pick(raw_mapping, ("to_edition", "target_edition"), "to_edition"),
            f"mappings[{i}].to_edition",
        )
        edge = _edge_index(from_id, to_id, editions, edition_ids, f"mappings[{i}]")
        grouped_flat.setdefault(edge, []).append(raw_mapping)

    for table_index, raw_table in enumerate(raw_tables):
        where = f"tables[{table_index}]"
        if not isinstance(raw_table, dict):
            raise ArchiveValidationError(f"{where} must be an object")

        from_id = _as_id(
            _pick(raw_table, ("from_edition", "source_edition"), f"{where}.from_edition"),
            f"{where}.from_edition",
        )
        to_id = _as_id(
            _pick(raw_table, ("to_edition", "target_edition"), f"{where}.to_edition"),
            f"{where}.to_edition",
        )
        edge = _edge_index(from_id, to_id, editions, edition_ids, where)
        if tables[edge] is not None:
            raise ArchiveValidationError(
                f"duplicate comparison table for edge {from_id!r} -> {to_id!r}"
            )

        raw_records = raw_table.get("mappings", raw_table.get("records", []))
        if raw_records is None:
            raw_records = []
        if not isinstance(raw_records, list):
            raise ArchiveValidationError(f"{where}.mappings must be a list")

        records = [
            _validate_mapping(
                raw_mapping,
                editions=editions,
                edge=edge,
                seen_record_ids=seen_record_ids,
                context=f"{where}.mappings[{j}]",
            )
            for j, raw_mapping in enumerate(raw_records)
        ]
        tables[edge] = tuple(records)

    for edge, raw_records in grouped_flat.items():
        existing = list(tables[edge] or ())
        for j, raw_mapping in enumerate(raw_records):
            existing.append(
                _validate_mapping(
                    raw_mapping,
                    editions=editions,
                    edge=edge,
                    seen_record_ids=seen_record_ids,
                    context=f"mappings[{j}]",
                )
            )
        tables[edge] = tuple(existing)

    return Archive(
        editions=editions,
        tables=tuple(tables),
        edition_ids=edition_ids,
    )


def _edge_index(
    from_id: str,
    to_id: str,
    editions: tuple[EditionInfo, ...],
    edition_ids: dict[str, int],
    context: str,
) -> int:
    if from_id not in edition_ids:
        raise ArchiveValidationError(f"{context}: unknown from_edition {from_id!r}")
    if to_id not in edition_ids:
        raise ArchiveValidationError(f"{context}: unknown to_edition {to_id!r}")
    edge = edition_ids[from_id]
    if edge >= len(editions) - 1 or editions[edge + 1].edition_id != to_id:
        raise ArchiveValidationError(
            f"{context}: {from_id!r} -> {to_id!r} is not an adjacent edition pair"
        )
    return edge


def _validate_mapping(
    raw: Any,
    *,
    editions: tuple[EditionInfo, ...],
    edge: int,
    seen_record_ids: set[str],
    context: str,
) -> MappingRecord:
    if not isinstance(raw, dict):
        raise ArchiveValidationError(f"{context} must be an object")

    record_id = _as_id(
        _pick(raw, ("id", "mapping_id", "record_id"), f"{context}.id"),
        f"{context}.id",
    )
    if record_id in seen_record_ids:
        raise ArchiveValidationError(f"{context}: duplicate mapping id {record_id!r}")
    seen_record_ids.add(record_id)

    from_edition = editions[edge]
    to_edition = editions[edge + 1]

    from_page_id = _as_id(
        _pick(
            raw,
            ("from_page", "from_page_id", "source_page", "source_page_id"),
            f"{context}.from_page",
        ),
        f"{context}.from_page",
    )
    to_page_id = _as_id(
        _pick(
            raw,
            ("to_page", "to_page_id", "target_page", "target_page_id"),
            f"{context}.to_page",
        ),
        f"{context}.to_page",
    )

    try:
        from_page = from_edition.page(from_page_id)
    except KeyError:
        raise ArchiveValidationError(
            f"{context}: unknown from_page {from_page_id!r} in edition "
            f"{from_edition.edition_id!r}"
        ) from None
    try:
        to_page = to_edition.page(to_page_id)
    except KeyError:
        raise ArchiveValidationError(
            f"{context}: unknown to_page {to_page_id!r} in edition "
            f"{to_edition.edition_id!r}"
        ) from None

    from_start, from_end = _interval(
        raw, prefix="from", page=from_page, context=context
    )
    to_start, to_end = _interval(raw, prefix="to", page=to_page, context=context)

    if from_end - from_start != to_end - to_start:
        raise IllegalCoordinateError(
            f"{context}: mapping {record_id!r} maps length "
            f"{from_end - from_start} to unequal length {to_end - to_start}"
        )

    return MappingRecord(
        record_id=record_id,
        edge_index=edge,
        from_page=from_page_id,
        from_start=from_start,
        from_end=from_end,
        to_page=to_page_id,
        to_start=to_start,
        to_end=to_end,
    )


def load_archive(path: str | Path) -> Archive:
    """Read and validate an archive JSON file."""

    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArchiveValidationError(f"cannot read archive {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArchiveValidationError(f"invalid JSON in {path}: {exc}") from exc
    return archive_from_dict(data)
