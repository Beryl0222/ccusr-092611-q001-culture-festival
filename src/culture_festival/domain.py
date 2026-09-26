"""华服活动编排台中的基础对象。"""
from dataclasses import dataclass
from datetime import datetime, timezone

# 节目/场地类型：展览、舞台、无人机灯光秀。
PROGRAM_KINDS = ("exhibition", "stage", "drone")
KIND_LABELS = {"exhibition": "展览", "stage": "舞台", "drone": "无人机灯光秀"}

# 各类节目发布前必须集齐的审批方；安全确认项按节目单独配置。
DEFAULT_REQUIRED_PARTIES = {
    "exhibition": ("site", "safety"),
    "stage": ("stage_manager", "safety"),
    "drone": ("airspace", "safety", "tech"),
}


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    """解析 ISO 8601 时间；缺时区按 UTC 处理，统一返回 UTC ISO 字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间不能为空")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"时间格式无效: {value}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def parse_date(value):
    """校验并归一化 YYYY-MM-DD 日期。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("日期不能为空")
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"日期格式应为 YYYY-MM-DD: {value}")
    return day.strftime("%Y-%m-%d")
