# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2019

import csv
import os
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from colorama import Fore
from typing_extensions import Unpack

from ...bt_types import TrType
from ...config import config
from ..dataparser import DataParser, ParserArgs, ParserType
from ..datarow import TxRawPos
from ..exceptions import (
    DataFilenameError,
    DataRowError,
    UnexpectedTradingPairError,
    UnexpectedTypeError,
)
from ..out_record import TransactionOutRecord

if TYPE_CHECKING:
    from ..datarow import DataRow

PRECISION = Decimal("0." + "0" * 8)

WALLET = "Binance"

def _load_withdraw_history_fees() -> Dict[Tuple[str, Decimal], Decimal]:
    # Frais réseau RÉELS des retraits Binance, lus depuis l'export officiel "Withdrawal History"
    # (colonnes Time,Coin,Network,Amount,Fee,Address,TXID,Status) pointé par la variable
    # d'environnement BINANCE_WITHDRAW_HISTORY_FILE. C'est la source de vérité : Amount = montant
    # NET transféré, Fee = frais réseau réel, et Amount + Fee == |Change| de la ligne de statement.
    #
    # Un Withdraw du statement dont le Remark vaut "Withdraw fee is included" débite un Change qui
    # INCLUT le frais, mais ne donne pas son montant ; on l'isole ici puis on le retranche du
    # sell_quantity (= Amount) pour que le contrôle de transferts BittyTax équilibre (sortie =
    # montant reçu côté destination) tout en enregistrant le frais comme dépense.
    #
    # Clé d'appariement statement <-> withdraw-history = (Coin, Amount+Fee) = (Coin, |Change|),
    # vérifiée UNIQUE sur l'export. On n'apparie PAS par horodatage : le Time du withdraw-history
    # diffère du UTC_Time du statement (fuseau / délai de traitement). Cf. docs/adr/0008 et
    # datas/wallets/Binance-Withdraw-History-*.csv.
    path = os.environ.get("BINANCE_WITHDRAW_HISTORY_FILE", "")
    fees: Dict[Tuple[str, Decimal], Decimal] = {}
    if not path or not os.path.exists(path):
        return fees
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            coin = (row.get("Coin") or "").strip()
            amount = (row.get("Amount") or "").strip()
            fee = (row.get("Fee") or "").strip()
            if coin and amount and fee:
                key = (coin, Decimal(amount) + Decimal(fee))
                fee_dec = Decimal(fee)
                # Garde d'unicité (fail-loud, cf. ADR 0008) : la clé (Coin, Amount+Fee) = (Coin,
                # |Change|) est l'identifiant du retrait ; elle DOIT être unique. Un écrasement
                # "last-wins" silencieux appliquerait un mauvais frais réseau au retrait du
                # statement → sell_quantity faux → solde/valeur_globale (III-C) faussés. On lève
                # sur une clé dupliquée à valeur DIVERGENTE (un doublon à frais identique est inerte,
                # toléré). Même esprit que le garde "Raw Data vide" d'_load_binance_override_keys.
                if key in fees and fees[key] != fee_dec:
                    raise RuntimeError(
                        f"{path}: ligne {line_no}: clé de retrait dupliquée à frais divergent "
                        f"(Coin={coin}, Amount+Fee={key[1]}) : frais {fees[key]} puis {fee_dec}. "
                        f"La clé (Coin, |Change|) doit être unique — un frais réseau erroné "
                        f"fausserait le solde. Vérifier l'export Withdrawal History."
                    )
                fees[key] = fee_dec
    return fees


def _load_gas_hors_perimetre() -> Dict[Tuple[str, Decimal], Decimal]:
    # Frais réseau (gas) DÉRIVÉS, dépensés HORS des wallets tracés, à porter en Fee ADDITIONNELLE
    # sur un retrait Binance. Chargés depuis le CSV pointé par BINANCE_GAS_OVERRIDES_FILE
    # (colonnes Coin,Change,GasExtra,Note), indexés sur (Coin, |Change|) comme la table des frais
    # de retrait. Distinct de la fee officielle du Withdrawal History : ce montant n'est PAS dans
    # un export Binance, il est mesuré on-chain (gas consommé sur des wallets intermédiaires non
    # suivis). On l'AJOUTE à la fee du retrait pour que (a) le solde reste exact et (b) le contrôle
    # de transferts équilibre (le gas qui n'est jamais "revenu" est isolé de la quantité transférée).
    # Fiscalement : frais réseau = non-cession (ni PV ni MV). Cf. docs/adr/0010 et
    # datas/overrides/binance_gas_hors_perimetre.csv.
    path = os.environ.get("BINANCE_GAS_OVERRIDES_FILE", "")
    gas: Dict[Tuple[str, Decimal], Decimal] = {}
    if not path or not os.path.exists(path):
        return gas
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            coin = (row.get("Coin") or "").strip()
            change = (row.get("Change") or "").strip()
            extra = (row.get("GasExtra") or "").strip()
            if coin and change and extra:
                key = (coin, abs(Decimal(change)))
                extra_dec = Decimal(extra)
                # Garde d'unicité (fail-loud, cf. ADR 0010) : (Coin, |Change|) identifie le retrait
                # sur lequel porter le gas additionnel. Un écrasement "last-wins" silencieux
                # appliquerait un mauvais gas → sell_quantity faux → solde/valeur_globale faussés.
                # On lève sur clé dupliquée à valeur divergente (doublon identique toléré).
                if key in gas and gas[key] != extra_dec:
                    raise RuntimeError(
                        f"{path}: ligne {line_no}: clé de gas dupliquée à valeur divergente "
                        f"(Coin={coin}, |Change|={key[1]}) : {gas[key]} puis {extra_dec}. "
                        f"La clé (Coin, |Change|) doit être unique. Vérifier le CSV de gas."
                    )
                gas[key] = extra_dec
    return gas


WITHDRAW_NETWORK_FEE = _load_withdraw_history_fees()
GAS_HORS_PERIMETRE = _load_gas_hors_perimetre()


def _load_binance_override_keys() -> Set[str]:
    # Binance statement rows requalified manually via a BittyTax-format CSV imported alongside
    # the Binance export (pointed to by BINANCE_OVERRIDES_FILE). A statement export carries no
    # txid, so a row is identified by the composite key UTC_Time|Coin|Change. Use case: a USDT
    # "Deposit" that is actually a gift received from a third party (acquisition à titre gratuit)
    # and must be requalified as Gift-Received — the default Deposit would otherwise be read as an
    # internal transfer (INTRA) with no acquisition cost. The override CSV provides the
    # Gift-Received line; we skip the matching statement row here to avoid double-counting.
    # See docs/adr/0007 and datas/binance_overrides.csv.
    path = os.environ.get("BINANCE_OVERRIDES_FILE", "")
    keys: Set[str] = set()
    if not path or not os.path.exists(path):
        return keys
    with open(path, newline="", encoding="utf-8") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            # Garde anti-décalage silencieux : chaque ligne d'override DOIT porter sa clé
            # composite UTC_Time|Coin|Change en dernière colonne (Raw Data). Une cellule Note
            # contenant une virgule NON quotée décale tout d'une colonne et vide Raw Data — le
            # skip du Deposit d'origine ne s'arme alors plus et l'audit signale un transfers
            # mismatch sans cause apparente (cf. ADR 0007). On échoue bruyamment plutôt que de
            # laisser passer une clé vide. Fix : quoter la cellule Note dans le CSV.
            key = (row.get("Raw Data") or "").strip()
            if not key:
                raise RuntimeError(
                    f"{path}: ligne {line_no}: colonne 'Raw Data' vide "
                    f"(clé d'override manquante). Cause probable : une virgule non quotée "
                    f"dans la colonne 'Note' a décalé les colonnes. Quoter la cellule 'Note'. "
                    f"Ligne lue : {row}"
                )
            keys.add(key)
    return keys


BINANCE_OVERRIDE_KEYS = _load_binance_override_keys()

QUOTE_ASSETS = [
    "AEUR",
    "ARS",
    "AUD",
    "BIDR",
    "BKRW",
    "BNB",
    "BRL",
    "BTC",
    "BUSD",
    "BVND",
    "COP",
    "CZK",
    "DAI",
    "DOGE",
    "DOT",
    "ETH",
    "EUR",
    "EURI",
    "FDUSD",
    "GBP",
    "IDR",
    "GYEN",
    "IDRT",
    "JPY",
    "MXN",
    "NGN",
    "PAX",
    "PLN",
    "RLUSD",
    "RON",
    "RUB",
    "SOL",
    "TRX",
    "TRY",
    "TUSD",
    "U",
    "UAH",
    "USD",
    "USD1",
    "USDC",
    "USDP",
    "USDS",
    "USDSOLD",
    "USDT",
    "UST",
    "VAI",
    "XMR",
    "XRP",
    "ZAR",
]

BASE_ASSETS = [
    "0G",
    "1000CAT",
    "1000CHEEMS",
    "1000SATS",
    "1INCH",
    "1INCHDOWN",
    "1INCHUP",
    "1MBABYDOGE",
    "2Z",
]

TRADINGPAIR_TO_QUOTE_ASSET = {
    "ADAEUR": "EUR",
    "ARBIDR": "IDR",
    "BNBIDR": "IDR",
    "BNBUSD": "USD",
    "ENAEUR": "EUR",
    "GALAEUR": "EUR",
    "LUNAEUR": "EUR",
    "THETAEUR": "EUR",
    "USDTUSD": "USD",
}


def parse_binance_trades(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date(UTC)"])

    base_asset, quote_asset = _split_trading_pair(row_dict["Market"])
    if base_asset is None or quote_asset is None:
        raise UnexpectedTradingPairError(
            parser.in_header.index("Market"), "Market", row_dict["Market"]
        )

    if row_dict["Type"] == "BUY":
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Amount"]),
            buy_asset=base_asset,
            sell_quantity=Decimal(row_dict["Total"]),
            sell_asset=quote_asset,
            fee_quantity=Decimal(row_dict["Fee"]),
            fee_asset=row_dict["Fee Coin"],
            wallet=WALLET,
        )
    elif row_dict["Type"] == "SELL":
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Total"]),
            buy_asset=quote_asset,
            sell_quantity=Decimal(row_dict["Amount"]),
            sell_asset=base_asset,
            fee_quantity=Decimal(row_dict["Fee"]),
            fee_asset=row_dict["Fee Coin"],
            wallet=WALLET,
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Type"), "Type", row_dict["Type"])


def parse_binance_convert(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date"])

    if row_dict["Status"] != "Successful":
        return

    base_asset, quote_asset = _split_trading_pair(row_dict["Pair"])
    if base_asset is None or quote_asset is None:
        raise UnexpectedTradingPairError(parser.in_header.index("Pair"), "Pair", row_dict["Pair"])

    data_row.t_record = TransactionOutRecord(
        TrType.TRADE,
        data_row.timestamp,
        buy_quantity=Decimal(row_dict["Buy"].split(" ")[0]),
        buy_asset=row_dict["Buy"].split(" ")[1],
        sell_quantity=Decimal(row_dict["Sell"].split(" ")[0]),
        sell_asset=row_dict["Sell"].split(" ")[1],
        wallet=WALLET,
    )


def parse_binance_trades_statement(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date(UTC)"])
    fee_quantity, fee_asset = _split_asset(row_dict["Fee"].replace(",", ""))

    if row_dict["Side"] == "BUY":
        buy_quantity, buy_asset = _split_asset(row_dict["Executed"].replace(",", ""))
        sell_quantity, sell_asset = _split_asset(row_dict["Amount"].replace(",", ""))

        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=buy_quantity,
            buy_asset=buy_asset,
            sell_quantity=sell_quantity,
            sell_asset=sell_asset,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            wallet=WALLET,
        )
    elif row_dict["Side"] == "SELL":
        buy_quantity, buy_asset = _split_asset(row_dict["Amount"].replace(",", ""))
        sell_quantity, sell_asset = _split_asset(row_dict["Executed"].replace(",", ""))

        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=buy_quantity,
            buy_asset=buy_asset,
            sell_quantity=sell_quantity,
            sell_asset=sell_asset,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            wallet=WALLET,
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Side"), "Side", row_dict["Side"])


def _split_trading_pair(trading_pair: str) -> Tuple[Optional[str], Optional[str]]:
    if trading_pair in TRADINGPAIR_TO_QUOTE_ASSET:
        quote_asset = TRADINGPAIR_TO_QUOTE_ASSET[trading_pair]
        base_asset = trading_pair[: -len(quote_asset)]
        return base_asset, quote_asset

    for quote_asset in QUOTE_ASSETS:
        if trading_pair.endswith(quote_asset):
            return trading_pair[: -len(quote_asset)], quote_asset

    return None, None


def _split_asset(amount: str) -> Tuple[Optional[Decimal], str]:
    for base_asset in BASE_ASSETS:
        if amount.endswith(base_asset):
            return Decimal(amount[: -len(base_asset)]), base_asset

    match = re.match(r"(\d+|\d+\.\d+)(\w+)$", amount)
    if match:
        return Decimal(match.group(1)), match.group(2)
    raise RuntimeError(f"Cannot split Quantity from Asset: {amount}")


def parse_binance_deposits_withdrawals_crypto_v2(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(_get_timestamp(row_dict["Date(UTC+0)"]))
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TXID"), tx_dest_pos=parser.in_header.index("Address")
    )

    if row_dict["Status"] != "Completed":
        return

    if "Fee" not in row_dict:
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Amount"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    else:
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict["Amount"]),
            sell_asset=row_dict["Coin"],
            fee_quantity=Decimal(row_dict["Fee"]),
            fee_asset=row_dict["Coin"],
            wallet=WALLET,
        )


def _get_timestamp(timestamp: str) -> str:
    match = re.match(r"^\d{2}-\d{2}-\d{2}.*$", timestamp)

    if match:
        return f"20{timestamp}"
    return timestamp


def parse_binance_deposits_withdrawals_crypto_v1(
    data_row: "DataRow", parser: DataParser, **kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(data_row.row[0])
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TXID"), tx_dest_pos=parser.in_header.index("Address")
    )

    if row_dict["Status"] != "Completed":
        return

    if "deposit" in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Amount"]),
            buy_asset=row_dict["Coin"],
            fee_quantity=Decimal(row_dict["TransactionFee"]),
            fee_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif "withdraw" in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict["Amount"]),
            sell_asset=row_dict["Coin"],
            fee_quantity=Decimal(row_dict["TransactionFee"]),
            fee_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    else:
        raise DataFilenameError(kwargs["filename"], "Transaction Type (Deposit or Withdrawal)")


def parse_binance_deposits_withdrawals_cash(
    data_row: "DataRow", parser: DataParser, **kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict

    timestamp_hdr = parser.args[0].group(1)
    utc_offset = parser.args[0].group(2)

    if utc_offset == "UTCnull":
        utc_offset = "UTC"

    data_row.timestamp = DataParser.parse_timestamp(f"{row_dict[timestamp_hdr]} {utc_offset}")

    if row_dict["Status"] != "Successful":
        return

    if "deposit" in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Indicated Amount"]),
            buy_asset=row_dict["Coin"],
            fee_quantity=Decimal(row_dict["Fee"]),
            fee_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif "withdraw" in kwargs["filename"].lower():
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict["Amount"]),
            sell_asset=row_dict["Coin"],
            fee_quantity=Decimal(row_dict["Fee"]),
            fee_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    else:
        raise DataFilenameError(kwargs["filename"], "Transaction Type (Deposit or Withdrawal)")


def parse_binance_statements(
    data_rows: List["DataRow"], parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    tx_times: Dict[datetime, List["DataRow"]] = {}

    for dr in data_rows:
        # Binance statement exports use a strict, homogeneous UTC_Time format: "YY-MM-DD HH:MM:SS"
        # (e.g. "25-01-19 15:38:09"). Parse it EXPLICITLY with strptime rather than through
        # dateutil's heuristics: yearfirst=True only pins the LEADING token as the year — the
        # remaining MM-DD pair still relied on dateutil's month-first DEFAULT to disambiguate.
        # That default happens to be correct for this stable format, but it is an *implicit*
        # contract: a border value (day <= 12) could silently invert month and day, corrupting
        # the date and thus the chronological order -> wrong weighted-average cost (pta) -> wrong
        # capital gain. A fixed format string removes the ambiguity at the source. We fail loud on
        # any drift from the expected format instead of falling back to a silent (mis)parse.
        # Cf. ADR 0005 (year-first fix) and the "block rather than emit a wrong figure" principle.
        raw = dr.row_dict["UTC_Time"]
        try:
            naive = datetime.strptime(raw, "%y-%m-%d %H:%M:%S")
        except ValueError as e:
            raise RuntimeError(
                f"Horodatage Binance Statements au format inattendu : {raw!r} "
                f"(attendu 'YY-MM-DD HH:MM:SS', ex. '25-01-19 15:38:09'). Le format d'export a "
                f"peut-être changé — le parser dépend de ce format EXACT pour désambiguïser "
                f"MM-DD (mois vs jour) et fixer l'ordre chronologique (→ pta/PV). À câbler "
                f"explicitement au vu du nouveau format, jamais deviné. Cf. ADR 0005."
            ) from e
        # Exports horodatés en UTC (nom de fichier "(UTC0)", cf. ADR 0019). On reformate en
        # ISO non ambigu et on repasse par parse_timestamp pour conserver l'UNIQUE point de
        # normalisation de fuseau (datetime naïf -> tagué UTC) déjà utilisé partout.
        dr.timestamp = DataParser.parse_timestamp(naive.strftime("%Y-%m-%d %H:%M:%S"))
        if dr.timestamp in tx_times:
            tx_times[dr.timestamp].append(dr)
        else:
            tx_times[dr.timestamp] = [dr]

    # Garde de CARDINALITÉ des overrides (fail-loud, cf. ADR 0007) : chaque clé d'override
    # (UTC_Time|Coin|Change, colonne Raw Data de binance_overrides.csv) DOIT matcher EXACTEMENT
    # UNE ligne du statement. La clé composite n'est pas un identifiant garanti unique (des lignes
    # distinctes peuvent partager UTC_Time|Coin|Change), et un override peut viser une ligne
    # absente (typo, mauvais format de date YY-MM-DD vs YYYY-MM-DD). Deux dérives silencieuses :
    #   - 0 match  → la ligne d'origine n'est PAS skippée → elle coexiste avec la ligne de
    #                remplacement (double-import → holding / valeur_globale III-C faussés) ;
    #   - ≥2 match → le skip efface PLUSIEURS lignes légitimes (sous-déclaration).
    # Seul "exactement 1" correspond à l'intention (requalifier UNE ligne précise). On compte les
    # matches ici (data_rows complet en main) et on lève sinon — plutôt que de produire un chiffre
    # faux en silence. Complète le garde "Raw Data vide" d'_load_binance_override_keys (format de
    # la clé) par un contrôle d'appariement effectif (la clé pointe bien sur 1 ligne unique).
    if BINANCE_OVERRIDE_KEYS:
        match_counts: Dict[str, int] = {k: 0 for k in BINANCE_OVERRIDE_KEYS}
        for dr in data_rows:
            key = "|".join(
                (dr.row_dict["UTC_Time"], dr.row_dict["Coin"], dr.row_dict["Change"])
            )
            if key in match_counts:
                match_counts[key] += 1
        bad = {k: n for k, n in match_counts.items() if n != 1}
        if bad:
            details = "; ".join(f"{k!r} → {n} ligne(s)" for k, n in sorted(bad.items()))
            raise RuntimeError(
                f"binance_overrides.csv : clé(s) d'override n'appariant pas exactement 1 ligne "
                f"du statement (0 = ligne absente/typo → double-import ; ≥2 = collision → lignes "
                f"perdues) : {details}. Corriger la clé Raw Data (UTC_Time|Coin|Change) ou le "
                f"statement (cf. ADR 0007)."
            )

    for data_row in data_rows:
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f"row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_binance_statements_row(tx_times, parser, data_row)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_binance_statements_row(
    tx_times: Dict[datetime, List["DataRow"]], parser: DataParser, data_row: "DataRow"
) -> None:
    row_dict = data_row.row_dict

    # Row requalified via BINANCE_OVERRIDES_FILE (e.g. a Deposit that is actually a third-party
    # gift): skip it so the imported override CSV provides the Gift-Received line without
    # double-counting. The composite key UTC_Time|Coin|Change stands in for the absent txid.
    if BINANCE_OVERRIDE_KEYS:
        override_key = "|".join(
            (row_dict["UTC_Time"], row_dict["Coin"], row_dict["Change"])
        )
        if override_key in BINANCE_OVERRIDE_KEYS:
            return

    if row_dict["Account"] in ("USDT-Futures", "USD-MFutures", "USD-M Futures", "Coin-M Futures"):
        _parse_binance_statements_futures_row(tx_times, parser, data_row)
        return

    if row_dict["Account"] in ("Isolated Margin", "CrossMargin", "Cross Margin"):
        _parse_binance_statements_margin_row(tx_times, parser, data_row)
        return

    if row_dict["Account"].lower() not in ("spot", "earn", "pool", "savings", "funding"):
        raise UnexpectedTypeError(parser.in_header.index("Account"), "Account", row_dict["Account"])

    if row_dict["Operation"] in (
        "Commission History",
        "Referrer rebates",
        "Commission Rebate",
        "Commission Fee Shared With You",
        "Referral Kickback",
        "Referral Commission",
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.REFERRAL,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "Airdrop Assets",
        "Cash Voucher distribution",
        "Simple Earn Flexible Airdrop",
        "Campaign Related Reward",
        "Launchpool Airdrop",
        "HODLer Airdrops Distribution",
        "Megadrop Rewards",
        "Launchpool Airdrop - User Claim Distribution",
        "Launchpool Airdrop - System Distribution",
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.AIRDROP,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "Distribution",
        "Asset - Transfer",
        "Token Swap - Distribution",
        "Token Swap - Redenomination/Rebranding",
    ):
        if Decimal(row_dict["Change"]) > 0:
            data_row.t_record = TransactionOutRecord(
                TrType.AIRDROP,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.SPEND,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Change"])),
                sell_asset=row_dict["Coin"],
                sell_value=Decimal(0),
                wallet=WALLET,
            )
    elif row_dict["Operation"] == "Super BNB Mining":
        data_row.t_record = TransactionOutRecord(
            TrType.MINING,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "Savings Interest",
        "Simple Earn Flexible Interest",
        "Pool Distribution",
        "Savings distribution",
        "Savings Distribution",
        "Launchpool Interest",
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.INTEREST,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "POS savings interest",
        "Staking Rewards",
        "ETH 2.0 Staking Rewards",
        "Liquid Swap rewards",
        "Simple Earn Locked Rewards",
        "DOT Slot Auction Rewards",
        "Launchpool Earnings Withdrawal",
        "BNB Vault Rewards",
        "Swap Farming Rewards",
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.STAKING_REWARD,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in ("Asset Recovery", "Leveraged Coin Consolidation"):
        data_row.t_record = TransactionOutRecord(
            TrType.SPEND,
            data_row.timestamp,
            sell_quantity=abs(Decimal(row_dict["Change"])),
            sell_asset=row_dict["Coin"],
            sell_value=Decimal(0),
            wallet=WALLET,
        )
    elif row_dict["Operation"] == "Binance Card Spending":
        if Decimal(row_dict["Change"]) < 0:
            data_row.t_record = TransactionOutRecord(
                TrType.SPEND,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Change"])),
                sell_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.FEE_REBATE,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
    elif row_dict["Operation"] == "Binance Card Cashback":
        data_row.t_record = TransactionOutRecord(
            TrType.CASHBACK,
            data_row.timestamp,
            buy_quantity=abs(Decimal(row_dict["Change"])),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] == "Crypto Box":
        if Decimal(row_dict["Change"]) < 0:
            data_row.t_record = TransactionOutRecord(
                TrType.GIFT_SENT,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Change"])),
                sell_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.GIFT_RECEIVED,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
    elif row_dict["Operation"] in (
        "Small assets exchange BNB",
        "Small Assets Exchange BNB",
        "BNB Fee Deduction",
    ):
        if config.binance_multi_bnb_split_even:
            _make_bnb_trade(
                _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
            )
        else:
            _make_trade(
                _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
            )
    elif row_dict["Operation"] in (
        "ETH 2.0 Staking",
        "Leverage Token Redemption",
        "Stablecoins Auto-Conversion",
    ):
        _make_trade(
            _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
        )
    elif row_dict["Operation"] in (
        "Swap Farming Transaction",
        "Liquid Swap Sell",
    ):
        # Trade before a Liquid Swap
        _make_trade(
            _get_op_rows(
                tx_times, data_row.timestamp, ("Swap Farming Transaction", "Liquid Swap Sell")
            ),
        )
    elif row_dict["Operation"] in (
        "transfer_out",
        "transfer_in",
        "Savings purchase",
        "Savings Principal redemption",
        "POS savings purchase",
        "POS savings redemption",
        "Staking Purchase",
        "Staking Redemption",
        "Simple Earn Locked Subscription",
        "Simple Earn Locked Redemption",
        "Transfer Between Spot Account and UM Futures Account",
        "Transfer Between Spot Account and CM Futures Account",
        "Transfer Between Main Account/Futures and Margin Account",
        "Transfer Between Main and Funding Wallet",
        "Transfer Between Main Account And Mining Account",
        "Transfer Between Main And Mining Account",
        "Launchpool Subscription/Redemption",
        "Launchpad Subscribe",
        "Simple Earn Flexible Subscription",  # See merger
        "Simple Earn Flexible Redemption",  # See merger
        "Liquid Swap Add",  # See merger
        "Liquid Swap Add/Sell",  # See merger
        "Liquidity Farming Remove",  # See merger
    ):
        # Skip non-taxable events and those which are handled by the merger
        return
    elif row_dict["Operation"] in (
        "Deposit",
        "Fiat Deposit",
        "Fiat OCBS - Add Fiat and Fees",
    ):
        if config.binance_statements_only:
            data_row.t_record = TransactionOutRecord(
                TrType.DEPOSIT,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            # Skip duplicate operations
            return
    elif row_dict["Operation"] in ("Withdraw", "Fiat Withdraw", "Fiat Withdrawal", "Send"):
        if config.binance_statements_only:
            sell_quantity = abs(Decimal(row_dict["Change"]))
            fee_quantity = None
            fee_asset = ""
            # "Withdraw fee is included" : le Change inclut le frais réseau que Binance n'exporte
            # pas. On l'isole depuis WITHDRAW_NETWORK_FEE (frais réel issu du Withdrawal History,
            # indexé sur (Coin, |Change|)) et on réduit le sell_quantity au montant réellement
            # transféré, sinon BittyTax compte sortie = Change > montant reçu et signale un
            # transfers mismatch (le frais "disparaît"). Cf. WITHDRAW_NETWORK_FEE.
            if "fee is included" in row_dict["Remark"].lower():
                fee_key = (row_dict["Coin"], abs(Decimal(row_dict["Change"])))
                network_fee = WITHDRAW_NETWORK_FEE.get(fee_key)
                if network_fee is not None and network_fee < sell_quantity:
                    sell_quantity -= network_fee
                    fee_quantity = network_fee
                    fee_asset = row_dict["Coin"]
            # Gas réseau DÉRIVÉ dépensé hors des wallets tracés (mesuré on-chain), porté en Fee
            # ADDITIONNELLE sur ce retrait pour que le solde reste exact et que le contrôle de
            # transferts équilibre (le gas n'est jamais "revenu"). Cf. GAS_HORS_PERIMETRE, ADR 0010.
            gas_extra = GAS_HORS_PERIMETRE.get(
                (row_dict["Coin"], abs(Decimal(row_dict["Change"])))
            )
            if gas_extra is not None and gas_extra < sell_quantity:
                sell_quantity -= gas_extra
                fee_quantity = (fee_quantity or Decimal(0)) + gas_extra
                fee_asset = row_dict["Coin"]
            data_row.t_record = TransactionOutRecord(
                TrType.WITHDRAWAL,
                data_row.timestamp,
                sell_quantity=sell_quantity,
                sell_asset=row_dict["Coin"],
                fee_quantity=fee_quantity,
                fee_asset=fee_asset,
                wallet=WALLET,
            )
        else:
            # Skip duplicate operations
            return
    elif row_dict["Operation"] == "Buy Crypto With Fiat":
        if config.binance_statements_only:
            op_rows = _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],))
            buy_rows = [r for r in op_rows if Decimal(r.row_dict["Change"]) > 0]
            sell_rows = [r for r in op_rows if Decimal(r.row_dict["Change"]) < 0]
            if sell_rows:
                # Cas DEUX jambes (débit + crédit exportés) : JAMAIS rencontré sur les données
                # réelles (Binance n'exporte que la jambe crypto d'un "Buy Crypto With Fiat").
                # On NE DEVINE PAS son traitement : `_make_trade` produirait un Trade dont la
                # qualification aval dépend de la nature de la jambe débit — un débit EUR donnerait
                # une acquisition onéreuse (CASH_IN, correct), mais un débit stablecoin (USDT/USDC)
                # basculerait SILENCIEUSEMENT en sursis (SWAP) au lieu d'une acquisition. C'est le
                # seul chemin de ce handler qui ne fail-loud pas. Plutôt que de coder une logique
                # pour un format hypothétique (le CLAUDE.md proscrit de traiter un cas que les
                # données n'exercent pas), on LÈVE : le jour où ce format apparaît réellement, il
                # faudra regarder la vraie donnée et câbler explicitement la jambe débit (EUR →
                # laisser en Trade/CASH_IN ; stablecoin → override fiat_buy). Cf. ADR 0013/0028.
                sell_row = sell_rows[0]
                raise RuntimeError(
                    f"'Buy Crypto With Fiat' à DEUX jambes non rencontré à "
                    f"{sell_row.row_dict['UTC_Time']} (débit {sell_row.row_dict['Change']} "
                    f"{sell_row.row_dict['Coin']}) : format non prévu. La qualification dépend de "
                    f"la nature de la jambe débit (EUR = acquisition onéreuse ; stablecoin = "
                    f"basculerait en sursis SWAP) → à câbler explicitement au vu de la donnée "
                    f"réelle, jamais deviné. Cf. ADR 0013/0028."
                )
            elif buy_rows:
                # Achat fiat mono-jambe : Binance n'exporte PAS le débit EUR → le prix
                # d'acquisition RÉEL (coût effectivement décaissé) est MANQUANT. On NE DEVINE
                # PAS : ni valeur-marché via Gift-Received (qualification "gratuit" d'un achat
                # payé, fiscalement fausse — la valeur de marché est réservée au titre gratuit,
                # CGI 150 VH bis III-B 2e al. ; un achat onéreux se valorise au prix acquitté,
                # III-B 1er al. + BOI-RPPM-PVBMC-30-20 §70), ni Deposit à coût nul (sous-estime
                # le pta → PV sur-déclarée). Un achat fiat est une acquisition à titre ONÉREUX
                # au coût réel justifié par le relevé (ADR 0013) : elle DOIT être fournie via
                # binance_overrides.csv en Trade EUR→crypto (tag `fiat_buy`), skippée en tête de
                # _parse_binance_statements_row AVANT d'arriver ici. Sans override, la donnée
                # nécessaire est absente → on LÈVE plutôt que produire un pta faux en silence
                # (principe "bloquer plutôt que produire faux", cf. ADR 0013/0024/0028).
                buy_row = buy_rows[0]
                raise RuntimeError(
                    f"'Buy Crypto With Fiat' sans override à {buy_row.row_dict['UTC_Time']} "
                    f"({buy_row.row_dict['Change']} {buy_row.row_dict['Coin']}) : Binance "
                    f"n'exporte pas la jambe EUR, le coût d'acquisition réel est manquant. "
                    f"Ajouter une ligne 'fiat_buy' (Trade EUR->crypto au coût réel justifié par "
                    f"le relevé) dans binance_overrides.csv, clé Raw Data "
                    f"{buy_row.row_dict['UTC_Time']}|{buy_row.row_dict['Coin']}|"
                    f"{buy_row.row_dict['Change']} — cf. ADR 0013."
                )
        else:
            # Skip duplicate operations
            return
    elif row_dict["Operation"] in ("Binance Convert", "Large OTC trading", "Buy Crypto With Card"):
        if config.binance_statements_only:
            _make_trade(
                _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
            )
        else:
            # Skip duplicate operations
            return
    elif row_dict["Operation"] in (
        "Buy",
        "Sell",
        "Fee",
        "Transaction Related",
        "Transaction Buy",
        "Transaction Fee",
        "Transaction Spend",
        "Transaction Sold",
        "Transaction Revenue",
    ):
        if config.binance_statements_only:
            _make_trade_with_fee(
                _get_op_rows(
                    tx_times,
                    data_row.timestamp,
                    (
                        "Buy",
                        "Sell",
                        "Fee",
                        "Transaction Related",
                        "Transaction Buy",
                        "Transaction Fee",
                        "Transaction Spend",
                        "Transaction Sold",
                        "Transaction Revenue",
                    ),
                ),
            )
        else:
            # Skip duplicate operations
            return
    else:
        raise UnexpectedTypeError(
            parser.in_header.index("Operation"), "Operation", row_dict["Operation"]
        )


def _parse_binance_statements_futures_row(
    tx_times: Dict[datetime, List["DataRow"]], parser: DataParser, data_row: "DataRow"
) -> None:
    row_dict = data_row.row_dict

    if row_dict["Operation"] in ("Realize profit and loss", "Realized Profit and Loss"):
        if Decimal(row_dict["Change"]) > 0:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_GAIN,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_LOSS,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Change"])),
                sell_asset=row_dict["Coin"],
                wallet=WALLET,
            )
    elif row_dict["Operation"] in ("Fee", "Insurance Fund Compensation"):
        data_row.t_record = TransactionOutRecord(
            TrType.MARGIN_FEE,
            data_row.timestamp,
            sell_quantity=abs(Decimal(row_dict["Change"])),
            sell_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in ("Funding Fee", "Insurance Fund Refund"):
        if Decimal(row_dict["Change"]) > 0:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_FEE_REBATE,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Change"]),
                buy_asset=row_dict["Coin"],
                wallet=WALLET,
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_FEE,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Change"])),
                sell_asset=row_dict["Coin"],
                wallet=WALLET,
            )
    elif row_dict["Operation"] in ("Referrer rebates", "Referee rebates"):
        data_row.t_record = TransactionOutRecord(
            TrType.REFERRAL,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in ("Asset Conversion Transfer", "Futures Convert"):
        _make_trade(
            _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
        )
    elif row_dict["Operation"] in (
        "transfer_out",
        "transfer_in",
        "Transfer Between Spot Account and UM Futures Account",
        "Transfer Between Spot Account and CM Futures Account",
        "Transfer Between Main Account/Futures and Margin Account",
    ):
        # Skip not taxable events
        return
    else:
        raise UnexpectedTypeError(
            parser.in_header.index("Operation"), "Operation", row_dict["Operation"]
        )


def _parse_binance_statements_margin_row(
    tx_times: Dict[datetime, List["DataRow"]], parser: DataParser, data_row: "DataRow"
) -> None:
    row_dict = data_row.row_dict

    if row_dict["Operation"] in ("Margin loan", "Margin Loan", "Isolated Margin Loan"):
        data_row.t_record = TransactionOutRecord(
            TrType.LOAN,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Change"]),
            buy_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "Margin Repayment",
        "Isolated Margin Repayment",
        "Cross Margin Liquidation - Repayment",
    ):
        data_row.t_record = TransactionOutRecord(
            TrType.LOAN_REPAYMENT,
            data_row.timestamp,
            sell_quantity=abs(Decimal(row_dict["Change"])),
            sell_asset=row_dict["Coin"],
            wallet=WALLET,
        )
    elif row_dict["Operation"] in (
        "Buy",
        "Sell",
        "Fee",
        "Transaction Buy",
        "Transaction Fee",
        "Transaction Spend",
        "Transaction Sold",
        "Transaction Revenue",
    ):
        _make_trade_with_fee(
            _get_op_rows(
                tx_times,
                data_row.timestamp,
                (
                    "Buy",
                    "Sell",
                    "Fee",
                    "Transaction Buy",
                    "Transaction Fee",
                    "Transaction Spend",
                    "Transaction Sold",
                    "Transaction Revenue",
                ),
            ),
        )
    elif row_dict["Operation"] in (
        "Small assets exchange BNB",
        "Small Assets Exchange BNB",
        "BNB Fee Deduction",
    ):
        if config.binance_multi_bnb_split_even:
            _make_bnb_trade(
                _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
            )
        else:
            _make_trade(
                _get_op_rows(tx_times, data_row.timestamp, (row_dict["Operation"],)),
            )
    elif row_dict["Operation"] == "Transfer Between Main Account/Futures and Margin Account":
        # Skip not taxable events
        return
    else:
        raise UnexpectedTypeError(
            parser.in_header.index("Operation"), "Operation", row_dict["Operation"]
        )


def parse_binance_futures(
    data_rows: List["DataRow"], parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    tx_ids: Dict[str, List["DataRow"]] = {}
    for dr in data_rows:
        dr.timestamp = DataParser.parse_timestamp(dr.row_dict["Date(UTC)"])
        # Normalise fields to be compatible with Statements functions
        dr.row_dict["Operation"] = dr.row_dict["type"]
        dr.row_dict["Change"] = dr.row_dict["Amount"]
        dr.row_dict["Coin"] = dr.row_dict["Asset"]

        if dr.row_dict["Transaction ID"] in tx_ids:
            tx_ids[dr.row_dict["Transaction ID"]].append(dr)
        else:
            tx_ids[dr.row_dict["Transaction ID"]] = [dr]

    for data_row in data_rows:
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f"row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_binance_futures_row(tx_ids, parser, data_row)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_binance_futures_row(
    tx_ids: Dict[str, List["DataRow"]], parser: DataParser, data_row: "DataRow"
) -> None:
    row_dict = data_row.row_dict

    if row_dict["type"] == "REALIZED_PNL":
        if Decimal(row_dict["Amount"]) > 0:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_GAIN,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Amount"]),
                buy_asset=row_dict["Asset"],
                wallet=WALLET,
                note=row_dict["Symbol"],
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_LOSS,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Amount"])),
                sell_asset=row_dict["Asset"],
                wallet=WALLET,
                note=row_dict["Symbol"],
            )
    elif row_dict["type"] == "COMMISSION":
        data_row.t_record = TransactionOutRecord(
            TrType.MARGIN_FEE,
            data_row.timestamp,
            sell_quantity=abs(Decimal(row_dict["Amount"])),
            sell_asset=row_dict["Asset"],
            wallet=WALLET,
            note=row_dict["Symbol"],
        )
    elif row_dict["type"] == "FUNDING_FEE":
        if Decimal(row_dict["Amount"]) > 0:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_FEE_REBATE,
                data_row.timestamp,
                buy_quantity=Decimal(row_dict["Amount"]),
                buy_asset=row_dict["Asset"],
                wallet=WALLET,
                note=row_dict["Symbol"],
            )
        else:
            data_row.t_record = TransactionOutRecord(
                TrType.MARGIN_FEE,
                data_row.timestamp,
                sell_quantity=abs(Decimal(row_dict["Amount"])),
                sell_asset=row_dict["Asset"],
                wallet=WALLET,
                note=row_dict["Symbol"],
            )
    elif row_dict["type"] in ("COIN_SWAP_DEPOSIT", "COIN_SWAP_WITHDRAW"):
        _make_trade(tx_ids[row_dict["Transaction ID"]])
    elif row_dict["type"] in ("DEPOSIT", "WITHDRAW", "TRANSFER"):
        # Skip transfers
        return
    else:
        raise UnexpectedTypeError(parser.in_header.index("type"), "type", row_dict["type"])


def _get_op_rows(
    tx_times: Dict[datetime, List["DataRow"]],
    timestamp: datetime,
    operations: Tuple[str, ...],
) -> List["DataRow"]:
    timestamp_next_second = timestamp + timedelta(seconds=1)

    if timestamp_next_second in tx_times:
        tx_period = tx_times[timestamp] + tx_times[timestamp_next_second]
    else:
        tx_period = tx_times[timestamp]

    return [dr for dr in tx_period if dr.row_dict["Operation"] in operations and not dr.parsed]


def _make_bnb_trade(op_rows: List["DataRow"]) -> None:
    buy_quantity = _get_bnb_quantity(op_rows)
    sell_rows = [dr for dr in op_rows if not dr.parsed]
    tot_buy_quantity = Decimal(0)

    for cnt, sell_row in enumerate(sell_rows):
        sell_row.parsed = True

        if buy_quantity:
            if cnt < len(sell_rows) - 1:
                split_buy_quantity = Decimal(buy_quantity / len(sell_rows)).quantize(PRECISION)
                tot_buy_quantity += split_buy_quantity
            else:
                split_buy_quantity = buy_quantity - tot_buy_quantity

            if config.debug:
                sys.stderr.write(f"{Fore.GREEN}conv: split_buy_quantity={split_buy_quantity}\n")
        else:
            split_buy_quantity = None

        sell_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            sell_row.timestamp,
            buy_quantity=split_buy_quantity,
            buy_asset="BNB",
            sell_quantity=abs(Decimal(sell_row.row_dict["Change"])),
            sell_asset=sell_row.row_dict["Coin"],
            wallet=WALLET,
        )


def _get_bnb_quantity(op_rows: List["DataRow"]) -> Optional[Decimal]:
    buy_quantity = None

    for data_row in op_rows:
        if Decimal(data_row.row_dict["Change"]) > 0:
            data_row.parsed = True

            if data_row.row_dict["Coin"] != "BNB":
                continue

            if buy_quantity is None:
                buy_quantity = Decimal(data_row.row_dict["Change"])
            else:
                buy_quantity += Decimal(data_row.row_dict["Change"])

    return buy_quantity


def _make_trade(op_rows: List["DataRow"], t_type: TrType = TrType.TRADE) -> None:
    buy_quantity = sell_quantity = None
    buy_asset = sell_asset = ""
    trade_row = None

    for data_row in op_rows:
        row_dict = data_row.row_dict

        if Decimal(row_dict["Change"]) > 0:
            if buy_quantity is None:
                buy_quantity = Decimal(row_dict["Change"])
                buy_asset = row_dict["Coin"]
                data_row.parsed = True

        if Decimal(row_dict["Change"]) <= 0:
            if sell_quantity is None:
                sell_quantity = abs(Decimal(row_dict["Change"]))
                sell_asset = row_dict["Coin"]
                data_row.parsed = True

        if not trade_row:
            trade_row = data_row

        if buy_quantity and sell_quantity:
            break

    if trade_row:
        trade_row.t_record = TransactionOutRecord(
            t_type,
            trade_row.timestamp,
            buy_quantity=buy_quantity,
            buy_asset=buy_asset,
            sell_quantity=sell_quantity,
            sell_asset=sell_asset,
            wallet=WALLET,
        )


def _make_trade_with_fee(op_rows: List["DataRow"]) -> None:
    buy_quantity = sell_quantity = fee_quantity = None
    buy_asset = sell_asset = fee_asset = ""
    trade_row = None

    for data_row in op_rows:
        row_dict = data_row.row_dict

        if Decimal(row_dict["Change"]) > 0:
            if buy_quantity is None:
                buy_quantity = Decimal(row_dict["Change"])
                buy_asset = row_dict["Coin"]
                data_row.parsed = True

        if Decimal(row_dict["Change"]) <= 0:
            if row_dict["Operation"] in ("Fee", "Transaction Fee"):
                if fee_quantity is None:
                    fee_quantity = abs(Decimal(row_dict["Change"]))
                    fee_asset = row_dict["Coin"]
                    data_row.parsed = True
            else:
                if sell_quantity is None:
                    sell_quantity = abs(Decimal(row_dict["Change"]))
                    sell_asset = row_dict["Coin"]
                    data_row.parsed = True

        if not trade_row:
            trade_row = data_row

        if buy_quantity and sell_quantity and fee_quantity:
            break

    if trade_row:
        trade_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            trade_row.timestamp,
            buy_quantity=buy_quantity,
            buy_asset=buy_asset,
            sell_quantity=sell_quantity,
            sell_asset=sell_asset,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            wallet=WALLET,
        )


DataParser(
    ParserType.EXCHANGE,
    "Binance Trades",
    ["Date(UTC)", "Market", "Type", "Price", "Amount", "Total", "Fee", "Fee Coin"],
    worksheet_name="Binance T",
    row_handler=parse_binance_trades,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Trades",
    [
        "Date",
        "Pair",
        "Type",
        "Sell",
        "Buy",
        "Price",
        "Inverse Price",
        "Date Updated",
        "Status",
    ],
    worksheet_name="Binance T",
    row_handler=parse_binance_convert,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Trades",
    [
        "Date",
        "Wallet",
        "Pair",
        "Type",
        "Sell",
        "Buy",
        "Price",
        "Inverse Price",
        "Date Updated",
        "Status",
    ],
    worksheet_name="Binance T",
    row_handler=parse_binance_convert,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Trades",
    ["Date(UTC)", "Pair", "Side", "Price", "Executed", "Amount", "Fee"],
    worksheet_name="Binance T",
    row_handler=parse_binance_trades_statement,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Deposits",
    ["Date(UTC+0)", "Coin", "Network", "Amount", "Address", "TXID", "Status"],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_crypto_v2,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Withdrawals",
    ["Date(UTC+0)", "Coin", "Network", "Amount", "Fee", "Address", "TXID", "Status"],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_crypto_v2,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Deposits/Withdrawals",
    [
        "Date(UTC)",
        "Coin",
        "Network",
        "Amount",
        "TransactionFee",
        "Address",
        "TXID",
        "SourceAddress",
        "PaymentID",
        "Status",
    ],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_crypto_v1,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Deposits/Withdrawals",
    [
        "Date(UTC)",
        "Coin",
        "Amount",
        "TransactionFee",
        "Address",
        "TXID",
        "SourceAddress",
        "PaymentID",
        "Status",
    ],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_crypto_v1,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Deposits/Withdrawals",
    [
        "Date",
        "Coin",
        "Amount",
        "TransactionFee",
        "Address",
        "TXID",
        "SourceAddress",
        "PaymentID",
        "Status",
    ],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_crypto_v1,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Deposits/Withdrawals",
    [
        lambda c: re.match(r"(^Date\((UTC|UTCnull|UTC[-+]\d{1,2})\))", c),
        "Coin",
        "Amount",
        "Status",
        "Payment Method",
        "Indicated Amount",
        "Fee",
        "Order ID",
    ],
    worksheet_name="Binance D,W",
    row_handler=parse_binance_deposits_withdrawals_cash,
)

statements = DataParser(
    ParserType.EXCHANGE,
    "Binance Statements",
    ["User_ID", "UTC_Time", "Account", "Operation", "Coin", "Change", "Remark"],
    worksheet_name="Binance S",
    all_handler=parse_binance_statements,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Statements",
    ["UTC_Time", "Account", "Operation", "Coin", "Change", "Remark"],
    worksheet_name="Binance S",
    all_handler=parse_binance_statements,
)

DataParser(
    ParserType.EXCHANGE,
    "Binance Futures",
    ["Date(UTC)", "type", "Amount", "Asset", "Symbol", "Transaction ID"],
    worksheet_name="Binance F",
    all_handler=parse_binance_futures,
)
