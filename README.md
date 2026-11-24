# 光纤接续核验台

验收员从 A、B 两端 OTDR 事件原件和设计清单复算接续损耗。首页比对双端轨迹帧与设计接头，按 `J`、`K` 或使用跳检按钮定位证据。左侧八个入口分别处理采信、校准、配对、判损、对账、留档、签发和双线路纠正。

`docker compose up --build` 后访问 `http://localhost:18468/`。本地先执行 `pip install -r requirements.txt`，再运行 `python -m flask --app app.main run --port=18468`，用 `pytest -q` 验收。`FIBER_DB` 设置 SQLite 文件，Compose 的命名卷保留运行记录。

各预览和留档 API 接收 `{"sample":"dirty"}`、`{"sample":"clean"}` 或 `{"traces":{"a":"CSV","b":"CSV","design":"CSV"},"settings":{...}}`。参数示例见 `sample_data/dirty/link.json`。从留档页提交，在签发页下载核验包。

双线路远端原件贴反时走纠正签发：先 `POST /api/lines/<代号>/recordings` 登记两条线路的生效留档，再 `POST /api/corrections`（`{"lines":["L1","L2"]}`）成对交换 B 端原件并各自复算。一条超限则两条暂缓，留档前任一线路录入新测量会退回重核，`POST /api/corrections/<id>/recheck` 重核、`/archive` 在两条都合格时于同一事务联动切换生效留档。

- [模块与独立复算](docs/DEPTH.md)
- [字段与规则](docs/DATA-DICTIONARY.md)
