# 深度登记 光纤接续核验台

- 规模：中型
- 核心规则：校准双向链程后做单调一对一最优配对，按带符号均值验设计接头。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 轨迹文件采信 | 检查原行与隔离原因 | POST /api/trace | `app/modules/trace/service.py` | `tests/test_trace.py` |
| M2 距离与方向校准 | 核对双端坐标与起偏 | POST /api/calibration | `app/modules/calibration/service.py` | `tests/test_calibration.py` `tests/test_settings.py` |
| M3 双端事件最优配对 | 核查相邻冲突及孤测 | POST /api/pairs | `app/modules/pairs/optimal.py` | `tests/test_pairs.py` `tests/test_pair_oracle.py` |
| M4 接续净损耗判定 | 对照带符号读数和均值 | POST /api/loss | `app/modules/loss/service.py` | `tests/test_loss.py` |
| M5 设计与实测对账 | 核对逐接头是否缺证 | POST /api/design | `app/modules/design/service.py` | `tests/test_design.py` |
| M6 批次留档复跑 | 留档和重跑同一轨迹组 | POST /api/inspections | `app/modules/archive/storage.py` | `tests/test_archive.py` |
| M7 光纤证据签发 | 复核签发条件并下载 | GET /api/certificate/<id> | `app/modules/certificate/service.py` | `tests/test_certificate.py` |
| M8 双线路纠正签发 | 成对交换远端原件并联动留档 | POST /api/corrections | `app/modules/correction/service.py` | `tests/test_correction.py` |

## M8 双线路纠正规则

两条线路的远端测量原件（B 端，见 `config.REMOTE_SOURCE`）贴反时，纠正单必须成对开立：

- 只交换两条线路的远端原件，A 端原件与设计清单各自保留；单独纠正一条会被 `CORRECTION_NEEDS_TWO_LINES` 拒绝，避免同一原件被两条线路采信。
- 开立即对两条线路分别重跑全链路复算，各自给出合格判定与通过/超限/缺证计数；一条合格、一条超限则状态 `SUSPENDED`，两条都暂缓签发，留档仍指向旧版。
- 两张纠正单共用任一线路时，`line_locks` 行级互斥只放行一张，另一张返回 `CORRECTION_LINE_BUSY`（409）并附占用方纠正单号；归档完成才释放锁。
- 留档前任一线路录入新测量记录（`lines.version` 偏离纠正单钉版），归档请求把纠正单置为 `RETURNED` 并返回 `LINE_MEASUREMENT_CHANGED`；重核按当前有效留档重新交换、复算并推进钉版。
- 只有两条都合格才更新留档；两线路的新版本在同一 SQLite 事务内写入，`claim_archived` 条件更新保证并发归档只生效一次，写入中断整体回滚，两线路同见旧版或同见新版。
- 签发始终以线路当前生效留档（`lines.inspection_id`）为准，经 `/api/certificate/<id>` 复算。

## 陷阱登记

| TRAP | 现象 | 朴素错误 | 处理 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | A 端首列 BOM | 事件列读不到 | 清理首 BOM | `app/modules/trace/reader.py` | `tests/test_trace.py::test_bom_and_repeated_header` |
| TRAP-04 | A 端续行表头 | 当配对记录 | 按分段另计 | `app/modules/trace/reader.py` | `tests/test_trace.py::test_bom_and_repeated_header` |
| TRAP-05 | A-Y 列尾多字段 | 错配字段值 | 原行隔离 | `app/modules/trace/reader.py` | `tests/test_trace.py::test_extra_fields_keep_raw_location` |
| TRAP-07 | A-N 哨兵、B-G 空值 | 当成零损耗 | 两种原因分别留档 | `app/reasons.py` | `tests/test_trace.py::test_missing_sentinel_and_blank_are_isolated` |
| TRAP-10 | 0.2005 dB 半毫精度 | 先舍入误过限 | 用两倍毫 dB 判界 | `app/modules/loss/verdict.py` | `tests/test_loss.py::test_double_mdb_preserves_half_increment` |
| TRAP-14 | A-C 同键异距 | 后写覆盖 | 整组隔离 | `app/modules/trace/service.py` | `tests/test_trace.py::test_conflicting_records_both_withdrawn` |
| TRAP-15 | A-02 米和千米并存 | 两条虚假轨迹 | 归一毫米后比对 | `app/modules/trace/units.py` | `tests/test_trace.py::test_equivalent_units_dedupe` |
| TRAP-19 | A100 可匹配 B97 或 B101 | 最近优先导致 A105 无法配对 | 先最大配对数再最小间差 | `app/modules/pairs/optimal.py` | `tests/test_pairs.py::test_global_pairing_beats_greedy_and_keeps_competition` |
| TRAP-21 | A700 和 B820 均无对侧 | 只报一端孤测 | 分别记录缺 A 或缺 B | `app/modules/pairs/service.py` | `tests/test_pairs.py::test_opposite_ends_missing_in_both_directions` |

## 对账规则

- 6 个设计接头 = 3 通过 + 1 超限 + 2 缺证。
- 12 条窗内端点 = 2×(4 已挂接 + 1 孤配) + 2 单端。窗外测量另计 1 条。
- F-01 双向均值 (−0.100+0.300)/2=0.1000 dB，F-03 为 (0.33+0.31)/2=0.3200 dB，超出 0.200 dB。

输入为设备导出的事件表。二进制曲线解码、坡度自校与电子签章未纳入该工作台。
