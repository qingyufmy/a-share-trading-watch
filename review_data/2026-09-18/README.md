# 2026-09-18 专家评审数据包

本目录是最新已完成交易日 `2026-09-18` 的只读评审快照，生成于 `2026-09-20`。它用于核对盘前授权、盘中策略判定、信号状态、影子研究和模拟账户执行，不作为生产运行目录。

## 内容

- `reports/`：盘前、竞价、盘中、实时工作台及质量报告。
- `inputs/`：竞价候选、竞价路径摘要和完整竞价路径压缩文件。
- `runtime/signal_audit_20260918.jsonl.gz`：当日完整策略循环审计证据。
- `runtime/signal_state_full.sql.gz`：截至快照时的完整信号状态数据库 SQL 导出。
- `runtime/paper_trading_full.sql.gz`：截至快照时的完整模拟账户数据库 SQL 导出。
- `runtime/shadow_research_full.sql.gz`：影子研究台账 SQL 导出。
- `runtime/market_bars_20260918.csv.gz`：当日 90,680 根本地行情K线。
- `runtime/paper_close_20260918.json`：15:15 模拟账户收盘消息数据。
- 其余 JSON：当日龙头池、观察池、数据质量、最新信号和工作台运行状态。

## 账本规模

- 模拟订单：1,035
- 当前持仓：4
- 买入批次：82
- 账户快照：74,894
- 持仓快照：496,740
- 信号：6,136
- 信号事件：6,048
- 信号跟踪：3,242
- 跟踪事件：213,326
- 市场决策事件：7,194
- 影子研究事件：8；采样：903

## 恢复数据库

在本目录执行：

```bash
gzip -dc runtime/paper_trading_full.sql.gz | sqlite3 paper_trading_review.sqlite
gzip -dc runtime/signal_state_full.sql.gz | sqlite3 signal_state_review.sqlite
gzip -dc runtime/shadow_research_full.sql.gz | sqlite3 shadow_research_review.sqlite3
gzip -dc runtime/market_bars_20260918.csv.gz > market_bars_20260918.csv
```

恢复后的数据库仅用于评审，请勿替换生产数据库。

## 已知边界

- 9月18日没有归档到可验证的开盘啦盘中/复盘人气榜，因此本包不借用9月16日榜单补齐。
- 9月18日没有生成16:30正式盘后复盘 JSON；本包仅包含15:15模拟账户收盘数据和15:00盘中报告。
- 未纳入完整 `market_data.sqlite`（612MB）、重复体量较大的 `paper_trades.jsonl`（626MB）、日志、浏览器痕迹、通知凭据和本机路径配置。
- `signal_state_full.sql.gz` 与 `paper_trading_full.sql.gz` 是截至生成时的完整历史账本；按日判断必须使用记录中的 `trading_date`，不能把后续状态回填到9月18日盘中。
- 压缩文件的 SHA-256 校验值见 `SHA256SUMS`。

