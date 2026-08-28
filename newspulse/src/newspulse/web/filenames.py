"""What a mandate's downloaded artefacts are called.

Three of them now leave the application — the monthly report, the Pressespiegel
and the editorial plan — and they land in one folder on one consultant's machine.
The rule ``report._filename`` was written for is that they sort beside each
other there, which only holds while all three spell the mandate's name the same
way. Three copies of one regex is how that stops being true: the day somebody
widens the allow-list for a mandate called "Müller & Co." in one of them, that
mandate's plan sorts away from its reports and nothing says why.

So the half they share lives here, and each caller keeps its own prefix and its
own span — those genuinely differ, and pretending otherwise would be the other
kind of mistake.
"""

from __future__ import annotations

import re

#: What a name survives sanitising as when it survives as nothing at all — a name
#: written entirely in a script the allow-list drops. A file called
#: ``bericht__2026-07.html`` reads as broken software.
FALLBACK = "mandant"

#: ASCII, digits, underscore and hyphen. Deliberately narrow: the string goes
#: into a ``Content-Disposition`` header and then onto three filesystems with
#: three different opinions about what a filename may contain.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def client_slug(name: str) -> str:
    """A mandate's name as a filename may carry it, never empty."""
    return _UNSAFE.sub("_", name).strip("_") or FALLBACK


__all__ = ["FALLBACK", "client_slug"]
