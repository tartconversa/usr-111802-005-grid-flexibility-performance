"""响应事件的报名承诺和生产停线日历。"""
from decimal import Decimal
class ResponseRegistry:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS commitments(event TEXT, site TEXT, target TEXT, PRIMARY KEY(event,site))")
        connection.execute("CREATE TABLE IF NOT EXISTS shutdowns(site TEXT, day TEXT, reason TEXT, PRIMARY KEY(site,day))")

    def commit(self, event, site, target):
        if Decimal(str(target)) <= 0: raise ValueError("承诺量必须为正")
        self.connection.execute("INSERT OR REPLACE INTO commitments VALUES(?,?,?)", (event,site,str(Decimal(str(target)))))

    def shutdown(self, site, day, reason):
        self.connection.execute("INSERT OR REPLACE INTO shutdowns VALUES(?,?,?)", (site,day,reason))

    def is_shutdown(self, site, day):
        return bool(self.connection.execute("SELECT 1 FROM shutdowns WHERE site=? AND day=?", (site,day)).fetchone())
