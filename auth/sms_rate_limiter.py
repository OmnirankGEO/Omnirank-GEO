"""短信发送频率限制器（内存版，单worker安全）"""
import time
from collections import defaultdict

class SMSRateLimiter:
    """短信发送频率限制器"""

    def __init__(self):
        self._phone_records = defaultdict(list)  # phone -> [timestamp, ...]
        self._ip_records = defaultdict(list)      # ip -> [timestamp, ...]

    def _clean_old(self, records: list, window: int):
        """清理超出时间窗口的记录"""
        now = time.time()
        while records and records[0] < now - window:
            records.pop(0)

    def check_phone(self, phone: str) -> tuple:
        """检查手机号是否可以发送，返回 (ok, msg)"""
        records = self._phone_records[phone]
        now = time.time()

        self._clean_old(records, 86400)

        # 60秒冷却
        if records and now - records[-1] < 60:
            remaining = int(60 - (now - records[-1]))
            return False, f"请{remaining}秒后再试"

        # 1小时最多5次
        hour_count = sum(1 for t in records if t > now - 3600)
        if hour_count >= 5:
            return False, "该号码发送过于频繁，请1小时后再试"

        # 24小时最多10次
        if len(records) >= 10:
            return False, "该号码今日发送次数已达上限"

        return True, ""

    def check_ip(self, ip: str) -> tuple:
        """检查IP是否可以发送，返回 (ok, msg)"""
        records = self._ip_records[ip]
        now = time.time()

        self._clean_old(records, 86400)

        # 1分钟最多3次
        minute_count = sum(1 for t in records if t > now - 60)
        if minute_count >= 3:
            return False, "请求过于频繁，请稍后再试"

        # 1小时最多20次
        hour_count = sum(1 for t in records if t > now - 3600)
        if hour_count >= 20:
            return False, "请求过于频繁，请1小时后再试"

        return True, ""

    def record(self, phone: str, ip: str):
        """记录一次发送"""
        now = time.time()
        self._phone_records[phone].append(now)
        self._ip_records[ip].append(now)


# 全局单例
sms_limiter = SMSRateLimiter()
