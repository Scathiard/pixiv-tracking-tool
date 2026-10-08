# 可靠性仪器观测方法修订专项

- 用途：先保留旧采样耦合的 mock 故障红证据，再专项验证资源／真实任务 HTTP／关闭后有界持久审计与独立门槛。
- 范围：仅平铺 tests 仪器及约定，不修改 app/src、索引、配置或真实图库。
- 数据：无真实数据；反例创建微型合成 SQLite 和模拟 HTTP/故障；获准追加 integration-01 的正式 run_case 1000 works/1024 history/4096-byte/12s 仪器整合自检，不算规模验收，不新增 15k 长负载。
- 保留：脚本、首次红证据、真实子进程退出码、stdout/stderr 与新 pytest 临时目录保留，不自动删除。
- 方法边界：失去额外 COUNT 在线时延，增加同频真实任务详情 HTTP；不改判历史 -10，不称完全等价，不弱化 5s/2s/规模/8h/完整维护门槛。

- 旧观察器红证据：注入旧直接统计 read 的 mock ReadDeadline，真实 exit 1，资源记录丢失；old-probe-base.py 是与已记录 SHA 完全相同的 HEAD 原文，reproduce_old_coupling_red.py 可独立复现，原 red 输出保留。不是磁盘故障或真实断电验证。
- 定向验证：pytest-01 28 passed；复审修复表名/导入后 pytest-02 32 passed；加入总审计截止与非 JSON 状态反例后 pytest-03 34 passed (19.81s)。三轮均独立 basetemp、真实 exit 0、stderr 空、faulthandler 保留。
- integration-01：正式 run_case 的 1000/1024/4096、12 秒小型仪器整合自检，真实 exit 0、stderr 空。40 个混合 HTTP + 3 次真实任务 HTTP 成功，资源/维护各 3 个样本；35 批完整扫描五表 4195 行，正常关闭后重算四摘要。maintenance_completed=false（仍 planning / 12 pending），不证明完整维护或八小时规模验收。
- 生产 ef797cb / f540 源码前后未变；测试文件以 implementation-review-receipt.json 散列冻结，主会话审查后决定后续规模试跑。
