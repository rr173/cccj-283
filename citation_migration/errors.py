"""Errors for the offline citation migration tool."""

from __future__ import annotations


class CitationToolError(Exception):
    """Base class for all expected, user-facing errors."""


class ArchiveValidationError(CitationToolError):
    """The supplied archive description is structurally or semantically invalid."""


class MissingTableError(CitationToolError):
    """A required adjacent-edition comparison table is absent.

    This must not be interpreted as an empty table or as content deletion.
    """


class IllegalCoordinateError(CitationToolError):
    """A page interval or mapping coordinate is outside its legal domain."""


class EditionNotFoundError(CitationToolError):
    """An edition id or index does not exist in the index."""


class PageNotFoundError(CitationToolError):
    """A page id does not exist in the specified edition."""


class MigrationDirectionError(CitationToolError):
    """The requested target edition is earlier than the source edition."""


class IndexBuildError(CitationToolError):
    """The SQLite disk index could not be built atomically."""


class IndexLoadError(CitationToolError):
    """An existing SQLite index could not be opened or verified."""
