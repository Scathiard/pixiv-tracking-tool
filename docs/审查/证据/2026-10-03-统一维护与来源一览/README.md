# 统一维护与来源一览：验证证据

仅含合成图库、合成媒体、模拟主站与本机服务结果；不含真实图库、配置、令牌或真实账号响应。文件保留，不自动清理。

- `source-checks.json`、`coverage.json`：最终源码、构建、接口与分层验证索引及改动文件范围。
- `backend-first-failure.txt`、`backend-conflict-fix.txt`：首轮 445 通过、1 项失败，以及修复后 68 项回归；保留失败，不以旧结果代替最终验证。
- `backend-final.txt`：修复后完整 448 项回归通过，739.40 秒；177 条既有弃用提醒。
- `frontend-tests.txt`、`frontend-build.txt`：69 项前端测试、类型检查与正式静态构建。
- `browser-regression-source.json`：实际 5,000 件／2,000 页合成数据的基本阅览、窗口上限、主题与手机尺寸验证。
- `maintenance-source.json`：144 件实际合成数据库的来源分页、独立主页核对、统一任务提交；另有明确标记的 960 件元数据回放验证单画师长图库。三种尺寸的滚轮、卡片尺寸、资料头和入口点击均实际执行。
- `source-smoke-final.json`：最终源码在本机合成媒体服务上的真实下载、核验、补齐和统一操作，24 项通过；早期 `source-smoke.json` 只作过程记录。
- `browser-regression-exe.json`、`maintenance-exe.json`、`reader-controls-exe.json`、`exe-smoke.json`：最终独立包验证，29／19／36 项浏览器及 25 项包内冒烟通过；具体合成数据库、浏览器回放和未验收层见结果自身字段，EXE 退出码由基础验证明确记录为 0。

截图来自对应结果目录，均为合成图片。红框使用明确的合成删除证据；真实 404 不产生已确认删除。手机尺寸和触摸模拟不等于实体手机。

源码临时运行、初轮失败与完整截图保留在仓库根 `.devtools/test-runs/b12-*`；本目录只收录与结论直接有关的脱敏结果和代表截图。
