"""Typed data objects used by the archive, index, and migration engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class PageInfo:
    page_id: str
    page_index: int
    length: int


@dataclass(frozen=True)
class EditionInfo:
    edition_index: int
    edition_id: str
    pages: tuple[PageInfo, ...]

    def page(self, page_id: str) -> PageInfo:
        for p in self.pages:
            if p.page_id == page_id:
                return p
        raise KeyError(page_id)

    def page_by_index(self, page_index: int) -> PageInfo:
        if not isinstance(page_index, int) or isinstance(page_index, bool):
            raise TypeError("page_index must be an integer")
        if page_index < 0 or page_index >= len(self.pages):
            raise IndexError(page_index)
        return self.pages[page_index]


@dataclass(frozen=True)
class MappingRecord:
    record_id: str
    edge_index: int
    from_page: str
    from_start: int
    from_end: int
    to_page: str
    to_start: int
    to_end: int

    @property
    def length(self) -> int:
        return self.from_end - self.from_start


@dataclass(frozen=True)
class Archive:
    """A validated volume description.

    ``tables[edge]`` is a tuple when the comparison table for
    ``editions[edge] -> editions[edge + 1]`` was supplied.  It is ``None``
    when the table is missing.  An empty tuple means a present but empty
    table; that distinction is semantically important.
    """

    editions: tuple[EditionInfo, ...]
    tables: tuple[Optional[tuple[MappingRecord, ...]], ...]
    edition_ids: dict[str, int] = field(compare=False)

    def edge_count(self) -> int:
        return max(0, len(self.editions) - 1)


@dataclass(frozen=True)
class Destination:
    """One actual target-edition landing interval and one witnessing chain."""

    page_id: str
    start: int
    end: int
    chain: tuple[str, ...]

    @property
    def location_key(self) -> tuple[str, int, int]:
        return (self.page_id, self.start, self.end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page_id,
            "start": self.start,
            "end": self.end,
            "interval": [self.start, self.end],
            "chain": list(self.chain),
        }


@dataclass(frozen=True)
class Segment:
    """A continuous piece of the original citation with one verdict."""

    start: int
    end: int
    destinations: tuple[Destination, ...]

    @property
    def status(self) -> str:
        if not self.destinations:
            return "lost"
        if len(self.destinations) == 1:
            return "unique"
        return "multiple"

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        if self.destinations:
            dests = [d.to_dict() for d in self.destinations]
        else:
            dests = []
        return {
            "start": self.start,
            "end": self.end,
            "interval": [self.start, self.end],
            "length": self.length,
            "status": self.status,
            "destinations": dests,
        }


@dataclass(frozen=True)
class MigrationResult:
    source_edition: str
    target_edition: str
    page_id: str
    start: int
    end: int
    segments: tuple[Segment, ...]
    source_edition_index: int = 0
    target_edition_index: int = 0

    @property
    def overall_status(self) -> str:
        if self.end == self.start:
            return "empty"
        statuses = {s.status for s in self.segments if s.length > 0}
        if not statuses:
            return "empty"
        if len(statuses) == 1:
            return next(iter(statuses))
        return "mixed"

    def summary_lengths(self) -> dict[str, int]:
        counts = {"unique": 0, "multiple": 0, "lost": 0}
        for s in self.segments:
            counts[s.status] += s.length
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_edition": self.source_edition,
            "target_edition": self.target_edition,
            "source_page": self.page_id,
            "source_interval": [self.start, self.end],
            "overall_status": self.overall_status,
            "lengths_by_status": self.summary_lengths(),
            "segments": [s.to_dict() for s in self.segments],
        }


def destinations_sorted(dests: Iterable[Destination]) -> tuple[Destination, ...]:
    return tuple(sorted(dests, key=lambda d: (d.page_id, d.start, d.end, d.chain)))
