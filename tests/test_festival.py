"""华服活动编排台的功能测试：版本、幂等、冲突、安全门控、撤回与持久化。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid

from culture_festival.api import create_server
from culture_festival.service import DomainStore

DATE = "2026-10-01"


def make_store():
    store = DomainStore()
    for venue_id, name, kind, capacity in (
            ("V-STAGE", "主舞台", "stage", 500),
            ("V-EXH", "展厅A", "exhibition", 200),
            ("V-DRONE", "夜空场", "drone", 1000)):
        env = store.add_venue(venue_id, name, kind, capacity)
        assert env["ok"], env
    return store


def add_program(store, pid, kind="stage", size=100, owner="owner-1"):
    env = store.create_program(pid, owner, f"节目{pid}", kind, size,
                               safety_items=["fire_check", "power_check"])
    assert env["ok"], env
    return env


def slot_change(store, pid, venue, start, end, key=None, **kw):
    env = store.submit_change(
        pid, "owner-1", key or f"chg-{uuid.uuid4().hex[:8]}",
        venue_id=venue, start_at=f"{DATE}T{start}:00+00:00",
        end_at=f"{DATE}T{end}:00+00:00", **kw)
    assert env["ok"], env
    return env


def confirm_all(store, pid):
    view = store.program_view(pid)
    for item in view["safety_items"]:
        env = store.confirm_safety(pid, item, "safety-officer", f"sf-{pid}-{item}-{view['current_version']}")
        assert env["ok"], env
    for party in view["required_parties"]:
        env = store.grant_approval(pid, party, f"lead-{party}", f"ap-{pid}-{party}-{view['current_version']}")
        assert env["ok"], env
    return store.program_view(pid)


def ready_program(store, pid, venue, start, end, kind="stage", size=100):
    add_program(store, pid, kind=kind, size=size)
    slot_change(store, pid, venue, start, end)
    return confirm_all(store, pid)


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        self.store.close()

    def test_change_creates_version_and_resets_confirmations(self):
        add_program(self.store, "P1")
        confirm_all(self.store, "P1")
        self.assertEqual(self.store.program_view("P1")["state"], "approved")
        env = slot_change(self.store, "P1", "V-STAGE", "10:00", "11:00")
        view = env["result"]
        self.assertEqual(view["current_version"], 2)
        self.assertEqual(view["state"], "pending")
        self.assertEqual(view["slot"]["venue_id"], "V-STAGE")
        self.assertEqual(view["missing_safety"], ["fire_check", "power_check"])
        self.assertTrue(view["missing_approvals"])

    def test_duplicate_request_produces_single_result(self):
        add_program(self.store, "P1")
        first = slot_change(self.store, "P1", "V-STAGE", "10:00", "11:00", key="req-dup")
        second = self.store.submit_change(
            "P1", "owner-1", "req-dup", venue_id="V-STAGE",
            start_at=f"{DATE}T10:00:00+00:00", end_at=f"{DATE}T11:00:00+00:00")
        self.assertEqual(first, second)
        self.assertEqual(self.store.program_view("P1")["current_version"], 2)
        accepted = [e for e in self.store.list_events("P1") if e["kind"] == "change_accepted"]
        self.assertEqual(len(accepted), 1)

    def test_rejected_change_replays_and_is_logged(self):
        add_program(self.store, "P1", size=100)
        env1 = self.store.submit_change(
            "P1", "owner-1", "req-cap", venue_id="V-STAGE",
            start_at=f"{DATE}T10:00:00+00:00", end_at=f"{DATE}T11:00:00+00:00",
            party_size=600)
        self.assertFalse(env1["ok"])
        self.assertEqual(env1["status"], 409)
        env2 = self.store.submit_change(
            "P1", "owner-1", "req-cap", venue_id="V-STAGE",
            start_at=f"{DATE}T10:00:00+00:00", end_at=f"{DATE}T11:00:00+00:00",
            party_size=600)
        self.assertEqual(env1, env2)
        self.assertEqual(self.store.program_view("P1")["current_version"], 1)
        rejected = [e for e in self.store.list_events("P1") if e["kind"] == "change_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("容量", rejected[0]["body"]["error"])

    def test_change_validation(self):
        add_program(self.store, "P1")
        not_owner = self.store.submit_change("P1", "someone-else", "req-x1", party_size=50)
        self.assertEqual(not_owner["status"], 403)
        empty = self.store.submit_change("P1", "owner-1", "req-x2")
        self.assertFalse(empty["ok"])
        bad_venue = self.store.submit_change(
            "P1", "owner-1", "req-x3", venue_id="V-EXH",
            start_at=f"{DATE}T10:00:00+00:00", end_at=f"{DATE}T11:00:00+00:00")
        self.assertFalse(bad_venue["ok"])
        self.assertIn("类型不匹配", bad_venue["error"])
        bad_time = self.store.submit_change(
            "P1", "owner-1", "req-x4", venue_id="V-STAGE",
            start_at=f"{DATE}T11:00:00+00:00", end_at=f"{DATE}T10:00:00+00:00")
        self.assertFalse(bad_time["ok"])

    def test_expected_version_conflict(self):
        add_program(self.store, "P1")
        slot_change(self.store, "P1", "V-STAGE", "10:00", "11:00")
        stale = self.store.submit_change(
            "P1", "owner-1", "req-ver", party_size=80, expected_version=1)
        self.assertEqual(stale["status"], 409)
        self.assertIn("版本冲突", stale["error"])
        fresh = self.store.submit_change(
            "P1", "owner-1", "req-ver2", party_size=80, expected_version=2)
        self.assertTrue(fresh["ok"])

    def test_safety_and_approval_validation(self):
        add_program(self.store, "P1")
        unknown = self.store.confirm_safety("P1", "unknown_item", "s1", "sf-x1")
        self.assertFalse(unknown["ok"])
        self.assertIn("未知的安全确认项", unknown["error"])
        wrong_party = self.store.grant_approval("P1", "nobody", "a1", "ap-x1")
        self.assertFalse(wrong_party["ok"])
        self.assertIn("不需要", wrong_party["error"])

    def test_confirmations_are_per_version(self):
        add_program(self.store, "P1")
        confirm_all(self.store, "P1")
        slot_change(self.store, "P1", "V-STAGE", "10:00", "11:00")
        view = self.store.program_view("P1")
        self.assertFalse(view["eligible"])
        self.assertEqual(view["approvals"], [])
        self.assertEqual(view["safety_confirmations"], [])


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        self.store.close()

    def test_publish_success(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        ready_program(self.store, "P2", "V-EXH", "10:00", "12:00", kind="exhibition", size=50)
        env = self.store.publish_schedule(DATE, "pub-1")
        self.assertTrue(env["ok"], env)
        self.assertEqual(len(env["result"]["items"]), 2)
        schedule = self.store.get_schedule(DATE)
        self.assertEqual(schedule["status"], "published")
        self.assertEqual(self.store.program_view("P1")["state"], "published")

    def test_unconfirmed_program_cannot_be_published(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        add_program(self.store, "P2", kind="exhibition", size=50)
        slot_change(self.store, "P2", "V-EXH", "10:00", "12:00")
        env = self.store.publish_schedule(DATE, "pub-block")
        self.assertFalse(env["ok"])
        self.assertEqual(env["status"], 409)
        blockers = env["details"]["blockers"]
        self.assertEqual([b["program_id"] for b in blockers], ["P2"])
        self.assertEqual(blockers[0]["missing_safety"], ["fire_check", "power_check"])
        self.assertEqual(self.store.get_schedule(DATE)["status"], "unpublished")
        confirm_all(self.store, "P2")
        retry = self.store.publish_schedule(DATE, "pub-ok")
        self.assertTrue(retry["ok"], retry)

    def test_conflicting_slots_name_affected_programs(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        ready_program(self.store, "P2", "V-STAGE", "10:30", "11:30")
        env = self.store.publish_schedule(DATE, "pub-conflict")
        self.assertFalse(env["ok"])
        conflicts = env["details"]["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["venue_id"], "V-STAGE")
        involved = {p["program_id"] for p in conflicts[0]["programs"]}
        self.assertEqual(involved, {"P1", "P2"})
        self.assertEqual(self.store.get_schedule(DATE)["status"], "unpublished")

    def test_adjacent_slots_do_not_conflict(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        ready_program(self.store, "P2", "V-STAGE", "11:00", "12:00")
        env = self.store.publish_schedule(DATE, "pub-adjacent")
        self.assertTrue(env["ok"], env)

    def test_publish_empty_date_rejected(self):
        env = self.store.publish_schedule(DATE, "pub-empty")
        self.assertFalse(env["ok"])
        self.assertEqual(env["status"], 404)

    def test_publish_is_idempotent(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        first = self.store.publish_schedule(DATE, "pub-same")
        second = self.store.publish_schedule(DATE, "pub-same")
        self.assertEqual(first, second)
        active = [p for p in self.store.list_publications(DATE) if p["status"] == "active"]
        self.assertEqual(len(active), 1)


class WithdrawTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def tearDown(self):
        self.store.close()

    def test_withdraw_requires_reason(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        self.store.publish_schedule(DATE, "pub-1")
        env = self.store.withdraw_schedule(DATE, "", "wd-1")
        self.assertFalse(env["ok"])
        self.assertIn("原因", env["error"])
        self.assertEqual(self.store.get_schedule(DATE)["status"], "published")

    def test_withdraw_without_active_publication(self):
        env = self.store.withdraw_schedule(DATE, "测试", "wd-none")
        self.assertEqual(env["status"], 404)

    def test_withdraw_restores_last_valid_publication(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        first = self.store.publish_schedule(DATE, "pub-v1")
        first_id = first["result"]["publish_id"]
        first_version = self.store.program_view("P1")["current_version"]
        slot_change(self.store, "P1", "V-STAGE", "20:00", "21:00")
        confirm_all(self.store, "P1")
        second = self.store.publish_schedule(DATE, "pub-v2")
        self.assertEqual(self.store.get_schedule(DATE)["publish_id"],
                         second["result"]["publish_id"])
        env = self.store.withdraw_schedule(DATE, "无人机空域临时管制", "wd-1")
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["result"]["restored_publish_id"], first_id)
        schedule = self.store.get_schedule(DATE)
        self.assertEqual(schedule["publish_id"], first_id)
        self.assertEqual(schedule["items"][0]["version"], first_version)
        self.assertEqual(schedule["items"][0]["start_at"], f"{DATE}T10:00:00+00:00")
        skew = env["result"]["version_skew"]
        self.assertEqual(skew[0]["program_id"], "P1")
        self.assertEqual(skew[0]["snapshot_version"], first_version)
        history = {p["publish_id"]: p for p in self.store.list_publications(DATE)}
        self.assertEqual(history[second["result"]["publish_id"]]["status"], "withdrawn")
        self.assertEqual(history[second["result"]["publish_id"]]["reason"], "无人机空域临时管制")
        self.assertEqual(history[first_id]["status"], "active")

    def test_withdraw_without_previous_unpublishes(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        self.store.publish_schedule(DATE, "pub-only")
        env = self.store.withdraw_schedule(DATE, "暴雨橙色预警", "wd-2")
        self.assertTrue(env["ok"], env)
        self.assertIsNone(env["result"]["restored_publish_id"])
        self.assertEqual(self.store.get_schedule(DATE)["status"], "unpublished")
        self.assertEqual(self.store.program_view("P1")["state"], "approved")
        events = [e for e in self.store.list_events("P1") if e["kind"] == "publication_withdrawn"]
        self.assertEqual(events[0]["body"]["reason"], "暴雨橙色预警")

    def test_withdraw_is_idempotent(self):
        ready_program(self.store, "P1", "V-STAGE", "10:00", "11:00")
        self.store.publish_schedule(DATE, "pub-i")
        first = self.store.withdraw_schedule(DATE, "原因", "wd-i")
        second = self.store.withdraw_schedule(DATE, "原因", "wd-i")
        self.assertEqual(first, second)


class PersistenceTests(unittest.TestCase):
    def test_restart_keeps_approvals_and_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "festival.db")
            store = DomainStore(path)
            store.add_venue("V-STAGE", "主舞台", "stage", 500)
            add_program(store, "P1")
            slot_change(store, "P1", "V-STAGE", "10:00", "11:00", key="chg-persist")
            confirm_all(store, "P1")
            published = store.publish_schedule(DATE, "pub-persist")
            store.close()

            reopened = DomainStore(path)
            try:
                schedule = reopened.get_schedule(DATE)
                self.assertEqual(schedule["status"], "published")
                self.assertEqual(schedule["publish_id"], published["result"]["publish_id"])
                view = reopened.program_view("P1")
                self.assertEqual(view["state"], "published")
                self.assertEqual(len(view["approvals"]), 2)
                self.assertEqual(len(view["safety_confirmations"]), 2)
                replay = reopened.submit_change(
                    "P1", "owner-1", "chg-persist", venue_id="V-STAGE",
                    start_at=f"{DATE}T10:00:00+00:00", end_at=f"{DATE}T11:00:00+00:00")
                self.assertEqual(replay["result"]["current_version"], 2)
                self.assertEqual(reopened.program_view("P1")["current_version"], 2)
                republish = reopened.publish_schedule(DATE, "pub-persist")
                self.assertEqual(republish, published)
            finally:
                reopened.close()


def http_post(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def http_get(base, path, headers=None):
    request = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = DomainStore(os.path.join(cls.tmp.name, "api.db"))
        cls.server = create_server(cls.store, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()
        cls.tmp.cleanup()

    def test_full_flow_over_http(self):
        tag = uuid.uuid4().hex[:6]
        venue_id, pid = f"V-{tag}", f"P-{tag}"
        date = "2026-11-05"
        code, body = http_post(self.base, "/venues", {
            "venue_id": venue_id, "name": "主舞台", "kind": "stage", "capacity": 300})
        self.assertEqual(code, 200, body)
        code, body = http_post(self.base, "/programs", {
            "program_id": pid, "owner_id": "u1", "title": "华服开场秀", "kind": "stage",
            "party_size": 80, "safety_items": ["fire_check", "power_check"]})
        self.assertEqual(code, 200, body)
        change = {"actor_id": "u1", "request_key": f"chg-{tag}", "venue_id": venue_id,
                  "start_at": f"{date}T19:00:00+08:00", "end_at": f"{date}T20:00:00+08:00"}
        code, body = http_post(self.base, f"/programs/{pid}/changes", change)
        self.assertEqual(code, 200, body)
        code, replay = http_post(self.base, f"/programs/{pid}/changes", change)
        self.assertEqual(body, replay)
        self.assertEqual(replay["result"]["current_version"], 2)
        for item in ("fire_check", "power_check"):
            code, body = http_post(self.base, f"/programs/{pid}/safety", {
                "item": item, "confirmer_id": "s1", "request_key": f"sf-{tag}-{item}"})
            self.assertEqual(code, 200, body)
        for party in ("stage_manager", "safety"):
            code, body = http_post(self.base, f"/programs/{pid}/approvals", {
                "party": party, "approver_id": "m1", "request_key": f"ap-{tag}-{party}"})
            self.assertEqual(code, 200, body)
        self.assertTrue(body["result"]["eligible"])
        code, body = http_post(self.base, f"/schedules/{date}/publish",
                               {"request_key": f"pub-{tag}"})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["result"]["published"])

        code, text = http_get(self.base, f"/schedules/{date}", {"Accept": "application/json"})
        schedule = json.loads(text)["result"]
        self.assertEqual(schedule["status"], "published")
        self.assertEqual(schedule["items"][0]["program_id"], pid)
        self.assertEqual(schedule["items"][0]["start_at"], f"{date}T11:00:00+00:00")

        code, text = http_get(self.base, f"/schedules/{date}?format=text")
        self.assertEqual(code, 200)
        self.assertIn("华服开场秀", text)
        self.assertIn("主舞台", text)
        self.assertIn("11:00-12:00", text)

        code, duty = http_get(self.base, f"/duty/{date}?format=text")
        self.assertEqual(code, 200)
        self.assertIn("变更记录", duty)
        self.assertIn("发布历史", duty)
        self.assertIn("已生效", duty)

        code, body = http_post(self.base, f"/schedules/{date}/withdraw",
                               {"request_key": f"wd-bad-{tag}"})
        self.assertEqual(code, 400)
        self.assertIn("原因", body["error"])
        code, body = http_post(self.base, f"/schedules/{date}/withdraw",
                               {"request_key": f"wd-{tag}", "reason": "设备检修"})
        self.assertEqual(code, 200, body)
        code, text = http_get(self.base, f"/schedules/{date}")
        self.assertEqual(json.loads(text)["result"]["status"], "unpublished")

        code, changes = http_get(self.base, f"/changes?date={date}&program_id={pid}")
        kinds = {c["kind"] for c in json.loads(changes)["result"]}
        self.assertEqual(kinds, {"change_accepted"})

    def test_conflict_response_names_programs_over_http(self):
        tag = uuid.uuid4().hex[:6]
        venue_id = f"VC-{tag}"
        date = "2026-11-06"
        http_post(self.base, "/venues", {
            "venue_id": venue_id, "name": "展厅B", "kind": "exhibition", "capacity": 100})
        for index, (start, end) in enumerate((("10:00", "11:00"), ("10:30", "11:30"))):
            pid = f"PC-{tag}-{index}"
            http_post(self.base, "/programs", {
                "program_id": pid, "owner_id": "u1", "title": f"展览{index}",
                "kind": "exhibition", "party_size": 20, "safety_items": ["fire_check"]})
            http_post(self.base, f"/programs/{pid}/changes", {
                "actor_id": "u1", "request_key": f"chg-{pid}", "venue_id": venue_id,
                "start_at": f"{date}T{start}:00+00:00", "end_at": f"{date}T{end}:00+00:00"})
            http_post(self.base, f"/programs/{pid}/safety", {
                "item": "fire_check", "confirmer_id": "s1", "request_key": f"sf-{pid}"})
            for party in ("site", "safety"):
                http_post(self.base, f"/programs/{pid}/approvals", {
                    "party": party, "approver_id": "m1", "request_key": f"ap-{pid}-{party}"})
        code, body = http_post(self.base, f"/schedules/{date}/publish",
                               {"request_key": f"pub-{tag}"})
        self.assertEqual(code, 409)
        conflict = body["details"]["conflicts"][0]
        self.assertEqual(conflict["venue_id"], venue_id)
        self.assertEqual({p["program_id"] for p in conflict["programs"]},
                         {f"PC-{tag}-0", f"PC-{tag}-1"})

    def test_unknown_route_and_bad_json(self):
        code, body = http_get(self.base, "/nope")
        self.assertEqual(code, 404)
        request = urllib.request.Request(
            self.base + "/venues", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
