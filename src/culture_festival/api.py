"""华服活动编排台的轻量 HTTP 边界。

无页面依赖：所有查询支持 JSON（默认）与纯文本（?format=text 或 Accept: text/plain），
值班人员用 curl 即可核对当天日程、变更记录与发布历史。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .service import (DomainStore, ServiceError, render_changes_text,
                      render_duty_text, render_publications_text,
                      render_schedule_text)


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CultureFestival/0.1"

        def _send(self, code, data, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _send_text(self, code, text):
            self._send(code, text.encode(), "text/plain; charset=utf-8")

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                data = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是有效 JSON")
            if not isinstance(data, dict):
                raise ServiceError("请求体应为 JSON 对象")
            return data

        def _query(self):
            return parse_qs(urlparse(self.path).query)

        def _segments(self):
            return [unquote(s) for s in urlparse(self.path).path.strip("/").split("/") if s]

        def _wants_text(self):
            fmt = self._query().get("format", [""])[0]
            if fmt:
                return fmt == "text"
            accept = self.headers.get("Accept", "")
            return "text/plain" in accept and "application/json" not in accept

        def _envelope(self, envelope):
            self._send_json(200 if envelope["ok"] else envelope.get("status", 400), envelope)

        def _guard(self, action):
            try:
                action()
            except ServiceError as exc:
                self._send_json(exc.status, {"ok": False, "error": str(exc), "details": exc.details})
            except Exception:
                self._send_json(500, {"ok": False, "error": "服务内部错误"})

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def _get(self):
            seg = self._segments()
            query = self._query()
            if seg == ["health"]:
                return self._send_json(200, {"ok": True})
            if seg == ["venues"]:
                return self._send_json(200, {"ok": True, "result": store.list_venues()})
            if seg == ["programs"]:
                return self._send_json(200, {"ok": True, "result": store.list_programs()})
            if len(seg) == 2 and seg[0] == "programs":
                return self._send_json(200, {"ok": True, "result": store.program_view(seg[1])})
            if len(seg) == 3 and seg[0] == "programs" and seg[2] == "events":
                return self._send_json(200, {"ok": True, "result": store.list_events(seg[1])})
            if len(seg) == 2 and seg[0] == "schedules":
                schedule = store.get_schedule(seg[1])
                if self._wants_text():
                    return self._send_text(200, render_schedule_text(schedule))
                return self._send_json(200, {"ok": True, "result": schedule})
            if seg == ["changes"]:
                changes = store.list_changes(program_id=query.get("program_id", [None])[0],
                                             date=query.get("date", [None])[0])
                if self._wants_text():
                    return self._send_text(200, render_changes_text(changes))
                return self._send_json(200, {"ok": True, "result": changes})
            if seg == ["publications"]:
                date = query.get("date", [None])[0]
                if not date:
                    raise ServiceError("缺少 date 参数")
                publications = store.list_publications(date)
                if self._wants_text():
                    return self._send_text(200, render_publications_text(publications))
                return self._send_json(200, {"ok": True, "result": publications})
            if len(seg) == 2 and seg[0] == "duty":
                view = store.duty_view(seg[1])
                if self._wants_text():
                    return self._send_text(200, render_duty_text(view))
                return self._send_json(200, {"ok": True, "result": view})
            raise ServiceError(f"未知路径: {self.path}", 404)

        def _post(self):
            seg = self._segments()
            body = self._body()
            if seg == ["venues"]:
                return self._envelope(store.add_venue(
                    body.get("venue_id"), body.get("name"), body.get("kind"),
                    body.get("capacity"), body.get("request_key")))
            if seg == ["programs"]:
                return self._envelope(store.create_program(
                    body.get("program_id"), body.get("owner_id"), body.get("title"),
                    body.get("kind"), body.get("party_size"), body.get("required_parties"),
                    body.get("safety_items"), body.get("request_key")))
            if len(seg) == 3 and seg[0] == "programs" and seg[2] == "changes":
                return self._envelope(store.submit_change(
                    seg[1], body.get("actor_id"), body.get("request_key"),
                    body.get("venue_id"), body.get("start_at"), body.get("end_at"),
                    body.get("party_size"), body.get("note", ""), body.get("expected_version")))
            if len(seg) == 3 and seg[0] == "programs" and seg[2] == "safety":
                return self._envelope(store.confirm_safety(
                    seg[1], body.get("item"), body.get("confirmer_id"), body.get("request_key")))
            if len(seg) == 3 and seg[0] == "programs" and seg[2] == "approvals":
                return self._envelope(store.grant_approval(
                    seg[1], body.get("party"), body.get("approver_id"), body.get("request_key")))
            if len(seg) == 3 and seg[0] == "schedules" and seg[2] == "publish":
                return self._envelope(store.publish_schedule(
                    seg[1], body.get("request_key"), body.get("actor_id", "")))
            if len(seg) == 3 and seg[0] == "schedules" and seg[2] == "withdraw":
                return self._envelope(store.withdraw_schedule(
                    seg[1], body.get("reason"), body.get("request_key"), body.get("actor_id", "")))
            raise ServiceError(f"未知路径: {self.path}", 404)

        def log_message(self, *_):
            return

    return Handler


def create_server(store, host="127.0.0.1", port=8080):
    return ThreadingHTTPServer((host, port), make_handler(store))


def serve(host="127.0.0.1", port=8080, database="festival.db"):
    store = DomainStore(database)
    server = create_server(store, host, port)
    print(f"华服活动编排台服务已启动: http://{host}:{server.server_address[1]}"
          f"（数据库: {database}）", flush=True)
    try:
        server.serve_forever()
    finally:
        store.close()
