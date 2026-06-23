# 股票監控控制檔

這個檔案是 DuckDB 股票分析儀表板的控制檔。夏會讀取下方清單，自動更新每一檔股票的 CSV 與 JSON。

## 🎯 當前追蹤標的

- 2330 台積電 @twse
- 4979 華星光 @tpex
- 6770 力積電 @twse
- 0050 元大台灣50 @twse
- 2317 鴻海 @twse
- 2308 台達電 @twse

## 📝 使用方式

- 上市股票可標記 `@twse`，例如 `- 2454 聯發科 @twse`
- 上櫃股票可標記 `@tpex`，例如 `- 4979 華星光 @tpex`
- 不確定市場別可省略標記，系統會用 `@auto` 自動嘗試
- 網頁網址可使用 `?stock=代號` 切換，例如：
  - `https://jimmychiu14.github.io/duckdb-research/?stock=2330`
  - `https://jimmychiu14.github.io/duckdb-research/?stock=4979`
