# 华服活动编排台

面向华服嘉年华（展览、舞台、无人机灯光秀）的本地编排服务：保存节目版本、场地容量、
设备安全前置确认、多方审批与发布/撤回记录。核心写入使用 SQLite 事务，接口无页面依赖，
值班人员使用 curl 即可核对当天安排。

## 业务规则

- **版本化编排**：每次被接受的变更生成节目新版本；变更后旧版本的审批与安全确认不再继承，
  需在新版本上重新确认，避免"已批准的旧安排"被误当成"已批准的新安排"。
- **幂等**：所有写操作必须携带 `request_key`。同一请求键重复提交只执行一次，
  重放返回首次结果——**包括被拒绝的结果**（例如首次因容量超限被拒，修复内容后请换新键）。
  冲突的变更可携带 `expected_version` 做乐观锁校验。
- **前置条件**：场地容量不小于节目人数，且场地类型必须与节目类型匹配
  （舞台节目不能占用展览/空域场地）；各类节目的必审方可按类型默认配置，也可自定义。
- **发布门控**：安全确认项未集齐或审批方未全部通过的节目不得进入已发布日程；
  发布时对当天全部已编排节目做整体校验，同一场地时段重叠会逐对列出受影响节目并整体拒绝。
- **撤回与恢复**：撤回必须填写原因并永久留痕；撤回后自动恢复该日期最近一次被取代的
  有效发布（发布快照冻结版本与时间）；无历史版本时日程回到未发布状态。
- **持久化与留痕**：审批、确认、发布、撤回、被接受与被拒绝的变更全部写入 SQLite
  与事件表，服务重启后状态保留。

## 目录

- `src/culture_festival/domain.py`：领域常量、时间/日期约定。
- `src/culture_festival/service.py`：事务、版本、幂等、审批/安全门控、冲突检测、发布与撤回。
- `src/culture_festival/api.py`：本地 HTTP 接口（JSON + 纯文本两种视图）。
- `tests/`：幂等、冲突、门控、撤回、重启持久化与 HTTP 端到端测试。

## 运行

    PYTHONPATH=src FESTIVAL_DB=festival.db python3 -m culture_festival
    # 默认 127.0.0.1:8080，可用 HOST / PORT / FESTIVAL_DB 环境变量覆盖

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/venues` | 登记场地（id、名称、类型 exhibition/stage/drone、容量） |
| GET | `/venues` | 场地列表 |
| POST | `/programs` | 登记节目（id、负责人、类型、人数、安全确认项、可选必审方） |
| GET | `/programs` | 节目列表（含当前版本、缺口、审批/确认明细） |
| GET | `/programs/{id}` | 单个节目状态 |
| GET | `/programs/{id}/events` | 单个节目的完整事件流水 |
| POST | `/programs/{id}/changes` | 提交变更（场地/开始/结束/人数/备注，`request_key` 必填） |
| POST | `/programs/{id}/safety` | 安全确认（确认项 + 确认人） |
| POST | `/programs/{id}/approvals` | 某一方审批（party + 审批人） |
| POST | `/schedules/{YYYY-MM-DD}/publish` | 校验并发布当天日程 |
| POST | `/schedules/{YYYY-MM-DD}/withdraw` | 撤回发布（reason 必填），恢复最近有效版本 |
| GET | `/schedules/{YYYY-MM-DD}` | 已发布日程 |
| GET | `/changes?date=&program_id=` | 变更记录（已生效/被拒绝及原因） |
| GET | `/publications?date=` | 发布历史（含 rejected / active / superseded / withdrawn） |
| GET | `/duty/{YYYY-MM-DD}` | 值班视图：日程 + 当天变更 + 发布历史 |

写操作返回统一信封：`{"ok": true, "result": ...}` 或
`{"ok": false, "error": "...", "status": 409, "details": {...}}`
（冲突时 `details.conflicts` 逐对列出场地与受影响节目，`details.blockers` 列出未确认节目）。

GET 查询默认返回 JSON；追加 `?format=text`（或 `Accept: text/plain`）返回纯文本，
终端即可阅读。示例：

```
华服嘉年华日程 2026-10-01 状态:published 发布号:pub-2026-10-01-002 ...
11:00-11:40 | 华服巡游(P1) v2 | 舞台 | 场地:主舞台 | 120人
```

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
