"""Command line interface for the citation migration tool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from .errors import CitationToolError
from .index import IndexStore, build_index
from .engine import MigrationEngine


STATUS_ZH = {
    "unique": "唯一落点",
    "multiple": "多处落点",
    "lost": "无去向",
    "mixed": "混合结果",
    "empty": "空区间",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citation_tool",
        description=(
            "Offline migration of old archival citations using only approved, "
            "adjacent-edition mapping tables."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build-index", help="build an atomic SQLite disk index")
    build_p.add_argument("archive", type=Path, help="validated archive JSON file")
    build_p.add_argument("index", type=Path, help="destination SQLite index path")
    build_p.add_argument(
        "--force",
        action="store_true",
        help="atomically replace an existing index after successful rebuild",
    )
    build_p.add_argument(
        "--strict-tables",
        action="store_true",
        help="fail if any adjacent-edition table in the whole volume is missing",
    )
    build_p.set_defaults(func=cmd_build)

    query_p = sub.add_parser("query", help="migrate one old page interval")
    query_p.add_argument("index", type=Path, help="prebuilt SQLite index")
    query_p.add_argument("--source-edition", required=True, help="source edition id")
    query_p.add_argument("--target-edition", required=True, help="target edition id")
    page_group = query_p.add_mutually_exclusive_group(required=True)
    page_group.add_argument("--page", dest="page_id", help="edition-local page id")
    page_group.add_argument(
        "--page-index",
        "--page-number",
        type=int,
        dest="page_index",
        help="zero-based page ordinal inside the source edition",
    )
    query_p.add_argument("--start", type=int, required=True, help="interval start")
    query_p.add_argument("--end", type=int, required=True, help="interval end (exclusive)")
    query_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    query_p.add_argument(
        "--fail-if-not-unique",
        action="store_true",
        help="exit with status 4 unless the whole citation has one unique verdict",
    )
    query_p.set_defaults(func=cmd_query)

    return parser


def cmd_build(args: argparse.Namespace) -> int:
    store = build_index(
        args.archive,
        args.index,
        overwrite=args.force,
        require_all_tables=args.strict_tables,
    )
    with store:
        missing = store.conn.execute(
            """
            SELECT edge_index, from_edition_id, to_edition_id
            FROM edges
            WHERE table_present = 0
            ORDER BY edge_index
            """
        ).fetchall()
        for row in missing:
            print(
                "警告: 缺少对照表 "
                f"{row['from_edition_id']} -> {row['to_edition_id']}; "
                "跨越该交接的查询会报错，不会按删除处理。",
                file=sys.stderr,
            )
        count = store.conn.execute("SELECT COUNT(*) AS n FROM mappings").fetchone()["n"]
    print(f"已建立索引: {args.index}（对照记录 {count} 条）")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with IndexStore.open(args.index) as store:
        source_edition = store.edition_by_id(args.source_edition)
        if args.page_id is not None:
            page_id = args.page_id
        else:
            if args.page_index < 0 or args.page_index >= len(source_edition.pages):
                raise CitationToolError(
                    f"page index {args.page_index} is outside source edition "
                    f"{args.source_edition!r}"
                )
            page_id = source_edition.pages[args.page_index].page_id

        result = MigrationEngine(store).migrate(
            source_edition=args.source_edition,
            target_edition=args.target_edition,
            page_id=page_id,
            start=args.start,
            end=args.end,
        )

    if args.format == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_text(result)

    if args.fail_if_not_unique and result.overall_status != "unique":
        return 4
    return 0


def print_text(result: object) -> None:
    data = result.to_dict()  # type: ignore[attr-defined]
    print(
        f"旧引用 {data['source_edition']}/{data['source_page']}:"
        f"[{data['source_interval'][0]},{data['source_interval'][1]})"
        f" -> 新版 {data['target_edition']}"
    )
    print(f"总体判定: {STATUS_ZH.get(data['overall_status'], data['overall_status'])}")
    lengths = data["lengths_by_status"]
    print(
        "长度统计: "
        f"唯一={lengths['unique']}, 多处={lengths['multiple']}, "
        f"无去向={lengths['lost']}"
    )

    for segment in data["segments"]:
        lo, hi = segment["interval"]
        status = STATUS_ZH[segment["status"]]
        if segment["status"] == "lost":
            print(f"  [{lo},{hi}) {status}")
            continue

        print(f"  [{lo},{hi}) {status}:")
        for dest in segment["destinations"]:
            dlo, dhi = dest["interval"]
            chain = " -> ".join(dest["chain"]) if dest["chain"] else "(原版位，无交接)"
            print(f"    -> {dest['page']}:[{dlo},{dhi})  凭据链: {chain}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CitationToolError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
