"""Official pricing boundaries, tested entirely from synthetic token receipts."""

import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "scripts/native_experiment")
)
from official_pricing import recorded_cost, request_price

RATES = {"input": 2, "cached_input": 0.2, "cache_write": 2.5, "output": 10}


def test_cache_writes_replace_input():
    u = {
        "input_tokens": 12000,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 12000,
        "output_tokens": 1000,
    }
    assert float(request_price(u, RATES)) == 0.04


def test_negative_ordinary_tokens_do_not_get_priced():
    u = {
        "input_tokens": 10,
        "cached_input_tokens": 8,
        "cache_write_input_tokens": 5,
        "output_tokens": 1,
    }
    assert request_price(u, RATES) is None


def test_per_request_tier_and_duplicate_notifications(tmp_path):
    session = tmp_path / "sessions/test.jsonl"
    session.parent.mkdir()
    short = {
        "input_tokens": 272000,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 100,
    }
    long = {
        "input_tokens": 272001,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 100,
    }
    total = {k: short[k] + long[k] for k in short}
    rows = [{"type": "session_meta", "payload": {"id": "test"}}]
    for cumulative, last in [(short, short), (short, short), (total, long)]:
        rows.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": cumulative, "last_token_usage": last},
                },
            }
        )
    session.write_text("".join(json.dumps(r) + "\n" for r in rows))
    pricing = {
        "source": "fixture",
        "date": "fixture",
        "long_context_threshold": 272000,
        "models": {"test": RATES},
        "long_context_models": {"test": dict(RATES, input=4, output=15)},
    }
    result = recorded_cost(tmp_path, total, pricing, "test")
    assert result["requests"] == 2
    assert result["long_context_requests"] == 1
    assert result["usd_estimate"] == 1.634504
    assert result["cost_status"] == "estimated"
    bad = dict(total, input_tokens=total["input_tokens"] + 1)
    assert recorded_cost(tmp_path, bad, pricing, "test")["usd_estimate"] is None


def test_deepseek_cache_misses_and_cny_are_not_usd(tmp_path):
    usage = {
        "input_tokens": 1000000,
        "cached_input_tokens": 500000,
        "output_tokens": 100000,
    }
    session = tmp_path / "sessions/request.jsonl"
    session.parent.mkdir()
    session.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": usage, "last_token_usage": usage},
                },
            }
        )
        + "\n"
    )
    pricing = {
        "currency": "CNY",
        "cache_write_usage_required": False,
        "source": "fixture",
        "date": "fixture",
        "models": {
            "flash": {"input": 1, "cached_input": 0.02, "cache_write": 1, "output": 4}
        },
    }
    result = recorded_cost(tmp_path, usage, pricing, "flash")
    assert result["cost_status"] == "estimated"
    assert result["cost_currency"] == "CNY"
    assert result["cost_estimate"] == result["cny_estimate"] == 0.91
    assert result["usd_estimate"] is None
    assert result["known_request_usd"] is None
    assert result["long_context_requests"] == 0
