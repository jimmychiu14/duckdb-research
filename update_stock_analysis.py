import argparse
import csv
import json
import os
import re
import sys
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
    for i in range(months_to_fetch):
        fetch_date = current_date - timedelta(days=30 * i)
        year, month = fetch_date.year, fetch_date.month
        if market in {"twse", "auto"}:
            month_rows, month_title, month_source = fetch_twse_data(year, month, stock_code)
            if month_rows:
                rows.extend(month_rows)
                title = title or month_title
                source = month_source
                continue
        if market in {"tpex", "auto"}:
            month_rows, month_title, month_source = fetch_tpex_data(year, month, stock_code)
            if month_rows:
                rows.extend(month_rows)
                title = title or month_title
                source = month_source
    return rows, title, source


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

    analysis_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": weekly_summary,
        "drawdown": drawdown_info,
        "signal": signal_info,
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
