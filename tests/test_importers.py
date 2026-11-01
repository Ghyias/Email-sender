"""Tabular importer edge cases."""

from __future__ import annotations

from app.importers.tabular import (
    guess_mapping,
    load_tabular,
    normalize_email,
    prepare_contacts,
)


def test_csv_bom_and_semicolon() -> None:
    raw = "\ufeffName;E-mail;University\nAda;ada@mit.edu;MIT\n".encode("utf-8-sig")
    df, _ = load_tabular(raw, "x.csv")
    mapping = guess_mapping(list(df.columns))
    assert mapping["email"]
    assert mapping["full_name"]
    upserts, report = prepare_contacts(
        [{"email": "ada@mit.edu", "full_name": "Ada", "institution": "MIT"}],
        groups=["professor"],
        region_type="western",
    )
    assert report.imported == 1
    assert upserts[0]["email"] == "ada@mit.edu"


def test_duplicate_and_invalid() -> None:
    rows = [
        {"email": "a@x.com", "full_name": "A"},
        {"email": "a@x.com", "full_name": "A2"},
        {"email": "not-an-email", "full_name": "B"},
    ]
    upserts, report = prepare_contacts(rows)
    assert report.imported == 1
    assert report.skipped_duplicate == 1
    assert report.invalid == 1


def test_txt_name_angle_email() -> None:
    raw = b"Ada Lovelace <ada@mit.edu>\nbadline\ncharles@ox.ac.uk\n"
    df, _ = load_tabular(raw, "x.txt")
    assert len(df) >= 2
    assert "ada@mit.edu" in df["email"].values


def test_header_on_row_2_csv() -> None:
    raw = b"report title\nName,Email\nBob,bob@uni.edu\n"
    df, _ = load_tabular(raw, "x.csv")
    assert "Email" in df.columns or "email" in [c.lower() for c in df.columns]
    assert len(df) == 1


def test_normalize_email() -> None:
    email, err = normalize_email(" Ada@MIT.EDU ")
    assert err is None
    assert email == "ada@mit.edu"
