"""本地演练入口：导入样本并查看业务摘要或需求响应履约分析。"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from .service import AnalyticsService

TZ = "+08:00"
ALL_SLOTS = [f"{hour:02d}:{minute:02d}"
             for hour in range(9, 18) for minute in (0, 15, 30, 45)]
WINDOW_SLOTS = ["14:00", "14:15", "14:30", "14:45",
                "15:00", "15:15", "15:30", "15:45"]


def iso(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}:00{TZ}").isoformat()


def add_meter(service, source, external, day, clock, amount,
              revision=1, observed_clock="20:00"):
    service.ingest({
        "source": source, "event_id": f"m-{day}-{clock}", "revision": revision,
        "kind": "meter", "entity": external,
        "occurred_at": iso(day, clock), "observed_at": iso(day, observed_clock),
        "payload": {"amount": amount},
    })


def run_response_demo(service, fixture_path):
    sample = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    for subject in sample["entities"]:
        service.register(**subject)
    for alias in sample["aliases"]:
        service.link(**alias)

    source, external = sample["aliases"][0]["source"], sample["aliases"][0]["external_id"]
    event_day = sample["enrollments"][0]["window_start"][:10]
    start = datetime.fromisoformat(f"{event_day}T00:00:00{TZ}")

    # 事件日前 5 个完整计量日，每区间 10kWh，构成可比基线
    baseline_days = [(start - timedelta(days=n)).date().isoformat()
                     for n in range(1, 6)]
    for day in baseline_days:
        for clock in ALL_SLOTS:
            add_meter(service, source, external, day, clock, 10)

    # 事件日 14:00-15:30 已计量（每区间 6），15:45 缺口待补传
    for clock in WINDOW_SLOTS[:-1]:
        add_meter(service, source, external, event_day, clock, 6)

    for enrollment in sample["enrollments"]:
        service.enroll(**enrollment)

    print("== 缺口未补传时的当前评估 ==")
    print(json.dumps(service.evaluate_response(sample["event"]),
                     ensure_ascii=False, indent=2, sort_keys=True))

    meta = sample["report"]
    service.publish_response_report(
        meta["report_id"], meta["created_at"], meta["title"],
        sample["event"], as_of=meta["as_of"])
    print("\n== 已发布报告（缺口版） ==")
    print(json.dumps(service.decisions.get(meta["report_id"])["body"],
                     ensure_ascii=False, indent=2, sort_keys=True))

    # 23:30 补传最后一个计量区间（更高修订）
    add_meter(service, source, external, event_day, "15:45", 6,
              revision=2, observed_clock="23:30")
    print("\n== 补传后的当前评估 ==")
    print(json.dumps(service.evaluate_response(sample["event"]),
                     ensure_ascii=False, indent=2, sort_keys=True))

    # 停线更正（随后撤销），演示更正只影响当前评估并标记受影响报告
    for item in sample["shutdowns"]:
        deleted = item.pop("deleted", False)
        if deleted:
            service.cancel_shutdown(item["site"], item["day"], item["observed_at"])
        else:
            service.record_shutdown(**item)

    print("\n== 受影响的已发布报告 ==")
    print(json.dumps(service.affected_response_reports(),
                     ensure_ascii=False, indent=2, sort_keys=True))

    print("\n== 报告按当时数据重放（与已发布正文一致） ==")
    print(json.dumps(service.replay_response_report(meta["report_id"]),
                     ensure_ascii=False, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/sample.json")
    parser.add_argument("--response-fixture", default="fixtures/response_sample.json")
    parser.add_argument("--response", action="store_true",
                        help="运行需求响应报名、基线与履约报告演练")
    args = parser.parse_args()
    service = AnalyticsService(args.database)
    try:
        if args.response:
            run_response_demo(service, args.response_fixture)
            return
        sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        for subject in sample["entities"]:
            service.register(**subject)
        for alias in sample["aliases"]:
            service.link(**alias)
        for event in sample["events"]:
            service.ingest(event)
        print(json.dumps(service.summary(), ensure_ascii=False, sort_keys=True))
    finally:
        service.close()


if __name__ == "__main__":
    main()
