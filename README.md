# DuckDB Research — 2330 台積電本週收盤報告

這個 repo 是 Hermes / DuckDB 練習專案，展示如何：

1. 從 TWSE 取得台股 2330 台積電本週每日收盤資料
2. 儲存為 CSV
3. 匯入 DuckDB 查詢
4. 產生靜態 HTML 網頁報告
5. 部署到 GitHub Pages

## GitHub Pages

首頁：`index.html`

## 主要檔案

- `index.html`：GitHub Pages 首頁，內容等同 2330 報告
- `2330_weekly_report.html`：原始 HTML 報告
- `data/2330_台積電_本週收盤資料_2026-05-04_2026-05-06.csv`：本週 CSV 資料
- `sample_portfolio.csv`：DuckDB 練習用投資組合範例

## DuckDB 查詢範例

```sql
SELECT *
FROM tw_stock_2330_weekly_close
ORDER BY date;
```

```sql
SELECT
  symbol,
  name,
  ROUND(AVG(close), 2) AS avg_close
FROM tw_stock_2330_weekly_close
GROUP BY symbol, name;
```

> 注意：本 repo 不提交本機 `.duckdb` database 檔，避免把分析快取/二進位資料放進 Pages repo。
