"""Price recorded requests at frozen official rates, without extra model calls."""

import json
from decimal import Decimal

from comparison_metrics import normalized_usage

FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


def request_price(usage, rates):
    ordinary = (
        usage["input_tokens"]
        - usage["cached_input_tokens"]
        - usage["cache_write_input_tokens"]
    )
    if ordinary < 0:
        return None
    return (
        Decimal(ordinary) * Decimal(str(rates["input"]))
        + Decimal(usage["cached_input_tokens"]) * Decimal(str(rates["cached_input"]))
        + Decimal(usage["cache_write_input_tokens"])
        * Decimal(str(rates["cache_write"]))
        + Decimal(usage["output_tokens"]) * Decimal(str(rates["output"]))
    ) / Decimal(1_000_000)


def recorded_cost(home, expected, pricing, model):
    """Require complete per-request usage and reconcile it with independent task counters."""
    previous = {}
    total = dict.fromkeys(FIELDS, 0)
    cost = Decimal(0)
    requests = long_requests = unknown_requests = 0
    for path in sorted((home / "sessions").glob("**/*.jsonl")):
        thread = str(path)
        for line in path.open():
            try:
                row = json.loads(line)
            except ValueError:
                unknown_requests += 1
                continue
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                thread = payload.get("id", thread)
            if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            current = normalized_usage(info.get("total_token_usage"))
            last_raw = info.get("last_token_usage")
            last = normalized_usage(last_raw)
            if current is None or last is None:
                continue  # Rate-limit-only notifications are not requests.
            prior = previous.get(thread, dict.fromkeys(FIELDS, 0))
            if current == prior:
                continue  # Repeated notification, not another billed request.
            previous[thread] = current
            delta = {k: current[k] - prior[k] for k in FIELDS}
            if delta != last or any(v < 0 for v in delta.values()):
                unknown_requests += 1
                continue
            for key in FIELDS:
                total[key] += last[key]
            requests += 1
            if "cache_write_input_tokens" not in last_raw and pricing.get(
                "cache_write_usage_required", True
            ):
                unknown_requests += 1
                continue
            threshold = pricing.get("long_context_threshold")
            long = threshold is not None and last["input_tokens"] > threshold
            long_requests += long
            rates = (
                pricing["long_context_models"][model]
                if long
                else pricing["models"][model]
            )
            amount = request_price(last, rates)
            if amount is None:
                unknown_requests += 1
            else:
                cost += amount
    reconciled = expected is not None and all(
        total[k] == expected.get(k, 0) for k in FIELDS
    )
    complete = requests > 0 and reconciled and unknown_requests == 0
    amount = float(cost.quantize(Decimal("0.00000001")))
    currency = pricing.get("currency", "USD")
    return {
        "cost_currency": currency,
        "cost_estimate": amount if complete else None,
        "usd_estimate": amount if complete and currency == "USD" else None,
        "cny_estimate": amount if complete and currency == "CNY" else None,
        "known_request_cost": amount,
        "known_request_usd": amount if currency == "USD" else None,
        "cost_status": "estimated" if complete else "unknown",
        "requests": requests,
        "long_context_requests": long_requests,
        "unknown_request_usage": unknown_requests,
        "usage_reconciled": reconciled,
        "observed_usage": total,
        "expected_usage": expected,
        "basis": pricing.get(
            "label",
            "Official Standard API prices per recorded request; cache writes replace ordinary input, not an additive charge",
        ),
        "source": pricing["source"],
        "date": pricing["date"],
    }
