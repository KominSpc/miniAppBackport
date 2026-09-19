# miniAppBackport

二次元美图与每日内容应用的**后端模拟服务**（FastAPI）。

- 对应客户端仓库：[KominSpc/MiniApp](https://github.com/KominSpc/MiniApp)
- 契约优先：所有客户端接口统一为 `/v1` 前缀，响应结构 `{data, meta, error}`
- Mock 优先：首期内容全部由本地静态数据与本地图片提供，不直连第三方平台
- 客户端不保存任何第三方凭据

## 规划结构

```
app/main.py
app/api/v1/{users,images,daily,games,pet,dev}.py
app/schemas/         # 统一响应、ContentItem、分页
app/services/        # 业务编排
app/repositories/    # 内存实现 + MySQL 仓储接口骨架
app/agents/          # ChatService、专家路由、敏感词过滤
app/fixtures/        # 静态 JSON 数据集
app/static/          # 示例图片
tests/               # 契约与边界测试
scripts/export_openapi.py
```

当前状态：仅初始化仓库，尚未实现接口。
执行计划见客户端仓库 `docs/EXECUTION_PLAN.md`。
