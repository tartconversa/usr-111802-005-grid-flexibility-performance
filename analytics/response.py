"""电网需求响应的可比基线、实际响应与归因分析。

口径说明：
- 基线取事件日之前 BASELINE_LOOKBACK_DAYS 天内、非停线且计量完整的可比日，
  按事件时段内的计量区间逐区间取均值；可比日少于 MIN_BASELINE_DAYS 则基线不足。
- 实际响应取事件时段内已到计量区间之和；缺区间即数据缺口。
- 事件日停线属于自然降耗，事件时段缺口无法核验，两类情形都暂不能结算，
  只有两者都不存在时，降幅才可归因于已确认的响应。
所有读取都基于带 as_of 的时间点视图，保证已确认报告可按当时数据重现。
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .domain import timestamp

BASELINE_LOOKBACK_DAYS = 14
MIN_BASELINE_DAYS = 3
QUANT = Decimal("0.000001")


def q(amount):
    return amount.quantize(QUANT, rounding=ROUND_HALF_UP)


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ResponseAnalyzer:
    def __init__(self, store, entities, registry):
        self.store = store
        self.entities = entities
        self.registry = registry

    def _meter_series(self, site, as_of):
        """返回该园区 (day, 分钟偏移) -> 计量点，全部为当前最新修订。"""
        points = {}
        used = []
        for event in self.store.current(as_of):
            if event["kind"] != "meter":
                continue
            subject = self.entities.resolve(event["source"], event["entity"])
            if subject != site:
                continue
            try:
                amount = Decimal(str(event["payload"].get("amount", 0)))
            except InvalidOperation as exc:
                raise ValueError("计量电量无效") from exc
            if not amount.is_finite() or amount < 0:
                raise ValueError("计量电量无效")
            occurred = parse_ts(event["occurred_at"])
            day = occurred.date().isoformat()
            offset = occurred.hour * 60 + occurred.minute
            current = points.get((day, offset))
            if current is None or parse_ts(event["observed_at"]) > current[1]:
                points[(day, offset)] = (amount, parse_ts(event["observed_at"]), occurred)
            used.append({"source": event["source"], "event_id": event["event_id"],
                         "revision": event["revision"],
                         "observed_at": event["observed_at"]})
        series = {key: (value[0], value[2]) for key, value in points.items()}
        used.sort(key=lambda item: (item["source"], item["event_id"], item["revision"]))
        return series, used

    @staticmethod
    def _offsets(series):
        return sorted({offset for (_, offset) in series})

    @staticmethod
    def _day_complete(series, day, offsets, reference_tz):
        for offset in offsets:
            hour, minute = divmod(offset, 60)
            expected = datetime.combine(
                datetime.fromisoformat(day).date(),
                datetime.min.time().replace(hour=hour, minute=minute),
                reference_tz)
            if not any(occurred == expected
                       for (d, off), (_, occurred) in series.items()
                       if d == day and off == offset):
                return False
        return True

    def evaluate(self, event, site=None, as_of=None):
        if as_of is not None:
            as_of = timestamp(as_of)
        commitments = self.registry.commitments(as_of)
        matches = [item for item in commitments if item["event"] == event]
        if not matches:
            raise ValueError("未找到已确认的响应报名")
        if site is None:
            if len({item["site"] for item in matches}) > 1:
                raise ValueError("同一事件存在多个园区报名，需指定园区")
            site = matches[0]["site"]
        commitment = next((item for item in matches if item["site"] == site), None)
        if commitment is None:
            raise ValueError("未找到已确认的响应报名")
        if not self._entity_exists(site):
            raise ValueError("未知分析主体")
        window_start, window_end = commitment["window_start"], commitment["window_end"]
        if not window_start or not window_end:
            raise ValueError("报名缺少事件时段")
        start, end = parse_ts(window_start), parse_ts(window_end)
        if end <= start or start.date().isoformat() != end.date().isoformat():
            raise ValueError("事件时段必须为同一日内的有效区间")
        event_day = start.date().isoformat()

        series, used = self._meter_series(site, as_of)
        if not series:
            raise ValueError("园区尚无可用计量数据")
        offsets = self._offsets(series)
        reference_tz = start.tzinfo
        window_offsets = [off for off in offsets
                          if start.hour * 60 + start.minute <= off < end.hour * 60 + end.minute]

        # 事件时段计量缺口
        gap_offsets = []
        for offset in window_offsets:
            if (event_day, offset) not in series:
                gap_offsets.append(offset)

        # 回看期内逐日筛选可比日
        excluded, comparable_days = [], []
        for lookback in range(1, BASELINE_LOOKBACK_DAYS + 1):
            day = (start.date() - timedelta(days=lookback)).isoformat()
            if self.registry.is_shutdown(site, day, as_of):
                excluded.append({"day": day, "reason": "生产停线"})
            elif not self._day_complete(series, day, offsets, reference_tz):
                excluded.append({"day": day, "reason": "计量数据缺口"})
            else:
                comparable_days.append(day)

        baseline_sufficient = len(comparable_days) >= MIN_BASELINE_DAYS
        baseline_by_offset, baseline_window_total = {}, None
        if baseline_sufficient:
            baseline_window_total = Decimal("0")
            for offset in window_offsets:
                values = [series[(day, offset)][0] for day in comparable_days]
                mean = sum(values, Decimal("0")) / Decimal(len(values))
                baseline_by_offset[offset] = q(mean)
                baseline_window_total += mean
            baseline_window_total = q(baseline_window_total)

        actual_total = None
        if not gap_offsets:
            actual_total = q(sum(
                (series[(event_day, offset)][0] for offset in window_offsets),
                Decimal("0")))

        event_day_shutdown = self.registry.is_shutdown(site, event_day, as_of)
        blocked_reasons = []
        if event_day_shutdown:
            blocked_reasons.append("事件日处于生产停线，降幅含自然降耗")
        if not baseline_sufficient:
            blocked_reasons.append("可比基线不足")
        if gap_offsets:
            blocked_reasons.append("事件时段存在计量数据缺口")
        settlable = not blocked_reasons

        target = Decimal(commitment["target"])
        gross_reduction = None
        if baseline_window_total is not None and actual_total is not None:
            gross_reduction = q(baseline_window_total - actual_total)
        attributable = gross_reduction if settlable else None
        fulfillment_ratio, fulfilled = None, None
        if attributable is not None:
            fulfillment_ratio = q(attributable / target)
            fulfilled = attributable >= target

        return {
            "event": event,
            "site": site,
            "as_of": as_of,
            "status": "可结算" if settlable else "暂不能结算",
            "window": {"start": window_start, "end": window_end},
            "commitment": {
                "target": str(target),
                "revision": commitment["revision"],
                "observed_at": commitment["observed_at"],
            },
            "baseline": {
                "method": f"事件日前{BASELINE_LOOKBACK_DAYS}天内非停线完整计量日的同时段均值",
                "lookback_days": BASELINE_LOOKBACK_DAYS,
                "min_comparable_days": MIN_BASELINE_DAYS,
                "comparable_days": comparable_days,
                "excluded_days": excluded,
                "interval_baseline": {
                    str(offset): str(value)
                    for offset, value in sorted(baseline_by_offset.items())},
                "window_total": (None if baseline_window_total is None
                                 else str(baseline_window_total)),
                "sufficient": baseline_sufficient,
            },
            "actual": {
                "window_total": None if actual_total is None else str(actual_total),
                "missing_offsets": [str(offset) for offset in gap_offsets],
            },
            "reduction": {
                "gross": None if gross_reduction is None else str(gross_reduction),
                "attributable": None if attributable is None else str(attributable),
                "blocked_reasons": blocked_reasons,
            },
            "fulfillment": {
                "fulfilled": fulfilled,
                "ratio": None if fulfillment_ratio is None else str(fulfillment_ratio),
            },
            "inputs": {
                "commitment_revision": commitment["revision"],
                "shutdown_days": sorted(
                    row["day"] for row in self.registry.shutdown_records(as_of)
                    if row["site"] == site),
                "meter_points": used,
            },
        }

    def _entity_exists(self, entity_id):
        row = self.entities.connection.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        return row is not None
