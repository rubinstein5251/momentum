from pathlib import Path


def test_directional_entry_does_not_apply_market_filters():
    source = Path("strategies/inventory_accumulation_strategy.py").read_text(encoding="utf-8")
    assert "evaluate_market_stability" not in source
    assert "check_executable_depth" not in source
    assert "worker.executable_buy(" not in source
    assert "choice.best_ask, min_res.minimum_price" in source


def test_dashboard_does_not_render_removed_filter_metrics():
    source = Path("main.py").read_text(encoding="utf-8")
    assert "Stability:" not in source
    assert "Liquidity:" not in source
