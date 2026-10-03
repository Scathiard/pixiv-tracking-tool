# 集中交付验证证据

对应 [实施与交付记录](../../2026-10-03-集中交付.md)。这里保存脱敏结果和合成图库截图；完整运行目录在主仓库 `.devtools/test-runs/`，不含真实图库和令牌。

| 文件 | 证明范围 |
| --- | --- |
| `backend.xml`、`task-compat.xml`、`task-final.xml` | 完整后端 499 项；其后旧在途分页兼容增量 32 项、最后 5 项；重复专项不叠加成新测试数 |
| `task-claim-2000.json`、`task-claim-20000.json`、`task-claim-100000.json` | 同一完整回归的领取 SQL、短写边界及实际查询计划 |
| `task-api-latency.json` | 十万项独立库、远端步骤等待时，20 次不同操作真实受理及本地详情／计数延迟 |
| `daily-browser.json` | 20 种画师几何、三张／列表尺寸、来源子页、周期、完整筛选、手机触摸、添加身份及真实单画师任务结果 |
| `browser-scale.json` | 真实 5,000 件／2,000 页、500 件分页完整性、持续窗口、多个短随机轮与资源采样 |
| `strict-startup-browser.json` | 正式界面确认后，真正旧 0002 DDL 合成数据库转换／启用／旧备份保留 |
| `reader-browser-final.json`、`reader-flow-320.png`、`reader-flow-1440.png` | 最终源码前端：固定历史版本、同版本缓存、隐藏后下一件、随机／顺序、控件、缩放、双击与后台回读的竞争；未加 final 的同名结果是修最后底栏前的保留记录 |
| `touch-browser-final.json` | 最终前端真实 CDP 触点的滑动、多指、长按、单／双击、缩放和位置稳定；未加 final 的结果保留先前轮次 |
| `source-smoke.json` | 源码模式真实本机下载、ZIP／时序、保留旧 GIF、统一维护与程序单例；执行时内部号仍为 .3，后续包必须单独核对 .4 |
| `artists-desktop.png`、`artists-mobile.png` | 合成画师列表的桌面／320px 画面 |
| `filters-mobile-last.png` | 320px 手机触摸到达完整标签尾部 |
| `exe-reading.json`、`exe-smoke.json` | 最终 EXE 的 34 项浏览器与 26 项本机下载／接口／媒体冒烟，散列和构建号一致 |
| `exe-strict-upgrade.json` | 普通离线 EXE 的真正旧库升级、确认前不写原库、再次启动复用、正常停止；失败页回放与真实转换明确区分 |
| `exe-flow-toolbar-320.png`、`exe-settings-wide.png`、`exe-upgrade-320.png` | 最终 EXE 的手机底栏、宽屏设置与首次升级界面 |
| `validation.json` | 本轮结果、源码提交与 EXE 散列、各层未验证边界 |

未列出的层次不能据此认定通过。大规模场景的后台为可见性事件模拟，远端回应及素材均为本地合成，不表示真实 Pixiv 或实体手机验收。普通 EXE 停止只操作隔离进程专属托盘窗口，不代表用户手点实体菜单已经验收。全部旧失败运行保留在 `.devtools/test-runs/`，没有为了得到通过结果而删除失败证据。
