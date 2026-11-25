"""Offline citation migration across approved archival mapping tables."""

from .archive import archive_from_dict, load_archive
from .engine import MigrationEngine, migrate
from .errors import (
    ArchiveValidationError,
    CitationToolError,
    EditionNotFoundError,
    IllegalCoordinateError,
    IndexBuildError,
    IndexLoadError,
    MissingTableError,
    MigrationDirectionError,
    PageNotFoundError,
)
from .index import IndexStore, build_index
from .model import (
    Destination,
    MigrationResult,
    Segment,
)

__all__ = [
    "ArchiveValidationError",
    "CitationToolError",
    "Destination",
    "EditionNotFoundError",
    "IllegalCoordinateError",
    "IndexBuildError",
    "IndexLoadError",
    "IndexStore",
    "MigrationDirectionError",
    "MigrationEngine",
    "MigrationResult",
    "MissingTableError",
    "PageNotFoundError",
    "Segment",
    "archive_from_dict",
    "build_index",
    "load_archive",
    "migrate",
]
