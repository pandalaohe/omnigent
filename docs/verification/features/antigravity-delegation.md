# Antigravity 原生子代理验收

在部署变更后，使用 [feature map](../../../feature-map/README.md) 的隔离环境和临时工作区。
记录应用版本、agy 可执行文件及其摘要、父会话和原生 child conversation ID。
Omnigent child row 属于可选显示；检查权限和停止时，以 vendor cascade 为准。

## 自动检查

通过 checkout 的既有隔离 pytest 入口运行：
`tests/harnesses/antigravity_native/` 中的 guard、RPC、reader、steps、bridge 和
`test_delegation_proof.py`，以及 `tests/harnesses/test_permission_tiers.py`、
`tests/runner/test_permission_tier_runtime.py`、`tests/runner/test_native_interrupt_runner.py`、
`tests/test_harness_plugins.py`。将 `tests/inner/test_antigravity_native_executor.py` 单独运行。
这些检查使用模拟原生 RPC；真实 agy 验收仍按下文进行。

## 授权前的真实验收

1. 创建临时 agy 会话，将 tier 设为 `ask`。启动一个原生 `self` 子代理；批准启动。
   让子代理读 seed 文件，再尝试写一个全新 marker。保持写卡打开，确认 marker 不存在；
   拒绝写卡后，确认仍无文件，子代理报告拒绝。对子代理 shell 和 MCP 调用重复拒绝检查。
2. 另起子代理，执行有明确结束时间的等待命令，再写一个新的 marker。
   **先布置捕获，再批准等待命令。** 确认实际进程已启动后，通过父会话 Stop 停止。
   等待越过原定结束时间至少 30 秒，确认进程终止、无 marker、无后续工具调用。
3. 另起子代理直接写一个新的 marker。批准启动，保持写卡未回答，然后停止父会话。
   确认子调用终止、卡片撤销、无文件；父级和可选 child row 不得把未确认的结果报为成功。
4. 返回每项的时间、tier/revision、审批 ID、native child ID、终态和 marker 存在性。
   HTTP 200、父级 idle、mirror 退出均不能单独证明原生子代理停止。

## 经批准后记录证明

证明记录会改变权限。只有操作者明确批准，并完成上述拒绝和停止验收后，才在执行主机运行：

```sh
python -m omnigent.harnesses.antigravity_native.delegation_proof record \
  --bridge-dir '<bridge-dir>' --evidence '<passing-evidence-reference>'
```

它写入 `~/.omnigent/agy-delegation-proof.json`，绑定当前可执行文件、guard、适配器和 hook 配置。
重新测试受限 tier：已证明的原生 `self` 启动不出现 tier 卡；子代理工具仍遵守父级规则，
`send_message` 也照常判定。可执行文件或 guard 更新后重新验收和批准。
缺失或过期证明保留旧启动规则；已知覆盖失败在受限 tier 直接拒绝启动。
`full` 不参与证明比较和覆盖失败撤销。

完成后，通过隔离环境的正常停止入口结束本次创建的会话和进程。
