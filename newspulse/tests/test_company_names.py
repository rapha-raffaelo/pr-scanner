"""Legal-form handling in company names (newspulse.company_names).

The case this exists for: a client entered as "IB-7 Beauty Tech GmbH" matched none
of the seven Google News items about it, because every headline writes "IB-7" or
"IB-7 Beauty Tech" and the matcher searches the entered name as a whole phrase.
"""

from __future__ import annotations

import pytest

from newspulse.company_names import strip_legal_form, variants


@pytest.mark.parametrize(
    ("entered", "expected"),
    [
        ("IB-7 Beauty Tech GmbH", "IB-7 Beauty Tech"),
        ("Siemens AG", "Siemens"),
        ("Zalando SE", "Zalando"),
        ("Beiersdorf Aktiengesellschaft", "Beiersdorf Aktiengesellschaft"),  # not listed
        ("Robert Bosch GmbH & Co. KG", "Robert Bosch"),
        ("Miele & Cie. KG", "Miele"),
        ("Beispiel UG (haftungsbeschränkt)", "Beispiel"),
        ("Muster G.m.b.H.", "Muster"),
        ("Example Ltd.", "Example"),
        ("Voorbeeld B.V.", "Voorbeeld"),
    ],
)
def test_trailing_legal_form_is_dropped(entered, expected):
    assert strip_legal_form(entered) == expected


@pytest.mark.parametrize(
    "entered",
    [
        "Zalando",  # nothing to strip
        "Marks & Spencer",  # the scan stops at "Spencer", so "&" survives
        "Gesellschaft für Konsumforschung",  # a form-ish word mid-name is untouched
        "AG",  # a name that *is* a legal form keeps it, or it would vanish
        "K GmbH",  # stripping would leave one character — too short to be a term
        "Nordisk AS",  # deliberately not listed: collides with ordinary words
        "Ericsson AB",
    ],
)
def test_names_that_must_be_left_alone(entered):
    assert strip_legal_form(entered) == entered


def test_variants_keep_the_entered_form_first():
    """The matcher wants both; the form the operator typed leads.

    Order matters wherever a caller has a term budget — it spends the entered
    name before the derived one.
    """
    assert variants("IB-7 Beauty Tech GmbH") == ["IB-7 Beauty Tech GmbH", "IB-7 Beauty Tech"]


def test_variants_of_a_name_without_a_legal_form_are_just_the_name():
    """No duplicate term for the (common) case of a plain brand name."""
    assert variants("Zalando") == ["Zalando"]


def test_variants_of_blank_is_empty():
    """A client with no usable name yields no terms rather than a blank one."""
    assert variants("   ") == []
