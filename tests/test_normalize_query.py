"""Pure unit tests for normalize_query — no network call, per the Phase 2
checklist item: 'Implement the normalization step first ... unit-testable
with no network call.'

These specifically guard against the regression where catalog prefixes
(HD, HIP, GJ, TYC) were being stripped entirely instead of canonicalized.
SIMBAD's ident.id stores the prefix as part of the identifier itself, so
stripping it turns a resolvable name into an unresolvable one.
"""

import pytest

from app.catalogs.simbad import normalize_query


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  51   peg  ", "51 peg"),
        ("HD 217014", "HD 217014"),
        ("HD217014", "HD 217014"),
        ("hd 217014", "HD 217014"),
        ("hip113357", "HIP 113357"),
        ("gj 882", "GJ 882"),
        ("tyc 1234-5678-1", "TYC 1234-5678-1"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_query(raw, expected):
    assert normalize_query(raw) == expected


def test_normalize_query_preserves_prefix_not_just_number():
    """The core regression: the prefix must survive normalization, not be
    discarded. A bare '217014' is not equivalent to 'HD 217014' in SIMBAD."""
    result = normalize_query("HD217014")
    assert result.startswith("HD")
    assert result == "HD 217014"
