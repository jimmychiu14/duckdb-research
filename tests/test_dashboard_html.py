from pathlib import Path

HTML_PATH = Path('/home/jimmyc/.hermes/workspace/duckdb-research/index.html')


def test_dashboard_dynamic_sections_have_ids_and_no_tsmc_specific_copy():
    html = HTML_PATH.read_text(encoding='utf-8')

    assert 'id="chart-title"' in html
    assert 'id="assistant-comment"' in html
    assert 'id="swot-strengths"' in html
    assert 'id="swot-weaknesses"' in html
    assert 'id="swot-opportunities"' in html
    assert 'id="swot-threats"' in html

    for hardcoded in ['3 奈米', 'CoWoS', 'COUPE', '台積電股價走勢']:
        assert hardcoded not in html


def test_dashboard_javascript_updates_dynamic_analysis_copy():
    html = HTML_PATH.read_text(encoding='utf-8')

    assert 'renderDynamicNarrative(data)' in html
    assert 'buildGenericSwot' in html
    assert 'chartTitleEl.innerText' in html
