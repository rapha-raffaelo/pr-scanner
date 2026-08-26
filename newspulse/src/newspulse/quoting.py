"""Marking text that came off the open web as material rather than instruction.

Headlines and feed summaries go into prompts that shell out to ``claude``. There
is no shell injection here — the subprocess takes a fixed argv with the prompt as
one argument and ``shell=False``, so nothing in a headline executes. What a
headline *can* do is read as an instruction to the model: a sentence in a German
feed saying "ignoriere die vorherigen Vorgaben" arrives in the same character
stream as the task.

The damage is bounded by what the output is for — a draft a person reads before
releasing it — but "bounded" is not "none", and the mailbox now has a send
permission behind two clicks. So the material is fenced and the fence is
explained: :file:`blocks/quoted_material.txt` tells the model that anything
between the markers is quoted text and never a job, and this module makes sure
the markers cannot be forged from inside.
"""

from __future__ import annotations

import re

#: Opens and closes a block of quoted material. Deliberately ugly and unlikely to
#: occur in prose, and stripped out of the material itself below, so a headline
#: cannot close the fence early and continue as if it were the prompt.
OPEN = "<<<ZITAT"
CLOSE = "ZITAT>>>"

_FORGERY = re.compile(r"<<<\s*ZITAT|ZITAT\s*>>>", re.IGNORECASE)


def scrub(text: str) -> str:
    """One piece of foreign text, with any attempt at forging the fence removed.

    Removed rather than escaped: this text is quoted into a prompt and read by a
    model, not parsed, so there is nothing to unescape it for. A headline that
    genuinely contained the marker would lose four angle brackets and keep its
    meaning.
    """
    return _FORGERY.sub("", text or "")


def fence(text: str, *, label: str = "") -> str:
    """A block of foreign text between markers, with a label saying what it is.

    Empty in, empty out: an unconditional fence around nothing is a prompt with a
    section that says only that the section is missing.
    """
    body = scrub(text).strip()
    if not body:
        return ""
    head = f"{OPEN} {label}".rstrip() if label else OPEN
    return f"{head}\n{body}\n{CLOSE}"
