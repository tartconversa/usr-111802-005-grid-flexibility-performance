"""响应事件的报名承诺和生产停线日历。

报名与停线均为追加式修订：同一键的更高修订替代先前修订，删除以墓碑记录表示。
所有读取接口都支持 as_of 时间点视图，使已确认报告能按当时数据重现。
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation


def now_iso():
    return datetime.now().astimezone().isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须为字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.isoformat()


def positive_decimal(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("承诺量必须为正数") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("承诺量必须为正数")
    return amount


class ResponseRegistry:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS commitment_revisions (
            event TEXT NOT NULL, site TEXT NOT NULL, revision INTEGER NOT NULL,
            target TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
            observed_at TEXT NOT NULL, deleted INTEGER NOT NULL,
            PRIMARY KEY(event, site, revision))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS shutdown_revisions (
            site TEXT NOT NULL, day TEXT NOT NULL, revision INTEGER NOT NULL,
            reason TEXT NOT NULL, observed_at TEXT NOT NULL, deleted INTEGER NOT NULL,
            PRIMARY KEY(site, day, revision))""")
        self._migrate_legacy()

    def _migrate_legacy(self):
        """把无修订的旧表数据迁入修订表，每个键视为第 1 版。"""
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "commitments" in names and "commitment_revisions" in names:
            empty = self.connection.execute(
                "SELECT 1 FROM commitment_revisions LIMIT 1").fetchone() is None
            if empty:
                for row in self.connection.execute(
                        "SELECT event,site,target FROM commitments").fetchall():
                    self.connection.execute(
                        "INSERT INTO commitment_revisions VALUES(?,?,?,?,?,?,?,?)",
                        (row[0], row[1], 1, row[2], "", "",
                         "1970-01-01T00:00:00+00:00", 0))
            self.connection.execute("DROP TABLE commitments")
        if "shutdowns" in names and "shutdown_revisions" in names:
            empty = self.connection.execute(
                "SELECT 1 FROM shutdown_revisions LIMIT 1").fetchone() is None
            if empty:
                for row in self.connection.execute(
                        "SELECT site,day,reason FROM shutdowns").fetchall():
                    self.connection.execute(
                        "INSERT INTO shutdown_revisions VALUES(?,?,?,?,?,?)",
                        (row[0], row[1], 1, row[2],
                         "1970-01-01T00:00:00+00:00", 0))
            self.connection.execute("DROP TABLE shutdowns")

    def _next_revision(self, table, key_columns, key_values):
        placeholders = " AND ".join(f"{column}=?" for column in key_columns)
        row = self.connection.execute(
            f"SELECT COALESCE(MAX(revision),0) FROM {table} WHERE {placeholders}",
            key_values).fetchone()
        return row[0] + 1

    def _check_conflict(self, table, key_columns, value_columns, key_values, values):
        placeholders = " AND ".join(f"{column}=?" for column in key_columns)
        columns = list(key_columns) + ["revision"] + list(value_columns)
        row = self.connection.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE {placeholders} AND revision=?",
            list(key_values) + [values[0]]).fetchone()
        if row is not None and tuple(row) != tuple(key_values) + tuple(values):
            raise ValueError("同键同修订内容冲突")

    def commit(self, event, site, target, window_start=None, window_end=None,
               observed_at=None, revision=None, deleted=False):
        if not all(isinstance(v, str) and v.strip() for v in (event, site)):
            raise ValueError("事件与园区标识不能为空")
        if type(deleted) is not bool:
            raise ValueError("删除标记无效")
        target_text = str(positive_decimal(target)) if not deleted else "0"
        window_start = timestamp(window_start) if window_start else ""
        window_end = timestamp(window_end) if window_end else ""
        if window_start and window_end and window_end <= window_start:
            raise ValueError("事件时段结束必须晚于开始")
        observed_at = timestamp(observed_at) if observed_at else now_iso()
        next_revision = self._next_revision(
            "commitment_revisions", ("event", "site"), (event, site))
        revision = next_revision if revision is None else revision
        if type(revision) is not int or revision < 1 or revision > next_revision:
            raise ValueError("修订号必须是不超过下一修订号的正整数")
        values = (revision, target_text, window_start, window_end,
                  observed_at, int(deleted))
        self._check_conflict(
            "commitment_revisions", ("event", "site"),
            ("target", "window_start", "window_end", "observed_at", "deleted"),
            (event, site), values)
        self.connection.execute(
            "INSERT OR REPLACE INTO commitment_revisions VALUES(?,?,?,?,?,?,?,?)",
            (event, site) + values)
        return {"event": event, "site": site, "revision": revision,
                "target": target_text, "window_start": window_start,
                "window_end": window_end, "observed_at": observed_at,
                "deleted": deleted}

    def cancel_commitment(self, event, site, observed_at=None):
        return self.commit(event, site, 0, observed_at=observed_at, deleted=True)

    def shutdown(self, site, day, reason, observed_at=None, revision=None, deleted=False):
        if not all(isinstance(v, str) and v.strip() for v in (site, day)):
            raise ValueError("园区与日期不能为空")
        if not deleted and not (isinstance(reason, str) and reason.strip()):
            raise ValueError("停线原因不能为空")
        if type(deleted) is not bool:
            raise ValueError("删除标记无效")
        observed_at = timestamp(observed_at) if observed_at else now_iso()
        next_revision = self._next_revision(
            "shutdown_revisions", ("site", "day"), (site, day))
        revision = next_revision if revision is None else revision
        if type(revision) is not int or revision < 1 or revision > next_revision:
            raise ValueError("修订号必须是不超过下一修订号的正整数")
        reason_text = reason if not deleted else "停线记录撤销"
        values = (revision, reason_text, observed_at, int(deleted))
        self._check_conflict(
            "shutdown_revisions", ("site", "day"),
            ("reason", "observed_at", "deleted"), (site, day), values)
        self.connection.execute(
            "INSERT OR REPLACE INTO shutdown_revisions VALUES(?,?,?,?,?,?)",
            (site, day) + values)
        return {"site": site, "day": day, "revision": revision,
                "reason": reason_text, "observed_at": observed_at,
                "deleted": deleted}

    def cancel_shutdown(self, site, day, observed_at=None):
        return self.shutdown(site, day, "", observed_at=observed_at, deleted=True)

    def _latest_view(self, table, key_columns, as_of=None):
        cutoff = timestamp(as_of) if as_of is not None else None
        keys = ",".join(key_columns)
        if cutoff is None:
            sql = f"""SELECT * FROM {table} t
                WHERE revision=(SELECT MAX(revision) FROM {table}
                    WHERE {(' AND '.join(f'{column}=t.{column}' for column in key_columns))})
                ORDER BY {keys}"""
            rows = self.connection.execute(sql).fetchall()
        else:
            sql = f"""SELECT * FROM {table} t
                WHERE observed_at<=? AND revision=(
                    SELECT MAX(revision) FROM {table}
                    WHERE observed_at<=? AND {
                        ' AND '.join(f'{column}=t.{column}' for column in key_columns)})
                ORDER BY {keys}"""
            rows = self.connection.execute(sql, (cutoff, cutoff)).fetchall()
        return [dict(row) for row in rows]

    def commitments(self, as_of=None):
        return [row for row in self._latest_view(
            "commitment_revisions", ("event", "site"), as_of) if not row["deleted"]]

    def commitment(self, event, site, as_of=None):
        rows = [row for row in self.commitments(as_of)
                if row["event"] == event and row["site"] == site]
        return rows[0] if rows else None

    def shutdown_records(self, as_of=None, include_deleted=False):
        rows = self._latest_view("shutdown_revisions", ("site", "day"), as_of)
        return rows if include_deleted else [row for row in rows if not row["deleted"]]

    def is_shutdown(self, site, day, as_of=None):
        rows = [row for row in self.shutdown_records(as_of)
                if row["site"] == site and row["day"] == day]
        return bool(rows)
