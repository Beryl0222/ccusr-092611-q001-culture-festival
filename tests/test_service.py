import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from culture_festival.api import make_handler
from culture_festival.service import (
    BlockingError,
    ConflictError,
    DomainStore,
    NotFoundError,
    ServiceError,
)

DAY = "2026-10-01"
T = lambda h: f"2026-10-01T{h}:00+08:00"
ROLES = ("operations", "venue", "production")


def approve_all(store, change_id, approver="审批人"):
    view = None
    for role in ROLES:
        view = store.approve_change(change_id, role, f"{approver}-{role}")
    return view


class VenueAndProgramTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore()

    def tearDown(self):
        self.store.close()

    def test_register_venue_idempotent(self):
        a = self.store.register_venue("k-venue-1", "v1", "主展厅", 500)
        b = self.store.register_venue("k-venue-1", "v1", "主展厅", 500)
        self.assertEqual(a, b)
        self.assertEqual(len(self.store.list_venues()), 1)
        with self.assertRaises(ServiceError):  # 同一编号不同内容
            self.store.register_venue("k-venue-2", "v1", "另一个展厅", 10)
        with self.assertRaises(ServiceError):  # 容量非法
            self.store.register_venue("k-venue-3", "v2", "水上舞台", 0)

    def test_create_program_validation(self):
        self.store.register_venue("k1", "v1", "主展厅", 100)
        with self.assertRaises(ServiceError):  # 结束早于开始
            self.store.create_program(
                "k2", "p1", "汉服展演", "owner", "v1", T("10"), T("09"), 10)
        with self.assertRaises(NotFoundError):  # 场地不存在
            self.store.create_program(
                "k3", "p2", "无人机秀", "owner", "nope", T("10"), T("11"), 10)
        with self.assertRaises(ServiceError):  # 人数非法
            self.store.create_program(
                "k4", "p3", "灯光秀", "owner", "v1", T("10"), T("11"), -1)

    def test_capacity_checked_on_activation(self):
        self.store.register_venue("k1", "v1", "主展厅", 100)
        self.store.create_program(
            "k2", "p1", "灯光秀", "owner", "v1", T("10"), T("11"), 999)
        change = self.store.submit_change("k3", "p1", {"name": "灯光秀"})
        self.assertEqual(change["conflicts"][0]["type"], "capacity_exceeded")
        self.assertIn("p1", change["conflicts"][0]["affected"])
        self.assertIn("v1", change["conflicts"][0]["affected"])

    def test_self_dependency_rejected(self):
        self.store.register_venue("k1", "v1", "主展厅", 100)
        with self.assertRaises(ServiceError):
            self.store.create_program(
                "k2", "p1", "展演", "owner", "v1", T("10"), T("11"), 10,
                dependencies=["p1"])

    def test_dependency_cycle_and_missing_rejected(self):
        self.store.register_venue("k1", "v1", "主展厅", 100)
        self.store.create_program(
            "k2", "p1", "展演一", "owner", "v1", T("09"), T("10"), 10,
            requires_safety=False)
        self.store.create_program(
            "k3", "p2", "展演二", "owner", "v1", T("11"), T("12"), 10,
            requires_safety=False, dependencies=["p1"])
        with self.assertRaises(ServiceError):  # p1 -> p2 -> p1 成环
            self.store.submit_change("k4", "p1", {"dependencies": ["p2"]})
        with self.assertRaises(ServiceError):  # 依赖的节目不存在
            self.store.submit_change("k5", "p2", {"dependencies": ["p1", "p3"]})


class ChangeAndApprovalTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore()
        self.store.register_venue("kv", "v1", "主展厅", 100)
        self.store.register_venue("kv2", "v2", "水上舞台", 200)
        self.store.create_program(
            "kp1", "p1", "汉服展演", "owner", "v1",
            T("09"), T("10"), 80, dependencies=[])
        self.store.create_program(
            "kp2", "p2", "无人机灯光秀", "owner", "v2",
            T("11"), T("12"), 150, dependencies=["p1"])

    def tearDown(self):
        self.store.close()

    def _activate_p1(self):
        change = self.store.submit_change("kc1", "p1", {"name": "汉服展演（终稿）"})
        self.assertEqual(change["state"], "pending")
        self.assertEqual(change["conflicts"], [])
        approve_all(self.store, change["change_id"])
        return self.store.get_change(change["change_id"])

    def test_change_requires_all_roles(self):
        change = self.store.submit_change("kc1", "p1", {"name": "汉服展演（终稿）"})
        self.store.approve_change(change["change_id"], "operations", "甲")
        self.assertEqual(self.store.get_change(change["change_id"])["state"], "pending")
        with self.assertRaises(ServiceError):  # 不在审批角色中
            self.store.approve_change(change["change_id"], "boss", "老板")
        with self.assertRaises(ServiceError):  # 同一角色不能换人重复会签
            self.store.approve_change(change["change_id"], "operations", "乙")
        same = self.store.approve_change(change["change_id"], "operations", "甲")
        self.assertEqual(len(same["approvals"]), 1)  # 同一人重复提交结果不变
        self.store.approve_change(change["change_id"], "venue", "乙")
        approved = self.store.approve_change(change["change_id"], "production", "丙")
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["result_version"], 1)
        with self.assertRaises(ServiceError):  # 已终结不能再审批
            self.store.approve_change(change["change_id"], "operations", "甲")
        p1 = self.store.get_program("p1")
        self.assertEqual(p1["current_version"], 1)
        self.assertEqual(p1["state"], "approved")  # 安全未确认，尚不可发布

    def test_change_idempotent_submission(self):
        a = self.store.submit_change("dup-key", "p1", {"expected_attendance": 90})
        b = self.store.submit_change("dup-key", "p1", {"expected_attendance": 90})
        self.assertEqual(a["change_id"], b["change_id"])
        self.assertEqual(len(self.store.list_changes(program_id="p1")), 1)

    def test_reject_change_blocks_effect(self):
        change = self.store.submit_change("kc-r", "p1", {"name": "改名"})
        self.store.reject_change(change["change_id"], "venue", "场地管理员", "时间撞档")
        view = self.store.get_change(change["change_id"])
        self.assertEqual(view["state"], "rejected")
        self.assertEqual(view["reason"], "时间撞档")
        self.assertEqual(self.store.get_program("p1")["current_version"], 0)
        with self.assertRaises(ServiceError):
            self.store.approve_change(change["change_id"], "operations", "甲")

    def test_conflict_double_booking_lists_affected(self):
        done = self._activate_p1()
        self.assertEqual(done["state"], "approved")
        # p2 搬到 v1 09:30，与 p1 重复占用同一场地，且早于依赖节目结束
        change = self.store.submit_change(
            "kc2", "p2",
            {"venue_id": "v1", "start_at": T("09:30"), "end_at": T("10:30")})
        types = {c["type"] for c in change["conflicts"]}
        self.assertIn("double_booking", types)
        self.assertIn("dependency_timing", types)
        booking = next(c for c in change["conflicts"] if c["type"] == "double_booking")
        self.assertEqual(set(booking["affected"]), {"p1", "p2", "v1"})
        # 冲突变更即使走完审批也不能生效
        approve_all(self.store, change["change_id"])
        view = self.store.get_change(change["change_id"])
        self.assertEqual(view["state"], "rejected")
        self.assertIsNone(view["result_version"])
        p2 = self.store.get_program("p2")
        self.assertEqual(p2["venue_id"], "v2")
        self.assertEqual(p2["current_version"], 0)

    def test_capacity_conflict(self):
        self._activate_p1()
        change = self.store.submit_change(
            "kc3", "p2", {"venue_id": "v1", "start_at": T("11"), "end_at": T("12")})
        cap = next(c for c in change["conflicts"] if c["type"] == "capacity_exceeded")
        self.assertIn("p2", cap["affected"])
        self.assertIn("v1", cap["affected"])

    def test_dependency_must_be_scheduled(self):
        # p2 依赖 p1，但 p1 尚未生效
        change = self.store.submit_change("kc4", "p2", {"name": "无人机秀"})
        self.assertTrue(any(
            c["type"] == "dependency_unscheduled" and c["affected"] == ["p2", "p1"]
            for c in change["conflicts"]))

    def test_adjacent_intervals_do_not_conflict(self):
        self._activate_p1()
        c2 = self.store.submit_change("kc6", "p2", {"name": "无人机灯光秀"})
        self.assertEqual(c2["conflicts"], [])
        approve_all(self.store, c2["change_id"])
        # p2 挪到 v1 紧跟 p1 之后：首尾相接不算重复占用
        change = self.store.submit_change(
            "kc7", "p2",
            {"venue_id": "v1", "start_at": T("10"), "end_at": T("11"),
             "expected_attendance": 90})
        self.assertEqual(change["conflicts"], [])

    def test_dependent_program_timing_change_flagged(self):
        self._activate_p1()
        c2 = self.store.submit_change("kc6", "p2", {"name": "无人机灯光秀"})
        approve_all(self.store, c2["change_id"])
        # 把 p1 延后到 11:30 结束，依赖它的 p2 11:00 开始 -> 冲突并指出 p2
        change = self.store.submit_change(
            "kc7", "p1", {"start_at": T("10:30"), "end_at": T("11:30")})
        flagged = [c for c in change["conflicts"] if c["type"] == "dependency_timing"]
        self.assertTrue(any(set(c["affected"]) == {"p2", "p1"} for c in flagged))


class SafetyAndPublishTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore()
        self.store.register_venue("kv", "v1", "主展厅", 500)
        self.store.create_program(
            "kp", "p1", "汉服展演", "owner", "v1",
            T("09"), T("10"), 200, requires_safety=True)

    def tearDown(self):
        self.store.close()

    def _activate(self, key="kc", **patch):
        change = self.store.submit_change(key, "p1", patch or {"name": "汉服展演"})
        approve_all(self.store, change["change_id"])
        return self.store.get_program("p1")

    def test_publish_blocked_without_safety(self):
        self._activate()
        with self.assertRaises(BlockingError) as ctx:
            self.store.publish("kpub", DAY)
        self.assertEqual(ctx.exception.blocked[0]["program_id"], "p1")
        self.assertEqual(ctx.exception.blocked[0]["version"], 1)

    def test_safety_and_publish_flow(self):
        self._activate()
        with self.assertRaises(ServiceError):  # 版本不存在
            self.store.confirm_safety("ks1", "p1", 9, "安全员")
        result = self.store.confirm_safety("ks2", "p1", 1, "安全员老张", "设备巡检通过")
        self.assertEqual(result["safety"], "confirmed")
        again = self.store.confirm_safety("ks2", "p1", 1, "安全员老张", "设备巡检通过")
        self.assertEqual(again, result)  # 幂等
        self.assertEqual(self.store.get_program("p1")["state"], "ready")
        pub = self.store.publish("kpub1", DAY)
        self.assertEqual(len(pub["items"]), 1)
        self.assertEqual(pub["items"][0]["version"], 1)
        same = self.store.publish("kpub1", DAY)  # 重复发布请求只产生一次结果
        self.assertEqual(same["publication_id"], pub["publication_id"])
        self.assertEqual(len(self.store.list_publications(DAY)), 1)
        schedule = self.store.get_schedule(DAY)
        self.assertEqual(schedule["publication_id"], pub["publication_id"])
        self.assertEqual(schedule["items"][0]["venue_name"], "主展厅")

    def test_new_version_invalidates_safety(self):
        self._activate()
        self.store.confirm_safety("ks1", "p1", 1, "安全员")
        self.store.publish("kpub1", DAY)
        self._activate("kc2", end_at=T("10:30"))  # 新版本：安全确认需要重做
        p1 = self.store.get_program("p1")
        self.assertEqual(p1["current_version"], 2)
        self.assertFalse(p1["safety_ok"])
        with self.assertRaises(BlockingError):
            self.store.publish("kpub2", DAY)
        with self.assertRaises(ServiceError):  # 只能确认当前版本
            self.store.confirm_safety("ks2", "p1", 1, "安全员")
        self.store.confirm_safety("ks3", "p1", 2, "安全员")
        self.store.publish("kpub2", DAY)
        self.assertEqual(self.store.get_schedule(DAY)["items"][0]["version"], 2)

    def test_publish_conflict_backstop(self):
        # 绕过审批直接改库制造不一致，验证发布环节的兜底拦截
        self._activate()
        self.store.create_program(
            "kp2", "p2", "无人机灯光秀", "owner", "v1",
            T("11"), T("12"), 100, requires_safety=False)
        change = self.store.submit_change("kc2", "p2", {"name": "无人机灯光秀"})
        approve_all(self.store, change["change_id"])
        self.store.confirm_safety("ks1", "p1", 1, "安全员")
        self.store.connection.execute(
            "UPDATE programs SET start_at=?, end_at=? WHERE program_id='p2'",
            (T("09:30"), T("10:30")))
        self.store.connection.commit()
        with self.assertRaises(ConflictError) as ctx:
            self.store.publish("kpub1", DAY)
        conflict = ctx.exception.conflicts[0]
        self.assertEqual(conflict["type"], "double_booking")
        self.assertEqual(set(conflict["affected"]), {"p1", "p2", "v1"})


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.store = DomainStore()
        self.store.register_venue("kv", "v1", "主展厅", 500)
        self.store.create_program(
            "kp", "p1", "汉服展演", "owner", "v1",
            T("09"), T("10"), 200)

    def tearDown(self):
        self.store.close()

    def _publish_version(self, change_key, pub_key, end_at, safety_key):
        change = self.store.submit_change(change_key, "p1", {"end_at": end_at})
        approve_all(self.store, change["change_id"])
        version = self.store.get_change(change["change_id"])["result_version"]
        self.store.confirm_safety(safety_key, "p1", version, "安全员")
        return self.store.publish(pub_key, DAY)

    def test_rollback_restores_previous_and_keeps_reason(self):
        first = self._publish_version("kc1", "kpub1", T("10"), "ks1")
        second = self._publish_version("kc2", "kpub2", T("10:30"), "ks2")
        self.assertNotEqual(first["publication_id"], second["publication_id"])
        with self.assertRaises(ServiceError):  # 撤回必须给原因
            self.store.rollback("krb0", DAY, "  ")
        result = self.store.rollback("krb1", DAY, "无人机空域临时管制，撤回到上午场方案")
        self.assertEqual(result["rolled_back"], second["publication_id"])
        self.assertEqual(result["restored"], first["publication_id"])
        # 值班核对接口回到最近一次有效版本
        schedule = self.store.get_schedule(DAY)
        self.assertEqual(schedule["publication_id"], first["publication_id"])
        self.assertEqual(schedule["items"][0]["end_at"], T("10"))
        # 撤回原因可追溯
        pubs = {p["publication_id"]: p for p in self.store.list_publications(DAY)}
        self.assertEqual(pubs[second["publication_id"]]["status"], "rolled_back")
        self.assertIn("空域临时管制", pubs[second["publication_id"]]["rolled_back_reason"])
        self.assertIsNotNone(pubs[second["publication_id"]]["rolled_back_at"])
        # 撤回操作本身幂等
        again = self.store.rollback("krb1", DAY, "无人机空域临时管制，撤回到上午场方案")
        self.assertEqual(again["rolled_back"], second["publication_id"])
        # 事件流可审计
        kinds = [e["kind"] for e in self.store.list_events(DAY)]
        self.assertIn("publication_created", kinds)
        self.assertIn("publication_rolled_back", kinds)

    def test_rollback_without_previous_leaves_no_schedule(self):
        self._publish_version("kc1", "kpub1", T("10"), "ks1")
        result = self.store.rollback("krb1", DAY, "场地消防检查")
        self.assertIsNone(result["restored"])
        self.assertEqual(self.store.get_schedule(DAY)["items"], [])
        with self.assertRaises(NotFoundError):  # 已无发布可撤
            self.store.rollback("krb2", DAY, "再次撤回")

    def test_rollback_marks_pending_changes_superseded(self):
        self._publish_version("kc1", "kpub1", T("10"), "ks1")
        pending = self.store.submit_change("kc2", "p1", {"end_at": T("11")})
        self.store.rollback("krb1", DAY, "节目单调整")
        view = self.store.get_change(pending["change_id"])
        self.assertEqual(view["state"], "superseded")
        self.assertIn("节目单调整", view["reason"])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "festival.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_survives_restart(self):
        store = DomainStore(self.path)
        store.register_venue("kv", "v1", "主展厅", 500)
        store.create_program(
            "kp", "p1", "汉服展演", "owner", "v1",
            T("09"), T("10"), 200)
        change = store.submit_change("kc1", "p1", {"end_at": T("10:30")})
        store.approve_change(change["change_id"], "operations", "甲")
        store.approve_change(change["change_id"], "venue", "乙")
        store.close()

        store2 = DomainStore(self.path)  # 重启：审批进度还在
        partial = store2.get_change(change["change_id"])
        self.assertEqual(partial["state"], "pending")
        self.assertEqual(len(partial["approvals"]), 2)
        self.assertEqual(store2.get_program("p1")["current_version"], 0)
        store2.approve_change(change["change_id"], "production", "丙")
        store2.confirm_safety("ks1", "p1", 1, "安全员")
        pub = store2.publish("kpub1", DAY)
        store2.close()

        store3 = DomainStore(self.path)  # 再次重启：发布状态与幂等记录都在
        self.assertEqual(store3.get_program("p1")["state"], "ready")
        schedule = store3.get_schedule(DAY)
        self.assertEqual(schedule["publication_id"], pub["publication_id"])
        self.assertEqual(schedule["items"][0]["end_at"], T("10:30"))
        again = store3.publish("kpub1", DAY)
        self.assertEqual(again["publication_id"], pub["publication_id"])
        self.assertEqual(len(store3.list_publications(DAY)), 1)
        store3.close()


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = DomainStore()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.store))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        status, _ = self._request("POST", "/venues", {
            "request_key": "hk1", "venue_id": "v1", "name": "主展厅", "capacity": 500})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/venues", {  # 重复提交，结果不变
            "request_key": "hk1", "venue_id": "v1", "name": "主展厅", "capacity": 500})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/programs", {
            "request_key": "hk2", "program_id": "p1", "name": "汉服展演",
            "owner_id": "owner", "venue_id": "v1",
            "start_at": T("09"), "end_at": T("10"), "expected_attendance": 200})
        self.assertEqual(status, 200)
        status, change = self._request("POST", "/changes", {
            "request_key": "hk3", "program_id": "p1", "patch": {"end_at": T("10:30")}})
        self.assertEqual(status, 200)
        cid = change["change_id"]
        for role, who in zip(ROLES, ("甲", "乙", "丙")):
            status, body = self._request("POST", f"/changes/{cid}/approvals",
                                         {"role": role, "approver": who})
            self.assertEqual(status, 200)
        self.assertEqual(body["state"], "approved")
        # 未完成安全确认 -> 422，不得进入已发布日程
        status, body = self._request("POST", "/publications",
                                     {"request_key": "hk4", "day": DAY})
        self.assertEqual(status, 422)
        self.assertEqual(body["code"], "blocked")
        self.assertEqual(body["blocked"][0]["program_id"], "p1")
        status, _ = self._request("POST", "/programs/p1/safety-confirmations",
                                  {"request_key": "hk5", "version": 1,
                                   "confirmed_by": "安全员"})
        self.assertEqual(status, 200)
        status, pub = self._request("POST", "/publications",
                                    {"request_key": "hk4", "day": DAY})
        self.assertEqual(status, 200)
        # 值班核对：日程 + 变更记录 + 事件流
        status, schedule = self._request("GET", f"/schedule?day={DAY}")
        self.assertEqual(status, 200)
        self.assertEqual(len(schedule["items"]), 1)
        self.assertEqual(schedule["items"][0]["version"], 1)
        status, changes = self._request("GET", "/changes?program_id=p1&state=approved")
        self.assertEqual(status, 200)
        self.assertEqual(changes["items"][0]["result_version"], 1)
        status, rb = self._request("POST", "/publications/rollback",
                                   {"request_key": "hk6", "day": DAY,
                                    "reason": "临时天气原因"})
        self.assertEqual(status, 200)
        self.assertIsNone(rb["restored"])
        status, events = self._request("GET", f"/events?entity_id={DAY}")
        self.assertEqual(status, 200)
        self.assertTrue(any(e["kind"] == "publication_rolled_back" for e in events["items"]))

    def test_error_mapping(self):
        status, body = self._request("GET", "/programs/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["code"], "not_found")
        status, body = self._request("POST", "/venues", {"request_key": "x"})
        self.assertEqual(status, 400)
        status, body = self._request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
