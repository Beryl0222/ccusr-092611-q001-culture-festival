"""华服活动编排台的领域对象、状态约定与时间工具。"""
from dataclasses import dataclass
from datetime import datetime, timezone

# ---- 节目生命周期 ----
PROGRAM_DRAFT = "draft"            # 草稿：尚无已生效编排
PROGRAM_IN_REVIEW = "in_review"    # 存在待多方审批的变更
PROGRAM_APPROVED = "approved"      # 编排已生效，等待设备安全确认
PROGRAM_READY = "ready"            # 可进入已发布日程

# ---- 变更请求状态 ----
CHANGE_PENDING = "pending"
CHANGE_APPROVED = "approved"
CHANGE_REJECTED = "rejected"
CHANGE_SUPERSEDED = "superseded"   # 因发布撤回等原因失效

# ---- 发布单状态 ----
PUBLISH_SUCCEEDED = "succeeded"      # 当前有效日程
PUBLISH_SUPERSEDED = "superseded"    # 被更新的发布取代，撤回时可恢复
PUBLISH_ROLLED_BACK = "rolled_back"  # 已撤回，仅用于追溯

# 变更补丁允许修改的字段
PATCHABLE_FIELDS = frozenset({
    "name", "venue_id", "start_at", "end_at",
    "expected_attendance", "requires_safety", "dependencies",
})

# 默认需要会签的角色：运营、场地、制作三方
DEFAULT_APPROVAL_ROLES = ("operations", "venue", "production")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    """解析 ISO8601 时间；无法识别时抛 ValueError。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO8601 字符串")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def intervals_overlap(start_a, end_a, start_b, end_b):
    """半开区间 [start, end) 是否重叠；首尾相接不算冲突。"""
    return start_a < end_b and start_b < end_a


@dataclass(frozen=True)
class Conflict:
    """编排冲突：type 指明类型，affected 逐项列出受影响的节目/场地。"""
    type: str
    message: str
    affected: tuple
    scope: str = "effective"  # effective=与已生效编排冲突, published=发布集合内部冲突

    def to_dict(self):
        return {
            "type": self.type,
            "message": self.message,
            "affected": list(self.affected),
            "scope": self.scope,
        }
