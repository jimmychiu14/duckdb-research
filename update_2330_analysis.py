import os
import json
import urllib.request
import csv
from datetime import datetime, timedelta, date
import duckdb

def make_serializable(obj):
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_serializable(i) for i in obj]
    elif isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj

# Define absolute paths
WORKSPACE_DIR = "/home/jimmyc/.hermes/workspace/duckdb-research"
DATA_DIR = os.path.join(WORKSPACE_DIR, "data")
CSV_PATH = os.path.join(DATA_DIR, "2330_daily_history.csv")
JSON_PATH = os.path.join(DATA_DIR, "2330_analysis.json")

os.makedirs(DATA_DIR, exist_ok=True)

# 1. Fetch data from TWSE STOCK_DAY API
def fetch_twse_data(year, month):
    date_str = f"{year}{month:02d}01"
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo=2330"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get('stat') == 'OK':
                return data.get('data', [])
            else:
                print(f"TWSE returned non-OK status for {year}-{month:02d}: {data.get('stat')}")
    except Exception as e:
        print(f"Error fetching data for {year}-{month:02d}: {e}")
    return []

def clean_twse_row(row):
    # row format: ["日期","成交股數","成交金額","開盤價","最高價","最低價","收盤價","漲跌價差","成交筆數"]
    # raw_date represents Minguo date like "115/06/22" -> "2026-06-22"
    raw_date = row[0]
    parts = raw_date.split('/')
    if len(parts) == 3:
        try:
            year = int(parts[0]) + 1911
            date_str = f"{year}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            return None
    else:
        date_str = raw_date

    def clean_num(val):
        if not val:
            return 0.0
        val_clean = val.replace(',', '').strip()
        val_clean = val_clean.replace('+', '').replace('－', '-').replace('＋', '')
        if not val_clean or val_clean == '--' or val_clean == 'X' or val_clean == 'null':
            return 0.0
        try:
            return float(val_clean)
        except ValueError:
            return 0.0

    return {
        "Date": date_str,
        "Volume": int(row[1].replace(',', '')),
        "Amount": int(row[2].replace(',', '')),
        "Open": clean_num(row[3]),
        "High": clean_num(row[4]),
        "Low": clean_num(row[5]),
        "Close": clean_num(row[6]),
        "Change": clean_num(row[7]),
        "Transactions": int(row[8].replace(',', ''))
    }

def main():
    print("=== Starting 2330 DuckDB Analysis Pipeline ===")
    
    # Load existing CSV data
    existing_data = {}
    if os.path.exists(CSV_PATH):
        print(f"Loading existing historical data from {CSV_PATH}...")
        with open(CSV_PATH, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    existing_data[row['Date']] = {
                        "Date": row['Date'],
                        "Volume": int(row['Volume']),
                        "Amount": int(row['Amount']),
                        "Open": float(row['Open']),
                        "High": float(row['High']),
                        "Low": float(row['Low']),
                        "Close": float(row['Close']),
                        "Change": float(row['Change']),
                        "Transactions": int(row['Transactions'])
                    }
                except Exception as e:
                    print(f"Warning: Failed to parse row {row}: {e}")

    # Determine which months to fetch
    current_date = datetime.now()
    months_to_fetch = []
    
    # If history is empty or too short, bootstrap last 5 months of data to ensure we have enough data for 60MA
    if len(existing_data) < 80:
        print("Historical dataset is small. Bootstrapping last 5 months of 2330 stock price data...")
        for i in range(5):
            m = current_date.month - i
            y = current_date.year
            while m <= 0:
                m += 12
                y -= 1
            months_to_fetch.append((y, m))
    else:
        # Fetch current month and previous month to capture latest updates
        months_to_fetch = [(current_date.year, current_date.month)]
        m_prev = current_date.month - 1
        y_prev = current_date.year
        if m_prev == 0:
            m_prev = 12
            y_prev -= 1
        months_to_fetch.append((y_prev, m_prev))

    # Fetch and merge
    new_rows_count = 0
    # Fetch oldest first to be logical
    for y, m in reversed(months_to_fetch):
        print(f"Fetching 2330 stock data from TWSE for {y}-{m:02d}...")
        rows = fetch_twse_data(y, m)
        for r in rows:
            cleaned = clean_twse_row(r)
            if cleaned:
                date_key = cleaned['Date']
                if date_key not in existing_data:
                    new_rows_count += 1
                existing_data[date_key] = cleaned

    # Save merged data sorted by Date
    sorted_dates = sorted(existing_data.keys())
    with open(CSV_PATH, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Volume", "Amount", "Open", "High", "Low", "Close", "Change", "Transactions"])
        writer.writeheader()
        for d in sorted_dates:
            writer.writerow(existing_data[d])
            
    print(f"Saved {len(sorted_dates)} rows to {CSV_PATH} ({new_rows_count} new rows added).")

    if len(sorted_dates) == 0:
        print("Error: No data available for analysis!")
        return

    # 2. Run analysis using DuckDB
    print("\nRunning SQL analysis using DuckDB...")
    con = duckdb.connect()

    # Query 1: Calculate moving averages (MA5, MA20, MA60)
    print("Calculating technical indicators (moving averages)...")
    ma_sql = f"""
    WITH ma_calc AS (
      SELECT 
        Date, Open, High, Low, Close, Volume, Amount, Change, Transactions,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW), 2) AS MA5,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 2) AS MA20,
        ROUND(AVG(Close) OVER (ORDER BY Date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW), 2) AS MA60
      FROM read_csv_auto('{CSV_PATH}')
    )
    SELECT * FROM ma_calc
    ORDER BY Date DESC
    LIMIT 60;
    """
    ma_results = con.execute(ma_sql).fetchall()
    cols = [desc[0] for desc in con.description]
    
    # Format MA results to list of dicts (oldest first for chart)
    chart_data = []
    for row in reversed(ma_results):
        chart_data.append(dict(zip(cols, row)))

    # Query 2: Calculate Drawdown and Max Drawdown (MDD)
    print("Calculating maximum drawdowns (MDD)...")
    mdd_sql = f"""
    WITH peak_calc AS (
      SELECT 
        Date, Close,
        MAX(Close) OVER (ORDER BY Date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS rolling_peak
      FROM read_csv_auto('{CSV_PATH}')
    ),
    drawdown_calc AS (
      SELECT *,
        ROUND(((Close - rolling_peak) / rolling_peak) * 100, 2) AS drawdown
      FROM peak_calc
    )
    SELECT 
      Date, Close, rolling_peak, drawdown,
      (SELECT MIN(drawdown) FROM drawdown_calc) AS max_history_drawdown
    FROM drawdown_calc
    ORDER BY Date DESC
    LIMIT 1;
    """
    mdd_row = con.execute(mdd_sql).fetchone()
    if mdd_row is None:
        print("Error: DuckDB MDD query returned no rows.")
        return
        
    drawdown_info = {
        "as_of_date": mdd_row[0],
        "close": mdd_row[1],
        "historical_peak": mdd_row[2],
        "current_drawdown_pct": mdd_row[3],
        "max_historical_drawdown_pct": mdd_row[4]
    }

    # Query 3: Weekly Summary
    print("Generating weekly aggregate metrics...")
    weekly_sql = f"""
    WITH ref_date AS (
      SELECT MAX(Date) AS max_d FROM read_csv_auto('{CSV_PATH}')
    )
    SELECT 
      MIN(Date) AS start_date,
      MAX(Date) AS end_date,
      ROUND(AVG(Close), 2) AS avg_close,
      MAX(High) AS highest_price,
      MIN(Low) AS lowest_price,
      SUM(Volume) AS total_volume,
      SUM(Amount) AS total_amount
    FROM read_csv_auto('{CSV_PATH}')
    WHERE Date >= (SELECT max_d FROM ref_date) - INTERVAL '7' DAY;
    """
    weekly_row = con.execute(weekly_sql).fetchone()
    if weekly_row is None:
        print("Error: DuckDB weekly summary query returned no rows.")
        return
        
    weekly_summary = {
        "start_date": weekly_row[0],
        "end_date": weekly_row[1],
        "avg_close": float(weekly_row[2]) if weekly_row[2] is not None else 0.0,
        "highest_price": float(weekly_row[3]) if weekly_row[3] is not None else 0.0,
        "lowest_price": float(weekly_row[4]) if weekly_row[4] is not None else 0.0,
        "total_volume": int(weekly_row[5]) if weekly_row[5] is not None else 0,
        "total_amount": int(weekly_row[6]) if weekly_row[6] is not None else 0,
        "last_close": float(chart_data[-1]["Close"]),
        "weekly_return_pct": round(((chart_data[-1]["Close"] - chart_data[-5]["Close"]) / chart_data[-5]["Close"] * 100), 2) if len(chart_data) >= 5 else 0.0
    }

    # Combine everything into clean JSON structure
    analysis_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": weekly_summary,
        "drawdown": drawdown_info,
        "chart_data": chart_data
    }

    # Write out the clean JSON
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(make_serializable(analysis_payload), f, ensure_ascii=False, indent=2)
    print(f"Successfully generated clean JSON analysis at {JSON_PATH} ✅")
    print("=== Pipeline Complete ===")

if __name__ == "__main__":
    main()
