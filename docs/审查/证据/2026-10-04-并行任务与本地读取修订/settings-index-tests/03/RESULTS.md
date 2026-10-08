# 精确复验结果

- 测试：原样重复 -02 的提交事件异常、联合 DDL 回滚、最终 receipt 回滚三项，未修改产品代码或断言。
- 运行时：同一 .devtools/v012-env/Scripts/python.exe；没有关闭 faulthandler 或更换环境。
- 捕获：父 Python subprocess.run 分别保存 stdout/stderr，并记录子进程真实 returncode。
- 结果：子 pytest returncode=0；3 passed，4.84 秒；stdout/stderr 没有 access violation，stderr 为空；JUnit XML 为 3 项、0 失败、0 错误、0 跳过。
- 结论：该三个确切用例在本轮正常通过；-02 导入阶段异常仍未归因，不能宣称运行时根因已修。
- 源码：保持冻结，未新增产品变更，不再自行运行测试；磁盘测试窗口释放。
- 证据：run_exact_tests.py、process.json、stdout.log、stderr.log、pytest.xml 和 pytest/ 合成库全部保留，不自动删除。
