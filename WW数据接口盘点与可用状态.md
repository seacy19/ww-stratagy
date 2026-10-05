# WW 数据接口盘点与可用状态

盘点日期：2026-10-05。此文件记录同机项目代码、历史审计和不含凭据的连通性检查；不保存任何密钥。

## 当前结论

你之前接入的多类接口找到了，主要集中在同机的 QuantAgentChat 数据代理。其代码目录登记 **113 个行情/金融数据端点**，另有 Finsight 新闻检索；WW 当前项目自身还有巨潮公告抓取脚本、Tushare 适配器及已经保存的本地历史数据。

但“目录里有端点”不代表当前凭据有权限，也不代表在这台 Mac 上实时可访问。状态应区分如下：

| 连接/入口 | 盘点证据 | 当前状态 |
|---|---|---|
| Codex MCP 注册表 | 本机 `~/.codex/config.toml` | 当前只有 `node_repl` MCP 服务；股票行情接口没有作为此 Codex 会话的 MCP 工具注册 |
| QuickSync 金融数据代理 | QuantAgentChat 的 `server/quicksync.ts` 有统一 JSON HTTP 客户端；`server/.env` 配有代理 URL 和 token | 地址配置为明文 HTTP。无凭据的 HTTPS 探测发生 TLS 错误；没有把已有 token 发送到 HTTP。无凭据 HTTP 探测被服务以 `40101` 拒绝。因此目前不能安全确认有凭据请求是否可用 |
| QuickSync 113 个金融数据端点 | `server/quicksync.ts` 的 `KNOWN_APIS_RAW` 代码目录 | 代码已登记；逐接口实时权限需要在修好 HTTPS 后分别验证 |
| Finsight 新闻检索 | QuantAgentChat 的 `server/data/finsight.ts`，支持关键词/语义检索，代码列出 Eastmoney、10jqka、一财、华尔街见闻等来源 | 配置了服务地址；本次无凭据轻量请求遭远端断开，目前不可访问 |
| 巨潮公告 | WW 的 `build_mainline_pit_data.py`、`build_2019_evidence_pack.py` 和历史审计记录 | 历史接口与 PDF 下载曾成功；公告元数据、来源标识和 PDF 哈希已落库。不是当前 Codex 的独立 MCP 连接 |
| Tushare 直连接口 | WW 的 `tushare_data.py` 和数据建设/审计脚本 | 适配器与本地历史缓存存在；直连脚本依赖 `TUSHARE_TOKEN` 或原 Windows Tushare MCP Server 路径。当前 shell 没有 Tushare token，原脚本仍有 Windows 绝对路径 |
| WW 本地历史缓存 | `proxy_cache/`、`data/mainline_pit/` | 本地已有约 1 GB 行情缓存与 170 MB PIT 证据数据，可支持离线工作；内容覆盖和 PIT 缺口以既有审计报告为准 |

本次连通性测试只发了不含 token 的小型探测请求，没有读取、显示或上传 `.env` 中的任何凭据，也没有下载市场数据。

## 113 个端点覆盖范围

下面是代码登记数量，按 `KNOWN_APIS_RAW` 的分类统计；并非 113 个端点均有权限。

| 类别 | 数量 | 主要接口示例 |
|---|---:|---|
| 证券与基础资料 | 14 | `stock_basic`、`stock_company`、`namechange`、`stock_st`、`stk_limit`、`suspend_d`、`trade_cal` |
| 行情 | 12 | `daily`、`weekly`、`monthly`、`stk_mins`、`rt_k`、`rt_min`、`adj_factor`、`daily_basic` |
| 财务 | 9 | `income`、`balancesheet`、`cashflow`、`fina_indicator`、`forecast`、`express`、`fina_mainbz`、`dividend` |
| 股东 | 6 | 前十大股东、股东人数、增减持、质押 |
| 公司行动 | 3 | 回购、解禁、股权变动类数据 |
| 研报 | 2 | 券商预测与评级 |
| 技术 / 因子 | 6 | `stk_factor`、筹码分布、因子目录与估值快照 |
| 资金 / 两融 | 15 | `moneyflow`、`moneyflow_dc`、`moneyflow_ths`、行业资金、融资融券、北向汇总 |
| 市场情绪 | 8 | 涨跌停池、连板、开盘啦、龙虎榜、热榜 |
| 指数与行业 | 15 | `index_daily`、`index_weight`、`index_member_all`、申万/中信、东财/同花顺板块 |
| 基金 | 11 | ETF/基金基本资料、净值、份额、持仓、分红、日线 |
| 公告 / 互动 | 3 | `anns_d`、上证/深证互动问答 |
| 新闻 | 3 | `news`、`major_news`、`cctv_news` |
| 宏观 | 6 | Shibor、GDP、CPI、PPI、货币供应、PMI |

## 已知权限与时点约束

- 2026-07-22 的 WW 接口审计记录：Tushare `index_member_all`、`fina_mainbz`、`disclosure_date` 当时可用；巨潮公告列表和 PDF 当时可抓取。
- 同一审计中，当前 Tushare 账号对 `dc_member`、`ths_member`、`news`、`major_news` 被权限拒绝。历史概念成员和系统性新闻仍不能仅凭接口目录认定可用。
- QuantAgentChat 的提示词明确禁用 `ths_hot`、`kpl_concept` 等一部分端点；有目录记录也可能不能由工作流调用。
- 财报数据需要按真实披露时点使用。接口能返回报告期或历史值，不代表这些数值在当时已经公开；实时端点不应用来填充历史回测。
- Finsight 新闻源有关键词和语义两种模式，语义能力受后端 embedding 服务影响；当前无网络成功证据。

## 安全与接入建议

1. 先让 QuickSync 提供有效 HTTPS 地址与可信证书，再用已有代理凭据验证权限。不要把 token 放进 HTTP 请求。
2. 修复 Finsight 当前地址的可达性后，再确认新闻源覆盖、时间戳精度和历史检索范围。
3. 将 QuickSync 用作 WW 的统一在线数据适配器，保留现有离线缓存作为可复现回测输入；在安全连通前不把在线取数标成已接通。
4. 对 `dc_member`、`ths_member`、新闻、分钟线、送转上市日和历史涨跌停逐接口留存成功/拒绝审计，不根据名称推断权限。
5. 修复直连脚本依赖 Windows MCP Server 的绝对路径，并只从环境变量读取 `TUSHARE_TOKEN`。

## 关联文件

- QuantAgentChat 数据客户端：同机项目 `QuantAgentChat/server/quicksync.ts`
- QuantAgentChat 新闻适配器：同机项目 `QuantAgentChat/server/data/finsight.ts`
- WW Tushare 适配器：`tushare_data.py`
- WW 数据源实测：`WW主线测试_数据源接口实测.md`
- WW 数据就绪审计：`WW主线测试_数据就绪审计表.md`
- WW 数据流程与参数：`WW量化流程与参数手册.md`
