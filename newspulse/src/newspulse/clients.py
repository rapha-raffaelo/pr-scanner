"""Client management: Excel/CSV import and a small CRUD service.

The PR agent keeps his portfolio in a spreadsheet whose column layout is his own,
so import is driven by an *explicit* column mapping (source-column -> client-field)
rather than a fixed positional order. Two entry points share one parser:

* ``preview_import(path, mapping)`` parses and validates the sheet and returns the
  rows **without** touching the database, so the UI can show a preview first.
* ``import_clients(path, mapping, session)`` parses, then creates or updates
  clients, matching existing rows by name (case-insensitive, trimmed) so a
  re-import of the same sheet never duplicates a client.

The array-ish fields (``aliases``, ``keywords``, ``alert_topics``) live in a single
delimited spreadsheet cell; they are split on comma or semicolon, trimmed, and
emptied entries dropped.

The CRUD helpers (create / update / deactivate / list) are the write path the
settings UI (NP-09) builds on. Deactivation is soft — it flips ``active`` to
``False`` and never deletes the client or, crucially, its articles and analyses:
the archive the agent owns is permanent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import DEFAULT_COUNTRY, Client

# The client fields an import may populate. ``active`` and ``created_at`` are set
# by the service/model, never by the sheet, so they are deliberately absent.
_IMPORTABLE_FIELDS = frozenset(
    {"name", "aliases", "industry", "country", "keywords", "alert_topics"}
)

# ``name`` is the only field a row cannot omit — it is both required data and the
# key existing clients are matched on.
_REQUIRED_FIELDS = frozenset({"name"})

# The three list-valued fields, each read from one delimited cell.
_ARRAY_FIELDS = frozenset({"aliases", "keywords", "alert_topics"})

# Optional scalar fields whose empty cell means "leave the default / existing
# value" rather than "store an empty string".
_OPTIONAL_SCALAR_FIELDS = frozenset({"industry", "country"})

# A delimited cell splits on either comma or semicolon so the agent can use
# whichever his spreadsheet already contains without reformatting.
_ARRAY_DELIMITER = re.compile(r"[;,]")

# The first spreadsheet data row is line 2 (line 1 is the header), and pandas
# indexes it as 0 — so a human-facing row number is the DataFrame index plus this.
_HEADER_OFFSET = 2

# Only the modern Excel container (.xlsx, read via openpyxl) and .csv are
# supported. The pre-2007 .xls format needs the ``xlrd`` engine, which is not a
# declared dependency, so advertising it would surface a raw pandas error.
_SUPPORTED_SUFFIXES = frozenset({".csv", ".xlsx"})


class ImportValidationError(ValueError):
    """A sheet or mapping is malformed: an unknown/absent column, an unmapped
    required field, or a row missing its name. The message names the offending
    row or column so the UI can show it inline."""


@dataclass
class ImportResult:
    """Outcome of an ``import_clients`` call: how many rows created vs. updated."""

    created: int = 0
    updated: int = 0
    clients: list[Client] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated


def _split_delimited(cell: str) -> list[str]:
    """Split one delimited cell into a clean list: trim each part, drop blanks."""
    if not cell:
        return []
    return [part.strip() for part in _ARRAY_DELIMITER.split(cell) if part.strip()]


def _read_dataframe(path: Path) -> pd.DataFrame:
    """Read an ``.xlsx``/``.csv`` into an all-string DataFrame.

    ``keep_default_na=False`` keeps literal strings like ``"NA"`` or ``"null"``
    intact (a client legitimately named "NA" must survive), and empty cells become
    ``""`` (not NaN) so downstream trimming and splitting never trip over a float.
    Header whitespace is stripped so a mapping key matches a column that was typed
    with a trailing space. Any low-level pandas/openpyxl read failure is re-raised
    as ``ImportValidationError`` so callers only ever see the one contract error.
    """
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ImportValidationError(
            f"Unsupported file type {suffix!r}; expected one of "
            f"{sorted(_SUPPORTED_SUFFIXES)}"
        )
    try:
        if suffix == ".csv":
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        else:
            frame = pd.read_excel(path, dtype=str, keep_default_na=False).fillna("")
    except FileNotFoundError as exc:
        raise ImportValidationError(f"File not found: {path}") from exc
    except (ValueError, OSError, BadZipFile, InvalidFileException) as exc:
        # ParserError/EmptyDataError subclass ValueError; a corrupt or mislabelled
        # workbook raises BadZipFile/InvalidFileException.
        raise ImportValidationError(f"Could not read {path.name}: {exc}") from exc
    frame.columns = [str(col).strip() for col in frame.columns]
    return frame


def _validate_mapping(mapping: dict[str, str], columns: list[str]) -> None:
    """Reject a mapping before touching any row: unknown target fields, source
    columns absent from the sheet, or a required field left unmapped."""
    unknown = set(mapping.values()) - _IMPORTABLE_FIELDS
    if unknown:
        raise ImportValidationError(
            f"Mapping targets unknown client field(s): {sorted(unknown)}"
        )
    missing_columns = [src for src in mapping if src not in columns]
    if missing_columns:
        raise ImportValidationError(
            f"Mapped source column(s) not found in sheet: {sorted(missing_columns)}"
        )
    unmapped_required = _REQUIRED_FIELDS - set(mapping.values())
    if unmapped_required:
        raise ImportValidationError(
            f"Required field(s) not mapped to any column: {sorted(unmapped_required)}"
        )


def _parse_row(row: pd.Series, mapping: dict[str, str], line_number: int) -> dict:
    """Turn one spreadsheet row into a client-field dict, validating name."""
    parsed: dict[str, str | list[str]] = {}
    for source_column, target_field in mapping.items():
        cell = str(row[source_column]).strip()
        if target_field in _ARRAY_FIELDS:
            parsed[target_field] = _split_delimited(cell)
        else:
            parsed[target_field] = cell
    if not parsed.get("name"):
        raise ImportValidationError(
            f"Row {line_number}: required field 'name' is empty"
        )
    return parsed


def preview_import(path: str | Path, mapping: dict[str, str]) -> list[dict]:
    """Parse and validate a sheet, returning one client-field dict per row.

    Reads and validates exactly as ``import_clients`` does but writes nothing, so
    the UI can show the parsed rows (and surface a validation error) before commit.
    """
    # Strip mapping keys the same way the sheet headers are stripped, so a source
    # column typed with stray whitespace still lines up with its column.
    mapping = {source.strip(): target for source, target in mapping.items()}
    frame = _read_dataframe(Path(path))
    _validate_mapping(mapping, list(frame.columns))
    return [
        _parse_row(row, mapping, index + _HEADER_OFFSET)
        for index, row in frame.iterrows()
    ]


def _normalize_name(name: str) -> str:
    """The match key for de-duplication: trimmed and lowercased."""
    return name.strip().lower()


def _assign_fields(client: Client, parsed: dict) -> None:
    """Copy parsed fields onto a client. Array fields overwrite (the sheet is
    authoritative); an empty optional scalar keeps the default/existing value."""
    for target_field, value in parsed.items():
        if target_field in _OPTIONAL_SCALAR_FIELDS and not value:
            continue
        setattr(client, target_field, value)


def import_clients(
    path: str | Path, mapping: dict[str, str], session: Session
) -> ImportResult:
    """Import clients from ``path`` using ``mapping``; create new, update existing.

    Existing clients are matched by name (case-insensitive, trimmed), so
    re-importing the same sheet updates in place and never duplicates. A single
    sheet that lists the same name twice collapses onto one client for the same
    reason. Nothing is committed until every row parses cleanly.
    """
    rows = preview_import(path, mapping)

    # Build the match index by querying only the names this sheet actually
    # references (case-insensitive, trimmed) rather than loading the whole table;
    # new rows are added to it as they are created so an intra-sheet duplicate also
    # collapses onto one client.
    wanted = {_normalize_name(parsed["name"]) for parsed in rows}
    by_name: dict[str, Client] = {}
    if wanted:
        stmt = select(Client).where(
            func.lower(func.trim(Client.name)).in_(list(wanted))
        )
        by_name = {_normalize_name(c.name): c for c in session.scalars(stmt).all()}

    result = ImportResult()
    for parsed in rows:
        key = _normalize_name(parsed["name"])
        existing = by_name.get(key)
        if existing is not None:
            _assign_fields(existing, parsed)
            result.updated += 1
            result.clients.append(existing)
        else:
            client = Client(name=parsed["name"])
            _assign_fields(client, parsed)
            session.add(client)
            by_name[key] = client
            result.created += 1
            result.clients.append(client)

    session.commit()
    return result


# --- CRUD service --------------------------------------------------------------


def create_client(
    session: Session,
    *,
    name: str,
    aliases: list[str] | None = None,
    industry: str | None = None,
    country: str = DEFAULT_COUNTRY,
    keywords: list[str] | None = None,
    alert_topics: list[str] | None = None,
) -> Client:
    """Create and persist a client. List defaults use ``None`` sentinels rather
    than mutable ``[]`` defaults to avoid a shared-list footgun."""
    client = Client(
        name=name,
        aliases=aliases or [],
        industry=industry,
        country=country,
        keywords=keywords or [],
        alert_topics=alert_topics or [],
    )
    session.add(client)
    session.commit()
    return client


def update_client(session: Session, client_id: int, **fields) -> Client:
    """Update the given fields on a client. Raises ``LookupError`` if it does not
    exist and ``ValueError`` on an unknown field name. (``ImportValidationError`` is
    scoped to the import pipeline; a CRUD field error is a distinct concern.)"""
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError(f"No client with id {client_id}")
    unknown = set(fields) - _IMPORTABLE_FIELDS - {"active"}
    if unknown:
        raise ValueError(f"Unknown client field(s): {sorted(unknown)}")
    for name, value in fields.items():
        setattr(client, name, value)
    session.commit()
    return client


def deactivate_client(session: Session, client_id: int) -> Client:
    """Soft-deactivate a client: flip ``active`` to ``False``. The client and all
    of its articles and analyses stay — the archive is permanent."""
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError(f"No client with id {client_id}")
    client.active = False
    session.commit()
    return client


def list_clients(session: Session, *, include_inactive: bool = False) -> list[Client]:
    """List clients ordered by name. Active-only by default; pass
    ``include_inactive=True`` to include soft-deactivated clients."""
    stmt = select(Client).order_by(func.lower(Client.name))
    if not include_inactive:
        stmt = stmt.where(Client.active.is_(True))
    return list(session.scalars(stmt).all())


__all__ = [
    "ImportResult",
    "ImportValidationError",
    "create_client",
    "deactivate_client",
    "import_clients",
    "list_clients",
    "preview_import",
    "update_client",
]
