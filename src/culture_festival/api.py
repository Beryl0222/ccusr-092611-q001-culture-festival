"""华服活动编排台的本地 HTTP 边界：无页面依赖，值班人员可直接用 JSON 核对。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .service import BlockingError, ConflictError, DomainStore, NotFoundError, ServiceError

ROUTES = [
    "GET  /healthz",
    "POST /venues                       {request_key, venue_id, name, capacity}",
    "GET  /venues",
    "POST /programs                     {request_key, program_id, name, owner_id, venue_id, start_at, end_at, expected_attendance?, requires_safety?, dependencies?}",
    "GET  /programs?day=YYYY-MM-DD",
    "GET  /programs/{program_id}",
    "POST /changes                      {request_key, program_id, patch, actor?}",
    "GET  /changes?program_id=&state=",
    "GET  /changes/{change_id}",
    "POST /changes/{change_id}/approvals        {role, approver}",
    "POST /changes/{change_id}/rejection        {role, approver, reason?}",
    "POST /programs/{program_id}/safety-confirmations  {request_key, version, confirmed_by, note?}",
    "POST /publications                 {request_key, day}",
    "GET  /publications?day=YYYY-MM-DD",
    "POST /publications/rollback        {request_key, day, reason}",
    "GET  /schedule?day=YYYY-MM-DD",
    "GET  /events?entity_id=&kind=&limit=",
]


def _query_one(query, name):
    values = query.get(name)
    return values[0] if values else None


def dispatch(store, method, path, query, body):
    """路由分发；返回 (status, payload)。body 为已解析的 JSON 对象。"""
    parts = [unquote(p) for p in path.split("/") if p]
    if method == "GET" and path == "/healthz":
        return 200, {"ok": True}
    if method == "GET" and not parts:
        return 200, {"service": "华服活动编排台", "routes": ROUTES}
    if not parts:
        raise NotFoundError(f"接口不存在: {method} {path}")

    head = parts[0]
    if head == "venues":
        if method == "POST" and len(parts) == 1:
            return 200, store.register_venue(
                body.get("request_key"), body.get("venue_id"),
                body.get("name"), body.get("capacity"))
        if method == "GET" and len(parts) == 1:
            return 200, {"items": store.list_venues()}
    elif head == "programs":
        if method == "POST" and len(parts) == 1:
            return 200, store.create_program(
                body.get("request_key"), body.get("program_id"), body.get("name"),
                body.get("owner_id"), body.get("venue_id"), body.get("start_at"),
                body.get("end_at"), body.get("expected_attendance", 0),
                body.get("requires_safety", True), body.get("dependencies", ()))
        if method == "GET" and len(parts) == 1:
            return 200, {"items": store.list_programs(day=_query_one(query, "day"))}
        if len(parts) == 2 and method == "GET":
            return 200, store.get_program(parts[1])
        if len(parts) == 3 and parts[2] == "safety-confirmations" and method == "POST":
            return 200, store.confirm_safety(
                body.get("request_key"), parts[1], body.get("version"),
                body.get("confirmed_by"), body.get("note"))
    elif head == "changes":
        if method == "POST" and len(parts) == 1:
            return 200, store.submit_change(
                body.get("request_key"), body.get("program_id"),
                body.get("patch"), body.get("actor"))
        if method == "GET" and len(parts) == 1:
            return 200, {"items": store.list_changes(
                program_id=_query_one(query, "program_id"),
                state=_query_one(query, "state"))}
        if len(parts) == 2 and method == "GET":
            return 200, store.get_change(parts[1])
        if len(parts) == 3 and parts[2] == "approvals" and method == "POST":
            return 200, store.approve_change(parts[1], body.get("role"), body.get("approver"))
        if len(parts) == 3 and parts[2] == "rejection" and method == "POST":
            return 200, store.reject_change(
                parts[1], body.get("role"), body.get("approver"), body.get("reason"))
    elif head == "publications":
        if method == "POST" and len(parts) == 1:
            return 200, store.publish(body.get("request_key"), body.get("day"))
        if method == "GET" and len(parts) == 1:
            return 200, {"items": store.list_publications(day=_query_one(query, "day"))}
        if len(parts) == 2 and parts[1] == "rollback" and method == "POST":
            return 200, store.rollback(
                body.get("request_key"), body.get("day"), body.get("reason"))
    elif head == "schedule" and method == "GET" and len(parts) == 1:
        day = _query_one(query, "day")
        if not day:
            raise ServiceError("缺少 day 参数（YYYY-MM-DD）")
        return 200, store.get_schedule(day)
    elif head == "events" and method == "GET" and len(parts) == 1:
        limit = _query_one(query, "limit")
        if limit is not None:
            try:
                limit = int(limit)
            except ValueError as exc:
                raise ServiceError("limit 必须为整数") from exc
        return 200, {"items": store.list_events(
            entity_id=_query_one(query, "entity_id"),
            kind=_query_one(query, "kind"),
            limit=limit or 200)}
    raise NotFoundError(f"接口不存在: {method} {path}")


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ServiceError("请求体不是有效的 JSON") from exc
            if not isinstance(data, dict):
                raise ServiceError("请求体必须是 JSON 对象")
            return data

        def _handle(self, method):
            parsed = urlparse(self.path)
            try:
                body = self._read_body() if method == "POST" else {}
                status, payload = dispatch(
                    store, method, parsed.path, parse_qs(parsed.query), body)
                self._send(status, payload)
            except ConflictError as exc:
                self._send(409, {"error": str(exc), "code": "conflict", "conflicts": exc.conflicts})
            except BlockingError as exc:
                self._send(422, {"error": str(exc), "code": "blocked", "blocked": exc.blocked})
            except NotFoundError as exc:
                self._send(404, {"error": str(exc), "code": "not_found"})
            except ServiceError as exc:
                self._send(400, {"error": str(exc), "code": "bad_request"})
            except Exception as exc:  # 兜底，避免连接直接断开
                self._send(500, {"error": f"内部错误: {exc}", "code": "internal_error"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def log_message(self, *_args):
            return

    return Handler


def serve(host="127.0.0.1", port=8080, database=None):
    database = database or os.environ.get("CULTURE_FESTIVAL_DB", "culture_festival.db")
    store = DomainStore(database)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"华服活动编排台已启动: http://{host}:{port}（数据库 {database}）")
    try:
        server.serve_forever()
    finally:
        store.close()


if __name__ == "__main__":
    serve()
