import pytest

from services.client_status import (
    CLIENT_STATUS_OPTIONS,
    client_status_label,
    normalize_client_status,
)


def test_client_status_options_cover_sales_workflow():
    assert [item["value"] for item in CLIENT_STATUS_OPTIONS] == [
        "active",
        "undecided",
        "won",
        "archived",
    ]
    assert client_status_label("won") == "已成交"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "active"),
        ("", "active"),
        (" active ", "active"),
        ("pending", "undecided"),
        ("confirmed", "won"),
        ("closed_won", "won"),
        ("archived", "archived"),
    ],
)
def test_normalize_client_status_accepts_legacy_aliases(raw, expected):
    assert normalize_client_status(raw) == expected


def test_normalize_client_status_rejects_unknown_for_write_paths():
    assert normalize_client_status("paid", allow_default=False) is None
    assert normalize_client_status("definitely-not-a-status", allow_default=False) is None
