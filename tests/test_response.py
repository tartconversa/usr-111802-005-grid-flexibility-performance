"""核验需求响应报名、可比基线、归因分析与履约报告重现。"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analytics import AnalyticsService

TZ = "+08:00"
EVENT_DAY = "2026-11-18"
WINDOW_START = f"{EVENT_DAY}T14:00:00{TZ}"
WINDOW_END = f"{EVENT_DAY}T16:00:00{TZ}"
# 15 分钟一个计量区间，14:00-16:00 共 8 个区间
SLOTS = ["14:00", "14:15", "14:30", "14:45",
         "15:00", "15:15", "15:30", "15:45"]
ALL_SLOTS = [f"{hour:02d}:{minute:02d}"
             for hour in range(9, 18) for minute in (0, 15, 30, 45)]


def iso(day, clock, minute_delta=0):
    parsed = datetime.fromisoformat(f"{day}T{clock}:00{TZ}")
    return (parsed + timedelta(minutes=minute_delta)).isoformat()


class ResponseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        self.service.register("park-1", "示范园区", "group-1")
        self.service.link("meter-system", "meter-1", "park-1")
        self.sequence = 0

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def meter(self, day, clock, amount, *, revision=1, observed=None,
              source="meter-system", external="meter-1", event_id=None):
        self.sequence += 1
        event_id = event_id or f"m-{day}-{clock}"
        observed = observed or iso(day, "20:00")
        event = {
            "source": source, "event_id": event_id, "revision": revision,
            "kind": "meter", "entity": external,
            "occurred_at": iso(day, clock), "observed_at": observed,
            "payload": {"amount": amount},
        }
        self.service.ingest(event)
        return event_id

    def fill_day(self, day, amount, **kwargs):
        for clock in ALL_SLOTS:
            self.meter(day, clock, amount, **kwargs)

    def fill_window(self, day, amount, **kwargs):
        for clock in SLOTS:
            self.meter(day, clock, amount, **kwargs)


class FulfillmentTests(ResponseTestBase):
    def build_baseline_days(self):
        # 事件日前 9 个完整计量日（落在 14 天回看窗内），每区间 10
        start = datetime.fromisoformat(f"{EVENT_DAY}T00:00:00{TZ}")
        days = [(start - timedelta(days=n)).date().isoformat() for n in range(1, 10)]
        for day in days:
            self.fill_day(day, 10)
        return days

    def test_attributable_reduction_and_fulfillment(self):
        days = self.build_baseline_days()
        self.fill_window(EVENT_DAY, 6)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END,
                            observed_at=iso(EVENT_DAY, "08:00"))

        result = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(result["status"], "可结算")
        self.assertEqual(result["baseline"]["comparable_days"], days)
        self.assertEqual(result["baseline"]["window_total"], "80.000000")
        self.assertEqual(result["actual"]["window_total"], "48.000000")
        self.assertEqual(result["reduction"]["gross"], "32.000000")
        self.assertEqual(result["reduction"]["attributable"], "32.000000")
        self.assertEqual(result["reduction"]["blocked_reasons"], [])
        self.assertTrue(result["fulfillment"]["fulfilled"])
        self.assertEqual(result["fulfillment"]["ratio"], "1.600000")

    def test_missed_commitment_is_settled_but_not_fulfilled(self):
        self.build_baseline_days()
        self.fill_window(EVENT_DAY, 9)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        result = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(result["status"], "可结算")
        self.assertEqual(result["reduction"]["attributable"], "8.000000")
        self.assertFalse(result["fulfillment"]["fulfilled"])
        self.assertEqual(result["fulfillment"]["ratio"], "0.400000")

    def test_event_day_shutdown_blocks_settlement(self):
        self.build_baseline_days()
        self.fill_window(EVENT_DAY, 6)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        self.service.record_shutdown("park-1", EVENT_DAY, "产线检修",
                                     observed_at=iso(EVENT_DAY, "07:00"))
        result = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(result["status"], "暂不能结算")
        self.assertIsNone(result["reduction"]["attributable"])
        self.assertIsNone(result["fulfillment"]["fulfilled"])
        # 毛降幅仍展示，供运营判断自然降耗规模
        self.assertEqual(result["reduction"]["gross"], "32.000000")
        self.assertIn("事件日处于生产停线", result["reduction"]["blocked_reasons"][0])

    def test_data_gap_blocks_then_backfill_restores_settlement(self):
        self.build_baseline_days()
        # 事件日仅补到 7 个区间，15:45 尚未计量
        for clock in SLOTS[:-1]:
            self.meter(EVENT_DAY, clock, 6)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END)

        before = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(before["status"], "暂不能结算")
        self.assertEqual(before["actual"]["missing_offsets"], ["945"])
        self.assertIsNone(before["actual"]["window_total"])

        # 缺口补传：同一计量点的更高修订
        self.meter(EVENT_DAY, "15:45", 6, revision=2,
                   observed=iso(EVENT_DAY, "23:30"))
        after = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(after["status"], "可结算")
        self.assertEqual(after["actual"]["missing_offsets"], [])
        self.assertEqual(after["actual"]["window_total"], "48.000000")

    def test_baseline_excludes_shutdown_and_gap_days(self):
        start = datetime.fromisoformat(f"{EVENT_DAY}T00:00:00{TZ}")
        all_days = [(start - timedelta(days=n)).date().isoformat()
                    for n in range(1, 10)]
        for day in all_days:
            self.fill_day(day, 10)
        # 最近一天停线：从可比日剔除
        self.service.record_shutdown("park-1", all_days[0], "停产盘点")
        # 再前一天删掉两个区间，制造计量缺口
        gap_day = all_days[1]
        self.service.connection.execute(
            "DELETE FROM events WHERE event_id LIKE ?", (f"m-{gap_day}-%",))
        for clock in ALL_SLOTS[2:]:
            self.meter(gap_day, clock, 10)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        self.fill_window(EVENT_DAY, 6)

        result = self.service.evaluate_response("dr-2026-11")
        excluded = {item["day"]: item["reason"]
                    for item in result["baseline"]["excluded_days"]}
        self.assertEqual(excluded[all_days[0]], "生产停线")
        self.assertEqual(excluded[gap_day], "计量数据缺口")
        self.assertNotIn(all_days[0], result["baseline"]["comparable_days"])
        self.assertEqual(len(result["baseline"]["comparable_days"]), 7)

    def test_insufficient_baseline_blocks_settlement(self):
        start = datetime.fromisoformat(f"{EVENT_DAY}T00:00:00{TZ}")
        # 只有两个完整日，低于最低可比日要求
        for n in range(1, 3):
            day = (start - timedelta(days=n)).date().isoformat()
            self.fill_day(day, 10)
        self.fill_window(EVENT_DAY, 6)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        result = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(result["status"], "暂不能结算")
        self.assertFalse(result["baseline"]["sufficient"])
        self.assertIsNone(result["baseline"]["window_total"])
        self.assertIn("可比基线不足", result["reduction"]["blocked_reasons"])


class ReportReplayTests(ResponseTestBase):
    def prepare_event(self):
        start = datetime.fromisoformat(f"{EVENT_DAY}T00:00:00{TZ}")
        for n in range(1, 6):
            day = (start - timedelta(days=n)).date().isoformat()
            self.fill_day(day, 10)
        for clock in SLOTS[:-1]:
            self.meter(EVENT_DAY, clock, 6)
        self.service.enroll("dr-2026-11", "park-1", "20",
                            WINDOW_START, WINDOW_END,
                            observed_at=iso(EVENT_DAY, "08:00"))

    def test_published_report_replays_and_flags_affected_after_backfill(self):
        self.prepare_event()
        cutoff = iso(EVENT_DAY, "22:00")
        report = self.service.publish_response_report(
            "rr-1", iso(EVENT_DAY, "22:30"), "11月需求响应履约报告",
            "dr-2026-11", as_of=cutoff)
        self.assertEqual(report["body"]["evaluation"]["status"], "暂不能结算")

        # 已发布报告始终可按当时数据逐字段重现
        replay = self.service.replay_response_report("rr-1")
        self.assertEqual(replay, report["body"]["evaluation"])

        # 补传后当前评估恢复可结算，并指出受影响报告
        self.meter(EVENT_DAY, "15:45", 6, revision=2,
                   observed=iso(EVENT_DAY, "23:30"))
        current = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(current["status"], "可结算")
        self.assertEqual(self.service.replay_response_report("rr-1"),
                         report["body"]["evaluation"])

        affected = self.service.affected_response_reports()
        self.assertEqual(len(affected), 1)
        entry = affected[0]
        self.assertEqual(entry["report_id"], "rr-1")
        self.assertEqual(entry["published_status"], "暂不能结算")
        self.assertEqual(entry["current_status"], "可结算")
        fields = {change["field"] for change in entry["changes"]}
        self.assertIn("status", fields)
        self.assertIn("actual.window_total", fields)
        self.assertIn("inputs.meter_points", fields)

    def test_shutdown_correction_flags_report_and_replay_stays_fixed(self):
        self.prepare_event()
        self.meter(EVENT_DAY, "15:45", 6)
        cutoff = iso(EVENT_DAY, "22:00")
        self.service.publish_response_report(
            "rr-2", iso(EVENT_DAY, "22:30"), "履约报告",
            "dr-2026-11", as_of=cutoff)
        self.assertEqual(
            self.service.affected_response_reports(), [])

        # 次日更正：事件日实际停线
        self.service.record_shutdown(
            "park-1", EVENT_DAY, "突发设备故障",
            observed_at=iso(EVENT_DAY, "23:59"))
        affected = self.service.affected_response_reports("dr-2026-11")
        self.assertEqual(len(affected), 1)
        fields = {change["field"] for change in affected[0]["changes"]}
        self.assertIn("inputs.shutdown_days", fields)
        self.assertIn("reduction.attributable", fields)
        self.assertEqual(affected[0]["current_status"], "暂不能结算")

        # 再撤销停线更正，当前评估回到可结算，报告重放仍不变
        self.service.cancel_shutdown(
            "park-1", EVENT_DAY, observed_at=iso(EVENT_DAY, "23:59", 1))
        self.assertEqual(
            self.service.evaluate_response("dr-2026-11")["status"], "可结算")
        self.assertEqual(
            self.service.affected_response_reports(), [])
        self.service.replay_response_report("rr-2")  # 不抛异常即重现成功

    def test_response_report_id_conflict(self):
        self.prepare_event()
        self.meter(EVENT_DAY, "15:45", 6)
        kwargs = ("rr-3", iso(EVENT_DAY, "22:30"), "履约报告", "dr-2026-11")
        self.service.publish_response_report(*kwargs)
        with self.assertRaises(ValueError):
            self.service.publish_response_report(
                "rr-3", iso(EVENT_DAY, "22:30"), "被篡改的报告", "dr-2026-11")

    def test_as_of_view_hides_late_backfill_and_revisions(self):
        self.prepare_event()
        # 22:00 前缺 15:45；补传在 23:30 到达
        early = self.service.evaluate_response(
            "dr-2026-11", as_of=iso(EVENT_DAY, "22:00"))
        self.assertEqual(early["actual"]["missing_offsets"], ["945"])
        self.meter(EVENT_DAY, "15:45", 6, revision=2,
                   observed=iso(EVENT_DAY, "23:30"))
        still_early = self.service.evaluate_response(
            "dr-2026-11", as_of=iso(EVENT_DAY, "22:00"))
        self.assertEqual(still_early, early)
        latest = self.service.evaluate_response("dr-2026-11")
        self.assertEqual(latest["actual"]["window_total"], "48.000000")


class RegistryRevisionTests(ResponseTestBase):
    def test_commitment_and_shutdown_revision_history(self):
        self.service.enroll("dr-1", "park-1", "20",
                            WINDOW_START, WINDOW_END,
                            observed_at=iso(EVENT_DAY, "08:00"))
        self.service.enroll("dr-1", "park-1", "25",
                            WINDOW_START, WINDOW_END,
                            observed_at=iso(EVENT_DAY, "09:00"))
        current = self.service.domain.commitment("dr-1", "park-1")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["target"], "25")
        as_of = self.service.domain.commitment(
            "dr-1", "park-1", as_of=iso(EVENT_DAY, "08:30"))
        self.assertEqual(as_of["target"], "20")

        self.service.record_shutdown("park-1", EVENT_DAY, "检修",
                                     observed_at=iso(EVENT_DAY, "07:00"))
        self.assertTrue(self.service.domain.is_shutdown("park-1", EVENT_DAY))
        self.service.cancel_shutdown("park-1", EVENT_DAY,
                                     observed_at=iso(EVENT_DAY, "12:00"))
        self.assertFalse(self.service.domain.is_shutdown("park-1", EVENT_DAY))
        records = self.service.domain.shutdown_records(include_deleted=True)
        self.assertTrue(records[-1]["deleted"])
        # 历史时点仍能看到停线
        self.assertTrue(self.service.domain.is_shutdown(
            "park-1", EVENT_DAY, as_of=iso(EVENT_DAY, "10:00")))

    def test_enrollment_validation(self):
        with self.assertRaises(ValueError):
            self.service.enroll("dr-1", "park-1", "0",
                                WINDOW_START, WINDOW_END)
        with self.assertRaises(ValueError):
            self.service.enroll("dr-1", "park-1", "20",
                                WINDOW_END, WINDOW_START)
        with self.assertRaises(ValueError):
            self.service.enroll("dr-1", "unknown-park", "20",
                                WINDOW_START, WINDOW_END)
        with self.assertRaises(ValueError):
            self.service.record_shutdown("park-1", EVENT_DAY, "  ")

    def test_unknown_event_and_cancelled_enrollment(self):
        with self.assertRaises(ValueError):
            self.service.evaluate_response("nope")
        self.service.enroll("dr-9", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        self.service.cancel_enrollment("dr-9", "park-1",
                                       observed_at=iso(EVENT_DAY, "10:00"))
        with self.assertRaises(ValueError):
            self.service.evaluate_response("dr-9")

    def test_evaluation_without_meter_data_rejected(self):
        self.service.enroll("dr-x", "park-1", "20",
                            WINDOW_START, WINDOW_END)
        with self.assertRaises(ValueError):
            self.service.evaluate_response("dr-x")


if __name__ == "__main__":
    unittest.main()
