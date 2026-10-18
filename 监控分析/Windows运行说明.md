# Windows Codex接入

能力随本仓库保存，**当前Mac上的4个试建任务均已删除，没有保留本地定时调度**。服务器静默行情采集继续独立运行；Windows侧负责只读获取、检索、分析和重要变化提示。目标Windows机器尚未连接，本页是可执行接入流程，不代表那台机器已经验证。

## 准备和手动运行

在Windows上准备Python 3.10及以上、OpenSSH客户端和Codex，取得本仓库最新文件。使用自己已有的SSH身份配置`cb-monitor-shanghai`别名，确认能免交互读取服务器。私钥及主机配置独立安全迁移，不放本仓库；Mac配置中的绝对密钥路径需改为Windows实际路径。

在仓库根目录的PowerShell执行：

```powershell
if (-not (Test-Path "监控分析/client_config.local.json")) {
    Copy-Item "监控分析/client_config.example.json" "监控分析/client_config.local.json"
}
# 按Windows实际路径编辑本地配置。
py -3 "监控分析/fetch_snapshot.py" --out "课程视频/服务器监控分析/latest_snapshot.json"
```

配置中的`~/.ssh/config`会按Windows用户目录展开；也可写`C:/Users/自己的用户名/.ssh/config`。SSH程序若不在PATH，通过`ssh_binary`填写本机OpenSSH可执行文件路径。`--ssh-config`和`--ssh-host`可临时覆盖配置，输出文件始终由`--out`指定。读取脚本仅依赖Python标准库；Windows不运行服务器的Linux采集进程。

将仓库内的技能复制到项目发现目录，首次执行前确认没有同名自定义技能：

```powershell
New-Item -ItemType Directory -Force ".agents/skills" | Out-Null
if (-not (Test-Path ".agents/skills/cb-opportunity-research")) {
    Copy-Item -Recurse "监控分析/skills/cb-opportunity-research" ".agents/skills/cb-opportunity-research"
}
```

技能引用仓库内分析流程和模式文档，应作为项目技能使用，不单独复制到与仓库无关的位置。Codex在项目内发现`.agents/skills`；如界面没有出现可重新打开项目，见[官方技能说明](https://learn.chatgpt.com/docs/build-skills)。

在Windows Codex打开该仓库，手动调用：

> 使用 $cb-opportunity-research 手动分析一次。读取服务器快照，结合韭研公社、X及官方来源，检查市场、板块、股债候选和失败条件。先不要创建定时任务，报告数据日期、缺口及本地读取与检索是否正常。

检查实际SSH权限、源时间、交易日、至少一个可核实原文及失败时降级；未开市时只验证闭市链路，首个交易日再验收实时覆盖。Codex的网络/文件权限需在这台机器可用；X或网页不可读不能冒充已覆盖。无需为本能力另接模型API。

## 以后在Windows启用定时研究

当前只保存下面的任务要求，没有在任何新机器创建任务。Windows手动验证通过且用户要求启用时，由那台Codex调用可用的任务工具创建当前研究对话的定时唤醒；若该安装没有相应能力，明确说明，再由用户选择替代调度，不能假定已经运行。

| 阶段 | 北京时间与范围 | 附加研究目标 |
|---|---|---|
| 盘前 | 交易日09:10 | 新催化/更正、旧候选延续及待核池，上一交易日行情写明日期。 |
| 上午 | 09:30—11:30每10分钟 | 早封履历、板块助攻、债确认和价格空间，首日优先核验实时覆盖。 |
| 下午 | 13:00—15:00每10分钟 | 股市/债市共同风险、债兑现和已有计划保护；13:00没有完成午后分钟不制造信号。 |
| 盘后 | 15:15 | 核对全部候选、放弃、未触发、失败与漏报及新闻更新，保留路径。 |

各任务共用提示词：

> 使用 $cb-opportunity-research，遵循仓库里的“监控分析/分析流程.md”，读取本地checkpoint及最新服务器行情。新增新闻由本地搜索韭研公社、X及官方/可靠来源；材料和分析只存本地，不改服务器及原公社3条展示，不发飞书、不交易。只处理增量、去重且不重叠。无重要变化安静结束，只有影响候选/计划的新证据、关键更正、市场风险升级、目标/失效/到时或持续数据故障及恢复时提醒。休市跳过，行情陈旧或核心数据不全暂停新增参与判断，不把候选当持仓。

配置任务时使用北京时间或保证Windows系统时区为中国标准时间；核对下一次实际运行时间。电脑和Codex需要保持运行。每10分钟是计划触发间隔，不保证分析和通知准时完成，也不替代原监控的快速行情提醒。
