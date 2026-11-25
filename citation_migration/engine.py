"""Interval-level citation migration engine.

The algorithm never walks integer positions.  It splits intervals only at
mapping/result boundaries, memoizes evaluation by the exact interval reached
in an edition, and deduplicates equal target locations.  Memoization means
several incoming paths that converge on the same page interval share one
downstream evaluation instead of expanding every complete path.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Optional

from .errors import (
    IllegalCoordinateError,
    IndexLoadError,
    MissingTableError,
    MigrationDirectionError,
)
from .index import IndexStore
from .model import (
    Destination,
    MappingRecord,
    MigrationResult,
    Segment,
    destinations_sorted,
)


class MigrationEngine:
    def __init__(self, store: IndexStore):
        self.store = store
        self._cache: dict[tuple[int, int, str, int, int], tuple[Segment, ...]] = {}
        self._records: dict[str, MappingRecord] = {}

    def migrate(
        self,
        *,
        source_edition: object,
        target_edition: object,
        page_id: str,
        start: int,
        end: int,
    ) -> MigrationResult:
        source = self.store.resolve_edition(source_edition)  # type: ignore[arg-type]
        target = self.store.resolve_edition(target_edition)  # type: ignore[arg-type]
        if target.edition_index < source.edition_index:
            raise MigrationDirectionError(
                "this tool migrates old editions forward to a newer edition"
            )

        page = self.store.require_page(source, str(page_id))
        if isinstance(start, bool) or not isinstance(start, int):
            raise IllegalCoordinateError("start must be an integer")
        if isinstance(end, bool) or not isinstance(end, int):
            raise IllegalCoordinateError("end must be an integer")
        if start < 0 or end < 0 or start > end:
            raise IllegalCoordinateError(
                f"illegal source interval [{start},{end}) on page {page.page_id!r}"
            )
        if end > page.length:
            raise IllegalCoordinateError(
                f"source interval [{start},{end}) exceeds page "
                f"{page.page_id!r} length {page.length}"
            )

        # Check every crossed handoff before interpreting absence as loss.
        missing_edges = self.store.missing_table_edges(
            source.edition_index,
            target.edition_index,
        )
        if missing_edges:
            _edge_index, from_id, to_id = missing_edges[0]
            raise MissingTableError(
                "缺少中间对照表: "
                f"{from_id!r} -> {to_id!r}; "
                "缺表不能视为空对照表或内容已删除"
            )

        if start == end:
            segments: tuple[Segment, ...] = ()
        else:
            raw_segments = self._resolve(
                source.edition_index,
                page.page_id,
                start,
                end,
                target.edition_index,
            )
            segments = self._coalesce(
                raw_segments,
                source.edition_index,
                page.page_id,
            )

        return MigrationResult(
            source_edition=source.edition_id,
            target_edition=target.edition_id,
            page_id=page.page_id,
            start=start,
            end=end,
            segments=segments,
            source_edition_index=source.edition_index,
            target_edition_index=target.edition_index,
        )

    # ------------------------------------------------------------------
    # Core interval recursion
    # ------------------------------------------------------------------

    def _resolve(
        self,
        edition_index: int,
        page_id: str,
        start: int,
        end: int,
        target_index: int,
    ) -> tuple[Segment, ...]:
        key = (target_index, edition_index, page_id, start, end)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if edition_index == target_index:
            result = (
                Segment(
                    start=start,
                    end=end,
                    destinations=(
                        Destination(
                            page_id=page_id,
                            start=start,
                            end=end,
                            chain=(),
                        ),
                    ),
                ),
            )
            self._cache[key] = result
            return result

        records = self.store.fetch_overlaps(
            edition_index, page_id, start, end
        )
        for record in records:
            self._records[record.record_id] = record

        breaks: set[int] = {start, end}
        rule_results = []

        for record in records:
            overlap_start = max(start, record.from_start)
            overlap_end = min(end, record.from_end)
            if overlap_start >= overlap_end:
                continue

            breaks.add(overlap_start)
            breaks.add(overlap_end)

            mapped_start = record.to_start + (overlap_start - record.from_start)
            mapped_end = record.to_start + (overlap_end - record.from_start)
            parts = self._resolve(
                edition_index + 1,
                record.to_page,
                mapped_start,
                mapped_end,
                target_index,
            )

            # Inverse-map every downstream boundary into this source interval.
            for part in parts:
                inv_start = record.from_start + (part.start - record.to_start)
                inv_end = record.from_start + (part.end - record.to_start)
                breaks.add(inv_start)
                breaks.add(inv_end)

            rule_results.append(
                (
                    record,
                    overlap_start,
                    overlap_end,
                    parts,
                    tuple(p.start for p in parts),
                )
            )

        coordinates = sorted(breaks)
        output: list[Segment] = []

        for left, right in zip(coordinates, coordinates[1:]):
            if left >= right or not (start <= left and right <= end):
                continue

            destination_chains: dict[tuple[str, int, int], tuple[str, ...]] = {}

            for record, overlap_start, overlap_end, parts, part_starts in rule_results:
                # Coverage boundaries are in ``breaks``; hence coverage is
                # constant on [left, right).
                if not (overlap_start <= left and right <= overlap_end):
                    continue

                next_start = record.to_start + (left - record.from_start)
                next_end = record.to_start + (right - record.from_start)
                part = _find_part(parts, part_starts, next_start)

                if not (part.start <= next_start and next_end <= part.end):
                    # Should be impossible because all part boundaries were
                    # inverse-mapped into ``breaks``.
                    raise IndexLoadError(
                        "internal interval error: downstream partition does "
                        "not cover a mapped subinterval"
                    )

                for dest in part.destinations:
                    clipped_start = dest.start + (next_start - part.start)
                    clipped_end = dest.start + (next_end - part.start)
                    loc_key = (dest.page_id, clipped_start, clipped_end)
                    chain = (record.record_id,) + dest.chain
                    old_chain = destination_chains.get(loc_key)
                    if old_chain is None or chain < old_chain:
                        destination_chains[loc_key] = chain

            destinations = tuple(
                Destination(
                    page_id=page,
                    start=loc_start,
                    end=loc_end,
                    chain=chain,
                )
                for (page, loc_start, loc_end), chain in sorted(
                    destination_chains.items()
                )
            )
            output.append(
                Segment(
                    start=left,
                    end=right,
                    destinations=destinations_sorted(destinations),
                )
            )

        result = tuple(output)
        self._cache[key] = result
        return result

    # ------------------------------------------------------------------
    # Coalescing / witness continuity
    # ------------------------------------------------------------------

    def _coalesce(
        self,
        segments: tuple[Segment, ...],
        source_edition_index: int,
        source_page: str,
    ) -> tuple[Segment, ...]:
        if not segments:
            return ()

        merged: list[Segment] = [segments[0]]
        for nxt in segments[1:]:
            combined = self._merge_pair(
                merged[-1],
                nxt,
                source_edition_index,
                source_page,
            )
            if combined is None:
                merged.append(nxt)
            else:
                merged[-1] = combined
        return tuple(merged)

    def _merge_pair(
        self,
        left: Segment,
        right: Segment,
        source_edition_index: int,
        source_page: str,
    ) -> Optional[Segment]:
        if left.end != right.start:
            return None

        if not left.destinations and not right.destinations:
            return Segment(left.start, right.end, ())

        if not left.destinations or not right.destinations:
            return None
        if len(left.destinations) != len(right.destinations):
            return None

        left_by_loc = {d.location_key: d for d in left.destinations}
        right_by_loc = {d.location_key: d for d in right.destinations}
        common_chains: list[tuple[str, ...]] = []

        # A witness chosen on the right may also cover the whole left piece,
        # even if left had deduplicated another chain as its canonical witness.
        paired_left: set[tuple[str, int, int]] = set()
        ok = True
        for dest in right.destinations:
            applied = self._try_apply_chain(
                dest.chain,
                source_edition_index,
                source_page,
                left.start,
                left.end,
            )
            if applied is None:
                ok = False
                break
            _edition, page_id, lo, hi = applied
            loc = (page_id, lo, hi)
            if loc not in left_by_loc or loc in paired_left:
                ok = False
                break
            paired_left.add(loc)
            common_chains.append(dest.chain)

        if ok and len(paired_left) == len(left_by_loc):
            return self._merge_with_chains(
                left.start, right.end, common_chains,
                source_edition_index, source_page,
            )

        # Symmetrically try the left witnesses on the right piece.
        paired_right: set[tuple[str, int, int]] = set()
        common_chains = []
        ok = True
        for dest in left.destinations:
            applied = self._try_apply_chain(
                dest.chain,
                source_edition_index,
                source_page,
                right.start,
                right.end,
            )
            if applied is None:
                ok = False
                break
            _edition, page_id, lo, hi = applied
            loc = (page_id, lo, hi)
            if loc not in right_by_loc or loc in paired_right:
                ok = False
                break
            paired_right.add(loc)
            common_chains.append(dest.chain)

        if ok and len(paired_right) == len(right_by_loc):
            return self._merge_with_chains(
                left.start, right.end, common_chains,
                source_edition_index, source_page,
            )

        return None

    def _merge_with_chains(
        self,
        start: int,
        end: int,
        chains: list[tuple[str, ...]],
        source_edition_index: int,
        source_page: str,
    ) -> Optional[Segment]:
        destinations: list[Destination] = []
        for chain in chains:
            applied = self._try_apply_chain(
                chain,
                source_edition_index,
                source_page,
                start,
                end,
            )
            if applied is None:
                return None
            _edition, page_id, lo, hi = applied
            destinations.append(
                Destination(
                    page_id=page_id,
                    start=lo,
                    end=hi,
                    chain=chain,
                )
            )
        return Segment(start, end, destinations_sorted(destinations))

    def _try_apply_chain(
        self,
        chain: tuple[str, ...],
        edition_index: int,
        page_id: str,
        start: int,
        end: int,
    ) -> Optional[tuple[int, str, int, int]]:
        current_edge = edition_index
        current_page = page_id
        current_start = start
        current_end = end

        for record_id in chain:
            record = self._records.get(record_id)
            if record is None:
                raise IndexLoadError(
                    f"witness chain refers to {record_id!r}, which was not "
                    "read from the index for this query"
                )
            if record.edge_index != current_edge:
                return None
            if record.from_page != current_page:
                return None
            if not (
                record.from_start <= current_start
                and current_end <= record.from_end
            ):
                return None

            next_start = record.to_start + (
                current_start - record.from_start
            )
            next_end = record.to_start + (
                current_end - record.from_start
            )
            current_page = record.to_page  # type: ignore[attr-defined]
            current_start, current_end = next_start, next_end
            current_edge += 1

        return current_edge, current_page, current_start, current_end


def _find_part(
    parts: tuple[Segment, ...],
    starts: tuple[int, ...],
    coordinate: int,
) -> Segment:
    index = bisect_right(starts, coordinate) - 1
    if index < 0:
        raise IndexLoadError("internal interval error: coordinate before partition")
    return parts[index]


def migrate(
    store: IndexStore,
    *,
    source_edition: object,
    target_edition: object,
    page_id: str,
    start: int,
    end: int,
) -> MigrationResult:
    """Convenience function creating one engine for a single query."""

    return MigrationEngine(store).migrate(
        source_edition=source_edition,
        target_edition=target_edition,
        page_id=page_id,
        start=start,
        end=end,
    )
