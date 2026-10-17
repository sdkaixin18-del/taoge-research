# 强势回调与超跌反抽研究程序

按课程中的“环境与当前预期—对象选择—承接—空间—退出”顺序，输出两类日终观察候选，再用完整5分钟K线逐根验证。成交额只判断示例交易规模的可行性，不按名次淘汰。当前只有一套实现，旧版规则、代码副本和演示包已移除。

[研究协议](研究协议.md)说明课程时间点、计算定义及验证边界；[实际回放](../桃哥模式/反抽筛选回放_20261008.md)包含10月8日重跑与另外4个连续日期的全部结果。当前结果尚未证明筛选有效，事件及预期证据不全时只输出技术候选。

## 两类候选如何区分

| 类别 | 日终价格定义 |
|---|---|
| 强势回调 | 近20交易日收益为正且不弱于科技ETF515000，近5日收益为负 |
| 超跌反抽 | 不属于前一类；近5日价格下跌至少1个ATR，收盘距近20日最高价至少2个ATR |

最近20日日均真实成交额至少为“示例单笔金额÷允许日均参与比例”。当前示例为10万元、0.1%，即1亿元；不是推荐仓位，也不证明分钟级容量。两类没有名额限制，不保证每天有候选。窗口、ATR倍数、成本及盈亏比都是研究参数，课程没有给出这些数值公式。

同组最弱同伴可作为价格压力代理，不能据此认定事件因果。缺少同伴时用市场参照并标明弱证据；同伴少于两个不能升级为完整情境确认。当前预期、压力关系和失效条件须由带来源、首次可见时间、有效期限的情境记录补齐。

## 安装和收盘后运行

Python 3.11以上。离线筛选、回放、报告、测试只用标准库；联网采集需要`baostock==0.9.4`。没有账户、交易密钥或下单接口。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[data]'
python -m taoge_screen run --date 2026-10-08 --universe /srv/taoge/universe.json --rules 配置/反抽规则.json --data-dir /srv/taoge/data
```

在程序目录执行，也可安装后使用`taoge-screen`命令。时区固定UTC+8。日终运行须在指定日15:10以后，并等供应商发布完整当日日线；这不保证15:10供应商已更新。缺少当日数据或非交易日不会把上一日旧价当作当日。未安装定时任务，未连接服务器。

输出按日期与池/规则哈希分目录。快照采用原子写入，相同内容可重复，不同内容拒绝覆盖；修订行情应使用新目录并比较。退出码2表示数据、配置或执行不完整；退出码0只表示研究任务完成，不表示存在可买信号。

## 分步筛选及次日验证

```sh
python -m taoge_screen fetch --universe universe.json --start 2026-08-01 --end 2026-10-08 --out data/daily_screen.json
python -m taoge_screen screen --universe universe.json --daily data/daily_screen.json --rules 配置/反抽规则.json --asof 2026-10-08 --out data/screen.json
python -m taoge_screen fetch --universe universe.json --start 2026-08-01 --end 2026-10-16 --out data/daily_outcomes.json
python -m taoge_screen fetch --universe universe.json --start 2026-10-09 --end 2026-10-16 --interval 5m --out data/minutes.json
python -m taoge_screen validate --snapshot data/screen.json --daily data/daily_outcomes.json --minutes data/minutes.json --date 2026-10-09 --out data/validation.json
python -m taoge_screen report --snapshot data/screen.json --validation data/validation.json --out data/report
```

验证日必须是筛选后的第一交易日；再下一日用于T+1开盘对照。验证命令读取封存快照，不重选股票。入选、未入选、未触发、空间不足、成交不确定和数据不足全部输出。HTML可按日期、股票、类别和路径检索；CSV保留全部字段。连续回放使用配置中的固定日期计划：

```sh
python -m taoge_screen replay --universe universe.json --daily daily.json --minutes minutes.json --rules 配置/反抽规则.json --freeze freeze.json --out result
```

`freeze.json`保存`frozen_at,rules,rules_sha256,universe_sha256`，须在分钟采集前生成。本地案例另外保存协议哈希与执行修正记录；哈希可检查内容一致，不是第三方可信时间戳。所有日终筛选先完成，再评估各日结果，不搜索参数。

## 输入格式

股票池包含`stocks`和`references`。至少配置一个与规则一致的市场参照；可以增加有事前依据的压力工具。示意：

```json
{
  "schema_version": 1,
  "universe_id": "my-observation-pool",
  "reconstruction": false,
  "stocks": [{
    "symbol": "sz002428",
    "name": "云南锗业",
    "member_known_at": "2026-09-30T15:00:00+08:00",
    "source_url": "实际关注依据的链接",
    "peer_group": "optical",
    "peer_group_known_at": "2026-09-30T15:00:00+08:00",
    "peer_group_basis": "实际主题依据，勿将当前成分倒填历史",
    "event_review": "unknown",
    "corporate_action_review": "unknown",
    "context_records": []
  }],
  "references": [{
    "symbol": "sh515000",
    "name": "科技ETF",
    "member_known_at": "2019-08-16T00:00:00+08:00",
    "source_url": "https://www.fsfund.com/fund/515000/fundDetail.shtml?tabIndex=9"
  }]
}
```

核查状态为`unknown/clear/adverse`。非未知状态须提供对应的`event_review_at,event_review_source`或`corporate_action_review_at,corporate_action_review_source`；晚于筛选时点或没有来源仍按未知。已确认不利事项不入选，价格断点不能只靠写一个`clear`绕过。

情境记录格式如下；示意字段不是已经核查的事实：

```json
{
  "known_at": "2026-10-08T14:00:00+08:00",
  "valid_through": "2026-10-08T15:00:00+08:00",
  "state": "active",
  "premise": "当前预期及需要观察的变化",
  "link_basis": "该标的为何受到此压力影响的证据",
  "expectation_source": "当前预期尚在的原始资料链接",
  "pressure_symbol": "已放进stocks或references的实际代码",
  "invalidation": "什么变化使当前理由失效",
  "source_url": "事前资料链接"
}
```

状态支持`active/invalidated/unknown`。超时或盘后发布的记录不清除盘中未知；已知失效则退出候选。程序不会根据未来涨跌生成理由。参考工具是市场比较口径，科技ETF不是各细分行业的完整指数，也不能自动证明某个股票是其成分股。

行情顶层须包含`interval,price_basis,amount_unit,volume_unit,calendar,universe_sha256,stocks`。日线字段为`date,open,high,low,close,preclose,volume,amount,trading,is_st`，可附`turnover`；分钟线另含带时区的结束时间`end`。金额为人民币元，量为股；ETF份数按同等价格×份数口径。只使用真实量额，不用收盘价×成交量估算成交额。

## 盘中路径与数据处理

先出现下探，再等股票低点不再降低、越过前根高点并站上VWAP，同时检查市场、固定压力对象与同伴反馈。用下一根开盘价重新核算成本后空间，最多一次参与假设；目标是筛选日收盘，失效位是触发时最近3根低点减一档。完整数值在[配置](配置/反抽规则.json)。

同根K线同时触及目标和失效位，保留先后未知。入场K线只有一字价格时不假定成交。普通A股新买仓位的日内目标与失效只是路径标签，另列下一交易日开盘的含假设成本变化；没有实际成交、持仓和费用核对，不称为账户收益。

历史窗口、验证日或隔夜存在前收断点时，保留“价格口径未统一”，不计算跨口径收益；需要先接入公司行动及可追溯调整因子。当前宁可保留待核查，也不把送转后的降价当作超跌。日线与分钟不一致另列，不用其中一个悄悄覆盖另一个。

参考缺失暂停整轮；单股缺失保留该股错误，不影响其他股票的市场比较分母。同伴分钟缺失时不把剩余少数样本假装成完整组。资料是事后下载版本，数据实际到达时间和供应商修订历史尚未验证。全市场规模还需评估接口限额、历史成员及更新耗时。

## 测试和离线复现

```sh
python -m unittest discover -s tests
python 演示回放.py --data /path/to/demo --out /path/to/result
```

本地[服务器运行包](../课程视频/双模式连续回放_20261018/服务器运行包/taoge-screen-demo.zip)解压后执行`python3 run_demo.py`，无需联网或第三方包，会检查5个日终快照与95条验证记录的哈希。原始行情、图、封存与报告只在Git忽略的[素材目录](../课程视频/双模式连续回放_20261018/)保存。当前52项测试通过，覆盖未来数据隔离、绝对容量、两类互斥、情境时间、完整分钟、下一根入场、跳空、同根双触、VWAP单位、价格断点、T+1及全部样本导出。
