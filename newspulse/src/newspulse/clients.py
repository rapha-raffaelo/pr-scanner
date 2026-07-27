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
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import BadZipFile

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
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

# A country is stored as an ISO 3166-1 alpha-2 code (the model column is
# String(2)). SQLite ignores VARCHAR length, so a full name like "Deutschland"
# would silently persist and only fail on a stricter backend — the import enforces
# the width instead of trusting the column.
_COUNTRY_CODE_LEN = 2

# CSV encodings tried in order. German spreadsheets exported to CSV are frequently
# cp1252/latin-1, not UTF-8; utf-8-sig also transparently strips a BOM. latin-1
# decodes any byte sequence, so it is the last-resort catch-all.
_CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


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


def _read_csv_any_encoding(path: Path) -> pd.DataFrame:
    """Read a CSV, trying UTF-8 then the cp1252/latin-1 encodings common in German
    spreadsheet exports, so a non-UTF-8 portfolio doesn't fail with a cryptic
    ``UnicodeDecodeError``. Non-encoding read errors (e.g. a parser error) are left
    to propagate to ``_read_dataframe``'s handler."""
    last_error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            return pd.read_csv(
                path, dtype=str, keep_default_na=False, encoding=encoding
            )
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ImportValidationError(
        f"Could not decode {path.name} as any of {list(_CSV_ENCODINGS)}; "
        "re-save it as UTF-8"
    ) from last_error


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
            frame = _read_csv_any_encoding(path)
        else:
            frame = pd.read_excel(path, dtype=str, keep_default_na=False)
    except ImportValidationError:
        # An encoding-fallback failure already carries a clear message; don't let
        # the ValueError handler below (ImportValidationError subclasses it) rewrap
        # it into the generic "Could not read" text.
        raise
    except FileNotFoundError as exc:
        raise ImportValidationError(f"File not found: {path}") from exc
    except (ValueError, OSError, BadZipFile, InvalidFileException) as exc:
        # ParserError/EmptyDataError subclass ValueError; a corrupt or mislabelled
        # workbook raises BadZipFile/InvalidFileException.
        raise ImportValidationError(f"Could not read {path.name}: {exc}") from exc
    # Coerce NaN to "" for both read paths, not just Excel: a ragged row (fewer
    # fields than the header) yields NaN even with keep_default_na=False, and
    # str(float('nan')) is the truthy string 'nan' — which would pass the empty-name
    # check and store garbage. An explicit "" keeps trimming and splitting safe.
    frame = frame.fillna("")
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


def _normalize_country(cell: str, line_number: int) -> str:
    """Normalize a country cell to an uppercase 2-letter ISO code.

    An empty cell stays empty (import then keeps the client's default/existing
    country). A non-empty value that is not exactly two letters is rejected naming
    the row, so a full name like "Deutschland" can't silently pollute the
    ``String(2)`` column on SQLite (which ignores the length) and blow up on a
    stricter backend."""
    if not cell:
        return ""
    code = cell.upper()
    if len(code) != _COUNTRY_CODE_LEN or not code.isalpha():
        raise ImportValidationError(
            f"Row {line_number}: country {cell!r} is not a 2-letter ISO code "
            "(e.g. 'DE')"
        )
    return code


def _parse_row(
    row: pd.Series, mapping: dict[str, str], line_number: int
) -> dict[str, str | list[str]]:
    """Turn one spreadsheet row into a client-field dict, validating name."""
    parsed: dict[str, str | list[str]] = {}
    for source_column, target_field in mapping.items():
        cell = str(row[source_column]).strip()
        if target_field in _ARRAY_FIELDS:
            parsed[target_field] = _split_delimited(cell)
        elif target_field == "country":
            parsed[target_field] = _normalize_country(cell, line_number)
        else:
            parsed[target_field] = cell
    if not parsed.get("name"):
        raise ImportValidationError(
            f"Row {line_number}: required field 'name' is empty"
        )
    return parsed


def preview_import(
    path: str | Path, mapping: dict[str, str]
) -> list[dict[str, str | list[str]]]:
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
    """The match key for de-duplication: trimmed, NFC-normalized, and casefolded.

    ``str.casefold()`` (not ``.lower()``) does a full Unicode fold, so the German
    ``ß`` folds to ``ss`` and the customary uppercase spelling "GROSSE STRASSE AG"
    matches "Große Straße AG" instead of duplicating it. NFC first so a decomposed
    umlaut ("O" + combining diaeresis, e.g. from an odd export) matches its
    precomposed form rather than slipping the match and duplicating on re-import.
    """
    return unicodedata.normalize("NFC", name.strip()).casefold()


def _assign_fields(client: Client, parsed: dict[str, str | list[str]]) -> None:
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
    re-importing the same sheet updates in place and never duplicates. A
    soft-deactivated client that reappears in a sheet is reactivated (import is the
    primary way the agent adds clients, so re-importing a live portfolio must not
    leave rows invisible). A single sheet that lists the same name twice collapses
    onto one client, counted once. Nothing is committed until every row parses
    cleanly.
    """
    rows = preview_import(path, mapping)

    # Build the match index in Python, not SQL. SQLite's built-in lower()/trim()
    # fold only ASCII, so an uppercase umlaut (Ö/Ä/Ü) or a "ß" — pervasive in German
    # company names — would not match _normalize_name's full Unicode casefold, and a
    # re-import of "Öko AG" would create a duplicate instead of updating. All rows
    # are candidates for a match, including soft-deactivated ones, so a re-import
    # reactivates rather than duplicates. The portfolio is small, so loading
    # candidates and matching them the same way the wanted set is normalized is both
    # correct and cheap.
    wanted = {_normalize_name(parsed["name"]) for parsed in rows}
    by_name: dict[str, Client] = {}
    if wanted:
        by_name = {
            norm: client
            for client in session.scalars(select(Client)).all()
            if (norm := _normalize_name(client.name)) in wanted
        }

    # Track distinct clients touched this import so an intra-sheet duplicate name is
    # counted (and returned) once, not once per row. ``created_keys`` are the rows
    # that inserted a new client; everything else touched is an update.
    result = ImportResult()
    touched: dict[str, Client] = {}
    created_keys: set[str] = set()
    for parsed in rows:
        key = _normalize_name(parsed["name"])
        existing = by_name.get(key)
        if existing is not None:
            existing.active = True  # a re-imported client is a live one again
            _assign_fields(existing, parsed)
            touched[key] = existing
        else:
            client = Client(name=parsed["name"])
            _assign_fields(client, parsed)
            session.add(client)
            by_name[key] = client
            created_keys.add(key)
            touched[key] = client

    session.commit()
    result.created = len(created_keys)
    result.updated = len(touched) - len(created_keys)
    result.clients = list(touched.values())
    return result


# --- CRUD service --------------------------------------------------------------


def _find_active_duplicate(
    session: Session, normalized: str, *, exclude_id: int | None = None
) -> Client | None:
    """The active client whose name normalizes to ``normalized`` (case-insensitive,
    Unicode-folded, matching import's dedup), excluding ``exclude_id``, or ``None``.

    The invariant this enforces: at most one *active* client per normalized name.
    Two active same-name clients make an import's by-name dedup ambiguous — it would
    update only one and leave the other a stale orphan — so every write path that
    can activate a client (create, edit, reactivate) checks it here."""
    return next(
        (
            client
            for client in session.scalars(
                select(Client).where(Client.active.is_(True))
            ).all()
            if client.id != exclude_id and _normalize_name(client.name) == normalized
        ),
        None,
    )


def _duplicate_active_error(duplicate: Client) -> ValueError:
    """The shared error raised when a write would collide with an active same-name
    client, phrased so the settings UI can surface it inline."""
    return ValueError(
        f"An active client named {duplicate.name!r} already exists "
        f"(id {duplicate.id}); update it instead of creating a duplicate"
    )


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
    than mutable ``[]`` defaults to avoid a shared-list footgun.

    Raises ``ValueError`` if an active client already exists under the same name
    (matched the same case-insensitive, Unicode-folded way import matches): two
    active same-name clients would make an import's by-name dedup ambiguous —
    updating only one of them — so the CRUD path guards the invariant too."""
    duplicate = _find_active_duplicate(session, _normalize_name(name))
    if duplicate is not None:
        raise _duplicate_active_error(duplicate)
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
    exist and ``ValueError`` on an unknown field name, an empty ``name``, or an
    edit/reactivation that would collide with another active same-name client.
    (``ImportValidationError`` is scoped to the import pipeline; a CRUD field error
    is a distinct concern.)"""
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError(f"No client with id {client_id}")
    unknown = set(fields) - _IMPORTABLE_FIELDS - {"active", "is_competitor"}
    if unknown:
        raise ValueError(f"Unknown client field(s): {sorted(unknown)}")
    if "name" in fields and not str(fields["name"]).strip():
        raise ValueError("A client name cannot be empty")
    # Guard the "at most one active client per normalized name" invariant on the
    # write paths create_client can't see: an edit that renames onto another active
    # client, or a reactivation of a name a live client already holds. Compute the
    # post-update state — an omitted field keeps the client's current value.
    effective_active = bool(fields.get("active", client.active))
    effective_name = str(fields.get("name", client.name))
    if effective_active:
        duplicate = _find_active_duplicate(
            session, _normalize_name(effective_name), exclude_id=client.id
        )
        if duplicate is not None:
            raise _duplicate_active_error(duplicate)
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
    ``include_inactive=True`` to include soft-deactivated clients.

    Ordered in Python with ``str.lower`` rather than SQL ``lower()`` so casing folds
    consistently for umlaut names (SQLite's ``lower()`` folds only ASCII). Full
    locale-aware German collation (ä→a, ö→o) is out of scope and would need ICU."""
    stmt = select(Client)
    if not include_inactive:
        stmt = stmt.where(Client.active.is_(True))
    return sorted(session.scalars(stmt).all(), key=lambda c: c.name.lower())


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
