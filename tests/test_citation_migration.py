from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from citation_migration.archive import archive_from_dict  # noqa: E402
from citation_migration.engine import MigrationEngine  # noqa: E402
from citation_migration.errors import (  # noqa: E402
    IllegalCoordinateError,
    IndexBuildError,
    IndexLoadError,
    MissingTableError,
)
from citation_migration.index import IndexStore, build_index  # noqa: E402
from citation_migration.model import Archive, MappingRecord  # noqa: E402


def two_edition_data() -> dict:
    return {
        "editions": [
            {"id": "v1", "pages": [{"id": "p", "length": 10}]},
            {"id": "v2", "pages": [{"id": "q", "length": 30}]},
        ],
        "tables": [
            {
                "from_edition": "v1",
                "to_edition": "v2",
                "mappings": [
                    {
                        "id": "m1",
                        "from_page": "p",
                        "from_start": 2,
                        "from_end": 5,
                        "to_page": "q",
                        "to_start": 10,
                        "to_end": 13,
                    }
                ],
            }
        ],
    }


class IndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self._stores: list[IndexStore] = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.close()
        self._temp.cleanup()

    def store_for(
        self,
        data: dict,
        *,
        name: str = "index.sqlite",
        require_all_tables: bool = False,
    ) -> IndexStore:
        archive = archive_from_dict(data)
        path = self.tmp / name
        store = build_index(
            archive,
            path,
            overwrite=True,
            require_all_tables=require_all_tables,
        )
        self._stores.append(store)
        return store

    def assertPartitionContinuous(self, result, start: int, end: int) -> None:
        if start == end:
            self.assertEqual(result.segments, ())
            return
        self.assertEqual(result.segments[0].start, start)
        self.assertEqual(result.segments[-1].end, end)
        for left, right in zip(result.segments, result.segments[1:]):
            self.assertEqual(left.end, right.start)

    def assertChainsApply(self, engine: MigrationEngine, result) -> None:
        source_index = result.source_edition_index
        page = result.page_id
        for segment in result.segments:
            for dest in segment.destinations:
                applied = engine._try_apply_chain(  # noqa: SLF001
                    dest.chain,
                    source_index,
                    page,
                    segment.start,
                    segment.end,
                )
                self.assertIsNotNone(applied, msg=f"chain failed for {dest}")
                _edge, got_page, got_start, got_end = applied
                self.assertEqual(
                    (got_page, got_start, got_end),
                    dest.location_key,
                    msg="chain does not reproduce reported landing",
                )


class BasicMigrationTests(IndexTestCase):
    def test_unique_lost_and_exact_offset(self) -> None:
        data = two_edition_data()
        data["editions"].append(
            {"id": "v3", "pages": [{"id": "f", "length": 40}]}
        )
        data["tables"].append(
            {
                "from_edition": "v2",
                "to_edition": "v3",
                "mappings": [
                    {
                        "id": "m2",
                        "from_page": "q",
                        "from_start": 10,
                        "from_end": 13,
                        "to_page": "f",
                        "to_start": 20,
                        "to_end": 23,
                    }
                ],
            }
        )
        store = self.store_for(data)
        engine = MigrationEngine(store)
        result = engine.migrate(
            source_edition="v1",
            target_edition="v3",
            page_id="p",
            start=0,
            end=10,
        )

        self.assertPartitionContinuous(result, 0, 10)
        self.assertEqual(
            [(s.start, s.end, s.status) for s in result.segments],
            [(0, 2, "lost"), (2, 5, "unique"), (5, 10, "lost")],
        )
        unique = result.segments[1]
        self.assertEqual(len(unique.destinations), 1)
        dest = unique.destinations[0]
        self.assertEqual(dest.location_key, ("f", 20, 23))
        self.assertEqual(dest.chain, ("m1", "m2"))
        self.assertChainsApply(engine, result)

        clipped = MigrationEngine(store).migrate(
            source_edition="v1",
            target_edition="v3",
            page_id="p",
            start=3,
            end=4,
        )
        self.assertEqual(clipped.segments[0].destinations[0].location_key,
                         ("f", 21, 22))

    def test_present_empty_table_means_lost_but_same_page_id_does_not_revive(self):
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": 10}]},
                {"id": "v2", "pages": [{"id": "p", "length": 10}]},
            ],
            "tables": [
                {"from_edition": "v1", "to_edition": "v2", "mappings": []}
            ],
        }
        store = self.store_for(data)
        calls = []
        original = store.fetch_overlaps

        def spy(edge, page, start, end):
            calls.append((edge, page, start, end))
            return original(edge, page, start, end)

        store.fetch_overlaps = spy  # type: ignore[method-assign]
        engine = MigrationEngine(store)
        result = engine.migrate(
            source_edition="v1",
            target_edition="v2",
            page_id="p",
            start=1,
            end=6,
        )
        self.assertEqual(result.overall_status, "lost")
        self.assertEqual(result.segments[0].start, 1)
        self.assertEqual(result.segments[0].end, 6)
        self.assertEqual(result.segments[0].destinations, ())
        self.assertEqual(calls, [(0, "p", 1, 6)])

    def test_missing_intermediate_table_is_error_not_deletion(self) -> None:
        data = two_edition_data()
        data["editions"].append(
            {"id": "v3", "pages": [{"id": "f", "length": 40}]}
        )
        # Deliberately provide only the v1 -> v2 table; v2 -> v3 is missing.
        store = self.store_for(data)
        with self.assertRaises(MissingTableError):
            MigrationEngine(store).migrate(
                source_edition="v1",
                target_edition="v3",
                page_id="p",
                start=0,
                end=2,  # outside m1 and already lost at first handoff
            )

    def test_strict_build_reports_missing_table(self) -> None:
        data = two_edition_data()
        data["editions"].append(
            {"id": "v3", "pages": [{"id": "f", "length": 40}]}
        )
        with self.assertRaises(MissingTableError):
            self.store_for(data, name="strict.sqlite", require_all_tables=True)

    def test_illegal_query_coordinates(self) -> None:
        store = self.store_for(two_edition_data())
        engine = MigrationEngine(store)
        with self.assertRaises(IllegalCoordinateError):
            engine.migrate(
                source_edition="v1", target_edition="v2",
                page_id="p", start=9, end=11,
            )
        with self.assertRaises(IllegalCoordinateError):
            engine.migrate(
                source_edition="v1", target_edition="v2",
                page_id="p", start=5, end=4,
            )

    def test_illegal_mapping_coordinates_rejected(self) -> None:
        bad = two_edition_data()
        bad["tables"][0]["mappings"][0]["from_end"] = 99
        with self.assertRaises(IllegalCoordinateError):
            archive_from_dict(bad)

        unequal = two_edition_data()
        unequal["tables"][0]["mappings"][0]["to_end"] = 14
        with self.assertRaises(IllegalCoordinateError):
            archive_from_dict(unequal)


class MultiPathTests(IndexTestCase):
    def test_multiple_distinct_destinations(self) -> None:
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": 10}]},
                {"id": "v2", "pages": [
                    {"id": "x", "length": 20},
                    {"id": "y", "length": 120},
                ]},
            ],
            "tables": [{
                "from_edition": "v1",
                "to_edition": "v2",
                "mappings": [
                    {"id": "a", "from_page": "p", "from_start": 0,
                     "from_end": 10, "to_page": "x", "to_start": 0,
                     "to_end": 10},
                    {"id": "b", "from_page": "p", "from_start": 0,
                     "from_end": 10, "to_page": "y", "to_start": 100,
                     "to_end": 110},
                ],
            }],
        }
        store = self.store_for(data)
        engine = MigrationEngine(store)
        result = engine.migrate(
            source_edition="v1", target_edition="v2",
            page_id="p", start=0, end=10,
        )
        self.assertEqual(result.overall_status, "multiple")
        self.assertEqual(len(result.segments[0].destinations), 2)
        self.assertChainsApply(engine, result)

    def test_converging_paths_count_once(self) -> None:
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": 4}]},
                {"id": "v2", "pages": [
                    {"id": "x", "length": 4},
                    {"id": "y", "length": 4},
                ]},
                {"id": "v3", "pages": [{"id": "f", "length": 20}]},
            ],
            "tables": [
                {"from_edition": "v1", "to_edition": "v2", "mappings": [
                    {"id": "a", "from_page": "p", "from_start": 0,
                     "from_end": 4, "to_page": "x", "to_start": 0,
                     "to_end": 4},
                    {"id": "b", "from_page": "p", "from_start": 0,
                     "from_end": 4, "to_page": "y", "to_start": 0,
                     "to_end": 4},
                ]},
                {"from_edition": "v2", "to_edition": "v3", "mappings": [
                    {"id": "c", "from_page": "x", "from_start": 0,
                     "from_end": 4, "to_page": "f", "to_start": 10,
                     "to_end": 14},
                    {"id": "d", "from_page": "y", "from_start": 0,
                     "from_end": 4, "to_page": "f", "to_start": 10,
                     "to_end": 14},
                ]},
            ],
        }
        store = self.store_for(data)
        engine = MigrationEngine(store)
        result = engine.migrate(
            source_edition="v1", target_edition="v3",
            page_id="p", start=0, end=4,
        )
        self.assertEqual(result.overall_status, "unique")
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(len(result.segments[0].destinations), 1)
        self.assertEqual(
            result.segments[0].destinations[0].location_key,
            ("f", 10, 14),
        )
        self.assertChainsApply(engine, result)

    def test_temporary_duplicate_with_one_survivor_is_unique_and_coalesced(self):
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": 10}]},
                {"id": "v2", "pages": [
                    {"id": "x", "length": 10},
                    {"id": "y", "length": 10},
                ]},
                {"id": "v3", "pages": [{"id": "f", "length": 10}]},
            ],
            "tables": [
                {"from_edition": "v1", "to_edition": "v2", "mappings": [
                    {"id": "a", "from_page": "p", "from_start": 0,
                     "from_end": 10, "to_page": "x", "to_start": 0,
                     "to_end": 10},
                    {"id": "b", "from_page": "p", "from_start": 0,
                     "from_end": 10, "to_page": "y", "to_start": 0,
                     "to_end": 10},
                ]},
                {"from_edition": "v2", "to_edition": "v3", "mappings": [
                    # The x copy survives only for old positions [0,5).
                    {"id": "c", "from_page": "x", "from_start": 0,
                     "from_end": 5, "to_page": "f", "to_start": 0,
                     "to_end": 5},
                    # The y copy survives for the whole citation.
                    {"id": "d", "from_page": "y", "from_start": 0,
                     "from_end": 10, "to_page": "f", "to_start": 0,
                     "to_end": 10},
                ]},
            ],
        }
        store = self.store_for(data)
        engine = MigrationEngine(store)
        result = engine.migrate(
            source_edition="v1", target_edition="v3",
            page_id="p", start=0, end=10,
        )
        self.assertEqual(len(result.segments), 1)
        segment = result.segments[0]
        self.assertEqual(segment.status, "unique")
        self.assertEqual(segment.destinations[0].location_key, ("f", 0, 10))
        self.assertEqual(segment.destinations[0].chain, ("b", "d"))
        self.assertChainsApply(engine, result)


class PointwiseOracleTests(IndexTestCase):
    def test_random_small_archives_match_positionwise_oracle(self) -> None:
        for seed in range(120):
            with self.subTest(seed=seed):
                data, source_page, start, end, target_index = _random_archive(seed)
                store = IndexStore.in_memory(archive_from_dict(data))
                self.addCleanup(store.close)
                engine = MigrationEngine(store)
                result = engine.migrate(
                    source_edition="ed0",
                    target_edition=f"ed{target_index}",
                    page_id=source_page,
                    start=start,
                    end=end,
                )
                self.assertPartitionContinuous(result, start, end)

                actual = {}
                for segment in result.segments:
                    for x in range(segment.start, segment.end):
                        offset = x - segment.start
                        actual[x] = {
                            (d.page_id, d.start + offset)
                            for d in segment.destinations
                        }

                expected = _pointwise_oracle(
                    data, source_page, start, end, target_index
                )
                self.assertEqual(actual, expected)
                self.assertChainsApply(engine, result)


def _random_archive(seed: int):
    rnd = random.Random(seed)
    target_index = rnd.randint(1, 3)
    editions = []
    page_ids = []
    for edition_index in range(target_index + 1):
        pages = []
        ids = []
        for page_index in range(rnd.randint(1, 2)):
            page_id = f"e{edition_index}p{page_index}"
            ids.append(page_id)
            pages.append({"id": page_id, "length": rnd.randint(4, 9)})
        page_ids.append(ids)
        editions.append({"id": f"ed{edition_index}", "pages": pages})

    tables = []
    record_number = 0
    for edge in range(target_index):
        mappings = []
        for _ in range(rnd.randint(0, 7)):
            from_page = rnd.choice(page_ids[edge])
            to_page = rnd.choice(page_ids[edge + 1])
            from_length = next(
                p["length"] for p in editions[edge]["pages"]
                if p["id"] == from_page
            )
            to_length = next(
                p["length"] for p in editions[edge + 1]["pages"]
                if p["id"] == to_page
            )
            length = rnd.randint(1, min(4, from_length, to_length))
            from_start = rnd.randint(0, from_length - length)
            to_start = rnd.randint(0, to_length - length)
            mappings.append({
                "id": f"r{record_number}",
                "from_page": from_page,
                "from_start": from_start,
                "from_end": from_start + length,
                "to_page": to_page,
                "to_start": to_start,
                "to_end": to_start + length,
            })
            record_number += 1
        tables.append({
            "from_edition": f"ed{edge}",
            "to_edition": f"ed{edge + 1}",
            "mappings": mappings,
        })

    source_page = rnd.choice(page_ids[0])
    page_length = next(
        p["length"] for p in editions[0]["pages"] if p["id"] == source_page
    )
    start = rnd.randint(0, page_length)
    end = rnd.randint(start, page_length)
    return (
        {"editions": editions, "tables": tables},
        source_page,
        start,
        end,
        target_index,
    )


def _pointwise_oracle(data, source_page, start, end, target_index):
    expected = {}
    for position in range(start, end):
        states = {(source_page, position)}
        for edge in range(target_index):
            next_states = set()
            for page, pos in states:
                for record in data["tables"][edge]["mappings"]:
                    if (
                        record["from_page"] == page
                        and record["from_start"] <= pos < record["from_end"]
                    ):
                        next_states.add((
                            record["to_page"],
                            record["to_start"] + pos - record["from_start"],
                        ))
            states = next_states
        expected[position] = states
    return expected


class IndexBehaviorTests(IndexTestCase):
    def test_overlap_index_plan_and_minimal_calls(self) -> None:
        data = two_edition_data()
        data["editions"].append(
            {"id": "v3", "pages": [{"id": "f", "length": 40}]}
        )
        data["tables"].append(
            {
                "from_edition": "v2",
                "to_edition": "v3",
                "mappings": [
                    {"id": "m2", "from_page": "q", "from_start": 10,
                     "from_end": 13, "to_page": "f", "to_start": 20,
                     "to_end": 23}
                ],
            }
        )
        store = self.store_for(data)
        plan_rows = store.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT record_id
            FROM mappings
            WHERE edge_index = ? AND from_page = ?
              AND from_start < ? AND from_end > ?
            """,
            (0, "p", 5, 2),
        ).fetchall()
        plan = "\n".join(row["detail"] for row in plan_rows)
        self.assertIn("SEARCH", plan)
        self.assertIn("idx_mappings_overlap", plan)
        self.assertNotIn("SCAN mappings", plan)

        calls = []
        original = store.fetch_overlaps

        def spy(edge, page, start, end):
            calls.append((edge, page, start, end))
            return original(edge, page, start, end)

        store.fetch_overlaps = spy  # type: ignore[method-assign]
        result = MigrationEngine(store).migrate(
            source_edition="v1", target_edition="v3",
            page_id="p", start=2, end=5,
        )
        self.assertEqual(result.overall_status, "unique")
        self.assertEqual(
            calls,
            [(0, "p", 2, 5), (1, "q", 10, 13)],
        )

    def test_large_interval_is_not_processed_position_by_position(self) -> None:
        huge = 10**9
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": huge}]},
                {"id": "v2", "pages": [{"id": "q", "length": huge}]},
            ],
            "tables": [{
                "from_edition": "v1", "to_edition": "v2",
                "mappings": [{
                    "id": "whole", "from_page": "p",
                    "from_start": 0, "from_end": huge,
                    "to_page": "q", "to_start": 0, "to_end": huge,
                }],
            }],
        }
        store = self.store_for(data)
        calls = []
        original = store.fetch_overlaps

        def spy(edge, page, start, end):
            calls.append((edge, page, start, end))
            return original(edge, page, start, end)

        store.fetch_overlaps = spy  # type: ignore[method-assign]
        result = MigrationEngine(store).migrate(
            source_edition="v1", target_edition="v2",
            page_id="p", start=0, end=huge,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].destinations[0].end, huge)
        self.assertEqual(len(calls), 1)

    def test_convergent_identical_intervals_are_memoized(self) -> None:
        data = {
            "editions": [
                {"id": "v1", "pages": [{"id": "p", "length": 4}]},
                {"id": "v2", "pages": [{"id": "x", "length": 4}]},
                {"id": "v3", "pages": [{"id": "f", "length": 4}]},
            ],
            "tables": [
                {"from_edition": "v1", "to_edition": "v2", "mappings": [
                    {"id": "z1", "from_page": "p", "from_start": 0,
                     "from_end": 4, "to_page": "x", "to_start": 0,
                     "to_end": 4},
                    {"id": "z2", "from_page": "p", "from_start": 0,
                     "from_end": 4, "to_page": "x", "to_start": 0,
                     "to_end": 4},
                ]},
                {"from_edition": "v2", "to_edition": "v3", "mappings": [
                    {"id": "next", "from_page": "x", "from_start": 0,
                     "from_end": 4, "to_page": "f", "to_start": 0,
                     "to_end": 4},
                ]},
            ],
        }
        store = self.store_for(data)
        calls = []
        original = store.fetch_overlaps

        def spy(edge, page, start, end):
            calls.append((edge, page, start, end))
            return original(edge, page, start, end)

        store.fetch_overlaps = spy  # type: ignore[method-assign]
        result = MigrationEngine(store).migrate(
            source_edition="v1", target_edition="v3",
            page_id="p", start=0, end=4,
        )
        self.assertEqual(result.overall_status, "unique")
        self.assertEqual(calls.count((1, "x", 0, 4)), 1)


    def test_repeated_diamonds_do_not_expand_every_path(self) -> None:
        diamonds = 14  # a naive path expander would enumerate 2**14 routes
        editions = [{"id": "e0", "pages": [{"id": "m0", "length": 10}]}]
        tables = []
        for k in range(diamonds):
            middle_index = 2 * k + 1
            merged_index = 2 * k + 2
            editions.append({
                "id": f"e{middle_index}",
                "pages": [
                    {"id": f"a{k}", "length": 10},
                    {"id": f"b{k}", "length": 10},
                ],
            })
            editions.append({
                "id": f"e{merged_index}",
                "pages": [{"id": f"m{k + 1}", "length": 10}],
            })
            tables.append({
                "from_edition": f"e{2*k}",
                "to_edition": f"e{middle_index}",
                "mappings": [
                    {"id": f"out-a{k}", "from_page": f"m{k}",
                     "from_start": 0, "from_end": 10, "to_page": f"a{k}",
                     "to_start": 0, "to_end": 10},
                    {"id": f"out-b{k}", "from_page": f"m{k}",
                     "from_start": 0, "from_end": 10, "to_page": f"b{k}",
                     "to_start": 0, "to_end": 10},
                ],
            })
            tables.append({
                "from_edition": f"e{middle_index}",
                "to_edition": f"e{merged_index}",
                "mappings": [
                    {"id": f"in-a{k}", "from_page": f"a{k}",
                     "from_start": 0, "from_end": 10, "to_page": f"m{k+1}",
                     "to_start": 0, "to_end": 10},
                    {"id": f"in-b{k}", "from_page": f"b{k}",
                     "from_start": 0, "from_end": 10, "to_page": f"m{k+1}",
                     "to_start": 0, "to_end": 10},
                ],
            })

        data = {"editions": editions, "tables": tables}
        with IndexStore.in_memory(archive_from_dict(data)) as store:
            calls = []
            original = store.fetch_overlaps

            def spy(edge, page, start, end):
                calls.append((edge, page))
                return original(edge, page, start, end)

            store.fetch_overlaps = spy  # type: ignore[method-assign]
            engine = MigrationEngine(store)
            result = engine.migrate(
                source_edition="e0",
                target_edition=f"e{2*diamonds}",
                page_id="m0",
                start=0,
                end=10,
            )
            self.assertEqual(result.overall_status, "unique")
            self.assertEqual(
                result.segments[0].destinations[0].location_key,
                (f"m{diamonds}", 0, 10),
            )
            # One source-page lookup per diamond plus the two distinct middle
            # pages; convergence lookup is memoized rather than path-enumerated.
            self.assertEqual(len(calls), 3 * diamonds)
            self.assertChainsApply(engine, result)


class AtomicIndexTests(IndexTestCase):
    def test_failed_rebuild_preserves_existing_queryable_index(self) -> None:
        archive = archive_from_dict(two_edition_data())
        index_path = self.tmp / "keep.sqlite"
        first = build_index(archive, index_path)
        first.close()

        # Bypass archive validation and force a SQLite primary-key failure.
        duplicate = MappingRecord(
            record_id="dup",
            edge_index=0,
            from_page="p",
            from_start=0,
            from_end=1,
            to_page="q",
            to_start=0,
            to_end=1,
        )
        bad_archive = Archive(
            editions=archive.editions,
            tables=((duplicate, duplicate),),
            edition_ids=archive.edition_ids,
        )
        with self.assertRaises(IndexBuildError):
            build_index(bad_archive, index_path, overwrite=True)

        leftovers = list(self.tmp.glob(f".{index_path.name}.*.building"))
        self.assertEqual(leftovers, [])

        with IndexStore.open(index_path) as restored:
            count = restored.conn.execute(
                "SELECT COUNT(*) AS n FROM mappings"
            ).fetchone()["n"]
            self.assertEqual(count, 1)
            result = MigrationEngine(restored).migrate(
                source_edition="v1", target_edition="v2",
                page_id="p", start=2, end=5,
            )
            self.assertEqual(
                result.segments[0].destinations[0].location_key,
                ("q", 10, 13),
            )

    def test_corrupt_index_load_failure_leaves_file_in_place(self) -> None:
        bad = self.tmp / "corrupt.sqlite"
        bad.write_bytes(b"this is definitely not a sqlite database")
        with self.assertRaises(IndexLoadError):
            IndexStore.open(bad)
        self.assertTrue(bad.exists())
        self.assertGreater(bad.stat().st_size, 0)


class CliTests(unittest.TestCase):
    def test_build_and_query_subprocess_under_isolated_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            archive_path = temp_path / "archive.json"
            index_path = temp_path / "index.sqlite"
            data = two_edition_data()
            archive_path.write_text(json.dumps(data), encoding="utf-8")
            tool = ROOT / "citation_tool.py"

            built = subprocess.run(
                [sys.executable, "-I", str(tool), "build-index",
                 str(archive_path), str(index_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(index_path.exists())

            queried = subprocess.run(
                [sys.executable, "-I", str(tool), "query", str(index_path),
                 "--source-edition", "v1", "--target-edition", "v2",
                 "--page", "p", "--start", "2", "--end", "5",
                 "--format", "json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(queried.returncode, 0, queried.stderr)
            payload = json.loads(queried.stdout)
            self.assertEqual(payload["overall_status"], "unique")
            self.assertEqual(
                payload["segments"][0]["destinations"][0]["page"], "q"
            )

    def test_missing_table_cli_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            archive_path = temp_path / "archive.json"
            index_path = temp_path / "index.sqlite"
            data = two_edition_data()
            data["editions"].append(
                {"id": "v3", "pages": [{"id": "f", "length": 4}]}
            )
            archive_path.write_text(json.dumps(data), encoding="utf-8")
            tool = ROOT / "citation_tool.py"
            subprocess.run(
                [sys.executable, "-I", str(tool), "build-index",
                 str(archive_path), str(index_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            queried = subprocess.run(
                [sys.executable, "-I", str(tool), "query", str(index_path),
                 "--source-edition", "v1", "--target-edition", "v3",
                 "--page", "p", "--start", "0", "--end", "1",
                 "--format", "json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(queried.returncode, 2)
            self.assertIn("缺少中间对照表", queried.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
