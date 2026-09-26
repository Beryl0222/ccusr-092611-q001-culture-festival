# 华服活动编排台

这是面向华服活动编排台的本地服务基础，负责保存业务记录、状态变化和可追溯事件。核心写入使用 SQLite 事务，服务接口保持无页面依赖，便于运营人员在现场或后台系统中核对状态。

## 目录

- src/culture_festival/domain.py：领域对象与时间约定。
- src/culture_festival/service.py：事务、状态迁移、权限和幂等边界。
- src/culture_festival/api.py：本地 HTTP 接口。
- tests/：状态、版本、权限和重复请求测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
