"""Audit archived iFinD responses without changing the frozen trading replay.

Daily bars, rounded media seal reports and actual queue observations remain
different evidence types. This script never creates fills or strategy returns.
Only the Python standard library is required; no credentials or network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from limitdown_research import one_word, read_raw, write_csv

FIELDS = {
    "证券代码": "code",
    "股票代码": "code",
    "证券简称": "name",
    "股票简称": "name",
    "日期": "date",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "收盘价": "close",
    "成交量": "volume",
}
BAR_FIELDS = ("open", "high", "low", "close", "volume")


def number(value):
    """Keep units explicit: only plain numbers and 万/亿 multipliers are accepted."""
    value = str(value).strip().replace(",", "")
    if not value or value in {"--", "—", "None", "null"}:
        return None
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(万|亿)?", value
    )
    if not match:
        raise ValueError("Unsupported numeric value or unit")
    result = Decimal(match[1]) * {None: 1, "万": 10000, "亿": 100000000}[match[2]]
    if not result.is_finite():
        raise ValueError("Non-finite number")
    return result


def normalized_date(value):
    value = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError("Missing or invalid historical date")


def tables(text):
    """Return tables with original headers: dates on individual columns matter."""
    headers, rows = [], []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r"[-:]+", cell or "x") for cell in cells):
            continue
        if any(cell in {"证券代码", "股票代码"} for cell in cells):
            if headers:
                yield headers, rows
            headers, rows = cells, []
        elif headers:
            if len(cells) != len(headers):
                raise ValueError("Ragged source table")
            rows.append(dict(zip(headers, cells)))
    if headers:
        yield headers, rows


def column_dates(headers):
    return {
        header: match.group(1)
        for header in headers
        if (match := re.search(r"\[(\d{8})\]", header))
    }


def bar_row(row, trading_dates, volume_unit):
    mapped = {}
    for header, value in row.items():
        stem = re.split(r"[（(:：\[]", header)[0]
        if stem in FIELDS:
            mapped[FIELDS[stem]] = value
    if "date" not in mapped:
        return None, "missing_row_date"
    date = normalized_date(mapped["date"])
    if date not in trading_dates:
        return None, "not_a_trading_day"
    if any(normalized_date(d) != date for d in column_dates(row).values()):
        return None, "column_date_disagrees"
    if volume_unit.strip() != "股":
        return None, "volume_unit_unverified"
    if any(
        field not in mapped or number(mapped[field]) is None for field in BAR_FIELDS
    ):
        return None, "incomplete_bar"
    result = {field: number(mapped[field]) for field in BAR_FIELDS}
    if result["volume"] <= 0:
        return None, "no_traded_volume"
    if not (
        0
        < result["low"]
        <= min(result["open"], result["close"])
        <= max(result["open"], result["close"])
        <= result["high"]
    ):
        return None, "invalid_ohlc"
    result.update(code=mapped["code"], name=mapped.get("name"), date=date)
    return result, "complete_trading_bar"


def add_bar(index, bar):
    key = bar["code"], bar["date"]
    if key in index and any(index[key][field] != bar[field] for field in BAR_FIELDS):
        raise ValueError("Conflicting duplicate daily bars")
    index[key] = bar


def envelope(path):
    response = json.loads(path.read_text())
    result = response["result"]
    if result.get("isError"):
        raise ValueError("MCP tool failure")
    values = [
        json.loads(item["text"]) for item in result["content"] if item["type"] == "text"
    ]
    if len(values) != 1 or values[0].get("code") != 1:
        raise ValueError("Unsuccessful data envelope")
    return values[0]


def seal_columns(headers):
    # Generic turnover must never satisfy a remaining sell-inventory request.
    share_names = {"收盘跌停价卖一剩余未成交封单股数", "跌停封单量", "卖一封单股数"}
    amount_names = {"对应卖一封单金额", "跌停封单金额", "卖一封单金额"}
    stems = {re.split(r"[（(\[]", header)[0] for header in headers}
    return {
        "sell_queue_shares_present": bool(stems & share_names),
        "sell_queue_amount_present": bool(stems & amount_names),
    }


def inventory_change(previous_amount, previous_price, amount, price):
    """Descriptive media-derived quantity proxy, never raw Q or a prediction."""
    old_shares, shares = previous_amount / previous_price, amount / price
    return {
        "amount_change_pct": float((amount / previous_amount - 1) * 100),
        "implied_shares_change_pct": float((shares / old_shares - 1) * 100),
        "previous_implied_shares": float(old_shares),
        "implied_shares": float(shares),
        "qualified_Q_shares": None,
        "prediction_generated": False,
    }


def run(study, research, out):
    frozen = (study / "研究协议_封存.json").read_bytes()
    digest = hashlib.sha256(frozen).hexdigest()
    if digest != (study / "研究协议_SHA256.txt").read_text().strip():
        raise ValueError("Frozen protocol changed")
    protocol = json.loads(frozen)
    original = json.loads((study / "回放结果/回放.json").read_text())
    previous = json.loads(
        (study / "复核_分钟日线与封单/复核结果/复核.json").read_text()
    )
    calendar = json.loads((study / "原始行情/交易日历.json").read_text())["rows"]
    trading = {row["calendar_date"] for row in calendar if row["is_trading_day"] == "1"}
    daily = {
        sym: {row["date"]: row for row in read_raw(study, sym, "日线")}
        for sym in protocol["universe"]
    }
    base_audits = previous["audits"]
    bars, returned = {}, []
    for file in sorted((research / "原始返回").glob("*_daily_ifind.json")):
        data = envelope(file)["data"]
        unit = data.get("indicators_params", {}).get("成交量", {}).get("单位", "")
        for _, rows in tables(data["answer"]):
            for row in rows:
                bar, state = bar_row(row, trading, unit)
                returned.append(
                    {
                        "source": file.name,
                        "code": row.get("证券代码"),
                        "date": row.get("日期"),
                        "state": state,
                    }
                )
                if bar:
                    add_bar(bars, bar)
    comparisons = []
    for row in base_audits:
        sym, day = row["symbol"], row["date"]
        code = sym[2:] + "." + sym[:2].upper()
        current, base = bars.get((code, day)), daily[sym][day]
        rec = {
            "symbol": sym,
            "name": protocol["universe"][sym],
            "date": day,
            "ifind_present": current is not None,
            "ohlc_mismatches": [],
            "historical_isST_from_original": base["isST"],
        }
        if current:
            rec["current_display_name"] = current["name"]
            for field in BAR_FIELDS[:-1]:
                rec["ifind_" + field], rec["base_" + field] = (
                    str(current[field]),
                    base[field],
                )
                if current[field] != Decimal(base[field]):
                    rec["ohlc_mismatches"].append(field)
            delta = current["volume"] - Decimal(base["volume"])
            rec["volume_delta_shares"] = str(delta)
            rec["volume_within_one_share"] = abs(delta) <= 1
        comparisons.append(rec)
    # Prices are independently checked, but the disputed minute replay is untouched.
    if any(not row["ifind_present"] or row["ohlc_mismatches"] for row in comparisons):
        raise ValueError("Daily comparisons have missing or conflicting prices")

    observed = {(r["symbol"], r["asof"][:10]): r for r in original["observations"]}
    codes = {sym[2:]: sym for sym in protocol["universe"]}
    media = []
    for file in sorted((research / "媒体原文").glob("*.json")):
        article = json.loads(file.read_text())
        for table in article["tables"]:
            for row in table:
                if len(row) < 7 or row[1] not in codes:
                    continue
                sym, day = codes[row[1]], article["date"]
                base = daily[sym].get(day)
                if not base:
                    continue
                matched = (sym, day) in observed
                price, amount = number(row[4]), number(row[5])
                if price != Decimal(base["close"]):
                    raise ValueError(
                        "Media price does not match historical daily close"
                    )
                media.append(
                    {
                        "symbol": sym,
                        "name": protocol["universe"][sym],
                        "date": day,
                        "reported_price": float(price),
                        "reported_amount": row[5],
                        "reported_amount_yuan": float(amount),
                        "implied_shares_approx": float(amount / price),
                        "in_frozen_observations": matched,
                        "strict_one_word": one_word(base),
                        "next_day_opened": observed[sym, day][
                            "next_day_traded_above_limit"
                        ]
                        if matched
                        else None,
                        "source_url": article["url"],
                        "published_at_displayed": article["published_candidates"][0],
                        "first_seen_at": article["retrieved_at"],
                        "snapshot_time": None,
                        "historical_visibility_verified": False,
                        "precision": "media_rounded_amount_not_raw_inventory",
                        "qualified_Q_shares": None,
                        "usable_as_exact_Q": False,
                    }
                )
    changes = []
    for sym in sorted(codes.values()):
        sample = sorted(
            (r for r in media if r["symbol"] == sym and r["in_frozen_observations"]),
            key=lambda r: r["date"],
        )
        for index, new in enumerate(sample[1:]):
            old = sample[index]
            changes.append(
                {
                    "symbol": sym,
                    "name": new["name"],
                    "from_date": old["date"],
                    "date": new["date"],
                    "next_day_opened": new["next_day_opened"],
                    **inventory_change(
                        Decimal(str(old["reported_amount_yuan"])),
                        Decimal(str(old["reported_price"])),
                        Decimal(str(new["reported_amount_yuan"])),
                        Decimal(str(new["reported_price"])),
                    ),
                }
            )
    paths = []
    for episode in original["episodes"]:
        sym, day, following = (
            episode["symbol"],
            episode["opening_date"],
            episode["next_date"],
        )
        code = sym[2:] + "." + sym[:2].upper()
        close, next_open = bars[code, day]["close"], bars[code, following]["open"]
        prior = next(
            r
            for r in previous["price_paths"]
            if r["episode_id"] == episode["episode_id"]
        )
        paths.append(
            {
                "name": episode["name"],
                "opening_date": day,
                "opening_close": float(close),
                "next_date": following,
                "next_open": float(next_open),
                "close_to_next_open_pct": float((next_open / close - 1) * 100),
                "A_confirmation_price": prior["confirm_point"],
                "A_point_to_next_open_pct": prior["next_open_vs_confirm_pct"],
                "next_open_at_floor": prior["next_open_at_floor"],
                "fill_assumed": False,
            }
        )
    raw_seal = envelope(research / "原始返回/historical_sell_queue.json")["data"][
        "answer"
    ]
    seal_headers = [header for headers, _ in tables(raw_seal) for header in headers]
    control = envelope(research / "原始返回/historical_screen_control.json")["data"]
    control_tables = list(tables(control["answer"]))
    summary = {
        "protocol_sha256": digest,
        "ifind_returned_daily_rows": len(returned),
        "rejected_nontrading_rows": sum(
            r["state"] == "not_a_trading_day" for r in returned
        ),
        "rejected_unverified_volume_unit_rows": sum(
            r["state"] == "volume_unit_unverified" for r in returned
        ),
        "complete_trading_rows": len(bars),
        "frozen_stock_dates": len(comparisons),
        "matched_ohlc_fields": sum(4 - len(r["ohlc_mismatches"]) for r in comparisons),
        "volume_within_one_share": sum(
            r["volume_within_one_share"] for r in comparisons
        ),
        "media_rows": len(media),
        "media_matched_observation_days": sum(
            r["in_frozen_observations"] for r in media
        ),
        "media_non_oneword_controls": sum(not r["strict_one_word"] for r in media),
        "observation_days": len(observed),
        "exact_sell_queue_rows": 0,
        "historical_seal_response_columns": sorted(set(seal_headers)),
        **seal_columns(seal_headers),
        "simple_screen_count": control["selectedSecuritiesCount"],
        "simple_screen_column_dates": column_dates(control_tables[0][0]),
        "opening_episodes": len(paths),
        "next_open_below_opening_close": sum(
            r["close_to_next_open_pct"] < 0 for r in paths
        ),
        "median_close_to_next_open_pct": statistics.median(
            r["close_to_next_open_pct"] for r in paths
        ),
        "A_point_to_next_open_positive": sum(
            r["A_point_to_next_open_pct"] > 0 for r in paths
        ),
        "next_open_at_floor": sum(r["next_open_at_floor"] for r in paths),
        "qualified_strategy_returns": 0,
        "historical_minute_bars_added": 0,
    }
    out.mkdir(parents=True, exist_ok=True)
    for name, records in [
        ("日线跨源40日.csv", comparisons),
        ("返回行检查.csv", returned),
        ("媒体封单证据.csv", media),
        ("封单变化描述.csv", changes),
        ("次日价格路径.csv", paths),
    ]:
        write_csv(out / name, records)
    inputs = (
        sorted((research / "原始返回").glob("*.json"))
        + sorted((research / "媒体原文").glob("*"))
        + sorted((research / "公告原文").glob("*"))
        + [
            research / "请求清单.json",
            research / "工具边界.json",
            research / "取媒体封单.py",
        ]
    )
    result = {
        "summary": summary,
        "daily_comparisons": comparisons,
        "media_seals": media,
        "media_changes": changes,
        "price_paths": paths,
        "input_sha256": {
            str(p.relative_to(research)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in inputs
        },
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (out / "同花顺复核.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--study-dir", type=Path, required=True)
    parser.add_argument("--research-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.study_dir, args.research_dir, args.out),
            ensure_ascii=False,
            indent=2,
        )
    )
