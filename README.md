# 园区用电运营数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json
响应演练：python -m analytics --database demo.sqlite3 --response

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告；service.py 协调这些现有接口。当前摘要只表示原始观察，不提供行业专属分析结论。

需求响应分析口径：

- analytics/domain.py 的 ResponseRegistry 维护响应报名与生产停线日历，均为带修订号与观察时间的追加式记录，支持 as_of 历史视图与撤销留痕；旧版无修订表在首次打开时自动迁移。
- analytics/response.py 以事件日前 14 天内非停线且计量完整的可比日构建同时段基线（少于 3 个可比日则基线不足），并按事件时段计算实际用量与毛降幅。
- 事件日停线（自然降耗）、事件时段计量缺口、基线不足任一存在时结论为“暂不能结算”；否则降幅归因于已确认响应，并与承诺量比较给出履约比例与是否兑现。
- service.py 提供报名、停线登记/更正、响应评估、履约报告发布、按当时数据重放以及受影响报告扫描。补传计量或停线更正只改变当前评估，并通过 affected_response_reports 指出受影响的已发布报告；已发布报告内容保持不变且可重现。业务口径详见 docs/domain.md。
