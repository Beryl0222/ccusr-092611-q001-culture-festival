# 华服活动编排台

面向华服嘉年华承办团队的本地编排服务。在展览、舞台、无人机灯光秀之间反复调整时间与场地时，负责：

- 记录节目版本、场地容量、设备安全前置条件和多方审批；
- 同一变更请求重复提交只生效一次（request_key 幂等）；
- 冲突编排（重复占用场地、容量超限、依赖时序）明确指出受影响项目；
- 未完成设备安全确认的节目不得进入已发布日程；
- 撤回发布保留可追溯原因，并自动恢复最近一次有效版本；
- SQLite 落盘，服务重启后审批与发布状态保留；
- 无页面依赖，值班人员直接用 JSON 接口核对当天日程、变更记录与审计事件。

## 运行

    PYTHONPATH=src python3 -m culture_festival.api
    # 默认 127.0.0.1:8080，数据库 ./culture_festival.db
    # 用 CULTURE_FESTIVAL_DB=/path/to/festival.db 指定数据库位置

## 接口

所有写接口都带 `request_key`：重复提交返回首次结果，不会产生第二次效果。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /venues | 登记场地（名称、容量） |
| GET | /venues | 场地列表 |
| POST | /programs | 登记节目（场地、时间、预计人数、是否需要安全确认、依赖节目） |
| GET | /programs?day= | 节目列表，可按日期过滤 |
| GET | /programs/{id} | 节目详情：状态、当前版本、安全确认进度 |
| POST | /changes | 提交变更请求（patch 为变更字段），冲突时随记录返回受影响项目 |
| GET | /changes?program_id=&state= | 变更记录列表 |
| GET | /changes/{id} | 变更详情：审批进度、冲突、生效版本 |
| POST | /changes/{id}/approvals | 多方会签（默认运营、场地、制作三方全部通过才生效） |
| POST | /changes/{id}/rejection | 任一审批角色驳回，需给理由 |
| POST | /programs/{id}/safety-confirmations | 安全负责人对当前生效版本做设备安全确认 |
| POST | /publications | 发布某天日程（安全门 + 冲突兜底校验） |
| GET | /publications?day= | 发布历史（含撤回原因，可追溯） |
| POST | /publications/rollback | 撤回当天发布，必须给原因；自动恢复最近一次有效版本 |
| GET | /schedule?day= | 当天当前有效的已发布日程（值班核对用） |
| GET | /events?entity_id=&kind= | 审计事件流 |

错误码：400 参数/状态非法，404 不存在，409 编排冲突（`conflicts` 列出受影响项目），422 安全确认未完成（`blocked` 列出被阻断节目）。

## 典型流程

1. 登记场地、节目（节目初始为草稿，版本 0）。
2. 提交变更请求 → 三方会签全部通过 → 变更生效，节目版本 +1。
3. 安全负责人确认当前版本的设备安全前置条件。
4. 发布当天日程；之后每次调整重复 2-4，发布会取代旧发布。
5. 需要撤回时调用 rollback 并填写原因，日程自动回到最近一次有效版本。

## 目录

- src/culture_festival/domain.py：状态约定、时间工具与冲突对象。
- src/culture_festival/service.py：事务、状态迁移、冲突检测、审批、安全门、发布撤回与幂等边界。
- src/culture_festival/api.py：本地 HTTP 接口。
- tests/：状态、版本、冲突、安全门、撤回、持久化和 HTTP 流程测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
