# Examples

`holdings.sample.json` 使用公开基金名称和虚构金额，不代表任何真实账户。

运行：

```bash
./examples/run_mock.sh
```

会在 `examples/generated/` 生成离线演示数据和报告。仓库内提交的 `sample-analysis.json`、`sample-report.md` 和 `sample-report.html` 是一次冻结的演示结果，方便不运行代码时了解输出结构。

Mock 数据只验证流程、门控和页面布局：

- 所有收益和资金流均为合成数据。
- 基金规模、季度持仓、真实行情和两融历史不会在 mock 中伪造。
- 缺失区块会保留并明确显示降级原因。
- 任何 mock 结论都不能用于投资决策。
