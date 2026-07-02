# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2019

import csv
import os
import re
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Set

from colorama import Fore
from typing_extensions import Unpack

from ...bt_types import TrType
from ...config import config
from ...constants import WARNING
from ..dataparser import DataParser, ParserArgs, ParserType
from ..datarow import TxRawPos
from ..exceptions import DataFilenameError, DataRowError, UnknownCryptoassetError
from ..out_record import TransactionOutRecord

if TYPE_CHECKING:
    from ..datarow import DataRow


def _load_evm_override_txids() -> Set[str]:
    # Txids of EVM transactions requalified manually via a BittyTax-format CSV imported
    # alongside the explorer exports (pointed to by EVM_OVERRIDES_FILE). The explorer export
    # only carries the native leg of a DEX swap (e.g. "Swap Exact ETH For Tokens" on BNB Chain
    # exports only the BNB leg), so it parses as an orphan Withdrawal. The override CSV provides
    # the full Trade (sell native / buy token); we skip these txids here to avoid double-counting
    # the native leg. See docs/adr/0006 and datas/evm_overrides.csv.
    path = os.environ.get("EVM_OVERRIDES_FILE", "")
    txids: Set[str] = set()
    if not path or not os.path.exists(path):
        return txids
    with open(path, newline="", encoding="utf-8") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            txid = (row.get("Tx ID") or "").strip().lower()
            row_type = (row.get("Type") or "").strip()
            # Garde anti-décalage silencieux (fail-loud, symétrique au fix Binance e709b37) : un
            # `Trade` du CSV EVM requalifie un swap dont SEULE la jambe native est exportée par
            # l'explorateur — il DOIT porter le `Tx ID` de cette jambe pour l'exclure. Un `Tx ID`
            # vide sur un `Trade` (typiquement : une virgule non quotée dans `Note` a décalé les
            # colonnes) désarmerait le skip → la jambe native survivrait en Withdrawal orphelin →
            # double-comptage + transfers mismatch. On lève plutôt que de l'avaler en silence.
            # ⚠ NE PAS exiger de `Tx ID` sur les lignes d'AJOUT pur (ex. `reflection` = Gift-Received,
            # ADR 0014) : elles n'ont pas d'origine à skipper, leur `Tx ID` est vide à dessein.
            # Le discriminant est donc le Type (`Trade` = skip-row), pas l'absence de clé.
            if row_type == "Trade" and not txid:
                raise RuntimeError(
                    f"{path}: ligne {line_no}: `Trade` d'override sans `Tx ID` "
                    f"(clé d'exclusion manquante). Cause probable : une virgule non quotée dans "
                    f"la colonne `Note` a décalé les colonnes. Quoter la cellule `Note`. "
                    f"Ligne lue : {row}"
                )
            if txid:
                txids.add(txid)
    return txids


EVM_OVERRIDE_TXIDS = _load_evm_override_txids()


def parse_etherscan(data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(int(row_dict["UnixTimestamp"]))
    if "Txhash" in row_dict:
        tx_hash_pos = parser.in_header.index("Txhash")
        tx_hash = row_dict["Txhash"]
    else:
        tx_hash_pos = parser.in_header.index("Transaction Hash")
        tx_hash = row_dict["Transaction Hash"]

    # Native-leg row of a swap requalified via EVM_OVERRIDES_FILE: skip it so the imported
    # override CSV provides the full Trade without double-counting the native leg.
    if tx_hash.strip().lower() in EVM_OVERRIDE_TXIDS:
        return

    data_row.tx_raw = TxRawPos(
        tx_hash_pos,
        parser.in_header.index("From"),
        parser.in_header.index("To"),
    )

    # Skip over any args which are not regex
    matches = [arg for arg in parser.args if isinstance(arg, re.Match)]

    value_in_hdr = matches[0].group(1)
    buy_asset = matches[0].group(2)
    value_out_hdr = matches[1].group(1)
    sell_asset = matches[1].group(2)
    txn_fee_hdr = matches[2].group(1)
    fee_asset = matches[2].group(2)

    if row_dict["Status"] != "":
        # Failed transactions should not have a Value_OUT
        row_dict[value_out_hdr] = "0"

    if Decimal(row_dict[value_in_hdr]) > 0:
        if row_dict["Status"] == "":
            data_row.t_record = TransactionOutRecord(
                TrType.DEPOSIT,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict[value_in_hdr]),
                buy_asset=buy_asset,
                wallet=_get_wallet(fee_asset, row_dict["To"]),
                note=_get_note(row_dict),
            )
        data_row.worksheet_name = _get_worksheet_name(parser, fee_asset, row_dict["To"])
    elif Decimal(row_dict[value_out_hdr]) > 0:
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict[value_out_hdr]),
            sell_asset=sell_asset,
            fee_quantity=Decimal(row_dict[txn_fee_hdr]),
            fee_asset=fee_asset,
            wallet=_get_wallet(fee_asset, row_dict["From"]),
            note=_get_note(row_dict),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, fee_asset, row_dict["From"])
    else:
        data_row.t_record = TransactionOutRecord(
            TrType.SPEND,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict[value_out_hdr]),
            sell_asset=sell_asset,
            fee_quantity=Decimal(row_dict[txn_fee_hdr]),
            fee_asset=fee_asset,
            wallet=_get_wallet(fee_asset, row_dict["From"]),
            note=_get_note(row_dict),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, fee_asset, row_dict["From"])


def _get_wallet(chain: str, address: str) -> str:
    return f"{chain}-{address.lower()[0 : TransactionOutRecord.WALLET_ADDR_LEN]}"


def _get_worksheet_name(parser: DataParser, chain: str, address: str) -> str:
    wallet = _get_wallet(chain, address)
    return f"{parser.worksheet_name} {wallet}"


def _get_note(row_dict: Dict[str, str]) -> str:
    if row_dict["Status"] != "":
        if row_dict.get("Method"):
            return f'Failure ({row_dict["Method"]})'
        return "Failure"

    if row_dict.get("Method"):
        return row_dict["Method"]

    return row_dict.get("PrivateNote", "")


def parse_etherscan_internal(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(int(row_dict["UnixTimestamp"]))
    if "Txhash" in row_dict:
        tx_hash_pos = parser.in_header.index("Txhash")
    else:
        tx_hash_pos = parser.in_header.index("Transaction Hash")

    data_row.tx_raw = TxRawPos(
        tx_hash_pos,
        parser.in_header.index("From"),
        parser.in_header.index("TxTo"),
    )

    # Skip over any args which are not matches
    matches = [arg for arg in parser.args if isinstance(arg, re.Match)]

    value_in_hdr = matches[1].group(1)
    buy_asset = matches[1].group(2)
    value_out_hdr = matches[2].group(1)
    sell_asset = matches[2].group(2)

    # Failed internal transaction
    if row_dict["Status"] != "0":
        return

    if Decimal(row_dict[value_in_hdr]) > 0:
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict[value_in_hdr]),
            buy_asset=buy_asset,
            wallet=_get_wallet(buy_asset, row_dict["TxTo"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, buy_asset, row_dict["TxTo"])
    elif Decimal(row_dict[value_out_hdr]) > 0:
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict[value_out_hdr]),
            sell_asset=sell_asset,
            wallet=_get_wallet(sell_asset, row_dict["From"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, sell_asset, row_dict["From"])


def parse_etherscan_tokens(
    data_rows: List["DataRow"], parser: DataParser, **kwargs: Unpack[ParserArgs]
) -> None:
    symbol = kwargs["cryptoasset"]
    if not symbol:
        sys.stderr.write(f"{WARNING} Native cryptoasset for this chain cannot be identified\n")
        sys.stderr.write(f"{Fore.RESET}Enter symbol: ")
        symbol = input()
        if not symbol:
            raise UnknownCryptoassetError(kwargs["filename"], kwargs.get("worksheet", ""))

    for data_row in data_rows:
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f" row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_etherscan_tokens_row(data_row, parser, symbol, **kwargs)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_etherscan_tokens_row(
    data_row: "DataRow", parser: DataParser, symbol: str, **kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(int(row_dict["UnixTimestamp"]))
    if "Txhash" in row_dict:
        tx_hash_pos = parser.in_header.index("Txhash")
    else:
        tx_hash_pos = parser.in_header.index("Transaction Hash")

    data_row.tx_raw = TxRawPos(
        tx_hash_pos,
        parser.in_header.index("From"),
        parser.in_header.index("To"),
    )

    if row_dict["TokenSymbol"].endswith("-LP"):
        asset = row_dict["TokenSymbol"] + "-" + row_dict["ContractAddress"][0:10]
    else:
        asset = row_dict["TokenSymbol"]

    if "Value" in row_dict:
        quantity = Decimal(row_dict["Value"].replace(",", ""))
    else:
        quantity = Decimal(row_dict["TokenValue"].replace(",", ""))

    if row_dict["To"].lower() in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=quantity,
            buy_asset=asset,
            wallet=_get_wallet(symbol, row_dict["To"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, symbol, row_dict["To"])
    elif row_dict["From"].lower() in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=quantity,
            sell_asset=asset,
            wallet=_get_wallet(symbol, row_dict["From"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, symbol, row_dict["From"])
    else:
        raise DataFilenameError(kwargs["filename"], "Ethereum address")


def parse_etherscan_nfts(
    data_rows: List["DataRow"], parser: DataParser, **kwargs: Unpack[ParserArgs]
) -> None:
    symbol = kwargs["cryptoasset"]
    if not symbol:
        sys.stderr.write(f"{WARNING} Native cryptoasset for this chain cannot be identified\n")
        sys.stderr.write(f"{Fore.RESET}Enter symbol: ")
        symbol = input()
        if not symbol:
            raise UnknownCryptoassetError(kwargs["filename"], kwargs.get("worksheet", ""))

    for data_row in data_rows:
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f" row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_etherscan_nfts_row(data_row, parser, symbol, **kwargs)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_etherscan_nfts_row(
    data_row: "DataRow", parser: DataParser, symbol: str, **kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(int(row_dict["UnixTimestamp"]))
    if "Txhash" in row_dict:
        tx_hash_pos = parser.in_header.index("Txhash")
    else:
        tx_hash_pos = parser.in_header.index("Transaction Hash")

    data_row.tx_raw = TxRawPos(
        tx_hash_pos,
        parser.in_header.index("From"),
        parser.in_header.index("To"),
    )

    if "TokenId" in row_dict:
        row_dict["Token ID"] = row_dict["TokenId"]

    if row_dict["To"].lower() in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(1),
            buy_asset=f'{row_dict["TokenName"]} #{row_dict["Token ID"]}',
            wallet=_get_wallet(symbol, row_dict["To"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, symbol, row_dict["To"])
    elif row_dict["From"].lower() in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(1),
            sell_asset=f'{row_dict["TokenName"]} #{row_dict["Token ID"]}',
            wallet=_get_wallet(symbol, row_dict["From"]),
        )
        data_row.worksheet_name = _get_worksheet_name(parser, symbol, row_dict["From"])
    else:
        raise DataFilenameError(kwargs["filename"], "Ethereum address")


etherscan_txns = DataParser(
    ParserType.EXPLORER,
    "Etherscan (Transactions)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",
        "UnixTimestamp",
        lambda c: c in ("DateTime", "DateTime (UTC)"),  # Renamed
        "From",
        "To",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"(TxnFee\((\w+)\)?)", c),
        "TxnFee(USD)",
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
        "Method",  # New field
    ],
    worksheet_name="Etherscan",
    row_handler=parse_etherscan,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (Transactions)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",
        "UnixTimestamp",
        lambda c: c in ("DateTime", "DateTime (UTC)"),  # Renamed
        "From",
        "To",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"(TxnFee\((\w+)\)?)", c),
        "TxnFee(USD)",
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
        "Method",  # New field
        "PrivateNote",
    ],
    worksheet_name="Etherscan",
    row_handler=parse_etherscan,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (Transactions)",
    [
        "Txhash",
        "Blockno",
        "UnixTimestamp",
        "DateTime",
        "From",
        "To",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"(TxnFee\((\w+)\)?)", c),
        "TxnFee(USD)",
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
    ],
    worksheet_name="Etherscan",
    row_handler=parse_etherscan,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (Transactions)",
    [
        "Txhash",
        "Blockno",
        "UnixTimestamp",
        "DateTime",
        "From",
        "To",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"(TxnFee\((\w+)\)?)", c),
        "TxnFee(USD)",
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
        "PrivateNote",
    ],
    worksheet_name="Etherscan",
    row_handler=parse_etherscan,
)

etherscan_int = DataParser(
    ParserType.EXPLORER,
    "Etherscan (Internal Transactions)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",
        "UnixTimestamp",
        lambda c: c in ("DateTime", "DateTime (UTC)"),  # Renamed
        "ParentTxFrom",
        "ParentTxTo",
        lambda c: re.match(r"ParentTx(\w+)_Value", c),
        "From",
        "TxTo",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
        "Type",
    ],
    worksheet_name="Etherscan Int.",
    row_handler=parse_etherscan_internal,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (Internal Transactions)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",
        "UnixTimestamp",
        lambda c: c in ("DateTime", "DateTime (UTC)"),  # Renamed
        "ParentTxFrom",
        "ParentTxTo",
        lambda c: re.match(r"ParentTx(\w+)_Value", c),
        "From",
        "TxTo",
        "ContractAddress",
        lambda c: re.match(r"(Value_IN\((\w+)\)?)", c),
        lambda c: re.match(r"(Value_OUT\((\w+)\)?)", c),
        None,
        lambda c: re.match(r"Historical \$Price\/(\w+)", c),
        "Status",
        "ErrCode",
        "Type",
        "PrivateNote",
    ],
    worksheet_name="Etherscan Int.",
    row_handler=parse_etherscan_internal,
)

etherscan_tokens = DataParser(
    ParserType.EXPLORER,
    "Etherscan (Token Transfers ERC-20)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",  # New field
        "UnixTimestamp",
        lambda c: c in ("DateTime", "DateTime (UTC)"),  # Renamed
        "From",
        "To",
        "TokenValue",  # Renamed
        "USDValueDayOfTx",  # New field
        "ContractAddress",  # New field
        "TokenName",
        "TokenSymbol",
    ],
    worksheet_name="Etherscan Tokens",
    all_handler=parse_etherscan_tokens,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (Token Transfers ERC-20)",
    [
        "Txhash",
        "UnixTimestamp",
        "DateTime",
        "From",
        "To",
        "Value",
        "ContractAddress",
        "TokenName",
        "TokenSymbol",
    ],
    worksheet_name="Etherscan Tokens",
    all_handler=parse_etherscan_tokens,
)

etherscan_nfts = DataParser(
    ParserType.EXPLORER,
    "Etherscan (NFT Transfers ERC-721 & ERC-1155)",
    [
        lambda c: c in ("Txhash", "Transaction Hash"),  # Renamed
        "Blockno",
        "UnixTimestamp",
        "DateTime (UTC)",  # Renamed
        "From",
        "To",
        "ContractAddress",
        # "TokenId",
        "TokenName",
        "TokenSymbol",
        "Token ID",  # Moved/Renamed
        "Type",  # New field
        "Quantity",  # New field
    ],
    worksheet_name="Etherscan NFTs",
    all_handler=parse_etherscan_nfts,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (NFT Transfers ERC-721 & ERC-1155)",
    [
        "Txhash",
        "Blockno",  # New field
        "UnixTimestamp",
        "DateTime",
        "From",
        "To",
        "ContractAddress",
        "TokenId",
        "TokenName",
        "TokenSymbol",
    ],
    worksheet_name="Etherscan NFTs",
    all_handler=parse_etherscan_nfts,
)

DataParser(
    ParserType.EXPLORER,
    "Etherscan (NFT Transfers ERC-721 & ERC-1155)",
    [
        "Txhash",
        "UnixTimestamp",
        "DateTime",
        "From",
        "To",
        "ContractAddress",
        "TokenId",
        "TokenName",
        "TokenSymbol",
    ],
    worksheet_name="Etherscan NFTs",
    all_handler=parse_etherscan_nfts,
)
