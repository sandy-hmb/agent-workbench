---
name: workspace-instruction
description: Classify and maintain layered workspace instructions without duplicating rules.
---

# Workspace Instruction

用于判断规则应放在哪一层，并维护工作区的分层规范。

## 分层判据

按 `instructionContext.rules` 的单调收窄顺序读取：根 `AGENTS.md` 约束 Kit，`.workspace/AGENTS.md` 约束工作区，仓库 `AGENTS.md` 约束单仓，目录 `AGENTS.md` 约束就近范围。CONTEXT.md 与仓 profile 是事实轴，使用 `status --context-sources` 定位，不把事实复制进规则轴。

规则决策：跨所有工作区的约定放根层；只对本工作区成立的约定放 `.workspace/AGENTS.md`；只对一个仓成立的约定放仓根；只对一个目录成立的约定放就近目录。越具体的层只能收紧上层，冲突时就近规则优先。

## 冲突与维护

发现重复规则时保留适用范围最广的权威条目；下层只有实质收紧时才保留。无法判断时保留原文并报告重复。规则入口必须是普通 `AGENTS.md` 文件；`sourceInstruction` 指向 README 或 CLAUDE 等非规范文件时只能提示，不自动改写。

维护模式只读根层和业务仓层；工作区模式写入仅限 `.workspace/` 及当前需求授权的目标仓，不覆盖用户自定义段落或事实文件。
