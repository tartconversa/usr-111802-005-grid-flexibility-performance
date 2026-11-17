"""协调来源事件、主体目录、现有摘要和报告发布。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import ResponseRegistry, timestamp
from .response import ResponseAnalyzer

REPORT_KIND = "response_performance"


class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = ResponseRegistry(self.connection)
        self.responses = ResponseAnalyzer(self.store, self.entities, self.domain)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source,external_id,entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    # ---- 需求响应报名与生产日历 ----

    def _require_entity(self, entity_id):
        row = self.connection.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError("未知分析主体")

    def enroll(self, event, site, target, window_start, window_end, observed_at=None):
        """登记一次已确认的响应报名（含承诺削峰量与事件时段）。"""
        self._require_entity(site)
        with self.connection:
            return self.domain.commit(
                event, site, target, window_start, window_end, observed_at)

    def cancel_enrollment(self, event, site, observed_at=None):
        with self.connection:
            return self.domain.cancel_commitment(event, site, observed_at)

    def record_shutdown(self, site, day, reason, observed_at=None):
        """登记生产停线日历；更正同一日停线即产生更高修订。"""
        self._require_entity(site)
        with self.connection:
            return self.domain.shutdown(site, day, reason, observed_at)

    def cancel_shutdown(self, site, day, observed_at=None):
        """撤销一条停线记录（停线更正的一种，同样留痕为修订）。"""
        with self.connection:
            return self.domain.cancel_shutdown(site, day, observed_at)

    # ---- 响应分析与履约报告 ----

    def evaluate_response(self, event, site=None, as_of=None):
        """按当前（或指定时间点的）数据评估响应履约。"""
        if as_of is not None:
            as_of = timestamp(as_of)
        return self.responses.evaluate(event, site, as_of)

    @staticmethod
    def _response_report_body(evaluation):
        return {
            "report_kind": REPORT_KIND,
            "event": evaluation["event"],
            "site": evaluation["site"],
            "as_of": evaluation["as_of"],
            "evaluation": evaluation,
        }

    @staticmethod
    def _is_response_report(report):
        return report.get("body", {}).get("report_kind") == REPORT_KIND

    def publish_response_report(self, report_id, created_at, title,
                                event, site=None, as_of=None):
        """发布履约报告；正文固化发布时点的评估，后续补传不改变报告内容。"""
        if as_of is not None:
            as_of = timestamp(as_of)
        evaluation = self.responses.evaluate(event, site, as_of)
        body = self._response_report_body(evaluation)
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    def replay_response_report(self, report_id):
        """按报告固化的时间点与输入重新评估，应与报告正文逐字段一致。"""
        report = self.decisions.get(report_id)
        if report is None:
            raise ValueError("报告不存在")
        if not self._is_response_report(report):
            raise ValueError("报告不是需求响应履约报告")
        body = report["body"]
        replay = self.responses.evaluate(body["event"], body["site"], body["as_of"])
        if replay != body["evaluation"]:
            raise ValueError("报告无法按当时数据重现")
        return replay

    def affected_response_reports(self, event=None):
        """扫描已发布履约报告，找出评估结论已被补传计量或停线更正改变的报告。"""
        affected = []
        for report in self.decisions.list_all():
            if not self._is_response_report(report):
                continue
            body = report["body"]
            if event is not None and body["event"] != event:
                continue
            current = self.responses.evaluate(body["event"], body["site"])
            changes = self._diff_evaluation(body["evaluation"], current)
            if not changes:
                continue
            affected.append({
                "report_id": report["report_id"],
                "event": body["event"],
                "site": body["site"],
                "published_as_of": body["as_of"],
                "published_status": body["evaluation"]["status"],
                "current_status": current["status"],
                "changes": changes,
            })
        return affected

    @staticmethod
    def _diff_evaluation(published, current):
        changes = []
        path_values = (
            ("status",),
            ("baseline", "window_total"),
            ("baseline", "comparable_days"),
            ("baseline", "excluded_days"),
            ("actual", "window_total"),
            ("actual", "missing_offsets"),
            ("reduction", "gross"),
            ("reduction", "attributable"),
            ("reduction", "blocked_reasons"),
            ("fulfillment", "fulfilled"),
            ("fulfillment", "ratio"),
        )

        def lookup(section, path):
            node = section
            for key in path:
                node = node[key]
            return node

        for path in path_values:
            old, new = lookup(published, path), lookup(current, path)
            if old != new:
                changes.append({"field": ".".join(path), "published": old, "current": new})
        old_inputs, new_inputs = published["inputs"], current["inputs"]
        if old_inputs["commitment_revision"] != new_inputs["commitment_revision"]:
            changes.append({
                "field": "inputs.commitment_revision",
                "published": old_inputs["commitment_revision"],
                "current": new_inputs["commitment_revision"]})
        if old_inputs["shutdown_days"] != new_inputs["shutdown_days"]:
            changes.append({
                "field": "inputs.shutdown_days",
                "published": old_inputs["shutdown_days"],
                "current": new_inputs["shutdown_days"]})
        old_points = {(p["source"], p["event_id"], p["revision"])
                      for p in old_inputs["meter_points"]}
        new_points = {(p["source"], p["event_id"], p["revision"])
                      for p in new_inputs["meter_points"]}
        if old_points != new_points:
            changes.append({
                "field": "inputs.meter_points",
                "published": sorted(old_points),
                "current": sorted(new_points)})
        return changes
