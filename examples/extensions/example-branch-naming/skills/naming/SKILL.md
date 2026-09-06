---
name: naming
---

# Example Branch Naming

`example-branch-naming` 是替换 `branch.naming` capability 的最小 Provider 示例，命名规则是 `{type}/{owner}-{slug}`（Core 默认是 `{owner}/{type}/{slug}`）。

复制本示例后，只需要改 `commands/naming.py` 里的 `branch = f"..."` 一行为团队自己的命名规则；其余部分（stdin 读请求、stdout 写结果契约）保持不变。
