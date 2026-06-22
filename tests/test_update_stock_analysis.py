import importlib.util
from pathlib import Path

MODULE_PATH = Path('/home/jimmyc/.hermes/workspace/duckdb-research/update_stock_analysis.py')
spec = importlib.util.spec_from_file_location('update_stock_analysis', MODULE_PATH)
usa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usa)


def test_parse_watchlist_markdown_reads_stock_items(tmp_path):
    control = tmp_path / 'stock_watchlist.md'
    control.write_text(
        '''# 股票監控控制檔

## 🎯 當前追蹤標的

- 2330 台積電 @twse
- 4979 華星光 @tpex
- 2454 聯發科

## 其他備註
- 這行不是股票
''',
        encoding='utf-8',
    )

    targets = usa.parse_watchlist_markdown(str(control))

    assert [t.code for t in targets] == ['2330', '4979', '2454']
    assert [t.name for t in targets] == ['台積電', '華星光', '聯發科']
    assert [t.market for t in targets] == ['twse', 'tpex', 'auto']


def test_yahoo_chart_payload_converts_to_standard_rows():
    payload = {
        'chart': {
            'result': [
                {
                    'timestamp': [1719792000, 1719878400],
                    'indicators': {
                        'quote': [
                            {
                                'open': [10.0, 11.0],
                                'high': [12.0, 13.0],
                                'low': [9.0, 10.0],
                                'close': [11.0, 12.5],
                                'volume': [1000, 2000],
                            }
                        ]
                    },
                }
            ]
        }
    }

    rows = usa.yahoo_chart_to_standard_rows(payload)

    assert rows == [
        {
            'Date': '2024-07-01',
            'Volume': 1000,
            'Amount': 11000,
            'Open': 10.0,
            'High': 12.0,
            'Low': 9.0,
            'Close': 11.0,
            'Change': 0.0,
            'Transactions': 0,
        },
        {
            'Date': '2024-07-02',
            'Volume': 2000,
            'Amount': 25000,
            'Open': 11.0,
            'High': 13.0,
            'Low': 10.0,
            'Close': 12.5,
            'Change': 1.5,
            'Transactions': 0,
        },
    ]
