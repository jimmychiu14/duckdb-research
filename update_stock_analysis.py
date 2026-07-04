import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import duckdb

WORKSPACE_DIR = "/home/jimmyc/.hermes/workspace/duckdb-research"
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
DEFAULT_CONTROL_FILE = os.path.join(WORKSPACE_DIR, "stock_watchlist.md")
WATCHLIST_JSON_PATH = os.path.join(DATA_DIR, "watchlist.json")
FIELDNAMES = ["Date", "Volume", "Amount", "Open", "High", "Low", "Close", "Change", "Transactions"]

os.makedirs(DATA_DIR, exist_ok=True)


@dataclass(frozen=True)
class StockTarget:
    code: str
    name: str = ""
    market: str = "auto"  # auto | twse | tpex | yahoo


def make_serializable(obj):
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_serializable(i) for i in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def parse_watchlist_markdown(path):
    """Parse Markdown control file stock bullets under '當前追蹤標的'.

    Accepted examples:
      - 2330 台積電 @twse
      - 4979 華星光 @tpex
      - 2454 聯發科
    """
    if not os.path.exists(path):
        return []

    targets = []
    in_target_section = False
    saw_target_section = False
    bullet_re = re.compile(r"^\s*[-*]\s+(\d{4,6})(?:\s+([^@#]+?))?(?:\s+@(auto|twse|tpex|yahoo))?\s*(?:#.*)?$", re.I)

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line.startswith("##"):
                in_target_section = "追蹤標的" in line or "監控標的" in line or "watchlist" in line.lower()
                saw_target_section = saw_target_section or in_target_section
                continue

            if saw_target_section and not in_target_section:
                continue

            m = bullet_re.match(line)
            if not m:
                continue
            code = m.group(1)
            name = (m.group(2) or "").strip()
            market = (m.group(3) or "auto").lower()
            targets.append(StockTarget(code=code, name=name, market=market))

    # De-duplicate by code, preserving first occurrence.
    deduped = []
    seen = set()
    for target in targets:
        if target.code not in seen:
            deduped.append(target)
            seen.add(target.code)
    return deduped


def clean_num(val):
    if val is None:
        return 0.0
    val_clean = str(val).replace(",", "").strip()
    val_clean = val_clean.replace("+", "").replace("－", "-").replace("＋", "")
    if not val_clean or val_clean in {"--", "X", "null", "None"}:
        return 0.0
    try:
        return float(val_clean)
    except ValueError:
        return 0.0


def roc_to_iso(raw_date):
    parts = str(raw_date).split("/")
    if len(parts) == 3:
        return f"{int(parts[0]) + 1911}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return str(raw_date)


def clean_twse_row(row):
    try:
        return {
            "Date": roc_to_iso(row[0]),
            "Volume": int(str(row[1]).replace(",", "")),
            "Amount": int(str(row[2]).replace(",", "")),
            "Open": clean_num(row[3]),
            "High": clean_num(row[4]),
            "Low": clean_num(row[5]),
            "Close": clean_num(row[6]),
            "Change": clean_num(row[7]),
            "Transactions": int(str(row[8]).replace(",", "")),
        }
    except Exception:
        return None


def extract_stock_name(title, stock_code):
    if not title:
        return f"個股 {stock_code}"
    idx = title.find(str(stock_code))
    if idx != -1:
        name_part = title[idx + len(str(stock_code)):].strip()
        for suffix in ["個股日本來成交資訊", "個股日成交資訊", "個股日本來", "個股", "成交資訊"]:
            name_part = name_part.replace(suffix, "")
        return name_part.strip() or f"個股 {stock_code}"
    return f"個股 {stock_code}"


def fetch_json(url, timeout=20):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8-sig", errors="replace")
    if body.lstrip().startswith("<"):
        raise ValueError("Endpoint returned HTML instead of JSON")
    return json.loads(body)


def fetch_twse_data(year, month, stock_code):
    date_str = f"{year}{month:02d}01"
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"
    try:
        data = fetch_json(url, timeout=20)
        if data.get("stat") == "OK":
            rows = [clean_twse_row(row) for row in data.get("data", [])]
            return [row for row in rows if row], data.get("title", ""), "twse"
        print(f"TWSE returned non-OK status for {stock_code} {year}-{month:02d}: {data.get('stat')}")
    except Exception as e:
        print(f"TWSE fetch error for {stock_code} {year}-{month:02d}: {e}")
    finally:
        time.sleep(2)
    return [], "", "twse"


def fetch_tpex_data(year, month, stock_code):
    """Fetch TPEx monthly data when reachable.

    TPEx web endpoints are sometimes protected by Cloudflare from this VM. This function is
    intentionally best-effort; Yahoo Finance fallback keeps OTC monitoring usable.
    """
    roc_month = f"{year - 1911}/{month:02d}"
    urls = [
        f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?date={roc_month}&code={stock_code}&response=json",
        f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?l=zh-tw&d={roc_month}&stkno={stock_code}",
    ]
    for url in urls:
        try:
            data = fetch_json(url, timeout=20)
            raw_rows = data.get("data") or data.get("aaData") or data.get("tables", [{}])[0].get("data", [])
            rows = [clean_twse_row(row) for row in raw_rows]
            rows = [row for row in rows if row]
            if rows:
                title = data.get("title", f"{stock_code} 上櫃個股日成交資訊")
                return rows, title, "tpex"
        except Exception as e:
            print(f"TPEx fetch error for {stock_code} {year}-{month:02d}: {e}")
        finally:
            time.sleep(2)
    return [], "", "tpex"


def yahoo_chart_to_standard_rows(payload):
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows = []
    previous_close = None
    for idx, ts in enumerate(timestamps):
        close = closes[idx] if idx < len(closes) else None
        if close is None:
            continue
        open_price = opens[idx] if idx < len(opens) and opens[idx] is not None else close
        high = highs[idx] if idx < len(highs) and highs[idx] is not None else close
        low = lows[idx] if idx < len(lows) and lows[idx] is not None else close
        volume = int(volumes[idx] or 0) if idx < len(volumes) else 0
        change = 0.0 if previous_close is None else round(float(close) - float(previous_close), 2)
        rows.append(
            {
                "Date": datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
                "Volume": volume,
                "Amount": int(round(float(close) * volume)),
                "Open": round(float(open_price), 2),
                "High": round(float(high), 2),
                "Low": round(float(low), 2),
                "Close": round(float(close), 2),
                "Change": change,
                "Transactions": 0,
            }
        )
        previous_close = close
    return rows


def fetch_yahoo_data(stock_code, market="auto"):
    suffixes = {
        "twse": ["TW"],
        "tpex": ["TWO"],
        "yahoo": ["TW", "TWO"],
        "auto": ["TW", "TWO"],
    }.get(market, ["TW", "TWO"])

    for suffix in suffixes:
        symbol = f"{stock_code}.{suffix}"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
        try:
            payload = fetch_json(url, timeout=20)
            rows = yahoo_chart_to_standard_rows(payload)
            if rows:
                exchange_name = ((payload.get("chart", {}).get("result") or [{}])[0].get("meta") or {}).get("exchangeName", suffix)
                return rows, f"{stock_code} Yahoo Finance {exchange_name}", f"yahoo-{suffix.lower()}"
        except Exception as e:
            print(f"Yahoo fetch error for {symbol}: {e}")
    return [], "", "yahoo"


def load_existing_rows(csv_path):
    existing = {}
    if not os.path.exists(csv_path):
        return existing
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                existing[row["Date"]] = {
                    "Date": row["Date"],
                    "Volume": int(float(row["Volume"])),
                    "Amount": int(float(row["Amount"])),
                    "Open": float(row["Open"]),
                    "High": float(row["High"]),
                    "Low": float(row["Low"]),
                    "Close": float(row["Close"]),
                    "Change": float(row["Change"]),
                    "Transactions": int(float(row["Transactions"])),
                }
            except Exception as e:
                print(f"Skipping malformed row in {csv_path}: {e}")
    return existing


def fetch_official_months(stock_code, market, months_to_fetch):
    current_date = datetime.now()
    rows = []
    title = ""
    source = ""
    current_month_success = False
    for i in range(months_to_fetch):
        fetch_date = current_date - timedelta(days=30 * i)
        year, month = fetch_date.year, fetch_date.month
        if market in {"twse", "auto"}:
            month_rows, month_title, month_source = fetch_twse_data(year, month, stock_code)
            if month_rows:
                if i == 0:
                    current_month_success = True
                rows.extend(month_rows)
                title = title or month_title
                source = month_source
                continue
        if market in {"tpex", "auto"}:
            month_rows, month_title, month_source = fetch_tpex_data(year, month, stock_code)
            if month_rows:
                if i == 0:
                    current_month_success = True
                rows.extend(month_rows)
                title = title or month_title
                source = month_source
    if months_to_fetch > 0 and not current_month_success:
        print(f"Official source failed to fetch current month's data for {stock_code}. Forcing Yahoo Finance fallback.")
        return [], "", ""
    return rows, title, source


# ─── Technical Indicator Functions (pure Python, no extra deps) ───


def compute_sma(values, period):
    """Simple Moving Average — returns list with None for warmup period."""
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = round(sum(values[i - period + 1 : i + 1]) / period, 2)
    return result


def compute_ema(values, period):
    """Exponential Moving Average — seeded with SMA."""
    result = [None] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = round(seed, 4)
    for i in range(period, len(values)):
        result[i] = round(values[i] * k + result[i - 1] * (1 - k), 4)
    return result


def compute_rsi(closes, period=14):
    """Relative Strength Index (Wilder's smoothing)."""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = round(100 - 100 / (1 + rs), 2)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = round(100 - 100 / (1 + rs), 2)
    return result


def compute_macd(closes, fast=12, slow=26, signal=9):
    """MACD: returns (dif, macd_signal, osc_histogram)."""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)
    dif = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = round(ema_fast[i] - ema_slow[i], 4)
    first_valid = next((i for i, v in enumerate(dif) if v is not None), None)
    macd_line = [None] * len(closes)
    if first_valid is not None:
        signal_ema = compute_ema(dif[first_valid:], signal)
        for i, v in enumerate(signal_ema):
            if v is not None:
                macd_line[first_valid + i] = v
    osc = [None] * len(closes)
    for i in range(len(closes)):
        if dif[i] is not None and macd_line[i] is not None:
            osc[i] = round(dif[i] - macd_line[i], 4)
    return dif, macd_line, osc


def compute_kd(highs, lows, closes, period=9):
    """Stochastic Oscillator K & D (seeded at 50)."""
    k_vals = [None] * len(closes)
    d_vals = [None] * len(closes)
    if len(closes) < period:
        return k_vals, d_vals
    rsv = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        hh = max(highs[i - period + 1 : i + 1])
        ll = min(lows[i - period + 1 : i + 1])
        rsv[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(closes)):
        if rsv[i] is not None:
            k = 2 / 3 * prev_k + 1 / 3 * rsv[i]
            d = 2 / 3 * prev_d + 1 / 3 * k
            k_vals[i] = round(k, 2)
            d_vals[i] = round(d, 2)
            prev_k, prev_d = k, d
    return k_vals, d_vals


def compute_atr(highs, lows, closes, period=14):
    """Average True Range (Wilder's smoothing)."""
    result = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    trs = [0.0] * len(closes)
    for i in range(1, len(closes)):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    first_atr = sum(trs[1 : period + 1]) / period
    result[period] = round(first_atr, 4)
    for i in range(period + 1, len(closes)):
        atr = (result[i - 1] * (period - 1) + trs[i]) / period
        result[i] = round(atr, 4)
    return result


def compute_bollinger(closes, period=20, num_std=2):
    """Bollinger Bands: returns (upper, middle, lower)."""
    upper = [None] * len(closes)
    middle = [None] * len(closes)
    lower = [None] * len(closes)
    if len(closes) < period:
        return upper, middle, lower
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        middle[i] = round(mean, 2)
        upper[i] = round(mean + num_std * std, 2)
        lower[i] = round(mean - num_std * std, 2)
    return upper, middle, lower


def compute_volume_ma(volumes, period):
    """Volume Moving Average."""
    result = [None] * len(volumes)
    for i in range(period - 1, len(volumes)):
        result[i] = int(sum(volumes[i - period + 1 : i + 1]) / period)
    return result


def enrich_chart_data(chart_data):
    """Add per-day technical indicators to chart_data in-place."""
    closes = [float(d["Close"]) for d in chart_data]
    highs = [float(d["High"]) for d in chart_data]
    lows = [float(d["Low"]) for d in chart_data]
    volumes = [int(d["Volume"]) for d in chart_data]

    rsi = compute_rsi(closes, 14)
    dif, macd_line, osc = compute_macd(closes)
    k_vals, d_vals = compute_kd(highs, lows, closes)
    atr = compute_atr(highs, lows, closes)
    bb_upper, bb_mid, bb_lower = compute_bollinger(closes)
    vol_ma5 = compute_volume_ma(volumes, 5)
    vol_ma20 = compute_volume_ma(volumes, 20)

    for i, d in enumerate(chart_data):
        d["RSI"] = rsi[i]
        d["DIF"] = dif[i]
        d["MACD"] = macd_line[i]
        d["OSC"] = osc[i]
        d["K"] = k_vals[i]
        d["D"] = d_vals[i]
        d["ATR"] = atr[i]
        d["BB_Upper"] = bb_upper[i]
        d["BB_Middle"] = bb_mid[i]
        d["BB_Lower"] = bb_lower[i]
        d["Vol_MA5"] = vol_ma5[i]
        d["Vol_MA20"] = vol_ma20[i]
    return chart_data


def compute_extended_metrics(sorted_rows, chart_data):
    """Compute summary-level metrics from full history."""
    closes = [float(r["Close"]) for r in sorted_rows]
    highs = [float(r["High"]) for r in sorted_rows]
    lows = [float(r["Low"]) for r in sorted_rows]
    volumes = [int(r["Volume"]) for r in sorted_rows]
    amounts = [int(r["Amount"]) for r in sorted_rows]
    n = len(closes)
    last_close = closes[-1] if n else 0.0

    # Monthly return (~21 trading days)
    monthly_return = round((last_close - closes[-22]) / closes[-22] * 100, 2) if n >= 22 else 0.0
    # Quarterly return (~63 trading days)
    quarterly_return = round((last_close - closes[-64]) / closes[-64] * 100, 2) if n >= 64 else 0.0

    # Annualized volatility
    daily_returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, n)
        if closes[i - 1] != 0
    ]
    volatility = 0.0
    if len(daily_returns) >= 2:
        mean_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        volatility = round(variance ** 0.5 * (252 ** 0.5) * 100, 2)

    # 52-week high/low (~252 trading days)
    lookback = min(252, n)
    week_52_high = max(highs[-lookback:]) if lookback else last_close
    week_52_low = min(lows[-lookback:]) if lookback else last_close
    pct_from_high = round((last_close - week_52_high) / week_52_high * 100, 2) if week_52_high else 0.0
    pct_from_low = round((last_close - week_52_low) / week_52_low * 100, 2) if week_52_low else 0.0

    # VWAP (full history)
    total_volume = sum(volumes)
    vwap = round(sum(amounts) / total_volume, 2) if total_volume > 0 else 0.0

    # Max consecutive up/down days
    max_up = max_down = cur_up = cur_down = 0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            cur_up += 1; cur_down = 0; max_up = max(max_up, cur_up)
        elif closes[i] < closes[i - 1]:
            cur_down += 1; cur_up = 0; max_down = max(max_down, cur_down)
        else:
            cur_up = cur_down = 0

    # Support/Resistance (recent 60 days)
    recent_lb = min(60, n)
    resistance = max(highs[-recent_lb:]) if recent_lb else last_close
    support = min(lows[-recent_lb:]) if recent_lb else last_close

    # Volume-price divergence (recent 5 days vs prior 5 days)
    divergence = {"status": "無", "desc": "近期量價同步。"}
    if n >= 6:
        price_up = closes[-1] > closes[-6]
        recent_vol_avg = sum(volumes[-5:]) / 5
        prior_vol_avg = (sum(volumes[-10:-5]) / 5) if n >= 10 else (sum(volumes[:max(1, n - 5)]) / max(1, n - 5))
        if price_up and prior_vol_avg > 0 and recent_vol_avg < prior_vol_avg * 0.8:
            divergence = {"status": "價漲量縮", "desc": "近5日價格上漲但量能萎縮，可能反彈乏力。"}
        elif not price_up and prior_vol_avg > 0 and recent_vol_avg > prior_vol_avg * 1.2:
            divergence = {"status": "價跌量增", "desc": "近5日價格下跌但量能擴大，可能恐慌性賣壓。"}

    return {
        "monthly_return_pct": monthly_return,
        "quarterly_return_pct": quarterly_return,
        "annualized_volatility_pct": volatility,
        "week_52_high": week_52_high,
        "week_52_low": week_52_low,
        "pct_from_52_high": pct_from_high,
        "pct_from_52_low": pct_from_low,
        "vwap": vwap,
        "max_consecutive_up_days": max_up,
        "max_consecutive_down_days": max_down,
        "support_level": support,
        "resistance_level": resistance,
        "volume_price_divergence": divergence,
    }


def analyze_stock(target):
    print(f"=== Starting Stock Analysis Pipeline for {target.code} {target.name} @{target.market} ===")
    csv_path = os.path.join(DATA_DIR, f"{target.code}_daily_history.csv")
    json_path = os.path.join(DATA_DIR, f"{target.code}_analysis.json")

    existing_data = load_existing_rows(csv_path)
    months_to_fetch = 5 if len(existing_data) < 80 else 2

    fetched_rows, title, source = fetch_official_months(target.code, target.market, months_to_fetch)
    if not fetched_rows:
        print(f"Official source unavailable/no data for {target.code}; falling back to Yahoo Finance.")
        fetched_rows, title, source = fetch_yahoo_data(target.code, target.market)

    if not fetched_rows and not existing_data:
        raise RuntimeError(f"Could not retrieve any data for stock symbol {target.code}.")

    new_rows_count = 0
    for row in fetched_rows:
        if row["Date"] not in existing_data:
            new_rows_count += 1
        existing_data[row["Date"]] = row

    sorted_dates = sorted(existing_data.keys())
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for d in sorted_dates:
            writer.writerow(existing_data[d])
    print(f"Saved {len(sorted_dates)} rows to {csv_path} ({new_rows_count} new rows added).")

    stock_name = target.name or extract_stock_name(title, target.code)
    con = duckdb.connect()
    ma_sql = f"""
    WITH ma_calc AS (
      SELECT
        Date, Open, High, Low, Close, Volume, Amount, Change, Transactions,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW), 2) AS MA5,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 2) AS MA20,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW), 2) AS MA60
      FROM read_csv_auto('{csv_path}')
    )
    SELECT * FROM ma_calc
    ORDER BY Date DESC
    LIMIT 60;
    """
    ma_results = con.execute(ma_sql).fetchall()
    cols = [desc[0] for desc in con.description]
    chart_data = [dict(zip(cols, row)) for row in reversed(ma_results)]
    if not chart_data:
        raise RuntimeError(f"No chart data generated for {target.code}.")

    # Enrich chart_data with technical indicators (RSI, MACD, KD, ATR, Bollinger, Volume MA)
    enrich_chart_data(chart_data)

    mdd_sql = f"""
    WITH peak_calc AS (
      SELECT Date, Close,
        MAX(Close) OVER (ORDER BY Date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rolling_peak
      FROM read_csv_auto('{csv_path}')
    ),
    drawdown_calc AS (
      SELECT *, ROUND(((Close - rolling_peak) / rolling_peak) * 100, 2) AS drawdown
      FROM peak_calc
    )
    SELECT Date, Close, rolling_peak, drawdown,
      (SELECT MIN(drawdown) FROM drawdown_calc) AS max_history_drawdown
    FROM drawdown_calc
    ORDER BY Date DESC
    LIMIT 1;
    """
    mdd_row = con.execute(mdd_sql).fetchone()

    weekly_sql = f"""
    WITH ref_date AS (SELECT MAX(Date) AS max_d FROM read_csv_auto('{csv_path}'))
    SELECT MIN(Date), MAX(Date), ROUND(AVG(Close), 2), MAX(High), MIN(Low), SUM(Volume), SUM(Amount)
    FROM read_csv_auto('{csv_path}')
    WHERE Date >= (SELECT max_d FROM ref_date) - INTERVAL '7' DAY;
    """
    weekly_row = con.execute(weekly_sql).fetchone()
    con.close()

    weekly_summary = {
        "stock_code": target.code,
        "stock_name": stock_name,
        "market": target.market,
        "data_source": source or "existing-cache",
        "start_date": weekly_row[0],
        "end_date": weekly_row[1],
        "avg_close": float(weekly_row[2]) if weekly_row[2] is not None else 0.0,
        "highest_price": float(weekly_row[3]) if weekly_row[3] is not None else 0.0,
        "lowest_price": float(weekly_row[4]) if weekly_row[4] is not None else 0.0,
        "total_volume": int(weekly_row[5]) if weekly_row[5] is not None else 0,
        "total_amount": int(weekly_row[6]) if weekly_row[6] is not None else 0,
        "last_close": float(chart_data[-1]["Close"]),
        "weekly_return_pct": round(((chart_data[-1]["Close"] - chart_data[-5]["Close"]) / chart_data[-5]["Close"] * 100), 2)
        if len(chart_data) >= 5 and chart_data[-5]["Close"]
        else 0.0,
    }
    drawdown_info = {
        "as_of_date": mdd_row[0],
        "close": mdd_row[1],
        "historical_peak": mdd_row[2],
        "current_drawdown_pct": mdd_row[3],
        "max_historical_drawdown_pct": mdd_row[4],
    }
    
    # 判斷近期技術指標燈號
    latest_day = chart_data[-1]
    c_val = float(latest_day["Close"])
    ma5_val = float(latest_day["MA5"]) if latest_day.get("MA5") is not None else c_val
    ma20_val = float(latest_day["MA20"]) if latest_day.get("MA20") is not None else c_val
    ma60_val = float(latest_day["MA60"]) if latest_day.get("MA60") is not None else c_val

    # 判定規則：5MA 與 20MA 多空排列與收盤價關係
    if c_val >= ma5_val and ma5_val >= ma20_val:
        status = "看好"
        color = "red"  # 台股代表上漲
        emoji = "🔴"
        trend_desc = "多頭強勢排列 (Close >= 5MA >= 20MA)"
    elif c_val <= ma5_val and ma5_val <= ma20_val:
        status = "看差"
        color = "green"  # 台股代表下跌
        emoji = "🟢"
        trend_desc = "空頭整理排列 (Close <= 5MA <= 20MA)"
    else:
        status = "觀望"
        color = "yellow"
        emoji = "🟡"
        trend_desc = "區間糾纏震盪 (5MA 與 20MA 糾結)"

    signal_info = {
        "status": status,
        "color": color,
        "emoji": emoji,
        "desc": f"{trend_desc}。目前價格 TWD {c_val}，5MA({ma5_val})、20MA({ma20_val})。"
    }

    # Extended metrics (monthly/quarterly return, volatility, 52-week, VWAP, etc.)
    sorted_rows = [existing_data[d] for d in sorted_dates]
    extended_metrics = compute_extended_metrics(sorted_rows, chart_data)

    analysis_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": weekly_summary,
        "drawdown": drawdown_info,
        "signal": signal_info,
        "extended_metrics": extended_metrics,
        "chart_data": chart_data,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(make_serializable(analysis_payload), f, ensure_ascii=False, indent=2)
    print(f"Generated {json_path} ✅")
    return analysis_payload


def write_watchlist_json(targets):
    payload = [target.__dict__ for target in targets]
    with open(WATCHLIST_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Generated {WATCHLIST_JSON_PATH} ✅")


def resolve_targets(args):
    if args.stock_code:
        return [StockTarget(code=args.stock_code, market=args.market, name=args.name or "")]
    control_file = args.config or DEFAULT_CONTROL_FILE
    targets = parse_watchlist_markdown(control_file)
    if targets:
        return targets
    return [StockTarget(code="2330", name="台積電", market="twse")]


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Update DuckDB stock analysis JSON files from Markdown watchlist or one stock code.")
    parser.add_argument("stock_code", nargs="?", help="Optional single stock code, e.g. 2330 or 4979")
    parser.add_argument("--config", default=None, help=f"Markdown watchlist path (default: {DEFAULT_CONTROL_FILE})")
    parser.add_argument("--market", choices=["auto", "twse", "tpex", "yahoo"], default="auto", help="Market/source hint for single stock mode")
    parser.add_argument("--name", default="", help="Display name for single stock mode")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    targets = resolve_targets(args)
    print(f"Resolved {len(targets)} target(s): {', '.join(t.code for t in targets)}")
    write_watchlist_json(targets)
    failures = []
    for target in targets:
        try:
            analyze_stock(target)
        except Exception as e:
            print(f"ERROR: {target.code} failed: {e}")
            failures.append((target.code, str(e)))
    if failures:
        raise SystemExit(f"Failed targets: {failures}")


if __name__ == "__main__":
    main()
