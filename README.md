# WW 策略量化研究

WW is a point-in-time quantitative research project for identifying rising
industrial earnings trends, their real beneficiaries, and disciplined entry
and portfolio rules. The repository contains experimental research code; it
does not provide investment advice or broker execution.

## Research workflow

1. Freeze public evidence available at each decision date and allow a no-theme
   or no-position outcome.
2. Identify real value-capture companies in a theme and cross-check with
   bottom-up business and earnings evidence.
3. Rank eligible companies, then apply a separate price gate and freeze a
   next-session entry zone and stop.
4. Size positions from portfolio risk, cash, and exposure limits. Record fills,
   exits, costs, corporate actions, and rejected orders.
5. Save the complete configuration and input/code hashes, then assess
   parameters on chronological training and holdout periods.

The local-cache `price_proxy` mode tests price mechanics only. Formal `ww` mode
requires a frozen, dated evidence CSV. Missing fundamental history must not be
filled with a price proxy.

## Run

Requires Python 3.9+ and the repository's local historical data caches.

```bash
python3 -m wwquant.cli --help
python3 -m wwquant.cli audit --config configs/ww_price_proxy.json
python3 -m wwquant.cli run --config configs/ww_price_proxy.json --output runs/my_run
```

See [WW量化流程与参数手册.md](WW量化流程与参数手册.md) for the parameter
reference, evidence schema, validation rules, sample commands, and known
execution approximations. See [WW策略_整理总览.md](WW策略_整理总览.md) for the
broader research design.

The data caches and run outputs are local-only by default. The checked-in
configuration points to expected cache paths; obtain and audit your own data
before running. A backtest is an experiment, not a performance promise.

## 可配置研究流程（2026-10-05）

- [WW量化流程与参数手册.md](WW量化流程与参数手册.md)：完整研究流程、参数字典、数据边界与运行示例。
- [WW数据接口盘点与可用状态.md](WW数据接口盘点与可用状态.md)：同机 QuickSync/Finsight 接口目录、权限记录和当前连通性状态。
- `python3 -m wwquant.cli --help`：审计、组合回测、参数搜索与时间留出验证。
- [configs/ww_price_proxy.json](configs/ww_price_proxy.json)：全参数价格代理配置。
- [configs/ww_formal.json](configs/ww_formal.json)：需接入历史证据的 WW 配置。

新增流程是本地研究实现。历史 WW 证据不完整，日线成交也仍是执行代理，不能视为已经验证有效或可直接实盘。原事件回测的止损填充前视隐患已修复，相关旧结果需要重新计算。

本仓库只包含可复现研究流程、代码、配置和说明文档。个人会话记录、原始行情/财务数据、代理缓存、运行输出及本地凭据不纳入版本控制。历史研究材料如需复现，应单独导入并审计数据来源与可用日期；盲测结果尚未完成独立打分，不应表述为已经验证出 edge。
