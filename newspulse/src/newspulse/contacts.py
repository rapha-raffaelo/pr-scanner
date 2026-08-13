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
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Contact  # re-exported for the route layer


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


def save(
    session: Session,
    *,
    contact_id: int | None = None,
    name: str,
    outlet: str = "",
    email: str = "",
    phone: str = "",
    beat: str = "",
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

    contact = session.get(Contact, contact_id) if contact_id else None
    if contact is None:
        contact = find(session, cleaned, house)
        # Only reuse a name-only match when it carries no outlet of its own;
        # otherwise this is the same person at a different masthead, which is a
        # separate contact by design.
        if contact is not None and contact.outlet and house and contact.outlet.lower() != house.lower():
            contact = None
    if contact is None:
        contact = Contact(name=cleaned, outlet=house)
        session.add(contact)

    contact.name = cleaned
    contact.outlet = house or contact.outlet
    contact.email = (email or "").strip()
    contact.phone = (phone or "").strip()
    contact.beat = (beat or "").strip()
    contact.notes = (notes or "").strip()
    session.commit()
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
