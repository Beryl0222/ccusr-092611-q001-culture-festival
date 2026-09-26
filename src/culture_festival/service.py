"""华服活动编排台的持久化边界和状态服务。

职责：节目版本、场地容量、设备安全前置条件、多方审批、发布与撤回。
所有写入包在 SQLite 事务里；同一 request_key 重复提交只生效一次；
数据库落盘，服务重启后审批与发布状态保留。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from .domain import (
    CHANGE_APPROVED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    CHANGE_SUPERSEDED,
    DEFAULT_APPROVAL_ROLES,
    PATCHABLE_FIELDS,
    PROGRAM_APPROVED,
    PROGRAM_DRAFT,
    PROGRAM_IN_REVIEW,
    PROGRAM_READY,
    PUBLISH_ROLLED_BACK,
    PUBLISH_SUCCEEDED,
    PUBLISH_SUPERSEDED,
    Conflict,
    intervals_overlap,
    parse_time,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues(
    venue_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs(
    program_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    expected_attendance INTEGER NOT NULL,
    requires_safety INTEGER NOT NULL,
    dependencies TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    safety_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes(
    change_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    program_id TEXT NOT NULL REFERENCES programs(program_id),
    patch TEXT NOT NULL,
    state TEXT NOT NULL,
    conflicts TEXT NOT NULL,
    reason TEXT,
    base_version INTEGER NOT NULL,
    result_version INTEGER,
    created_by TEXT,
    decided_by_role TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals(
    change_id TEXT NOT NULL REFERENCES changes(change_id),
    role TEXT NOT NULL,
    approver TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY(change_id, role)
);
CREATE TABLE IF NOT EXISTS safety_confirmations(
    program_id TEXT NOT NULL REFERENCES programs(program_id),
    version INTEGER NOT NULL,
    confirmed_by TEXT NOT NULL,
    note TEXT,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY(program_id, version)
);
CREATE TABLE IF NOT EXISTS publications(
    publication_id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    rolled_back_reason TEXT,
    created_at TEXT NOT NULL,
    rolled_back_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
    request_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ServiceError(Exception):
    """业务校验失败，HTTP 层映射为 400。"""


class NotFoundError(ServiceError):
    """目标记录不存在，HTTP 层映射为 404。"""


class ConflictError(ServiceError):
    """编排冲突：conflicts 逐项列出受影响项目，HTTP 层映射为 409。"""

    def __init__(self, message, conflicts):
        super().__init__(message)
        self.conflicts = [c.to_dict() if isinstance(c, Conflict) else c for c in conflicts]


class BlockingError(ServiceError):
    """发布被设备安全前置条件阻断，HTTP 层映射为 422。"""

    def __init__(self, message, blocked):
        super().__init__(message)
        self.blocked = blocked


class DomainStore:
    """编排台核心服务：每个公开方法都是原子的，重复请求只生效一次。"""

    def __init__(self, database=":memory:", clock=utc_now, approval_roles=DEFAULT_APPROVAL_ROLES):
        self.clock = clock
        self.approval_roles = tuple(approval_roles)
        if database != ":memory:":
            directory = os.path.dirname(os.path.abspath(database))
            os.makedirs(directory, exist_ok=True)
        self.connection = sqlite3.connect(database, check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                self.connection.rollback()
                raise ServiceError(f"数据约束冲突: {exc}") from exc
            except Exception:
                self.connection.rollback()
                raise

    # ---- 内部工具 ----

    def _emit(self, kind, entity_id, body):
        self.connection.execute(
            "INSERT INTO events(event_id, kind, entity_id, body, created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, kind, entity_id, json.dumps(body, ensure_ascii=False), self.clock()),
        )

    def _idempotent(self, request_key, operation, action):
        """同一 request_key 全库只执行一次 action；重复提交返回首次响应。"""
        if not request_key or not str(request_key).strip():
            raise ServiceError("缺少 request_key，无法保证重复提交只生效一次")
        with self.transaction():
            row = self.connection.execute(
                "SELECT response FROM idempotency WHERE request_key=?", (request_key,)
            ).fetchone()
            if row is not None:
                return json.loads(row["response"])
            result = dict(action())
            result.setdefault("operation", operation)
            self.connection.execute(
                "INSERT INTO idempotency(request_key, operation, response, created_at) VALUES(?,?,?,?)",
                (request_key, operation, json.dumps(result, ensure_ascii=False), self.clock()),
            )
            return result

    def _require_program(self, program_id):
        row = self.connection.execute(
            "SELECT * FROM programs WHERE program_id=?", (program_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"节目不存在: {program_id}")
        return row

    def _require_venue(self, venue_id):
        row = self.connection.execute(
            "SELECT * FROM venues WHERE venue_id=?", (venue_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"场地不存在: {venue_id}")
        return row

    def _require_change(self, change_id):
        row = self.connection.execute(
            "SELECT * FROM changes WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"变更请求不存在: {change_id}")
        return row

    @staticmethod
    def _validate_day(day):
        if not isinstance(day, str):
            raise ServiceError("日期必须是 YYYY-MM-DD 字符串")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError as exc:
            raise ServiceError("日期必须是合法的 YYYY-MM-DD") from exc

    def _validate_spec(self, program_id, spec):
        if not isinstance(spec.get("name"), str) or not spec["name"].strip():
            raise ServiceError("节目名称不能为空")
        try:
            start = parse_time(spec["start_at"])
            end = parse_time(spec["end_at"])
        except ValueError as exc:
            raise ServiceError(f"时间格式无效: {exc}") from exc
        if end <= start:
            raise ServiceError("结束时间必须晚于开始时间")
        attendance = spec["expected_attendance"]
        if isinstance(attendance, bool) or not isinstance(attendance, int) or attendance < 0:
            raise ServiceError("预计人数必须是非负整数")
        deps = spec["dependencies"]
        if not isinstance(deps, (list, tuple)) or any(
            not isinstance(d, str) or not d.strip() for d in deps
        ):
            raise ServiceError("依赖列表必须是节目编号数组")
        if len(set(deps)) != len(deps):
            raise ServiceError("依赖列表存在重复节目")
        if program_id in deps:
            raise ServiceError("节目不能依赖自身")

    def _check_dependencies(self, program_id, deps):
        """依赖的节目必须存在，且依赖关系不得成环。"""
        rows = self.connection.execute("SELECT program_id, dependencies FROM programs").fetchall()
        graph = {r["program_id"]: set(json.loads(r["dependencies"])) for r in rows}
        graph[program_id] = set(deps)
        missing = sorted(d for d in deps if d not in graph)
        if missing:
            raise ServiceError(f"依赖的节目不存在: {', '.join(missing)}")
        visiting, visited, stack = set(), set(), []

        def dfs(node):
            if node in visiting:
                cycle = stack[stack.index(node):] + [node]
                raise ServiceError(f"依赖关系成环: {' -> '.join(cycle)}")
            if node in visited:
                return
            visiting.add(node)
            stack.append(node)
            for nxt in graph.get(node, ()):
                dfs(nxt)
            stack.pop()
            visiting.discard(node)
            visited.add(node)

        dfs(program_id)

    def _evaluate_conflicts(self, program_id, spec):
        """候选编排与全库已生效编排（已批准/已发布版本）的冲突评估。"""
        conflicts = []
        venue = self._require_venue(spec["venue_id"])
        if spec["expected_attendance"] > venue["capacity"]:
            conflicts.append(Conflict(
                "capacity_exceeded",
                f"预计人数 {spec['expected_attendance']} 超出场地「{venue['name']}」容量 {venue['capacity']}",
                (program_id, spec["venue_id"]),
            ))
        start = parse_time(spec["start_at"])
        end = parse_time(spec["end_at"])
        others = self.connection.execute(
            "SELECT * FROM programs WHERE program_id != ? AND current_version > 0", (program_id,)
        ).fetchall()
        by_id = {r["program_id"]: r for r in others}
        for other in others:
            if other["venue_id"] == spec["venue_id"] and intervals_overlap(
                start, end, parse_time(other["start_at"]), parse_time(other["end_at"])
            ):
                conflicts.append(Conflict(
                    "double_booking",
                    f"与「{other['name']}」重复占用场地「{venue['name']}」"
                    f"（{other['start_at']} ~ {other['end_at']}）",
                    (program_id, other["program_id"], spec["venue_id"]),
                ))
        for dep_id in spec["dependencies"]:
            dep = by_id.get(dep_id) or self._require_program(dep_id)
            if dep["current_version"] == 0:
                conflicts.append(Conflict(
                    "dependency_unscheduled",
                    f"依赖的节目「{dep['name']}」尚无已生效编排",
                    (program_id, dep_id),
                ))
            elif parse_time(dep["end_at"]) > start:
                conflicts.append(Conflict(
                    "dependency_timing",
                    f"开始时间早于依赖节目「{dep['name']}」的结束时间 {dep['end_at']}",
                    (program_id, dep_id),
                ))
        for other in others:
            if program_id in json.loads(other["dependencies"]) and parse_time(other["start_at"]) < end:
                conflicts.append(Conflict(
                    "dependency_timing",
                    f"「{other['name']}」依赖本节目，调整后本节目结束时间 {spec['end_at']} 晚于其开始时间",
                    (other["program_id"], program_id),
                ))
        return conflicts

    def _refresh_program_state(self, program_id):
        program = self._require_program(program_id)
        pending = self.connection.execute(
            "SELECT 1 FROM changes WHERE program_id=? AND state=? LIMIT 1",
            (program_id, CHANGE_PENDING),
        ).fetchone()
        if pending:
            state = PROGRAM_IN_REVIEW
        elif program["current_version"] == 0:
            state = PROGRAM_DRAFT
        elif program["requires_safety"] and program["safety_version"] < program["current_version"]:
            state = PROGRAM_APPROVED
        else:
            state = PROGRAM_READY
        self.connection.execute(
            "UPDATE programs SET state=?, updated_at=? WHERE program_id=?",
            (state, self.clock(), program_id),
        )

    # ---- 场地 ----

    def register_venue(self, request_key, venue_id, name, capacity):
        def action():
            if not venue_id or not str(venue_id).strip():
                raise ServiceError("场地编号不能为空")
            if not name or not str(name).strip():
                raise ServiceError("场地名称不能为空")
            if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
                raise ServiceError("场地容量必须为正整数")
            if self.connection.execute(
                "SELECT 1 FROM venues WHERE venue_id=?", (venue_id,)
            ).fetchone():
                raise ServiceError(f"场地已存在: {venue_id}")
            self.connection.execute(
                "INSERT INTO venues(venue_id, name, capacity, created_at) VALUES(?,?,?,?)",
                (venue_id, name, capacity, self.clock()),
            )
            self._emit("venue_registered", venue_id, {"name": name, "capacity": capacity})
            return {"venue_id": venue_id, "name": name, "capacity": capacity}

        return self._idempotent(request_key, "register_venue", action)

    def list_venues(self):
        rows = self.connection.execute("SELECT * FROM venues ORDER BY venue_id").fetchall()
        return [dict(r) for r in rows]

    # ---- 节目 ----

    def create_program(self, request_key, program_id, name, owner_id, venue_id,
                       start_at, end_at, expected_attendance=0,
                       requires_safety=True, dependencies=()):
        spec = {
            "name": name,
            "venue_id": venue_id,
            "start_at": start_at,
            "end_at": end_at,
            "expected_attendance": expected_attendance,
            "requires_safety": bool(requires_safety),
            "dependencies": list(dependencies or []),
        }

        def action():
            if not program_id or not str(program_id).strip():
                raise ServiceError("节目编号不能为空")
            if not owner_id or not str(owner_id).strip():
                raise ServiceError("节目负责人不能为空")
            if self.connection.execute(
                "SELECT 1 FROM programs WHERE program_id=?", (program_id,)
            ).fetchone():
                raise ServiceError(f"节目已存在: {program_id}")
            self._require_venue(venue_id)
            self._validate_spec(program_id, spec)
            self._check_dependencies(program_id, spec["dependencies"])
            now = self.clock()
            self.connection.execute(
                """INSERT INTO programs(program_id, name, owner_id, state, venue_id, start_at,
                   end_at, expected_attendance, requires_safety, dependencies,
                   current_version, safety_version, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (program_id, name, owner_id, PROGRAM_DRAFT, venue_id, start_at, end_at,
                 expected_attendance, int(spec["requires_safety"]),
                 json.dumps(sorted(spec["dependencies"])), 0, 0, now, now),
            )
            self._emit("program_created", program_id, {"name": name, "owner_id": owner_id})
            return {"program_id": program_id, "state": PROGRAM_DRAFT, "current_version": 0}

        return self._idempotent(request_key, "create_program", action)

    def get_program(self, program_id):
        return self._program_view(self._require_program(program_id))

    def list_programs(self, day=None):
        if day is not None:
            self._validate_day(day)
            rows = self.connection.execute(
                "SELECT * FROM programs WHERE substr(start_at, 1, 10)=? ORDER BY start_at, program_id",
                (day,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM programs ORDER BY program_id"
            ).fetchall()
        return [self._program_view(r) for r in rows]

    def _program_view(self, p):
        requires_safety = bool(p["requires_safety"])
        safety_ok = p["current_version"] > 0 and (
            not requires_safety or p["safety_version"] >= p["current_version"]
        )
        return {
            "program_id": p["program_id"],
            "name": p["name"],
            "owner_id": p["owner_id"],
            "state": p["state"],
            "venue_id": p["venue_id"],
            "start_at": p["start_at"],
            "end_at": p["end_at"],
            "expected_attendance": p["expected_attendance"],
            "requires_safety": requires_safety,
            "dependencies": json.loads(p["dependencies"]),
            "current_version": p["current_version"],
            "safety_version": p["safety_version"],
            "safety_ok": safety_ok,
            "created_at": p["created_at"],
            "updated_at": p["updated_at"],
        }

    # ---- 变更请求与多方审批 ----

    def submit_change(self, request_key, program_id, patch, actor=None):
        def action():
            program = self._require_program(program_id)
            if not isinstance(patch, dict) or not patch:
                raise ServiceError("变更内容不能为空")
            unknown = sorted(set(patch) - PATCHABLE_FIELDS)
            if unknown:
                raise ServiceError(f"不支持的变更字段: {', '.join(unknown)}")
            merged = self._merged_spec(program, patch)
            self._require_venue(merged["venue_id"])
            self._validate_spec(program_id, merged)
            self._check_dependencies(program_id, merged["dependencies"])
            conflicts = self._evaluate_conflicts(program_id, merged)
            change_id = uuid.uuid4().hex
            self.connection.execute(
                """INSERT INTO changes(change_id, request_key, program_id, patch, state, conflicts,
                   base_version, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (change_id, request_key, program_id,
                 json.dumps(patch, ensure_ascii=False, sort_keys=True), CHANGE_PENDING,
                 json.dumps([c.to_dict() for c in conflicts], ensure_ascii=False),
                 program["current_version"], actor, self.clock()),
            )
            self._refresh_program_state(program_id)
            self._emit("change_submitted", program_id, {
                "change_id": change_id,
                "request_key": request_key,
                "patch": patch,
                "conflicts": [c.to_dict() for c in conflicts],
            })
            return self._change_view(change_id)

        return self._idempotent(request_key, "submit_change", action)

    def approve_change(self, change_id, role, approver):
        with self.transaction():
            change = self._require_change(change_id)
            if change["state"] != CHANGE_PENDING:
                raise ServiceError(f"变更已终结（{change['state']}），无法审批")
            if role not in self.approval_roles:
                raise ServiceError(f"角色 {role} 不在审批角色 {list(self.approval_roles)} 中")
            if not approver or not str(approver).strip():
                raise ServiceError("审批人不能为空")
            existing = self.connection.execute(
                "SELECT approver FROM approvals WHERE change_id=? AND role=?", (change_id, role)
            ).fetchone()
            if existing is not None:
                if existing["approver"] == approver:
                    return self._change_view(change_id)  # 重复提交，结果不变
                raise ServiceError(f"角色 {role} 已审批过该变更")
            self.connection.execute(
                "INSERT INTO approvals(change_id, role, approver, decided_at) VALUES(?,?,?,?)",
                (change_id, role, approver, self.clock()),
            )
            self._emit("change_approval", change["program_id"],
                       {"change_id": change_id, "role": role, "approver": approver})
            approved = {r["role"] for r in self.connection.execute(
                "SELECT role FROM approvals WHERE change_id=?", (change_id,)
            ).fetchall()}
            if all(r in approved for r in self.approval_roles):
                self._apply_change(change)
            return self._change_view(change_id)

    def reject_change(self, change_id, role, approver, reason=None):
        with self.transaction():
            change = self._require_change(change_id)
            if change["state"] != CHANGE_PENDING:
                raise ServiceError(f"变更已终结（{change['state']}），无法驳回")
            if role not in self.approval_roles:
                raise ServiceError(f"角色 {role} 不在审批角色 {list(self.approval_roles)} 中")
            if not approver or not str(approver).strip():
                raise ServiceError("审批人不能为空")
            now = self.clock()
            self.connection.execute(
                """UPDATE changes SET state=?, reason=?, decided_by_role=?, decided_by=?,
                   decided_at=? WHERE change_id=?""",
                (CHANGE_REJECTED, reason or f"{role} 驳回了变更", role, approver, now, change_id),
            )
            self._emit("change_rejected", change["program_id"], {
                "change_id": change_id, "role": role, "approver": approver, "reason": reason,
            })
            self._refresh_program_state(change["program_id"])
            return self._change_view(change_id)

    def _merged_spec(self, program, patch):
        merged = {
            "name": program["name"],
            "venue_id": program["venue_id"],
            "start_at": program["start_at"],
            "end_at": program["end_at"],
            "expected_attendance": program["expected_attendance"],
            "requires_safety": bool(program["requires_safety"]),
            "dependencies": json.loads(program["dependencies"]),
        }
        merged.update(patch)
        return merged

    def _apply_change(self, change):
        """审批齐全后生效：以当前版本为基准合并补丁，重新校验后生成新版本。"""
        program = self._require_program(change["program_id"])
        patch = json.loads(change["patch"])
        merged = self._merged_spec(program, patch)
        self._require_venue(merged["venue_id"])
        self._validate_spec(change["program_id"], merged)
        self._check_dependencies(change["program_id"], merged["dependencies"])
        conflicts = self._evaluate_conflicts(change["program_id"], merged)
        now = self.clock()
        if conflicts:
            self.connection.execute(
                "UPDATE changes SET state=?, conflicts=?, reason=?, decided_at=? WHERE change_id=?",
                (CHANGE_REJECTED, json.dumps([c.to_dict() for c in conflicts], ensure_ascii=False),
                 "审批通过但生效时存在编排冲突", now, change["change_id"]),
            )
            self._emit("change_rejected", change["program_id"], {
                "change_id": change["change_id"],
                "reason": "审批通过但生效时存在编排冲突",
                "conflicts": [c.to_dict() for c in conflicts],
            })
        else:
            version = program["current_version"] + 1
            self.connection.execute(
                """UPDATE programs SET name=?, venue_id=?, start_at=?, end_at=?,
                   expected_attendance=?, requires_safety=?, dependencies=?,
                   current_version=?, updated_at=? WHERE program_id=?""",
                (merged["name"], merged["venue_id"], merged["start_at"], merged["end_at"],
                 merged["expected_attendance"], int(merged["requires_safety"]),
                 json.dumps(sorted(merged["dependencies"])), version, now, change["program_id"]),
            )
            self.connection.execute(
                "UPDATE changes SET state=?, conflicts=?, result_version=?, decided_at=? WHERE change_id=?",
                (CHANGE_APPROVED, "[]", version, now, change["change_id"]),
            )
            self._emit("change_applied", change["program_id"], {
                "change_id": change["change_id"],
                "version": version,
                "spec": merged,
            })
        self._refresh_program_state(change["program_id"])

    def get_change(self, change_id):
        return self._change_view(change_id)

    def list_changes(self, program_id=None, state=None):
        sql = "SELECT change_id FROM changes"
        clauses, params = [], []
        if program_id is not None:
            clauses.append("program_id=?")
            params.append(program_id)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, rowid"
        rows = self.connection.execute(sql, params).fetchall()
        return [self._change_view(r["change_id"]) for r in rows]

    def _change_view(self, change_id):
        change = self._require_change(change_id)
        approvals = self.connection.execute(
            "SELECT role, approver, decided_at FROM approvals WHERE change_id=? ORDER BY rowid",
            (change["change_id"],),
        ).fetchall()
        return {
            "change_id": change["change_id"],
            "request_key": change["request_key"],
            "program_id": change["program_id"],
            "patch": json.loads(change["patch"]),
            "state": change["state"],
            "conflicts": json.loads(change["conflicts"]),
            "reason": change["reason"],
            "base_version": change["base_version"],
            "result_version": change["result_version"],
            "required_roles": list(self.approval_roles),
            "approvals": [dict(a) for a in approvals],
            "created_by": change["created_by"],
            "decided_by_role": change["decided_by_role"],
            "decided_by": change["decided_by"],
            "created_at": change["created_at"],
            "decided_at": change["decided_at"],
        }

    # ---- 设备安全前置条件 ----

    def confirm_safety(self, request_key, program_id, version, confirmed_by, note=None):
        def action():
            program = self._require_program(program_id)
            if not confirmed_by or not str(confirmed_by).strip():
                raise ServiceError("安全确认人不能为空")
            if not program["requires_safety"]:
                raise ServiceError("该节目未声明设备安全前置条件")
            if not isinstance(version, int) or version < 1 or version > program["current_version"]:
                raise ServiceError(f"版本 {version} 不存在")
            if version != program["current_version"]:
                raise ServiceError("只能对当前生效版本做安全确认")
            existing = self.connection.execute(
                "SELECT * FROM safety_confirmations WHERE program_id=? AND version=?",
                (program_id, version),
            ).fetchone()
            if existing is not None:
                return {
                    "program_id": program_id,
                    "version": version,
                    "safety": "confirmed",
                    "confirmed_by": existing["confirmed_by"],
                    "confirmed_at": existing["confirmed_at"],
                }
            now = self.clock()
            self.connection.execute(
                """INSERT INTO safety_confirmations(program_id, version, confirmed_by, note, confirmed_at)
                   VALUES(?,?,?,?,?)""",
                (program_id, version, confirmed_by, note, now),
            )
            self.connection.execute(
                "UPDATE programs SET safety_version=?, updated_at=? WHERE program_id=?",
                (version, now, program_id),
            )
            self._emit("safety_confirmed", program_id,
                       {"version": version, "confirmed_by": confirmed_by, "note": note})
            self._refresh_program_state(program_id)
            return {
                "program_id": program_id,
                "version": version,
                "safety": "confirmed",
                "confirmed_by": confirmed_by,
                "confirmed_at": now,
            }

        return self._idempotent(request_key, "confirm_safety", action)

    # ---- 发布与撤回 ----

    def publish(self, request_key, day):
        self._validate_day(day)

        def action():
            programs = self.connection.execute(
                "SELECT * FROM programs WHERE substr(start_at, 1, 10)=? AND current_version > 0"
                " ORDER BY start_at, program_id",
                (day,),
            ).fetchall()
            blocked = [
                {
                    "program_id": p["program_id"],
                    "name": p["name"],
                    "version": p["current_version"],
                    "reason": "设备安全确认未完成",
                }
                for p in programs
                if p["requires_safety"] and p["safety_version"] < p["current_version"]
            ]
            if blocked:
                raise BlockingError("存在未完成安全确认的节目，不得进入已发布日程", blocked)
            items = [self._schedule_item(p) for p in programs]
            if not items:
                raise ServiceError("当天没有可发布的节目")
            conflicts = self._evaluate_set_conflicts(items)
            if conflicts:
                raise ConflictError("待发布编排存在冲突", conflicts)
            now = self.clock()
            self.connection.execute(
                "UPDATE publications SET status=? WHERE day=? AND status=?",
                (PUBLISH_SUPERSEDED, day, PUBLISH_SUCCEEDED),
            )
            publication_id = uuid.uuid4().hex
            self.connection.execute(
                """INSERT INTO publications(publication_id, day, status, snapshot, created_at)
                   VALUES(?,?,?,?,?)""",
                (publication_id, day, PUBLISH_SUCCEEDED,
                 json.dumps(items, ensure_ascii=False), now),
            )
            self._emit("publication_created", day, {
                "publication_id": publication_id,
                "program_ids": [i["program_id"] for i in items],
            })
            return {"publication_id": publication_id, "day": day, "items": items}

        return self._idempotent(request_key, "publish", action)

    def rollback(self, request_key, day, reason):
        self._validate_day(day)
        if not reason or not str(reason).strip():
            raise ServiceError("撤回发布必须填写可追溯的原因")

        def action():
            current = self.connection.execute(
                "SELECT * FROM publications WHERE day=? AND status=?",
                (day, PUBLISH_SUCCEEDED),
            ).fetchone()
            if current is None:
                raise NotFoundError("当天没有已发布的日程")
            now = self.clock()
            self.connection.execute(
                """UPDATE publications SET status=?, rolled_back_reason=?, rolled_back_at=?
                   WHERE publication_id=?""",
                (PUBLISH_ROLLED_BACK, reason, now, current["publication_id"]),
            )
            restored = self.connection.execute(
                "SELECT * FROM publications WHERE day=? AND status=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (day, PUBLISH_SUPERSEDED),
            ).fetchone()
            restored_id = None
            if restored is not None:
                self.connection.execute(
                    "UPDATE publications SET status=? WHERE publication_id=?",
                    (PUBLISH_SUCCEEDED, restored["publication_id"]),
                )
                restored_id = restored["publication_id"]
            # 受影响节目的待审变更基于已撤回的编排，一并置为失效
            superseded_changes = []
            for item in json.loads(current["snapshot"]):
                rows = self.connection.execute(
                    "SELECT change_id FROM changes WHERE program_id=? AND state=?",
                    (item["program_id"], CHANGE_PENDING),
                ).fetchall()
                for row in rows:
                    self.connection.execute(
                        "UPDATE changes SET state=?, reason=?, decided_at=? WHERE change_id=?",
                        (CHANGE_SUPERSEDED, f"发布撤回：{reason}", now, row["change_id"]),
                    )
                    superseded_changes.append(row["change_id"])
                self._refresh_program_state(item["program_id"])
            self._emit("publication_rolled_back", day, {
                "publication_id": current["publication_id"],
                "reason": reason,
                "restored_publication_id": restored_id,
                "superseded_changes": superseded_changes,
            })
            return {
                "day": day,
                "rolled_back": current["publication_id"],
                "reason": reason,
                "restored": restored_id,
                "superseded_changes": superseded_changes,
            }

        return self._idempotent(request_key, "rollback", action)

    def get_schedule(self, day):
        """值班核对用：当天当前有效的已发布日程。"""
        self._validate_day(day)
        row = self.connection.execute(
            "SELECT * FROM publications WHERE day=? AND status=?", (day, PUBLISH_SUCCEEDED)
        ).fetchone()
        if row is None:
            return {"day": day, "publication_id": None, "published_at": None, "items": []}
        return {
            "day": day,
            "publication_id": row["publication_id"],
            "published_at": row["created_at"],
            "items": json.loads(row["snapshot"]),
        }

    def list_publications(self, day=None):
        if day is not None:
            self._validate_day(day)
            rows = self.connection.execute(
                "SELECT * FROM publications WHERE day=? ORDER BY created_at, rowid", (day,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM publications ORDER BY created_at, rowid"
            ).fetchall()
        return [
            {
                "publication_id": r["publication_id"],
                "day": r["day"],
                "status": r["status"],
                "items": json.loads(r["snapshot"]),
                "rolled_back_reason": r["rolled_back_reason"],
                "created_at": r["created_at"],
                "rolled_back_at": r["rolled_back_at"],
            }
            for r in rows
        ]

    def _schedule_item(self, p):
        venue = self.connection.execute(
            "SELECT name, capacity FROM venues WHERE venue_id=?", (p["venue_id"],)
        ).fetchone()
        return {
            "program_id": p["program_id"],
            "name": p["name"],
            "owner_id": p["owner_id"],
            "version": p["current_version"],
            "venue_id": p["venue_id"],
            "venue_name": venue["name"] if venue else None,
            "start_at": p["start_at"],
            "end_at": p["end_at"],
            "expected_attendance": p["expected_attendance"],
            "requires_safety": bool(p["requires_safety"]),
            "safety_version": p["safety_version"],
            "dependencies": json.loads(p["dependencies"]),
        }

    def _evaluate_set_conflicts(self, items):
        """发布集合内部的一致性校验（兜底：正常流程下批准环节已拦截）。"""
        conflicts = []
        by_id = {i["program_id"]: i for i in items}
        for index, a in enumerate(items):
            venue = self._require_venue(a["venue_id"])
            if a["expected_attendance"] > venue["capacity"]:
                conflicts.append(Conflict(
                    "capacity_exceeded",
                    f"「{a['name']}」预计人数 {a['expected_attendance']} 超出场地「{venue['name']}」容量",
                    (a["program_id"], a["venue_id"]),
                    scope="published",
                ))
            for b in items[index + 1:]:
                if a["venue_id"] == b["venue_id"] and intervals_overlap(
                    parse_time(a["start_at"]), parse_time(a["end_at"]),
                    parse_time(b["start_at"]), parse_time(b["end_at"]),
                ):
                    conflicts.append(Conflict(
                        "double_booking",
                        f"「{a['name']}」与「{b['name']}」重复占用场地「{venue['name']}」",
                        (a["program_id"], b["program_id"], a["venue_id"]),
                        scope="published",
                    ))
        for item in items:
            for dep_id in item["dependencies"]:
                target = by_id.get(dep_id)
                if target is not None:
                    if parse_time(target["end_at"]) > parse_time(item["start_at"]):
                        conflicts.append(Conflict(
                            "dependency_timing",
                            f"「{item['name']}」开始时间早于依赖节目「{target['name']}」的结束时间",
                            (item["program_id"], dep_id),
                            scope="published",
                        ))
                    continue
                dep = self.connection.execute(
                    "SELECT * FROM programs WHERE program_id=?", (dep_id,)
                ).fetchone()
                if dep is None or dep["current_version"] == 0:
                    conflicts.append(Conflict(
                        "dependency_unscheduled",
                        f"「{item['name']}」依赖的节目 {dep_id} 没有已生效编排",
                        (item["program_id"], dep_id),
                        scope="published",
                    ))
                elif parse_time(dep["end_at"]) > parse_time(item["start_at"]):
                    conflicts.append(Conflict(
                        "dependency_timing",
                        f"「{item['name']}」开始时间早于依赖节目「{dep['name']}」的结束时间",
                        (item["program_id"], dep_id),
                        scope="published",
                    ))
        return conflicts

    # ---- 审计事件 ----

    def list_events(self, entity_id=None, kind=None, limit=200):
        sql = "SELECT * FROM events"
        clauses, params = [], []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        rows = self.connection.execute(sql, params).fetchall()
        return [
            {
                "seq": r["seq"],
                "event_id": r["event_id"],
                "kind": r["kind"],
                "entity_id": r["entity_id"],
                "body": json.loads(r["body"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
