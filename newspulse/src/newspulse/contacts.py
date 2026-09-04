"""The contact book: journalists the consultant knows, and how to reach them.

The pitch list can say who wrote a story. It can never say how to reach them —
feeds carry a byline sometimes and a contact never — so this is the one place in
the tool where contact details exist, and they arrive exactly one way: somebody
types them in.

Nothing here derives, guesses or completes an address. That is not squeamishness:
a plausible ``vorname.nachname@medium.de`` is worse than an empty field, because
it looks usable, gets used, and reaches the wrong person or nobody.

Lookup is case-insensitive on both name and outlet, because the same journalist
arrives from a feed as "Maria Berg" and gets typed as "maria berg", and a second
row for the same person is how a contact book stops being trusted.

Saving an entry also claims the letters that already went to that byline
(:func:`_link_released_letters`). The link on an outreach row is written at
release, but the pitch list invites the opposite order — write first, record the
contact afterwards — and without the repair those letters would sit outside the
journalist's file forever.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select, true, update
from sqlalchemy.orm import Session

from .models import Contact, Outreach  # Contact re-exported for the route layer


def find(session: Session, name: str, outlet: str = "") -> Contact | None:
    """The stored contact for this byline, or ``None``.

    Falls back to a match on the name alone when the outlet is unknown to the
    book: a freelancer filed once under a masthead the consultant never recorded
    is still the same person, and failing to find them would invite a duplicate.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None
    exact = session.scalars(
        select(Contact).where(
            func.lower(Contact.name) == wanted.lower(),
            func.lower(Contact.outlet) == (outlet or "").strip().lower(),
        )
    ).first()
    if exact is not None:
        return exact
    return session.scalars(
        select(Contact).where(func.lower(Contact.name) == wanted.lower())
    ).first()


def for_outlet(session: Session, outlet: str) -> list[Contact]:
    """Everyone recorded at one masthead — what to offer when a target has no
    byline but the outlet is known."""
    wanted = (outlet or "").strip()
    if not wanted:
        return []
    return list(
        session.scalars(
            select(Contact)
            .where(func.lower(Contact.outlet) == wanted.lower())
            .order_by(Contact.name)
        ).all()
    )


def list_all(session: Session, search: str = "") -> list[Contact]:
    """The whole book, optionally narrowed. Ordered by outlet then name, because
    that is how a pitch list is read: by masthead."""
    stmt = select(Contact).order_by(Contact.outlet, Contact.name)
    term = (search or "").strip()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(
            Contact.name.ilike(like)
            | Contact.outlet.ilike(like)
            | Contact.beat.ilike(like)
        )
    return list(session.scalars(stmt).all())


def _link_released_letters(session: Session, contact: Contact) -> int:
    """Point already-released letters at this entry. Returns how many were linked.

    ``Outreach.contact_id`` is written in exactly one place — at release, by
    looking the byline up in this book. The supported order is often the other way
    round: the pitch list offers "Kontakt hinterlegen" precisely for a byline the
    book does *not* have, so a letter goes out first and the contact is recorded
    afterwards. Nothing would ever connect the two, and the journalist's file
    would show an empty timeline and a zero count for somebody the agency
    demonstrably wrote to — indistinguishable on screen from never having written
    at all, which is the one sentence that page exists to prevent.

    Matched the way :func:`find` matches, and no wider — which means the entry's
    own masthead decides how far the claim reaches:

    * **The entry names a masthead.** The name matches case-insensitively and the
      outlet must be equal or absent on the letter. A letter to the same name at a
      *different* named masthead is left alone: two journalists sharing a name is
      likelier than one byline moving papers mid-book, and a wrong link here puts
      somebody else's letters in this file.
    * **The entry names none** — the Medium field left blank. Then every released
      letter under that byline is claimed, whichever masthead it went to, exactly
      as :func:`find` falls back to the name alone when the outlet is unknown to
      the book. An entry that declines to say where somebody writes cannot also
      insist the letters went to one place.

    Drafts stay unlinked. A draft's recipient is resolved when it is released, and
    claiming one now would put a letter nobody sent into a relationship file.
    """
    name = contact.name.strip()
    if not name:
        return 0
    house = (contact.outlet or "").strip()
    matches_outlet = (
        or_(func.lower(Outreach.outlet) == house.lower(), Outreach.outlet == "")
        if house
        else true()
    )
    linked = session.execute(
        update(Outreach)
        .where(
            Outreach.contact_id.is_(None),
            Outreach.released_at.is_not(None),
            func.lower(Outreach.journalist) == name.lower(),
            matches_outlet,
        )
        .values(contact_id=contact.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    session.commit()
    return linked


def _looks_like_address(value: str) -> bool:
    """Whether this could be a mailbox: one ``@`` with something either side, a
    dot in the domain, and no whitespace.

    Not a validator. RFC 5322 permits addresses no consultant will ever type and
    forbids none of the mistakes that actually happen here, so this rejects the
    empty field, the pasted name and the half-copied string — and lets everything
    else through to the mail server, which is the only thing that can really say.
    """
    if not value or any(ch.isspace() for ch in value):
        return False
    local, _, domain = value.partition("@")
    if not local or "." not in domain:
        return False
    return not domain.startswith(".") and not domain.endswith(".")


def _entry_for(
    session: Session, name: str, outlet: str, contact_id: int | None
) -> Contact:
    """The book's entry for this byline, created and added if there is none.

    Shared by :func:`save` and :func:`remember_address` so the two ways into the
    book cannot drift on the question of who counts as the same person — a
    disagreement there is how one journalist becomes two half-filled rows.

    Not committed here: the caller decides what it writes before flushing, and a
    row added and then abandoned is the caller's rollback to make.
    """
    contact = session.get(Contact, contact_id) if contact_id else None
    if contact is None:
        contact = find(session, name, outlet)
        # Only reuse a name-only match when it carries no outlet of its own;
        # otherwise this is the same person at a different masthead, which is a
        # separate contact by design.
        if (
            contact is not None
            and contact.outlet
            and outlet
            and contact.outlet.lower() != outlet.lower()
        ):
            contact = None
    if contact is None:
        contact = Contact(name=name, outlet=outlet)
        session.add(contact)
    return contact


def save(
    session: Session,
    *,
    contact_id: int | None = None,
    name: str,
    outlet: str = "",
    email: str = "",
    phone: str = "",
    beat: str = "",
    position: str = "",
    notes: str = "",
) -> Contact:
    """Create or update one contact. Raises ``ValueError`` on an empty name.

    An existing (name, outlet) is updated rather than duplicated, so saving the
    same person twice from two different pitch lists cannot split them into two
    half-filled rows.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Ein Kontakt braucht einen Namen.")
    house = (outlet or "").strip()

    contact = _entry_for(session, cleaned, house, contact_id)

    contact.name = cleaned
    contact.outlet = house or contact.outlet
    contact.email = (email or "").strip()
    contact.phone = (phone or "").strip()
    contact.beat = (beat or "").strip()
    contact.position = (position or "").strip()
    contact.notes = (notes or "").strip()
    session.commit()
    # A one-time repair per entry: letters that went out before this contact
    # existed still belong in their file. Run on an update too, because a
    # corrected outlet can bring a letter into range that was out of it.
    _link_released_letters(session, contact)
    return contact


def remember_address(
    session: Session,
    *,
    name: str,
    outlet: str = "",
    email: str,
    contact_id: int | None = None,
) -> Contact:
    """Give one recipient an address, touching nothing else on their entry.

    The letter card asks for exactly one field, so it must not go through
    :func:`save`: that writes every column, and saving a name and an address
    there would blank the phone, the beat and the notes of a contact who already
    had them.

    Creates the entry when the pitch list named somebody the book has never seen,
    which in practice is every letter — the book starts empty, and the address is
    the one thing standing between a written letter and a sent one. Before this
    existed the card could only link away to the contact form, so recording an
    address meant leaving the letter, filling in a full record and finding the
    way back.

    Raises ``ValueError`` on an empty name or an address that is not one. The
    check is deliberately shallow: it catches the typo and the pasted sentence,
    and refuses to be the arbiter of what a valid mailbox looks like.
    """
    person = (name or "").strip()
    if not person:
        raise ValueError("Ohne Namen lässt sich keine Adresse hinterlegen.")
    address = (email or "").strip()
    if not _looks_like_address(address):
        raise ValueError("Das ist keine E-Mail-Adresse.")
    contact = _entry_for(session, person, (outlet or "").strip(), contact_id)
    contact.email = address
    session.commit()
    # The same repair :func:`save` runs, and for the same reason: a letter
    # released before this address existed still belongs in the person's file.
    _link_released_letters(session, contact)
    return contact


def delete(session: Session, contact_id: int) -> bool:
    """Remove one contact. Returns whether there was one to remove."""
    contact = session.get(Contact, contact_id)
    if contact is None:
        return False
    session.delete(contact)
    session.commit()
    return True


__all__ = ["Contact", "delete", "find", "for_outlet", "list_all", "save"]
