"""华服活动编排台的持久化边界和状态服务。

职责边界：
- 节目按版本编排：每次被接受的变更生成新版本，审批与安全确认按版本重新收集；
- 幂等：同一 request_key 的重复提交只执行一次，重放返回首次结果（含被拒绝的结果）；
- 安全门控：安全确认或审批未集齐的节目不得进入已发布日程；
- 冲突检测：发布时整体校验，同场地时段重叠即拒绝，并明确指出受影响的节目；
- 撤回：撤回发布必须填写原因并留痕，自动恢复该日期最近一次被取代的有效发布；
- 持久化：全部状态存于 SQLite，事务保证整次写入要么成功要么回滚，重启后保留。
"""
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager

from .domain import (DEFAULT_REQUIRED_PARTIES, KIND_LABELS, PROGRAM_KINDS,
                     Record, parse_date, parse_time, utc_now)


class ServiceError(Exception):
    def __init__(self, message, status=400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records(
  record_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  state TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(record_id) REFERENCES records(record_id)
);
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY,
  result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venues(
  venue_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  capacity INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs(
  program_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  party_size INTEGER NOT NULL,
  required_parties TEXT NOT NULL,
  safety_items TEXT NOT NULL,
  state TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS program_versions(
  program_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  venue_id TEXT,
  start_at TEXT,
  end_at TEXT,
  party_size INTEGER NOT NULL,
  note TEXT NOT NULL,
  request_key TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(program_id, version)
);
CREATE TABLE IF NOT EXISTS approvals(
  program_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  party TEXT NOT NULL,
  approver_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(program_id, version, party)
);
CREATE TABLE IF NOT EXISTS safety_confirmations(
  program_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  item TEXT NOT NULL,
  confirmer_id TEXT NOT NULL,
  request_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(program_id, version, item)
);
CREATE TABLE IF NOT EXISTS publications(
  publish_id TEXT PRIMARY KEY,
  festival_date TEXT NOT NULL,
  seq INTEGER NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  request_key TEXT NOT NULL,
  details TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_items(
  publish_id TEXT NOT NULL,
  program_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  venue_id TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  party_size INTEGER NOT NULL,
  PRIMARY KEY(publish_id, program_id)
);
"""

# 节目状态：pending=待确认 approved=已集齐审批与安全确认 published=已发布
CHANGE_EVENT_KINDS = ("change_accepted", "change_rejected")


def _ok(result):
    return {"ok": True, "result": result}


def _err(exc):
    return {"ok": False, "error": str(exc), "status": exc.status, "details": exc.details}


class DomainStore:
    def __init__(self, database=":memory:", clock=utc_now):
        self.connection = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.lock = threading.RLock()
        self._txn_depth = 0
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """可重入事务：最外层 BEGIN IMMEDIATE，失败整体回滚。"""
        with self.lock:
            if self._txn_depth > 0:
                self._txn_depth += 1
                try:
                    yield
                finally:
                    self._txn_depth -= 1
                return
            self._txn_depth = 1
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                self._txn_depth = 0

    def close(self):
        self.connection.close()

    # ------------------------------------------------------------------
    # 通用记录（保留给既有调用方）
    # ------------------------------------------------------------------
    def create(self, record_id, owner_id, payload=None):
        with self.transaction():
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (record_id, owner_id, "draft", 1, json.dumps(payload or {}, ensure_ascii=False), self.clock()))
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                (record_id + ":created", record_id, "created", "{}", self.clock()))
        return self.get(record_id)

    def get(self, record_id):
        row = self.connection.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise ServiceError("记录不存在", 404)
        return Record(row["record_id"], row["owner_id"], row["state"], row["version"], row["updated_at"])

    def transition(self, record_id, owner_id, target, request_key, expected_version=None):
        with self.transaction():
            old = self.connection.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
            if old is None:
                raise ServiceError("记录不存在", 404)
            if old["owner_id"] != owner_id:
                raise ServiceError("无权操作", 403)
            cached = self.connection.execute(
                "SELECT result FROM idempotency WHERE request_key=?", (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            if expected_version is not None and old["version"] != expected_version:
                raise ServiceError("版本冲突", 409)
            allowed = {"draft": {"pending"}, "pending": {"approved", "cancelled"},
                       "approved": {"closed"}, "cancelled": set(), "closed": set()}
            if target not in allowed.get(old["state"], set()):
                raise ServiceError("状态迁移不允许", 409)
            version = old["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id))
            body = json.dumps({"from": old["state"], "to": target, "version": version}, ensure_ascii=False)
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)", (request_key + ":event", record_id, "transition", body, now))
            result = {"record_id": record_id, "state": target, "version": version}
            self.connection.execute(
                "INSERT INTO idempotency VALUES(?,?)", (request_key, json.dumps(result, ensure_ascii=False)))
            return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _key(prefix, request_key):
        if not request_key or not str(request_key).strip():
            raise ServiceError("缺少 request_key（幂等键）")
        return f"{prefix}:{str(request_key).strip()}"

    def _idempotent(self, request_key, operation):
        """同一 request_key 只执行一次；重复提交返回首次结果（含被拒绝的结果）。"""
        cached = self.connection.execute(
            "SELECT result FROM idempotency WHERE request_key=?", (request_key,)).fetchone()
        if cached is not None:
            return json.loads(cached["result"])
        try:
            envelope = _ok(operation())
        except ServiceError as exc:
            envelope = _err(exc)
        self.connection.execute(
            "INSERT INTO idempotency VALUES(?,?)", (request_key, json.dumps(envelope, ensure_ascii=False)))
        return envelope

    def _event(self, record_id, kind, body):
        self.connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, record_id, kind, json.dumps(body, ensure_ascii=False), self.clock()))

    def _date_or_400(self, festival_date):
        try:
            return parse_date(festival_date)
        except ValueError as exc:
            raise ServiceError(str(exc))

    def _program_exists(self, program_id):
        return self.connection.execute(
            "SELECT 1 FROM programs WHERE program_id=?", (program_id,)).fetchone() is not None

    def _program_row(self, program_id):
        row = self.connection.execute(
            "SELECT * FROM programs WHERE program_id=?", (program_id,)).fetchone()
        if row is None:
            raise ServiceError(f"节目不存在: {program_id}", 404)
        return row

    def _version_row(self, program_id, version):
        row = self.connection.execute(
            "SELECT * FROM program_versions WHERE program_id=? AND version=?",
            (program_id, version)).fetchone()
        if row is None:
            raise ServiceError(f"节目版本不存在: {program_id} v{version}", 404)
        return row

    def _eligibility(self, program_id, version=None):
        """返回 {eligible, missing_approvals, missing_safety}，按版本判定。"""
        row = self._program_row(program_id)
        version = version or row["current_version"]
        required = set(json.loads(row["required_parties"]))
        items = set(json.loads(row["safety_items"]))
        got_parties = {r["party"] for r in self.connection.execute(
            "SELECT party FROM approvals WHERE program_id=? AND version=?", (program_id, version))}
        got_items = {r["item"] for r in self.connection.execute(
            "SELECT item FROM safety_confirmations WHERE program_id=? AND version=?", (program_id, version))}
        return {
            "eligible": required <= got_parties and items <= got_items,
            "missing_approvals": sorted(required - got_parties),
            "missing_safety": sorted(items - got_items),
        }

    def _set_program_state(self, program_id, state, kind=None, body=None):
        now = self.clock()
        self.connection.execute(
            "UPDATE programs SET state=?, updated_at=? WHERE program_id=?", (state, now, program_id))
        self.connection.execute(
            "UPDATE records SET state=?, updated_at=? WHERE record_id=?", (state, now, program_id))
        if kind:
            self._event(program_id, kind, body or {})

    def _refresh_eligibility_state(self, program_id):
        """审批与安全确认集齐后 pending→approved。"""
        row = self._program_row(program_id)
        if row["state"] not in ("pending", "approved"):
            return
        target = "approved" if self._eligibility(program_id)["eligible"] else "pending"
        if target != row["state"]:
            self._set_program_state(program_id, target, "state_changed", {
                "from": row["state"], "to": target, "version": row["current_version"],
                "reason": "审批与安全确认已集齐" if target == "approved" else "确认不再完整"})

    # ------------------------------------------------------------------
    # 场地
    # ------------------------------------------------------------------
    def add_venue(self, venue_id, name, kind, capacity, request_key=None):
        key = f"venue:create:{request_key or venue_id}"
        with self.transaction():
            return self._idempotent(key, lambda: self._add_venue(venue_id, name, kind, capacity))

    def _add_venue(self, venue_id, name, kind, capacity):
        if not venue_id or not str(venue_id).strip():
            raise ServiceError("缺少 venue_id")
        if kind not in PROGRAM_KINDS:
            raise ServiceError(f"场地类型无效: {kind}（可选: {', '.join(PROGRAM_KINDS)}）")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ServiceError("场地容量必须为正整数")
        try:
            self.connection.execute(
                "INSERT INTO venues VALUES(?,?,?,?,?)",
                (str(venue_id).strip(), str(name or venue_id), kind, capacity, self.clock()))
        except sqlite3.IntegrityError:
            raise ServiceError(f"场地已存在: {venue_id}", 409)
        return self._venue_view(venue_id)

    def _venue_view(self, venue_id):
        row = self.connection.execute("SELECT * FROM venues WHERE venue_id=?", (venue_id,)).fetchone()
        return dict(row)

    def list_venues(self):
        with self.lock:
            rows = self.connection.execute("SELECT * FROM venues ORDER BY venue_id").fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 节目
    # ------------------------------------------------------------------
    def create_program(self, program_id, owner_id, title, kind, party_size,
                       required_parties=None, safety_items=(), request_key=None):
        key = f"program:create:{request_key or program_id}"
        with self.transaction():
            return self._idempotent(key, lambda: self._create_program(
                program_id, owner_id, title, kind, party_size, required_parties, safety_items, request_key))

    def _create_program(self, program_id, owner_id, title, kind, party_size,
                        required_parties, safety_items, request_key):
        if not program_id or not str(program_id).strip():
            raise ServiceError("缺少 program_id")
        if not owner_id or not str(owner_id).strip():
            raise ServiceError("缺少 owner_id（节目负责人）")
        if kind not in PROGRAM_KINDS:
            raise ServiceError(f"节目类型无效: {kind}（可选: {', '.join(PROGRAM_KINDS)}）")
        if not isinstance(party_size, int) or party_size <= 0:
            raise ServiceError("团队人数必须为正整数")
        parties = list(required_parties) if required_parties else list(DEFAULT_REQUIRED_PARTIES[kind])
        if not parties:
            raise ServiceError("审批方不能为空")
        items = list(safety_items) if safety_items else []
        if not items:
            raise ServiceError("安全确认项不能为空")
        program_id = str(program_id).strip()
        title = str(title or program_id)
        now = self.clock()
        try:
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (program_id, owner_id, "pending", 1,
                 json.dumps({"title": title, "kind": kind}, ensure_ascii=False), now))
            self.connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)", (program_id + ":created", program_id, "created", "{}", now))
        except sqlite3.IntegrityError:
            raise ServiceError(f"节目已存在: {program_id}", 409)
        self.connection.execute(
            "INSERT INTO programs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (program_id, owner_id, title, kind, party_size,
             json.dumps(parties, ensure_ascii=False), json.dumps(items, ensure_ascii=False),
             "pending", 1, now, now))
        self.connection.execute(
            "INSERT INTO program_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (program_id, 1, None, None, None, party_size, "", request_key or "", now))
        self._event(program_id, "program_registered", {
            "kind": kind, "title": title, "required_parties": parties, "safety_items": items})
        return self.program_view(program_id)

    def program_view(self, program_id):
        with self.lock:
            row = self._program_row(program_id)
            version = row["current_version"]
            ver = self._version_row(program_id, version)
            elig = self._eligibility(program_id, version)
            approvals = [dict(r) for r in self.connection.execute(
                "SELECT party, approver_id, created_at FROM approvals "
                "WHERE program_id=? AND version=? ORDER BY party", (program_id, version))]
            safety = [dict(r) for r in self.connection.execute(
                "SELECT item, confirmer_id, created_at FROM safety_confirmations "
                "WHERE program_id=? AND version=? ORDER BY item", (program_id, version))]
            slot = None
            if ver["venue_id"] is not None:
                slot = {"venue_id": ver["venue_id"], "start_at": ver["start_at"], "end_at": ver["end_at"]}
            return {
                "program_id": row["program_id"],
                "owner_id": row["owner_id"],
                "title": row["title"],
                "kind": row["kind"],
                "state": row["state"],
                "current_version": version,
                "party_size": ver["party_size"],
                "slot": slot,
                "required_parties": json.loads(row["required_parties"]),
                "safety_items": json.loads(row["safety_items"]),
                "approvals": approvals,
                "safety_confirmations": safety,
                "missing_approvals": elig["missing_approvals"],
                "missing_safety": elig["missing_safety"],
                "eligible": elig["eligible"],
                "updated_at": row["updated_at"],
            }

    def list_programs(self):
        with self.lock:
            ids = [r["program_id"] for r in self.connection.execute(
                "SELECT program_id FROM programs ORDER BY program_id")]
            return [self.program_view(pid) for pid in ids]

    # ------------------------------------------------------------------
    # 变更请求（每次被接受的变更生成新版本）
    # ------------------------------------------------------------------
    def submit_change(self, program_id, actor_id, request_key, venue_id=None,
                      start_at=None, end_at=None, party_size=None, note="", expected_version=None):
        key = self._key("change", request_key)
        with self.transaction():
            fresh = self.connection.execute(
                "SELECT 1 FROM idempotency WHERE request_key=?", (key,)).fetchone() is None
            envelope = self._idempotent(key, lambda: self._apply_change(
                program_id, actor_id, request_key, venue_id, start_at, end_at,
                party_size, note, expected_version))
            if fresh and not envelope["ok"] and self._program_exists(program_id):
                # 被拒绝的变更同样留痕，值班人员可核对"哪次调整没生效、为什么"。
                self._event(program_id, "change_rejected", {
                    "request_key": request_key, "actor_id": actor_id, "error": envelope["error"]})
            return envelope

    def _apply_change(self, program_id, actor_id, request_key, venue_id,
                      start_at, end_at, party_size, note, expected_version):
        if not actor_id or not str(actor_id).strip():
            raise ServiceError("缺少 actor_id（提交人）")
        row = self._program_row(program_id)
        if row["owner_id"] != actor_id:
            raise ServiceError("只有节目负责人可以提交变更", 403)
        if expected_version is not None and expected_version != row["current_version"]:
            raise ServiceError(
                f"版本冲突: 期望 v{expected_version}, 当前 v{row['current_version']}", 409)
        if venue_id is None and start_at is None and end_at is None and party_size is None:
            raise ServiceError("变更内容为空")
        cur = self._version_row(program_id, row["current_version"])
        new_venue = venue_id if venue_id is not None else cur["venue_id"]
        try:
            new_start = parse_time(start_at) if start_at is not None else cur["start_at"]
            new_end = parse_time(end_at) if end_at is not None else cur["end_at"]
        except ValueError as exc:
            raise ServiceError(str(exc))
        new_size = party_size if party_size is not None else cur["party_size"]
        if not isinstance(new_size, int) or new_size <= 0:
            raise ServiceError("团队人数必须为正整数")
        if new_venue is not None:
            if new_start is None or new_end is None:
                raise ServiceError("场地与时间必须同时提供")
            venue = self.connection.execute(
                "SELECT * FROM venues WHERE venue_id=?", (new_venue,)).fetchone()
            if venue is None:
                raise ServiceError(f"场地不存在: {new_venue}", 404)
            if venue["kind"] != row["kind"]:
                raise ServiceError(
                    f"场地类型不匹配: {KIND_LABELS[row['kind']]}节目不能使用{KIND_LABELS[venue['kind']]}场地")
            if new_start >= new_end:
                raise ServiceError("开始时间必须早于结束时间")
            if new_size > venue["capacity"]:
                raise ServiceError(
                    f"超出场地容量: {new_size}人 > {venue['capacity']}人", 409,
                    {"venue_id": new_venue, "capacity": venue["capacity"], "party_size": new_size})
        version = row["current_version"] + 1
        now = self.clock()
        self.connection.execute(
            "INSERT INTO program_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (program_id, version, new_venue, new_start, new_end, new_size, str(note or ""), request_key, now))
        self.connection.execute(
            "UPDATE programs SET current_version=?, party_size=?, state='pending', updated_at=? "
            "WHERE program_id=?", (version, new_size, now, program_id))
        self.connection.execute(
            "UPDATE records SET state='pending', version=?, updated_at=? WHERE record_id=?",
            (version, now, program_id))
        self._event(program_id, "change_accepted", {
            "request_key": request_key, "actor_id": actor_id, "version": version,
            "slot": {"venue_id": new_venue, "start_at": new_start, "end_at": new_end} if new_venue else None,
            "party_size": new_size, "note": str(note or "")})
        return self.program_view(program_id)

    # ------------------------------------------------------------------
    # 安全确认与多方审批（按当前版本收集）
    # ------------------------------------------------------------------
    def confirm_safety(self, program_id, item, confirmer_id, request_key):
        key = self._key("safety", request_key)
        with self.transaction():
            return self._idempotent(key, lambda: self._apply_safety(
                program_id, item, confirmer_id, request_key))

    def _apply_safety(self, program_id, item, confirmer_id, request_key):
        if not confirmer_id or not str(confirmer_id).strip():
            raise ServiceError("缺少 confirmer_id（确认人）")
        row = self._program_row(program_id)
        items = json.loads(row["safety_items"])
        if item not in items:
            raise ServiceError(f"未知的安全确认项: {item}（需要: {', '.join(items)}）")
        version = row["current_version"]
        try:
            self.connection.execute(
                "INSERT INTO safety_confirmations VALUES(?,?,?,?,?,?)",
                (program_id, version, item, confirmer_id, request_key, self.clock()))
        except sqlite3.IntegrityError:
            return self.program_view(program_id)  # 该版本此项已确认过
        self._event(program_id, "safety_confirmed", {
            "version": version, "item": item, "confirmer_id": confirmer_id, "request_key": request_key})
        self._refresh_eligibility_state(program_id)
        return self.program_view(program_id)

    def grant_approval(self, program_id, party, approver_id, request_key):
        key = self._key("approval", request_key)
        with self.transaction():
            return self._idempotent(key, lambda: self._apply_approval(
                program_id, party, approver_id, request_key))

    def _apply_approval(self, program_id, party, approver_id, request_key):
        if not approver_id or not str(approver_id).strip():
            raise ServiceError("缺少 approver_id（审批人）")
        row = self._program_row(program_id)
        required = json.loads(row["required_parties"])
        if party not in required:
            raise ServiceError(f"该节目不需要 {party} 方审批（需要: {', '.join(required)}）")
        version = row["current_version"]
        try:
            self.connection.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?)",
                (program_id, version, party, approver_id, request_key, self.clock()))
        except sqlite3.IntegrityError:
            return self.program_view(program_id)  # 该版本此方已审批过
        self._event(program_id, "approval_granted", {
            "version": version, "party": party, "approver_id": approver_id, "request_key": request_key})
        self._refresh_eligibility_state(program_id)
        return self.program_view(program_id)

    # ------------------------------------------------------------------
    # 发布与撤回
    # ------------------------------------------------------------------
    def publish_schedule(self, festival_date, request_key, actor_id=""):
        key = self._key("publish", request_key)
        with self.transaction():
            return self._idempotent(key, lambda: self._apply_publish(
                festival_date, request_key, actor_id))

    def _apply_publish(self, festival_date, request_key, actor_id):
        festival_date = self._date_or_400(festival_date)
        rows = self.connection.execute(
            "SELECT p.program_id, p.title, p.kind, p.current_version, "
            "v.venue_id, v.start_at, v.end_at, v.party_size "
            "FROM programs p JOIN program_versions v "
            "ON v.program_id=p.program_id AND v.version=p.current_version "
            "WHERE v.venue_id IS NOT NULL AND substr(v.start_at,1,10)=? "
            "ORDER BY v.start_at, p.program_id", (festival_date,)).fetchall()
        if not rows:
            raise ServiceError(f"{festival_date} 没有已编排的节目", 404)
        blockers = []
        for r in rows:
            elig = self._eligibility(r["program_id"])
            if not elig["eligible"]:
                blockers.append({
                    "program_id": r["program_id"], "title": r["title"],
                    "missing_approvals": elig["missing_approvals"],
                    "missing_safety": elig["missing_safety"]})
        conflicts = self._conflicts(rows)
        if blockers or conflicts:
            details = {"blockers": blockers, "conflicts": conflicts}
            self._record_publication(festival_date, request_key, "rejected",
                                     "存在未确认节目或场地冲突", details, actor_id)
            raise ServiceError("发布被拒绝: 存在未确认节目或场地冲突", 409, details)
        active = self._active_publication(festival_date)
        if active is not None:
            self.connection.execute(
                "UPDATE publications SET status='superseded' WHERE publish_id=?", (active["publish_id"],))
        publish_id = self._record_publication(festival_date, request_key, "active", "", {}, actor_id)
        new_ids = set()
        for r in rows:
            new_ids.add(r["program_id"])
            self.connection.execute(
                "INSERT INTO publication_items VALUES(?,?,?,?,?,?,?)",
                (publish_id, r["program_id"], r["current_version"], r["venue_id"],
                 r["start_at"], r["end_at"], r["party_size"]))
            self._set_program_state(r["program_id"], "published", "program_published", {
                "publish_id": publish_id, "version": r["current_version"], "festival_date": festival_date})
        if active is not None:
            for it in self._publication_items(active["publish_id"]):
                if it["program_id"] not in new_ids:
                    elig = self._eligibility(it["program_id"])
                    self._set_program_state(
                        it["program_id"], "approved" if elig["eligible"] else "pending",
                        "state_changed", {"from": "published", "reason": f"被新发布 {publish_id} 取代"})
        return {"published": True, "publish_id": publish_id, "festival_date": festival_date,
                "superseded": active["publish_id"] if active is not None else None,
                "items": self._publication_items(publish_id)}

    def _conflicts(self, rows):
        """同一场地时段重叠即冲突，逐对列出受影响节目。"""
        conflicts = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["venue_id"] != b["venue_id"]:
                    continue
                if a["start_at"] < b["end_at"] and b["start_at"] < a["end_at"]:
                    conflicts.append({
                        "type": "slot_overlap",
                        "venue_id": a["venue_id"],
                        "programs": [
                            {"program_id": a["program_id"], "title": a["title"],
                             "version": a["current_version"],
                             "start_at": a["start_at"], "end_at": a["end_at"]},
                            {"program_id": b["program_id"], "title": b["title"],
                             "version": b["current_version"],
                             "start_at": b["start_at"], "end_at": b["end_at"]}],
                        "message": f"场地 {a['venue_id']} 时段重叠: {a['program_id']} 与 {b['program_id']}"})
        return conflicts

    def _record_publication(self, festival_date, request_key, status, reason, details, actor_id=""):
        seq = self.connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM publications WHERE festival_date=?",
            (festival_date,)).fetchone()["seq"]
        publish_id = f"pub-{festival_date}-{seq:03d}"
        payload = dict(details)
        if actor_id:
            payload["actor_id"] = actor_id
        self.connection.execute(
            "INSERT INTO publications VALUES(?,?,?,?,?,?,?,?)",
            (publish_id, festival_date, seq, status, reason, request_key,
             json.dumps(payload, ensure_ascii=False), self.clock()))
        return publish_id

    def _active_publication(self, festival_date):
        return self.connection.execute(
            "SELECT * FROM publications WHERE festival_date=? AND status='active' "
            "ORDER BY seq DESC LIMIT 1", (festival_date,)).fetchone()

    def _publication_items(self, publish_id):
        rows = self.connection.execute(
            "SELECT i.program_id, i.version, i.venue_id, i.start_at, i.end_at, i.party_size, "
            "p.title, p.kind, v.name AS venue_name "
            "FROM publication_items i "
            "JOIN programs p ON p.program_id=i.program_id "
            "LEFT JOIN venues v ON v.venue_id=i.venue_id "
            "WHERE i.publish_id=? ORDER BY i.start_at, i.program_id", (publish_id,)).fetchall()
        return [dict(r) for r in rows]

    def withdraw_schedule(self, festival_date, reason, request_key, actor_id=""):
        key = self._key("withdraw", request_key)
        with self.transaction():
            return self._idempotent(key, lambda: self._apply_withdraw(
                festival_date, reason, request_key, actor_id))

    def _apply_withdraw(self, festival_date, reason, request_key, actor_id):
        festival_date = self._date_or_400(festival_date)
        if not reason or not str(reason).strip():
            raise ServiceError("撤回必须填写原因")
        reason = str(reason).strip()
        active = self._active_publication(festival_date)
        if active is None:
            raise ServiceError(f"{festival_date} 没有生效中的发布", 404)
        self.connection.execute(
            "UPDATE publications SET status='withdrawn', reason=? WHERE publish_id=?",
            (reason, active["publish_id"]))
        prev = self.connection.execute(
            "SELECT * FROM publications WHERE festival_date=? AND status='superseded' "
            "ORDER BY seq DESC LIMIT 1", (festival_date,)).fetchone()
        restored_id = None
        if prev is not None:
            restored_id = prev["publish_id"]
            self.connection.execute(
                "UPDATE publications SET status='active', reason=? WHERE publish_id=?",
                (f"撤回后恢复: {reason}", restored_id))
        affected_items = self._publication_items(active["publish_id"])
        restored_items = {it["program_id"]: it
                          for it in (self._publication_items(restored_id) if restored_id else [])}
        union_ids = sorted({it["program_id"] for it in affected_items} | set(restored_items))
        restored_programs, version_skew = [], []
        for pid in union_ids:
            row = self._program_row(pid)
            snap = restored_items.get(pid)
            if snap is not None:
                # 发布快照是冻结的历史有效版本，恢复日程即恢复该快照；
                # 若当前工作版本已更新，只做偏差提示，不阻断恢复。
                if row["current_version"] != snap["version"]:
                    version_skew.append({"program_id": pid, "snapshot_version": snap["version"],
                                         "current_version": row["current_version"]})
                if row["state"] != "published":
                    self._set_program_state(pid, "published", "program_restored", {
                        "publish_id": restored_id, "version": snap["version"], "reason": reason})
                restored_programs.append(pid)
                continue
            target = "approved" if self._eligibility(pid)["eligible"] else "pending"
            if row["state"] != target:
                self._set_program_state(pid, target, "state_changed", {
                    "from": row["state"], "to": target,
                    "reason": f"发布 {active['publish_id']} 已撤回: {reason}"})
        for it in affected_items:
            self._event(it["program_id"], "publication_withdrawn", {
                "publish_id": active["publish_id"], "reason": reason,
                "restored_publish_id": restored_id, "request_key": request_key})
        return {"withdrawn": active["publish_id"], "festival_date": festival_date, "reason": reason,
                "restored_publish_id": restored_id, "restored_programs": restored_programs,
                "version_skew": version_skew, "schedule": self.get_schedule(festival_date)}

    # ------------------------------------------------------------------
    # 查询（值班核对）
    # ------------------------------------------------------------------
    def get_schedule(self, festival_date):
        with self.lock:
            festival_date = self._date_or_400(festival_date)
            pub = self._active_publication(festival_date)
            if pub is None:
                return {"festival_date": festival_date, "status": "unpublished",
                        "publish_id": None, "published_at": None, "items": []}
            return {"festival_date": festival_date, "status": "published",
                    "publish_id": pub["publish_id"], "published_at": pub["created_at"],
                    "items": self._publication_items(pub["publish_id"])}

    def list_changes(self, program_id=None, date=None):
        """变更记录；date 按"节目当前编排落在该日期"过滤，便于核对当天安排。"""
        with self.lock:
            sql = ("SELECT e.* FROM events e "
                   "WHERE e.kind IN ('change_accepted','change_rejected')")
            args = []
            if program_id:
                sql += " AND e.record_id=?"
                args.append(program_id)
            if date:
                sql += (" AND e.record_id IN ("
                        "SELECT p.program_id FROM programs p JOIN program_versions v "
                        "ON v.program_id=p.program_id AND v.version=p.current_version "
                        "WHERE v.venue_id IS NOT NULL AND substr(v.start_at,1,10)=?)")
                args.append(self._date_or_400(date))
            sql += " ORDER BY e.created_at, e.event_id"
            return [{"event_id": r["event_id"], "record_id": r["record_id"], "kind": r["kind"],
                     "body": json.loads(r["body"]), "created_at": r["created_at"]}
                    for r in self.connection.execute(sql, args)]

    def list_events(self, program_id):
        with self.lock:
            self._program_row(program_id)
            rows = self.connection.execute(
                "SELECT * FROM events WHERE record_id=? ORDER BY created_at, event_id",
                (program_id,)).fetchall()
            return [{"event_id": r["event_id"], "kind": r["kind"],
                     "body": json.loads(r["body"]), "created_at": r["created_at"]} for r in rows]

    def list_publications(self, festival_date):
        with self.lock:
            festival_date = self._date_or_400(festival_date)
            rows = self.connection.execute(
                "SELECT * FROM publications WHERE festival_date=? ORDER BY seq",
                (festival_date,)).fetchall()
            return [{"publish_id": r["publish_id"], "festival_date": r["festival_date"],
                     "seq": r["seq"], "status": r["status"], "reason": r["reason"],
                     "request_key": r["request_key"], "details": json.loads(r["details"]),
                     "created_at": r["created_at"],
                     "items": self._publication_items(r["publish_id"])} for r in rows]

    def duty_view(self, festival_date):
        """一次调用返回值班核对所需的日程、变更记录与发布历史。"""
        festival_date = self._date_or_400(festival_date)
        return {"festival_date": festival_date,
                "schedule": self.get_schedule(festival_date),
                "changes": self.list_changes(date=festival_date),
                "publications": self.list_publications(festival_date)}


# ----------------------------------------------------------------------
# 纯文本视图（无页面核对）
# ----------------------------------------------------------------------
def _hm(iso):
    return iso[11:16] if iso else "--:--"


def render_schedule_text(schedule):
    head = f"华服嘉年华日程 {schedule['festival_date']} 状态:{schedule['status']}"
    if schedule.get("publish_id"):
        head += f" 发布号:{schedule['publish_id']} 发布时间:{schedule['published_at']}"
    lines = [head]
    if not schedule["items"]:
        lines.append("（暂无已发布节目）")
    for it in schedule["items"]:
        lines.append(
            f"{_hm(it['start_at'])}-{_hm(it['end_at'])} | {it['title']}({it['program_id']}) "
            f"v{it['version']} | {KIND_LABELS.get(it['kind'], it['kind'])} | "
            f"场地:{it.get('venue_name') or it['venue_id']} | {it['party_size']}人")
    return "\n".join(lines) + "\n"


def render_changes_text(changes):
    lines = [f"变更记录（共{len(changes)}条）:"]
    for ch in changes:
        body = ch["body"]
        if ch["kind"] == "change_accepted":
            detail = f"已生效 版本:v{body.get('version')} 提交人:{body.get('actor_id')}"
        else:
            detail = f"被拒绝 原因:{body.get('error')}"
        lines.append(f"{ch['created_at']} | {ch['record_id']} | {detail} | 请求:{body.get('request_key')}")
    if len(lines) == 1:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def render_publications_text(publications):
    lines = [f"发布历史（共{len(publications)}条）:"]
    for pub in publications:
        extra = f" 原因:{pub['reason']}" if pub["reason"] else ""
        lines.append(
            f"{pub['publish_id']} | {pub['status']} | {pub['created_at']} | "
            f"节目数:{len(pub['items'])}{extra}")
    if len(lines) == 1:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def render_duty_text(view):
    return (render_schedule_text(view["schedule"]) + "\n"
            + render_changes_text(view["changes"]) + "\n"
            + render_publications_text(view["publications"]))
