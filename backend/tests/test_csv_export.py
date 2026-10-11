"""CSV encoding and formula-injection protection (app/modules/admin/csv_export.py)."""

from __future__ import annotations

import csv
import io

import pytest

from app.modules.admin.csv_export import BOM, Kind, cell, encode_header, encode_rows, neutralise


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        '=HYPERLINK("http://evil.test","click")',
        "+1+2",
        "-2+3",
        "@SUM(A1:A9)",
        "=cmd|' /C calc'!A0",
        # hidden triggers: a spreadsheet ignores nothing, but a reader does not see these
        " =1+1",
        "   +1",
        "\t=1+1",
        "\r=1+1",
        "\n=1+1",
        "\x01=1+1",
        "\x1f@x",
        "\x7f=1",
        " =1+1",  # no-break space
        "​=1+1",  # zero-width space
        "⁠=1+1",  # word joiner
        "﻿=1+1",  # BOM
        " -1",  # line separator
        "　@a",  # ideographic space
        "\u0085=1",  # next line
        " \t  ​ =1",  # a mixture
    ],
)
def test_text_that_a_spreadsheet_could_run_gets_a_leading_apostrophe(value: str) -> None:
    safe = neutralise(value)
    assert safe == "'" + value
    assert cell(value, Kind.TEXT) == safe
    # ... also when the write goes through the CSV writer
    parsed = next(csv.reader(io.StringIO(encode_rows([[value]], [Kind.TEXT]).decode())))
    assert parsed == [safe]


@pytest.mark.parametrize("value", ["\t", "\tAda", "\rAda"])
def test_text_starting_with_a_tab_or_carriage_return_is_always_prefixed(value: str) -> None:
    assert neutralise(value) == "'" + value


@pytest.mark.parametrize(
    "value",
    [
        "Ada",
        "Əli",
        "İsmayıl",
        "ş ç ğ ı ö ü ə",
        "a=b",
        "a+b",
        "x-y",
        "name@example.test",
        "a @ b",
        "'quoted",
        "",
        "   ",
        "​",
        "1+1",
        "(=)",
        "https://www.linkedin.com/in/ada",
    ],
)
def test_ordinary_text_is_left_exactly_as_it_is(value: str) -> None:
    assert neutralise(value) == value


def test_server_generated_columns_are_never_changed() -> None:
    """A reversal's -5 must stay the number -5 (not the text '-5), booleans are
    plain words, timestamps and ids are untouched."""
    assert cell(-5, Kind.RAW) == "-5"
    assert cell(7, Kind.RAW) == "7"
    assert cell(True, Kind.RAW) == "true"
    assert cell(False, Kind.RAW) == "false"
    assert cell(None, Kind.RAW) == ""
    assert cell("2026-09-01T10:00:00Z", Kind.RAW) == "2026-09-01T10:00:00Z"
    assert cell("ACTIVE", Kind.RAW) == "ACTIVE"
    # ... and the same value typed as TEXT would have been neutralised
    assert cell("-5", Kind.TEXT) == "'-5"


def test_none_is_an_empty_cell_for_every_kind() -> None:
    for kind in Kind:
        assert cell(None, kind) == ""


@pytest.mark.parametrize(
    "phone",
    ["+994501234567", "+994 50 123 45 67", "+1 (555) 010-0100", "+44.20.7946.0958", "+1-1"],
)
def test_a_strict_phone_number_is_kept_intact(phone: str) -> None:
    assert cell(phone, Kind.PHONE) == phone


@pytest.mark.parametrize(
    "phone",
    [
        "+1+cmd|' /C calc'!A0",
        "+1 (555) 010-0100 =1",
        "+1@2",
        "+1!A1",
        "+a",
        "+",
        "=1+1",
        "-5",
        "@x",
        "+994\t=1",
        " +994501234567",  # leading space: not a strict phone, treated as text -> still safe
    ],
)
def test_anything_in_the_phone_column_that_is_not_a_plain_number_is_neutralised(
    phone: str,
) -> None:
    out = cell(phone, Kind.PHONE)
    assert out == neutralise(phone)
    if phone[:1] in "+=-@" or phone.lstrip()[:1] in "+=-@":
        assert out.startswith("'")


def test_phone_exception_is_limited_to_the_phone_column() -> None:
    assert cell("+994501234567", Kind.TEXT) == "'+994501234567"
    assert cell("+994501234567", Kind.RAW) == "+994501234567"


def test_commas_quotes_and_line_breaks_are_escaped_per_rfc_4180() -> None:
    raw = encode_rows(
        [['Əli, "Məmməd"\nİsmayıl', "plain", "a\r\nb"]], [Kind.TEXT, Kind.TEXT, Kind.TEXT]
    )
    text = raw.decode("utf-8")
    assert text == '"Əli, ""Məmməd""\nİsmayıl",plain,"a\r\nb"\r\n'
    assert next(csv.reader(io.StringIO(text, newline=""))) == [
        'Əli, "Məmməd"\nİsmayıl',
        "plain",
        "a\r\nb",
    ]


def test_azerbaijani_letters_survive_in_utf8() -> None:
    name = "Əhməd Çələbi Şirvanlı İbrahim Ğ Ö Ü ı ə ş ç ğ ö ü"
    raw = encode_rows([[name]], [Kind.TEXT])
    assert raw.decode("utf-8").rstrip("\r\n") == name
    assert name.encode("utf-8") in raw


def test_header_carries_the_byte_order_mark_that_excel_needs() -> None:
    header = encode_header(["id", "first_name"])
    assert header.startswith(b"\xef\xbb\xbf")
    assert header.decode("utf-8-sig") == "id,first_name\r\n"
    assert BOM == "﻿"
    assert not encode_header(["id"], bom=False).startswith(b"\xef\xbb\xbf")


def test_every_row_ends_with_crlf_and_columns_must_match() -> None:
    raw = encode_rows([[1, "a"], [2, "b"]], [Kind.RAW, Kind.TEXT])
    assert raw == b"1,a\r\n2,b\r\n"
    with pytest.raises(ValueError):
        encode_rows([[1, "a", "extra"]], [Kind.RAW, Kind.TEXT])
