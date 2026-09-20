# V2 A级迭代基线审计

- 审计区间：2026-08-14 至 2026-08-26
- 数据目录：`/Users/tonyyu/Library/Application Support/a-share-trading-watch`
- 生成方式：只读 SQLite URI；不写运行数据库，不触发飞书或交易。

## 信号漏斗基线

- 活动快照：581
- 有效状态事件：581
| 内部状态 | 数量 |
|---|---:|
| WAIT_MULTI_PERIOD | 579 |
| BUY_PROBE | 2 |

### 状态迁移

| 迁移 | 数量 |
|---|---:|
| IDLE -> WAIT_MULTI_PERIOD | 579 |
| IDLE -> BUY_PROBE | 2 |

### 主要阻断

| 排名 | 原因 | 次数 |
|---:|---|---:|
| 1 | 5分钟已收盘VWAP收复未完成 | 1043 |
| 2 | 5分钟未形成抬高低点 | 1015 |
| 3 | 已收盘5m/15m不足：0/3、64/20 | 769 |
| 4 | 位置处于MID_AIR，非支撑/结构收复入场 | 475 |
| 5 | 14:45后不新开仓 | 468 |
| 6 | 相对15分钟MA20过度延伸，禁止追价 | 461 |
| 7 | 延伸状态 CLIMAX，禁止追价 | 421 |
| 8 | V2市场/板块门控[CORE_GLOBAL_RISK_BLOCKED]：全市场系统性风险门控：核心池新增关闭 | 285 |
| 9 | REPAIR只允许E2/E3/E4确认，当前15分钟Setup未完成 | 268 |
| 10 | 120分钟BEAR，不允许新开多 | 233 |
| 11 | 结构状态 BEAR 不允许新开多 | 233 |
| 12 | 15分钟Setup未完成 | 134 |
| 13 | 盘前计划=未加载当日盘前仓位计划，当日不允许新增试仓 | 97 |
| 14 | 盘前仓位计划缺失/不完整，V2只观察不生成买入事件 | 97 |
| 15 | V2市场/板块门控[CORE_SECTOR_ROTATION_BLOCKED]：非科技方向需板块情绪确认：行业暂缺 未达到强势阈值 | 82 |
| 16 | 路径硬否决：GAP_FAILURE | 64 |
| 17 | 腾讯与新浪60分钟收盘交叉校验不一致 | 56 |
| 18 | 相对15分钟MA20已延伸，等待回踩 | 42 |
| 19 | 已收盘5m/15m不足：1/3、64/20 | 40 |
| 20 | 延伸状态 EXTENDED，禁止追价 | 38 |

## 模拟盘基线

- 订单：3
- 成交：0
- 当前开放持仓：2
| 订单状态 | 数量 |
|---|---:|
| UNFILLED | 2 |
| REJECTED | 1 |

## 市场数据分段

| 周期 | 数据源 | K线 | 股票 | 首条 | 末条 |
|---|---|---:|---:|---|---|
| 15m | sina | 14474 | 194 | 2026-08-14 09:45:00 | 2026-08-26 10:15:00 |
| 15m | tencent | 14487 | 194 | 2026-08-14 09:45:00 | 2026-08-26 10:15:00 |
| 1m | tencent | 198900 | 199 | 2026-08-14 09:30:00 | 2026-08-26 15:00:00 |
| 60m | sina | 3688 | 194 | 2026-08-14 10:30:00 | 2026-08-26 10:30:00 |
| 60m | tencent | 3702 | 194 | 2026-08-14 10:30:00 | 2026-08-26 10:30:00 |

## 四个重点案例原始路径

### 凯莱英（002821）
- 2026-08-14 11:22:00｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 168.79
- 2026-08-17 09:30:06｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 170.19
- 2026-08-18 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 176.58
- 2026-08-19 09:30:13｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 176.0
- 2026-08-20 09:30:09｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 180.12
- 2026-08-21 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 177.93
- 2026-08-24 09:30:10｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 170.0
- 2026-08-25 09:30:07｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 156.48
- 2026-08-26 09:30:12｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 174.53

### 赤天化（600227）
- 2026-08-14 11:22:00｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.29
- 2026-08-17 09:30:06｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.37
- 2026-08-18 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.35
- 2026-08-19 09:30:13｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.69
- 2026-08-20 09:30:09｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.62
- 2026-08-21 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.66
- 2026-08-24 09:30:10｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.96
- 2026-08-25 09:30:07｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.9
- 2026-08-26 09:30:12｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 3.72

### 多氟多（002407）
- 2026-08-20 09:30:36｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 36.52
- 2026-08-20 10:55:06｜V2_E2_STRUCTURAL_SUPPORT_REVERSAL｜state_change｜IDLE -> BUY_PROBE｜价格 36.63

### 彤程新材（603650）
- 2026-08-14 11:22:00｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 64.0
- 2026-08-17 09:30:06｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 64.88
- 2026-08-18 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 68.25
- 2026-08-19 09:30:13｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 68.0
- 2026-08-20 09:30:09｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 69.0
- 2026-08-21 09:30:08｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 65.47
- 2026-08-21 14:35:06｜V2_E2_STRUCTURAL_SUPPORT_REVERSAL｜state_change｜IDLE -> BUY_PROBE｜价格 64.66
- 2026-08-24 09:30:10｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 64.62
- 2026-08-25 09:30:07｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 62.31
- 2026-08-26 09:30:12｜V2_WAIT｜state_change｜IDLE -> WAIT_MULTI_PERIOD｜价格 62.7

## 当前评级

- 基线总分：**42/100（D）**
- 本分数是发布成熟度评级，不是收益评级；后续阶段必须用测试和回放证据替换人工保守分。

| 维度 | 得分 |
|---|---:|
| 数据正确性与时效 | 7 |
| 候选发现与覆盖 | 6 |
| 逐股状态跟踪 | 6 |
| 策略识别质量 | 5 |
| 执行与成交能力 | 6 |
| 持仓和风险管理 | 5 |
| 复盘和证据能力 | 4 |
| 工作台与消息体验 | 3 |

## 基线结论

1. 按 scenario 建主键会把同一股票的连续过程拆散，无法证明 WAIT 到 TRIGGER 的真实转移。
2. 数据故障、结构等待和普通观察尚未在主状态层严格分开。
3. 零订单不能仅解释为策略谨慎，因为上游状态漏斗没有保存足够的反事实证据。
4. 后续先修台账和数据不变量，再评价阈值及收益表现。
