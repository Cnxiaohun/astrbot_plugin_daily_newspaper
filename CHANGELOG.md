# 变更日志

## v0.2.2

这版是**上架审查的合规修正**，没有功能变化。

AstrBot 插件审查要求：日志记录器必须且只能从 `astrbot.api` 导入，严禁使用 Python 内置
`logging` 模块。此前 `core/service.py` 与 `core/announce.py` 保留了
`logging.getLogger(__name__)` 的兜底写法——在 AstrBot 运行时主入口已传入正确 logger、
兜底不会触发，但按规则不允许出现。

修改内容：

- `core/service.py`、`core/announce.py` 删除 `import logging` 与
  `logging.getLogger(__name__)` 兜底；`logger` 改为**必填参数**，缺失时直接抛
  `ValueError`（而不是悄悄退回内置日志），确保问题在开发阶段就暴露。
- 日志器一律由调用方注入：`main.py` 传 `astrbot.api` 的 `logger`；
  `tools/preview.py` 这类不依赖 AstrBot 的场景在调用处自建一个只往 stderr 打印的最小日志器，
  同样不引入 `logging` 模块。
- 测试里的日志替身也改为自定义空实现，测试代码同样不出现内置 `logging`。
- 新增 `tests/test_logging_policy.py`：扫描整个插件包，禁止 `import logging`、
  `from logging import`、`logging.getLogger/Logger/basicConfig` 等写法；
  同时断言入口确实从 `astrbot.api` 导入 logger、`ReportService` 与 `announce`
  在缺 logger 时会拒绝对待。以后再回退会被测试直接拦下。

## v0.2.1

> 这版只做了一件破坏性的事：改名字。
> 仓库 `astrbot_plugin_daily_image_report` → `astrbot_plugin_daily_newspaper`，
> 中文名「每日图报」→「每日报纸」。GitHub 旧地址会自动重定向。
> 升级时插件会把旧数据目录里的去重历史与发送收据迁到新目录；
> **插件配置要重新填一次**，因为 AstrBot 的配置文件路径跟着插件名走。

## v0.2.0

- **时段不再固定三档**：可新增、删除、改时间，最多 12 档；每档独立设置时间、模式、
  研究/图片提示词、目标群与生图参数。`/日报 列表` 查看全部档位。
- 新增指令：`/日报 新增 <名称> <HH:MM>`、`/日报 删除 <名称>`、`/日报 时间 <名称> <HH:MM>`、
  `/日报 列表`。旧指令与写法全部保持可用。
- 旧版固定三档配置在启动时**无损迁移**为动态列表，目标群、提示词、开关原样保留；
  迁移失败保持原状并记录日志。
- 每档的研究窗口改为「上一档的计划时间」：按时间排序取前一份刊物，
  当天第一档回溯到昨天最后一档；只有一档时窗口为最近 24 小时。
- 新增档位若未填自定义提示词，沿用内置早报版研究规则与报纸版式模板，不会出空白图。
- 保护：不能删除最后一档；正在生成的档位不允许删除；档位名称与 id 不允许重复。
- 时段配置在 WebUI 中改为可增删的列表（`template_list`）。

## v0.1.0

- 首个公开版本：早 08:00 / 午 12:30 / 晚 19:00 三档定时，
  研究 Agent 联网核实资讯后编译为逐字文案，交生图模型排成 1024×1536 竖版中文报纸图，
  只发图片到绑定的 NapCat / OneBot v11 群。
- 强制联网搜索证据、逐条来源与发布时间校验、当天只生成一次、跨刊去重、
  逐群发送收据、失败回群说明、生成前人格口吻告知、主备生图模型、可选视觉回检。
