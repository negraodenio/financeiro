"""RAM-only deterministic Leak X-Ray ("Raio-X de Vazamentos") CSV analyzer.

Security & Privacy invariants:
- Processes CSV strictly in memory (io.StringIO) with strict size/row caps.
- Never writes uploaded CSV data to disk or persists individual rows to DB.
- Never returns or stores personal account IDs, names, IBANs, or API keys.
- Never fabricates missing historical FX mid-market rates: unverified rows are explicitly counted.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import io
import logging
import re
import threading
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_CSV_BYTES: int = 512_000
MAX_CSV_ROWS: int = 2_000

_FX_HIST_LOCK = threading.Lock()
_FX_HIST_CACHE: dict[tuple[str, str], dict[str, dict[str, float]]] = {}


def clear_leak_xray_cache() -> None:
    """Clear cached historical ECB mid-market rate tables (used in tests)."""
    with _FX_HIST_LOCK:
        _FX_HIST_CACHE.clear()


def _parse_float(raw: Any) -> float:
    """Parse international/Brazilian formatted numbers safely without crashing."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return 0.0
    # Keep sign, digits, dots, and commas
    cleaned = re.sub(r"[^0-9,.\-]", "", s)
    if not cleaned or cleaned in ("-", ".", ","):
        return 0.0
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 6:
            cleaned = parts[0] + "." + parts[1]
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_amount_and_unit(raw: Any, default_unit: str = "") -> tuple[float, str]:
    """Parse Binance values like '0.025BTC' or '120.50' into (amount, unit)."""
    s = str(raw or "").strip()
    if not s:
        return 0.0, default_unit.upper()
    m = re.match(r"^\s*([\-0-9.,]+)\s*([A-Za-z]{2,10})?\s*$", s)
    if m:
        amt = _parse_float(m.group(1))
        unit = (m.group(2) or default_unit or "").upper()
        return amt, unit
    return _parse_float(s), default_unit.upper()


def _parse_date_ymd(raw: Any) -> str:
    """Extract YYYY-MM-DD from common statement timestamp formats."""
    s = str(raw or "").strip()
    if not s:
        return ""
    m_iso = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", s)
    if m_iso:
        return f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"
    m_dmy = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", s)
    if m_dmy:
        return f"{m_dmy.group(3)}-{m_dmy.group(2)}-{m_dmy.group(1)}"
    return ""


def _row_lookup(row: dict[str, Any], *aliases: str) -> str:
    """Case-insensitive / whitespace-insensitive CSV column lookup."""
    norm_map = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
    for alias in aliases:
        val = norm_map.get(alias.lower())
        if val is not None and val != "":
            return val
    return ""


def detect_provider(fieldnames: list[str] | None) -> str:
    """Infer CSV source provider from header columns."""
    cols = {str(c or "").strip().lower() for c in (fieldnames or [])}
    if not cols:
        return "generic"

    if (
        "transferwise id" in cols
        or ("source currency" in cols and "target currency" in cols)
        or ("exchange from" in cols and "exchange to" in cols)
        or ("total fees" in cols and "exchange rate" in cols and ("currency" in cols or "source currency" in cols))
    ):
        return "wise"

    if ("started date" in cols or "completed date" in cols) and "fee" in cols and "currency" in cols:
        return "revolut"

    if (
        ("date(utc)" in cols or "utc_time" in cols)
        and ("pair" in cols or "market" in cols or "operation" in cols or "fee coin" in cols or "change" in cols)
    ):
        return "binance"

    return "generic"


def _parse_wise_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        date_ymd = _parse_date_ymd(
            _row_lookup(row, "created on", "finished on", "date", "data", "created_at")
        )
        # Format A: Wise Activity / Transfers CSV
        src_curr = _row_lookup(row, "source currency", "exchange from", "currency").upper()
        dst_curr = _row_lookup(row, "target currency", "exchange to").upper()
        src_amt = abs(_parse_float(_row_lookup(row, "source amount (after fees)", "source amount", "amount")))
        dst_amt = abs(_parse_float(_row_lookup(row, "target amount (after fees)", "target amount", "exchange to amount")))
        eff_rate = _parse_float(_row_lookup(row, "exchange rate"))
        fee_amt = abs(_parse_float(_row_lookup(row, "source fee amount", "total fees", "fee")))
        fee_curr = (_row_lookup(row, "source fee currency", "fee currency") or src_curr).upper()
        desc = _row_lookup(row, "description", "note", "title", "direction", "type")

        iof_amt = abs(_parse_float(_row_lookup(row, "iof", "tax", "tax amount")))
        iof_curr = (_row_lookup(row, "iof currency", "tax currency") or dst_curr or src_curr).upper()
        if iof_amt == 0.0 and "iof" in desc.lower() and src_amt > 0 and not dst_curr:
            iof_amt = src_amt
            iof_curr = src_curr
            src_amt = 0.0

        if src_curr and dst_curr and src_curr != dst_curr and src_amt > 0 and dst_amt > 0:
            if eff_rate <= 0:
                eff_rate = dst_amt / src_amt
            tx_type = "FX_CONVERSION"
        elif iof_amt > 0 and fee_amt == 0 and src_amt == 0:
            tx_type = "TAX_IOF"
        elif fee_amt > 0 or src_amt > 0:
            tx_type = "TRANSFER"
        else:
            continue

        if not src_curr and not dst_curr and fee_amt == 0 and iof_amt == 0:
            continue

        normalized.append(
            {
                "date_utc": date_ymd,
                "tx_type": tx_type,
                "provider": "Wise",
                "src_currency": src_curr,
                "src_amount": src_amt,
                "dst_currency": dst_curr,
                "dst_amount": dst_amt,
                "explicit_fee": fee_amt,
                "fee_currency": fee_curr or src_curr,
                "iof_amount": iof_amt,
                "iof_currency": iof_curr or src_curr,
                "effective_rate": eff_rate,
                "description": desc[:80],
            }
        )
    return normalized


def _parse_revolut_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    exchange_legs: list[dict[str, Any]] = []

    for row in rows:
        state = _row_lookup(row, "state").upper()
        if state in ("REVERTED", "FAILED", "DECLINED", "PENDING"):
            continue
        raw_type = _row_lookup(row, "type").upper() or "TRANSFER"
        started = _row_lookup(row, "started date", "completed date")
        date_ymd = _parse_date_ymd(started)
        minute_key = started[:16] if started else date_ymd
        curr = _row_lookup(row, "currency").upper()
        amt = _parse_float(_row_lookup(row, "amount"))
        fee = abs(_parse_float(_row_lookup(row, "fee")))
        desc = _row_lookup(row, "description")

        if raw_type == "EXCHANGE" and curr:
            exchange_legs.append(
                {
                    "date_utc": date_ymd,
                    "minute_key": minute_key,
                    "currency": curr,
                    "amount": amt,
                    "fee": fee,
                    "description": desc[:80],
                }
            )
            continue

        if fee > 0:
            normalized.append(
                {
                    "date_utc": date_ymd,
                    "tx_type": "TRANSFER",
                    "provider": "Revolut",
                    "src_currency": curr,
                    "src_amount": abs(amt),
                    "dst_currency": "",
                    "dst_amount": 0.0,
                    "explicit_fee": fee,
                    "fee_currency": curr,
                    "iof_amount": 0.0,
                    "iof_currency": "",
                    "effective_rate": 0.0,
                    "description": desc[:80],
                }
            )

    # Pair outgoing (-) and incoming (+) Revolut EXCHANGE legs sharing timestamp minute
    used: set[int] = set()
    for i, leg_out in enumerate(exchange_legs):
        if i in used or leg_out["amount"] >= 0:
            continue
        matched_j: int | None = None
        for j, leg_in in enumerate(exchange_legs):
            if j in used or j == i or leg_in["amount"] <= 0:
                continue
            if leg_in["minute_key"] == leg_out["minute_key"] and leg_in["currency"] != leg_out["currency"]:
                matched_j = j
                break
        if matched_j is not None:
            leg_in = exchange_legs[matched_j]
            used.add(i)
            used.add(matched_j)
            src_amt = abs(leg_out["amount"])
            dst_amt = abs(leg_in["amount"])
            fee_amt = leg_out["fee"] if leg_out["fee"] > 0 else leg_in["fee"]
            fee_curr = leg_out["currency"] if leg_out["fee"] > 0 else leg_in["currency"]
            normalized.append(
                {
                    "date_utc": leg_out["date_utc"],
                    "tx_type": "FX_CONVERSION",
                    "provider": "Revolut",
                    "src_currency": leg_out["currency"],
                    "src_amount": src_amt,
                    "dst_currency": leg_in["currency"],
                    "dst_amount": dst_amt,
                    "explicit_fee": fee_amt,
                    "fee_currency": fee_curr,
                    "iof_amount": 0.0,
                    "iof_currency": "",
                    "effective_rate": (dst_amt / src_amt) if src_amt > 0 else 0.0,
                    "description": leg_out["description"],
                }
            )
        elif leg_out["fee"] > 0:
            used.add(i)
            normalized.append(
                {
                    "date_utc": leg_out["date_utc"],
                    "tx_type": "TRANSFER",
                    "provider": "Revolut",
                    "src_currency": leg_out["currency"],
                    "src_amount": abs(leg_out["amount"]),
                    "dst_currency": "",
                    "dst_amount": 0.0,
                    "explicit_fee": leg_out["fee"],
                    "fee_currency": leg_out["currency"],
                    "iof_amount": 0.0,
                    "iof_currency": "",
                    "effective_rate": 0.0,
                    "description": leg_out["description"],
                }
            )

    for idx, leg in enumerate(exchange_legs):
        if idx not in used and leg["fee"] > 0:
            normalized.append(
                {
                    "date_utc": leg["date_utc"],
                    "tx_type": "TRANSFER",
                    "provider": "Revolut",
                    "src_currency": leg["currency"],
                    "src_amount": abs(leg["amount"]),
                    "dst_currency": "",
                    "dst_amount": 0.0,
                    "explicit_fee": leg["fee"],
                    "fee_currency": leg["currency"],
                    "iof_amount": 0.0,
                    "iof_currency": "",
                    "effective_rate": 0.0,
                    "description": leg["description"],
                }
            )

    return normalized


def _parse_binance_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        date_ymd = _parse_date_ymd(_row_lookup(row, "date(utc)", "utc_time", "date"))
        pair = _row_lookup(row, "pair", "market").upper()
        operation = _row_lookup(row, "operation", "type", "side").upper()
        coin = _row_lookup(row, "coin", "asset").upper()
        fee_raw = _row_lookup(row, "fee", "transaction fee")
        fee_coin_col = _row_lookup(row, "fee coin").upper()
        fee_amt, fee_unit = _parse_amount_and_unit(fee_raw, fee_coin_col or coin)
        fee_amt = abs(fee_amt)

        executed_raw = _row_lookup(row, "executed", "amount")
        total_raw = _row_lookup(row, "total", "change")
        exec_amt, exec_unit = _parse_amount_and_unit(executed_raw, coin)
        tot_amt, tot_unit = _parse_amount_and_unit(total_raw, coin)

        is_withdrawal = "WITHDRAW" in operation or "SAQUE" in operation
        if is_withdrawal:
            # Handle either explicit Fee column or 'Withdraw Fee' negative Change row
            change_val = _parse_float(_row_lookup(row, "change", "amount"))
            wd_fee = fee_amt if fee_amt > 0 else (abs(change_val) if "FEE" in operation else 0.0)
            wd_amt = abs(change_val) if abs(change_val) > 0 and "FEE" not in operation else abs(exec_amt)
            if wd_fee > 0 or wd_amt > 0:
                normalized.append(
                    {
                        "date_utc": date_ymd,
                        "tx_type": "WITHDRAWAL",
                        "provider": "Binance",
                        "src_currency": coin or fee_unit,
                        "src_amount": wd_amt,
                        "dst_currency": "",
                        "dst_amount": 0.0,
                        "explicit_fee": wd_fee,
                        "fee_currency": fee_unit or coin,
                        "iof_amount": 0.0,
                        "iof_currency": "",
                        "effective_rate": 0.0,
                        "description": operation[:80],
                    }
                )
            continue

        if pair or "BUY" in operation or "SELL" in operation or "CONVERT" in operation or fee_amt > 0:
            src_curr = tot_unit or coin
            dst_curr = exec_unit or pair
            normalized.append(
                {
                    "date_utc": date_ymd,
                    "tx_type": "CRYPTO_TRADE",
                    "provider": "Binance",
                    "src_currency": src_curr,
                    "src_amount": abs(tot_amt) if tot_amt else abs(exec_amt),
                    "dst_currency": dst_curr,
                    "dst_amount": abs(exec_amt),
                    "explicit_fee": fee_amt,
                    "fee_currency": fee_unit or fee_coin_col or coin or "USDT",
                    "iof_amount": 0.0,
                    "iof_currency": "",
                    "effective_rate": 0.0,
                    "description": (pair or operation)[:80],
                }
            )
    return normalized


def _parse_generic_rows(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        date_ymd = _parse_date_ymd(_row_lookup(row, "data", "data_utc", "date", "date_utc"))
        raw_type = _row_lookup(row, "tipo", "type", "operacao", "tx_type").upper()
        provider = _row_lookup(row, "provedor", "plataforma", "corretora", "provider") or "Planilha"
        src_curr = _row_lookup(row, "moeda_origem", "moeda", "src_currency", "currency_from", "from_currency").upper()
        src_amt = abs(_parse_float(_row_lookup(row, "valor_origem", "valor", "src_amount", "amount_from", "amount")))
        dst_curr = _row_lookup(row, "moeda_destino", "dst_currency", "currency_to", "to_currency").upper()
        dst_amt = abs(_parse_float(_row_lookup(row, "valor_destino", "dst_amount", "amount_to")))
        fee_amt = abs(_parse_float(_row_lookup(row, "taxa", "taxa_explicita", "fee", "explicit_fee", "custo")))
        fee_curr = (_row_lookup(row, "moeda_taxa", "fee_currency") or src_curr or dst_curr).upper()
        iof_amt = abs(_parse_float(_row_lookup(row, "iof", "iof_amount", "imposto")))
        iof_curr = (_row_lookup(row, "moeda_iof", "iof_currency") or dst_curr or src_curr).upper()
        eff_rate = _parse_float(_row_lookup(row, "cotacao", "taxa_cambio", "effective_rate", "exchange_rate"))
        desc = _row_lookup(row, "descricao", "description", "nota", "note")

        if not src_curr and not dst_curr and fee_amt == 0 and iof_amt == 0:
            continue

        if raw_type in ("SAQUE", "WITHDRAW", "WITHDRAWAL", "REDE"):
            tx_type = "WITHDRAWAL"
        elif raw_type in ("CRIPTO", "TRADE", "CRYPTO_TRADE", "SWAP"):
            tx_type = "CRYPTO_TRADE"
        elif src_curr and dst_curr and src_curr != dst_curr and src_amt > 0 and dst_amt > 0:
            tx_type = "FX_CONVERSION"
            if eff_rate <= 0:
                eff_rate = dst_amt / src_amt
        elif raw_type in ("FX", "CAMBIO", "FX_CONVERSION", "REMESSA"):
            tx_type = "FX_CONVERSION"
        elif iof_amt > 0 and fee_amt == 0 and src_amt == 0:
            tx_type = "TAX_IOF"
        else:
            tx_type = "TRANSFER"

        normalized.append(
            {
                "date_utc": date_ymd,
                "tx_type": tx_type,
                "provider": provider[:24],
                "src_currency": src_curr,
                "src_amount": src_amt,
                "dst_currency": dst_curr,
                "dst_amount": dst_amt,
                "explicit_fee": fee_amt,
                "fee_currency": fee_curr,
                "iof_amount": iof_amt,
                "iof_currency": iof_curr,
                "effective_rate": eff_rate,
                "description": desc[:80],
            }
        )
    return normalized


def fetch_historical_fx_rates(
    start_date: str,
    end_date: str,
    allow_network: bool = True,
) -> dict[str, dict[str, float]]:
    """Fetch official historical ECB reference rates (EUR base -> BRL, USD) via Frankfurter API."""
    if not start_date or not end_date:
        return {}
    s_date, e_date = (start_date, end_date) if start_date <= end_date else (end_date, start_date)
    cache_key = (s_date, e_date)
    with _FX_HIST_LOCK:
        if cache_key in _FX_HIST_CACHE:
            return {k: dict(v) for k, v in _FX_HIST_CACHE[cache_key].items()}

    if not allow_network:
        return {}

    url = f"https://api.frankfurter.app/{s_date}..{e_date}?from=EUR&to=BRL,USD"
    try:
        resp = httpx.get(url, timeout=4.0)
        if resp.status_code == 200:
            data = resp.json()
            raw_rates = data.get("rates") or {}
            clean_rates: dict[str, dict[str, float]] = {}
            for dt_str, mapping in raw_rates.items():
                if isinstance(mapping, dict):
                    clean_rates[str(dt_str)] = {
                        str(k).upper(): float(v) for k, v in mapping.items() if float(v) > 0
                    }
            if clean_rates:
                with _FX_HIST_LOCK:
                    _FX_HIST_CACHE[cache_key] = clean_rates
                return {k: dict(v) for k, v in clean_rates.items()}
    except Exception as exc:
        logger.debug("Historical ECB lookup skipped (%s..%s): %s", s_date, e_date, exc)

    return {}


def _lookup_mid_rate(
    rates_by_date: dict[str, dict[str, float]],
    date_str: str,
    src_curr: str,
    dst_curr: str,
) -> float | None:
    """Return verified ECB mid-market conversion rate src_curr -> dst_curr for date_str (up to 4 prior days)."""
    sc = src_curr.upper()
    dc = dst_curr.upper()
    if not sc or not dc or not rates_by_date or not date_str:
        return None
    if sc == dc:
        return 1.0

    supported = {"EUR", "BRL", "USD"}
    if sc not in supported or dc not in supported:
        return None

    try:
        base_dt = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None

    day_map: dict[str, float] | None = None
    for offset in range(5):
        probe = (base_dt - timedelta(days=offset)).isoformat()
        if probe in rates_by_date:
            day_map = rates_by_date[probe]
            break

    if not day_map:
        return None

    eur_to = {"EUR": 1.0}
    if "BRL" in day_map:
        eur_to["BRL"] = float(day_map["BRL"])
    if "USD" in day_map:
        eur_to["USD"] = float(day_map["USD"])

    if sc not in eur_to or dc not in eur_to:
        return None
    return eur_to[dc] / eur_to[sc]


def _convert_amount_to_base(
    amount: float,
    currency: str,
    base_currency: str,
    date_str: str,
    rates_by_date: dict[str, dict[str, float]],
    fallback_eur_brl: float,
    fallback_usd_brl: float,
) -> float:
    """Convert an explicit fee or IOF into display base_currency using verified historical ECB when present."""
    if amount <= 0:
        return 0.0
    curr = (currency or base_currency).upper()
    # Treat stablecoins pegged ~1:1 to USD only for explicit fee currency display conversion
    fiat_proxy = "USD" if curr in ("USDT", "USDC", "BUSD") else curr
    if fiat_proxy == base_currency:
        return amount

    hist_rate = _lookup_mid_rate(rates_by_date, date_str, fiat_proxy, base_currency)
    if hist_rate is not None and hist_rate > 0:
        return amount * hist_rate

    eur_brl = fallback_eur_brl if fallback_eur_brl > 0 else 6.10
    usd_brl = fallback_usd_brl if fallback_usd_brl > 0 else 5.70
    eur_usd = eur_brl / usd_brl

    if base_currency == "EUR":
        if fiat_proxy == "BRL":
            return amount / eur_brl
        if fiat_proxy == "USD":
            return amount / eur_usd
    else:  # BRL
        if fiat_proxy == "EUR":
            return amount * eur_brl
        if fiat_proxy == "USD":
            return amount * usd_brl

    return amount


def get_sample_leak_csv() -> str:
    """Return an anonymized illustrative 10-row statement CSV spanning FX transfers and repeated withdrawals."""
    return "\n".join(
        [
            "data_utc,tipo,provedor,moeda_origem,valor_origem,moeda_destino,valor_destino,taxa_explicita,moeda_taxa,iof,moeda_iof,descricao",
            "2026-01-12,FX_CONVERSION,Wise,EUR,1200.00,BRL,7120.00,13.80,EUR,26.00,BRL,Remessa mensal Janeiro",
            "2026-02-10,FX_CONVERSION,Revolut,EUR,1000.00,BRL,5880.00,14.50,EUR,21.50,BRL,Transferencia Fevereiro",
            "2026-03-04,WITHDRAWAL,Binance,BTC,0.025,BTC,0.0246,0.0004,BTC,0,BRL,Saque rede BTC #1",
            "2026-03-19,WITHDRAWAL,Binance,BTC,0.015,BTC,0.0146,0.0004,BTC,0,BRL,Saque rede BTC #2",
            "2026-04-08,CRYPTO_TRADE,Binance,USDT,1500.00,BTC,0.018,3.20,USDT,0,BRL,Spot BTC/USDT",
            "2026-05-14,FX_CONVERSION,Banco,EUR,1500.00,BRL,8650.00,45.00,EUR,95.00,BRL,Remessa banco tradicional",
            "2026-06-11,WITHDRAWAL,Binance,USDT,400.00,USDT,392.00,8.00,USDT,0,BRL,Saque USDT taxa fixa #3",
            "2026-07-16,FX_CONVERSION,Wise,EUR,1100.00,BRL,6590.00,11.90,EUR,24.00,BRL,Remessa Julho",
            "2026-08-21,WITHDRAWAL,Binance,USDT,350.00,USDT,342.00,8.00,USDT,0,BRL,Saque USDT taxa fixa #4",
            "2026-09-18,FX_CONVERSION,Wise,USD,1000.00,BRL,5540.00,11.20,USD,20.00,BRL,Recebimento USD",
        ]
    )


def analyze_leak_xray(
    csv_text: str,
    provider: str = "auto",
    base_currency: str = "EUR",
    allow_network: bool = True,
    is_sample: bool = False,
    fallback_eur_brl: float = 6.10,
    fallback_usd_brl: float = 5.70,
) -> dict[str, Any]:
    """Parse uploaded CSV strictly in RAM and classify fees/spread into inevitable, reducible, and avoidable."""
    if not csv_text or not csv_text.strip():
        raise ValueError("Envie um arquivo CSV com pelo menos uma linha de extrato.")

    raw_bytes = csv_text.encode("utf-8", errors="ignore")
    if len(raw_bytes) > MAX_CSV_BYTES:
        raise ValueError(f"Arquivo excede o limite seguro de {MAX_CSV_BYTES // 1000} KB em memória.")

    cleaned_text = csv_text.lstrip("\ufeff").strip()
    try:
        sample_lines = cleaned_text[:4096]
        dialect = csv.Sniffer().sniff(sample_lines, delimiters=",;\t")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ";" if cleaned_text.splitlines()[0].count(";") > cleaned_text.splitlines()[0].count(",") else ","

    reader = csv.DictReader(io.StringIO(cleaned_text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("Cabeçalho CSV não reconhecido. Use extrato CSV da Wise, Revolut, Binance ou planilha padrão.")

    rows: list[dict[str, str]] = []
    for idx, row in enumerate(reader):
        if idx >= MAX_CSV_ROWS:
            raise ValueError(f"Extrato excede o limite de {MAX_CSV_ROWS} linhas por diagnóstico.")
        rows.append(row)

    if not rows:
        raise ValueError("O arquivo CSV não possui linhas de dados.")

    req_provider = (provider or "auto").strip().lower()
    detected_provider = detect_provider(list(reader.fieldnames)) if req_provider == "auto" else req_provider

    if detected_provider == "wise":
        txs = _parse_wise_rows(rows)
    elif detected_provider == "revolut":
        txs = _parse_revolut_rows(rows)
    elif detected_provider == "binance":
        txs = _parse_binance_rows(rows)
    else:
        txs = _parse_generic_rows(rows)

    if not txs:
        raise ValueError(
            "Nenhuma operação financeira reconhecida neste CSV. Confira o formato ou teste com o Exemplo Ilustrativo."
        )

    base_curr = "BRL" if (base_currency or "EUR").strip().upper() == "BRL" else "EUR"
    curr_sym = "R$" if base_curr == "BRL" else "€"

    valid_dates = sorted([tx["date_utc"] for tx in txs if tx.get("date_utc")])
    rates_by_date: dict[str, dict[str, float]] = {}
    if valid_dates:
        start_dt = valid_dates[0]
        try:
            start_pad = (datetime.strptime(start_dt, "%Y-%m-%d").date() - timedelta(days=4)).isoformat()
        except ValueError:
            start_pad = start_dt
        rates_by_date = fetch_historical_fx_rates(start_pad, valid_dates[-1], allow_network=allow_network)

    # Note: approximate current crypto conversions for BTC/ETH fee coins ONLY for fee display if CSV shows BTC fee
    crypto_usd_ref = {"BTC": 90000.0, "ETH": 3100.0, "SOL": 200.0, "BNB": 600.0}

    def fee_to_base(amt: float, curr: str, dt_str: str) -> float:
        if amt <= 0:
            return 0.0
        c = (curr or base_curr).upper()
        if c in crypto_usd_ref:
            usd_val = amt * crypto_usd_ref[c]
            return _convert_amount_to_base(
                usd_val, "USD", base_curr, dt_str, rates_by_date, fallback_eur_brl, fallback_usd_brl
            )
        return _convert_amount_to_base(
            amt, c, base_curr, dt_str, rates_by_date, fallback_eur_brl, fallback_usd_brl
        )

    withdrawals_count = sum(1 for t in txs if t["tx_type"] == "WITHDRAWAL")

    inevitable_total = 0.0
    reducible_total = 0.0
    avoidable_total = 0.0
    alt_estimated_total = 0.0

    fx_ops = 0
    verified_spread_count = 0
    unverified_spread_count = 0
    per_op_costs: list[dict[str, Any]] = []

    for tx in txs:
        dt_str = tx["date_utc"]
        tx_type = tx["tx_type"]
        explicit_base = fee_to_base(tx["explicit_fee"], tx["fee_currency"], dt_str)
        iof_base = fee_to_base(tx["iof_amount"], tx["iof_currency"], dt_str)

        spread_base = 0.0
        spread_status = "NOT_APPLICABLE"
        volume_base = 0.0

        if tx_type == "FX_CONVERSION":
            fx_ops += 1
            volume_base = _convert_amount_to_base(
                tx["src_amount"], tx["src_currency"], base_curr, dt_str, rates_by_date, fallback_eur_brl, fallback_usd_brl
            )
            mid_rate = _lookup_mid_rate(rates_by_date, dt_str, tx["src_currency"], tx["dst_currency"])
            if mid_rate is not None and mid_rate > 0 and tx["src_amount"] > 0 and tx["dst_amount"] > 0:
                verified_spread_count += 1
                spread_status = "VERIFIED"
                expected_dst = tx["src_amount"] * mid_rate
                if expected_dst > tx["dst_amount"]:
                    spread_in_dst = expected_dst - tx["dst_amount"]
                    spread_base = _convert_amount_to_base(
                        spread_in_dst, tx["dst_currency"], base_curr, dt_str, rates_by_date, fallback_eur_brl, fallback_usd_brl
                    )
            else:
                unverified_spread_count += 1
                spread_status = "UNVERIFIED"

        op_inev = iof_base
        op_red = 0.0
        op_avoid = 0.0
        op_alt = iof_base  # Taxes remain in alternative route estimate

        if tx_type == "FX_CONVERSION":
            combined_fx = explicit_base + spread_base
            # Spread + FX fees above 2.5% of transfer volume classified as excess/avoidable
            excess_threshold = volume_base * 0.025 if volume_base > 0 else combined_fx
            if combined_fx > excess_threshold > 0:
                op_red = excess_threshold
                op_avoid = combined_fx - excess_threshold
            else:
                op_red = combined_fx

            # Alternative regulated competitive FX route benchmark (~0.85%)
            alt_fx_cost = volume_base * 0.0085 if volume_base > 0 else combined_fx * 0.6
            op_alt += min(combined_fx, alt_fx_cost)

        elif tx_type == "WITHDRAWAL":
            op_vol_base = fee_to_base(tx["src_amount"], tx["src_currency"], dt_str)
            pct_of_wd = (explicit_base / op_vol_base) if op_vol_base > 0 else 0.0
            if withdrawals_count >= 2 or pct_of_wd > 0.01:
                op_avoid = explicit_base
            else:
                op_red = explicit_base
            # Grouping multiple small withdrawals saves ~65% of fixed network fees
            op_alt += explicit_base * (0.35 if withdrawals_count >= 3 else 0.70)

        elif tx_type == "CRYPTO_TRADE":
            op_red = explicit_base
            op_alt += min(explicit_base, explicit_base * 0.50)

        else:
            op_red = explicit_base
            op_alt += explicit_base * 0.70

        op_total = op_inev + op_red + op_avoid
        inevitable_total += op_inev
        reducible_total += op_red
        avoidable_total += op_avoid
        alt_estimated_total += op_alt

        if op_total > 0:
            pair_label = (
                f"{tx['src_currency']}→{tx['dst_currency']}"
                if tx["src_currency"] and tx["dst_currency"]
                else (tx["src_currency"] or tx["fee_currency"] or tx_type)
            )
            dom_cat = "inevitable"
            if op_avoid >= op_red and op_avoid >= op_inev:
                dom_cat = "avoidable"
            elif op_red >= op_inev:
                dom_cat = "reducible"

            per_op_costs.append(
                {
                    "date_utc": dt_str or "--",
                    "provider": tx["provider"],
                    "tx_type": tx_type,
                    "pair": pair_label,
                    "total_cost": round(op_total, 2),
                    "inevitable": round(op_inev, 2),
                    "reducible": round(op_red, 2),
                    "avoidable": round(op_avoid, 2),
                    "dominant_category": dom_cat,
                    "spread_status": spread_status,
                }
            )

    total_cost = round(inevitable_total + reducible_total + avoidable_total, 2)
    alt_estimated_total = round(min(total_cost, alt_estimated_total), 2)
    potential_diff = round(max(0.0, total_cost - alt_estimated_total), 2)

    per_op_costs.sort(key=lambda item: item["total_cost"], reverse=True)
    top_leaks = per_op_costs[:3]
    top3_sum = sum(item["total_cost"] for item in top_leaks)
    top3_share_pct = round((top3_sum / total_cost) * 100.0, 1) if total_cost > 0 else 0.0

    sim_prefix = "[EXEMPLO ILUSTRATIVO — SIMULAÇÃO]\n" if is_sample else ""
    whatsapp_share_text = sim_prefix + "\n".join(
        [
            f"🩺 *RAIO-X DE VAZAMENTOS | SCUDEX ({len(txs)} operações)*",
            f"Custos totais identificados: *{curr_sym} {total_cost:,.2f}*",
            f"• Evitáveis (saques repetidos / excesso): {curr_sym} {avoidable_total:,.2f}",
            f"• Reduzíveis (taxas + spread verificado): {curr_sym} {reducible_total:,.2f}",
            f"• Inevitáveis (IOF / impostos): {curr_sym} {inevitable_total:,.2f}",
            "",
            f"💡 Cenário com rota alternativa (estimativa): ≈ {curr_sym} {alt_estimated_total:,.2f}",
            f"Diferença potencial estimada: *≈ {curr_sym} {potential_diff:,.2f}*",
            "",
            "🛡️ _Extrato processado em RAM e descartado na hora — nenhum dado pessoal salvo._",
            "Informativo e comparativo; não é garantia ou recomendação.",
        ]
    )

    return {
        "detected_provider": detected_provider,
        "is_sample": bool(is_sample),
        "base_currency": base_curr,
        "currency_symbol": curr_sym,
        "operations_analyzed": len(txs),
        "fx_operations_count": fx_ops,
        "verified_spread_count": verified_spread_count,
        "unverified_spread_count": unverified_spread_count,
        "date_range": {"start": valid_dates[0], "end": valid_dates[-1]} if valid_dates else {"start": None, "end": None},
        "total_cost": total_cost,
        "categories": {
            "avoidable": round(avoidable_total, 2),
            "reducible": round(reducible_total, 2),
            "inevitable": round(inevitable_total, 2),
        },
        "alternative_estimated_cost": alt_estimated_total,
        "potential_difference": potential_diff,
        "alternative_disclaimer": (
            "Cenário comparativo informativo (remessadora regulada ~0,85% + agrupamento de saques + IOF mantido). "
            "Limites, KYC e variação cambial alteram o resultado real."
        ),
        "top_leaks": top_leaks,
        "top3_share_pct": top3_share_pct,
        "privacy_notice": (
            "Seu arquivo CSV foi processado apenas em memória RAM e descartado imediatamente após o diagnóstico. "
            "Nenhuma linha individual ou dado pessoal foi salvo."
        ),
        "whatsapp_share_text": whatsapp_share_text,
    }
