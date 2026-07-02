import datetime
from decimal import Decimal

import pytest
import requests

from bittytax.conv.datarow import DataRow
from bittytax.conv.parsers.binance import (
    BASE_ASSETS,
    QUOTE_ASSETS,
    _split_trading_pair,
    parse_binance_statements,
)

_STMT_HEADER = ["User_ID", "UTC_Time", "Account", "Operation", "Coin", "Change", "Remark"]


def _stmt_row(utc_time: str) -> DataRow:
    row = ["1", utc_time, "Spot", "Deposit", "BTC", "0.1", ""]
    return DataRow(1, row, _STMT_HEADER, "Binance S")


def test_split_trading_pair() -> None:
    response = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10)

    if response:
        for symbol in response.json()["symbols"]:
            quote = symbol["quoteAsset"]
            base = symbol["baseAsset"]

            assert quote in QUOTE_ASSETS

            if base[0].isdigit():
                assert base in BASE_ASSETS

            bt_base, bt_quote = _split_trading_pair(symbol["symbol"])

            assert bt_base == base
            assert bt_quote == quote


def test_statements_timestamp_day_le_12_not_inverted() -> None:
    # Non-régression F-07 / ADR 0005 : le format Binance Statements "YY-MM-DD HH:MM:SS" est parsé
    # EXPLICITEMENT (strptime), donc une date à jour <= 12 (ambiguë sous une heuristique month-first)
    # est lue comme YYYY-MM-DD sans inversion mois/jour. "25-01-05" DOIT être le 5 janvier 2025 —
    # jamais le 1er mai. Une inversion corromprait l'ordre chronologique -> pta -> plus-value.
    dr = _stmt_row("25-01-05 10:00:00")
    parse_binance_statements([dr], None)
    assert dr.timestamp == datetime.datetime(2025, 1, 5, 10, 0, 0, tzinfo=datetime.timezone.utc)


def test_statements_timestamp_is_tagged_utc() -> None:
    # Les exports statements sont horodatés UTC (ADR 0019) : le timestamp produit est aware/UTC.
    dr = _stmt_row("20-10-17 15:38:09")
    parse_binance_statements([dr], None)
    assert dr.timestamp == datetime.datetime(2020, 10, 17, 15, 38, 9, tzinfo=datetime.timezone.utc)


def test_statements_timestamp_wrong_format_fails_loud() -> None:
    # Fail-loud (principe transverse) : si le format d'export dérive (ici un YYYY-MM-DD à 4
    # chiffres d'année), le parser LÈVE au lieu de retomber sur un (mé)parse silencieux qui
    # fausserait la date et l'ordre chronologique.
    dr = _stmt_row("2025-01-05 10:00:00")
    with pytest.raises(RuntimeError, match="format inattendu"):
        parse_binance_statements([dr], None)
