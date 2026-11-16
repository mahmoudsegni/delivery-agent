"""Pure Monte Carlo tests for the sprint forecast — no DB, no network
(test_github_provider philosophy): fabricated throughput goes straight into
the simulation helpers; ``forecast()`` is thin glue over them."""

from statistics import mean

from app.services.forecast_service import _forecast_payload, _simulate, _summarize


def test_constant_throughput_exact_p50_and_on_time():
    # Every run needs exactly 2 weeks of 10 → all days are 14.
    s = _summarize(_simulate([10, 10, 10], 20), 14)
    assert s["p50_days"] == 14 and s["p85_days"] == 14
    assert s["on_time_probability"] == 1.0


def test_on_time_zero_when_budget_too_short():
    assert _summarize(_simulate([10, 10, 10], 20), 7)["on_time_probability"] == 0.0


def test_variable_throughput_spreads():
    # Half the weeks deliver nothing → median run needs 2+ non-zero weeks.
    days = _simulate([10, 0, 10, 0, 10, 0], 15)
    s = _summarize(days, 21)
    assert 21 <= mean(days) <= 35
    assert s["p85_days"] >= s["p50_days"] >= 14  # can't finish in under 2 weeks


def test_same_seed_same_numbers():
    kw = dict(remaining=13, iterations=500, seed=7)
    assert _summarize(_simulate([3, 5, 8], **kw), 21) == _summarize(
        _simulate([3, 5, 8], **kw), 21
    )


def test_guards():
    assert _forecast_payload([10, 10], 0, "pts", 14, 100, 42) == {"done": True}
    out = _forecast_payload([0, 0, 0, 0, 0, 1], 15, "items", 14, 100, 42)
    assert out["sufficient_history"] is False
    assert out["weekly_throughput"] == [0, 0, 0, 0, 0, 1]
