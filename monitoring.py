DEFAULT_PRICE_DROP_THRESHOLD_PCT = 10.0
PRICE_DROP_THRESHOLD_OPTIONS = [5, 8, 10, 15, 20]


def normalize_price_drop_threshold(value, default: float = DEFAULT_PRICE_DROP_THRESHOLD_PCT) -> float:
    try:
        threshold = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default
    return min(50.0, max(1.0, threshold))


def price_drop_alert_decision(
    last_price: float | int | None,
    current_price: float | int | None,
    threshold_pct: float | int | None,
    partial_data: bool = False,
) -> dict:
    last = float(last_price or 0)
    current = float(current_price or 0)
    threshold = normalize_price_drop_threshold(threshold_pct)

    if partial_data:
        return {
            "should_alert": False,
            "should_update_baseline": False,
            "reason": "partial_data",
            "drop_pct": 0.0,
            "threshold_pct": threshold,
        }

    if current <= 0:
        return {
            "should_alert": False,
            "should_update_baseline": False,
            "reason": "no_price",
            "drop_pct": 0.0,
            "threshold_pct": threshold,
        }

    if last <= 0:
        return {
            "should_alert": False,
            "should_update_baseline": True,
            "reason": "first_baseline",
            "drop_pct": 0.0,
            "threshold_pct": threshold,
        }

    # Price went up (or stayed equal-but-higher): track the new high so a future
    # drop is measured from a real peak, not from a stale low.
    if current > last:
        return {
            "should_alert": False,
            "should_update_baseline": True,
            "reason": "price_up",
            "drop_pct": 0.0,
            "threshold_pct": threshold,
        }

    drop_pct = ((last - current) / last) * 100.0
    alert = drop_pct >= threshold

    # IMPORTANT: do NOT ratchet the baseline down on sub-threshold drops.
    # If we lowered the baseline every day, a slow decline (e.g. 7%+7%) would
    # never cross the threshold. Keep the old baseline until a real alert fires,
    # so cumulative drops are measured against the last alerted/peak price.
    return {
        "should_alert": alert,
        "should_update_baseline": alert,
        "reason": "drop" if alert else "below_threshold",
        "drop_pct": max(0.0, drop_pct),
        "threshold_pct": threshold,
    }
