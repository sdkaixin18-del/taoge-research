"""Offline reconstruction of one-price limit-down sequences; never places orders.

Input: the archived BaoStock responses and frozen protocol in the study directory.
All fills below are next-bar-open price scenarios, not verified executions.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
from statistics import mean, median


TICK = Decimal("0.01")


def price(value):
    return Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP)


def limits(row):
    base = Decimal(str(row["preclose"]))
    return tuple((base * x).quantize(TICK, rounding=ROUND_HALF_UP)
                 for x in (Decimal("0.9"), Decimal("1.1")))


def regular(row):
    return row.get("isST") == "0" and row.get("tradestatus") == "1"


def one_word(row):
    if not regular(row) or Decimal(row["volume"] or "0") <= 0:
        return False
    floor, _ = limits(row)
    return all(price(row[k]) == floor for k in ("open", "high", "low", "close"))


def prior_up_run(rows, first, window=15):
    """Uses only days strictly before the first one-price down day."""
    longest = streak = 0
    for row in rows[max(0, first - window):first]:
        if regular(row) and price(row["close"]) == limits(row)[1]:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return longest


def minute_end(bar):
    return datetime.strptime(bar["time"][:14], "%Y%m%d%H%M%S")


def hhmm(bar):
    return minute_end(bar).strftime("%H:%M")


def bar_start(bar):
    return (minute_end(bar) - timedelta(minutes=5)).strftime("%H:%M")


def expected_ends(day):
    start = datetime.fromisoformat(day)
    return [(start + timedelta(hours=h, minutes=m + i * 5)).strftime("%H:%M")
            for h, m in ((9, 35), (13, 5)) for i in range(24)]


def quality(day, bars):
    issues = []
    if [hhmm(b) for b in bars] != expected_ends(day["date"]):
        issues.append("minute_coverage")
    if not bars:
        return issues + ["no_minutes"]
    values = {"open": bars[0]["open"], "close": bars[-1]["close"],
              "high": max(float(b["high"]) for b in bars),
              "low": min(float(b["low"]) for b in bars)}
    for key, value in values.items():
        if abs(float(value) - float(day[key])) > .011:
            issues.append("daily_minute_" + key)
    for key in ("volume", "amount"):
        total = sum(float(b[key]) for b in bars)
        if abs(total - float(day[key])) > max(1., abs(float(day[key])) * .0001):
            issues.append("daily_minute_" + key)
    return issues


def entry_signal(bars, floor, branch):
    """Return first completed-bar confirmation; never examine a future bar."""
    for i, b in enumerate(bars):
        if hhmm(b) > "14:30":
            break
        if price(b["close"]) <= floor:
            continue
        if branch == "A":
            return i
        if i:
            prev = bars[i - 1]
            # The lunch break is not two adjacent five-minute observations.
            contiguous = minute_end(b) - minute_end(prev) == timedelta(minutes=5)
            if (contiguous and price(prev["close"]) > floor
                    and price(b["low"]) >= price(prev["low"])
                    and price(b["close"]) > price(prev["close"])):
                return i
    return None


def execution_issue(bar, day, buy=False):
    floor, ceiling = limits(day)
    opening = price(bar["open"])
    if opening <= floor:
        return "at_down_limit"
    if buy and opening >= ceiling:
        return "at_up_limit"
    if price(bar["high"]) == price(bar["low"]):
        return "single_price_bar"
    if float(bar["volume"]) <= 0 or float(bar["amount"]) <= 0:
        return "no_turnover"
    if 10000 > float(bar["amount"]) * .01:
        return "capacity_unconfirmed"
    return None


def target_exit(bars, branch):
    if branch == "X1":
        return 0
    if branch == "X2":
        return next((i for i, b in enumerate(bars) if bar_start(b) == "09:45"), None)
    if not bars:
        return None
    if price(bars[0]["close"]) <= price(bars[0]["open"]):
        return 1
    for i in range(1, len(bars)):
        if hhmm(bars[i]) >= "10:00" or price(bars[i]["close"]) < price(bars[i - 1]["low"]):
            return i + 1 if i + 1 < len(bars) else None
    return None


def exit_scenario(bars, day, branch, entry):
    target = target_exit(bars, branch)
    if target is None:
        return {"exit_status": "no_exit_target", "net20_pct": None}
    blocked = []
    for j in range(target, len(bars)):
        issue = execution_issue(bars[j], day)
        if issue:
            blocked.append(issue)
            continue
        gross = float(bars[j]["open"]) / entry - 1
        return {"exit_status": "price_scenario" if not blocked else "delayed_price_scenario",
                "exit_target_time": bar_start(bars[target]),
                "exit_time": bar_start(bars[j]), "exit_price": float(bars[j]["open"]),
                "exit_delay_bars": j - target, "blocked_reasons": sorted(set(blocked)),
                "gross_pct": gross * 100,
                **{f"net{cost}_pct": (gross - cost / 10000) * 100 for cost in (20, 50, 100)}}
    return {"exit_status": "next_day_exit_unconfirmed", "net20_pct": None,
            "blocked_reasons": sorted(set(blocked)),
            "mark_to_close_pct": (float(day["close"]) / entry - 1) * 100}


def write_csv(path, records):
    fields = list(dict.fromkeys(k for r in records for k in r))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                    for k, v in r.items()} for r in records)


def read_raw(root, symbol, interval):
    return json.loads((root / "原始行情" / f"{symbol}_{interval}.json").read_text())["rows"]


def run(root, out):
    protocol_bytes = (root / "研究协议_封存.json").read_bytes()
    protocol = json.loads(protocol_bytes)
    digest = hashlib.sha256(protocol_bytes).hexdigest()
    if digest != (root / "研究协议_SHA256.txt").read_text().strip():
        raise ValueError("Frozen protocol digest mismatch")
    out.mkdir(parents=True, exist_ok=True)
    days = {s: read_raw(root, s, "日线") for s in protocol["universe"]}
    minute_days = {}
    for s in list(protocol["universe"]) + ["sz159628"]:
        grouped = defaultdict(list)
        for row in read_raw(root, s, "5分钟"):
            grouped[row["date"]].append(row)
        minute_days[s] = grouped
    market_days = {r["date"]: r for r in read_raw(root, "sz159628", "日线")}
    observations, episodes, plans, audits = [], [], [], []
    for sym, rows in days.items():
        i = 0
        while i < len(rows):
            if not one_word(rows[i]):
                i += 1
                continue
            first = i
            while i < len(rows) and one_word(rows[i]):
                i += 1
            last = i - 1
            if not (protocol["evaluation_start"] <= rows[first]["date"] <= protocol["evaluation_end"]):
                continue
            opening = rows[i] if i < len(rows) else None
            max_up = prior_up_run(rows, first)
            eligible = max_up >= 3
            eid = sym + "_" + rows[first]["date"]
            base = {"episode_id": eid, "symbol": sym, "name": protocol["universe"][sym],
                    "eligible_price_proxy": eligible, "event_review": "unknown",
                    "prior_15d_max_up_run": max_up}
            for k in range(first, last + 1):
                d, following = rows[k], rows[k + 1] if k + 1 < len(rows) else None
                observations.append({**base, "asof": d["date"] + "T15:00:00+08:00",
                    "known_one_word_days": k - first + 1,
                    "seal_amount": None, "seal_change": None, "seal_prediction": "unavailable",
                    "asof_turnover": float(d["amount"]),
                    "next_date": following["date"] if following else None,
                    "next_day_traded_above_limit": price(following["high"]) > limits(following)[0]
                    if following and regular(following) else None})
            e = {**base, "first_one_word": rows[first]["date"],
                 "last_one_word": rows[last]["date"], "one_word_days": last - first + 1,
                 "opening_date": opening["date"] if opening else None,
                 "opening_type": ("auction_already_above_limit" if opening and
                    price(opening["open"]) > limits(opening)[0] else "intraday_opening") if opening else "censored"}
            if opening:
                e.update(day_open=float(opening["open"]), day_close=float(opening["close"]),
                         day_high=float(opening["high"]), day_low=float(opening["low"]),
                         day_limit=float(limits(opening)[0]),
                         open_to_close_pct=(float(opening["close"]) / float(opening["open"]) - 1) * 100)
                if i + 1 < len(rows):
                    next_row = rows[i + 1]
                    if abs(float(next_row["preclose"]) - float(opening["close"])) <= .011:
                        e.update(next_date=next_row["date"], next_open=float(next_row["open"]),
                                 next_open_vs_day_open_pct=(float(next_row["open"]) / float(opening["open"]) - 1) * 100)
            episodes.append(e)
            # Keep the ineligible named course example as an explicit control.
            if opening is None:
                continue
            bars = minute_days[sym][opening["date"]]
            next_day = rows[i + 1] if i + 1 < len(rows) else None
            following_bars = minute_days[sym][next_day["date"]] if next_day else []
            issues = quality(opening, bars)
            if not regular(opening):
                issues.append("opening_not_regular")
            if abs(float(opening["preclose"]) - float(rows[last]["close"])) > .011:
                issues.append("entry_corporate_action")
            if next_day:
                issues += ["next_" + x for x in quality(next_day, following_bars)]
                if not regular(next_day):
                    issues.append("exit_not_regular")
                if abs(float(next_day["preclose"]) - float(opening["close"])) > .011:
                    issues.append("exit_corporate_action")
            else:
                issues.append("no_next_day")
            audits.append({**base, "opening_date": opening["date"], "issues": issues,
                           "opening_bars": len(bars), "next_bars": len(following_bars)})
            floor, _ = limits(opening)
            e["minute_first_trade_above_limit_end"] = next((hhmm(b) for b in bars if price(b["high"]) > floor), None)
            for branch in ("A", "B"):
                signal = entry_signal(bars, floor, branch)
                plan = {**e, "entry_branch": branch, "quality_issues": issues}
                if signal is None or signal + 1 >= len(bars):
                    plans.append({**plan, "entry_status": "not_triggered"})
                    continue
                b = bars[signal + 1]
                plan.update(confirm_time=hhmm(bars[signal]), entry_time=bar_start(b),
                            entry_price=float(b["open"]))
                market = [m for m in minute_days["sz159628"][opening["date"]]
                          if minute_end(m) <= minute_end(bars[signal])]
                market_day = market_days.get(opening["date"])
                if market and market_day:
                    plan["market_at_confirm_pct"] = (float(market[-1]["close"]) / float(market_day["preclose"]) - 1) * 100
                else:
                    plan["market_at_confirm_pct"] = None
                issue = execution_issue(b, opening, buy=True)
                if issue:
                    plans.append({**plan, "entry_status": "execution_unconfirmed", "entry_issue": issue})
                    continue
                plan["entry_status"] = "price_scenario" if not issues else "data_disputed"
                plan["entry_day_close_pct"] = (float(opening["close"]) / float(b["open"]) - 1) * 100
                plan["entry_day_worst_from_bar_open_pct"] = (min(float(x["low"]) for x in bars[signal + 1:]) / float(b["open"]) - 1) * 100
                for exit_branch in ("X1", "X2", "X3"):
                    plans.append({**plan, "exit_branch": exit_branch,
                                  "exit_date": next_day["date"] if next_day else None,
                                  **(exit_scenario(following_bars, next_day, exit_branch, float(b["open"]))
                                     if next_day else {"exit_status": "no_next_day"})})
    summary = {"protocol_sha256": digest, "universe_count": len(days),
               "episode_count": len(episodes),
               "eligible_episodes": sum(e["eligible_price_proxy"] for e in episodes),
               "observation_days": len(observations),
               "eligible_observation_days": sum(o["eligible_price_proxy"] for o in observations),
               "seal_history_observations": 0, "all_event_reviews": "unknown", "groups": []}
    summary["unique_entry_status_counts"] = dict(Counter(
        p["entry_status"] for p in plans if p.get("exit_branch", "X1") == "X1"))
    for branch in ("A", "B"):
        for exit_branch in ("X1", "X2", "X3"):
            group = [p for p in plans if p.get("entry_branch") == branch and p.get("exit_branch") == exit_branch
                     and p["eligible_price_proxy"] and p["entry_status"] == "price_scenario"]
            usable = [p for p in group if p.get("net20_pct") is not None]
            summary["groups"].append({"entry": branch, "exit": exit_branch, "valid_entries": len(group),
                "next_day_exit_unconfirmed": sum(p["exit_status"] == "next_day_exit_unconfirmed" for p in group),
                "resolved_price_scenarios": len(usable),
                "positive_net20": sum(p["net20_pct"] > 0 for p in usable),
                **{f"mean_net{cost}_pct": mean(p[f"net{cost}_pct"] for p in usable) if usable else None for cost in (20, 50, 100)},
                "median_net20_pct": median(p["net20_pct"] for p in usable) if usable else None})
    summary["data_disputed_episodes"] = sum(bool(a["issues"]) for a in audits)
    for name, records in (("一字日观察", observations), ("完整序列", episodes), ("参与退出情景", plans), ("行情核查", audits)):
        write_csv(out / (name + ".csv"), records)
    payload = {"summary": summary,
               "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "raw_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted((root / "原始行情").glob("*.json"))},
               "observations": observations, "episodes": episodes,
               "plans": plans, "audits": audits}
    (out / "回放.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.study_dir, args.out)
