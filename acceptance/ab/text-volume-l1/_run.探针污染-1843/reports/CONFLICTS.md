# CONFLICTS.md

约束、冲突与归因。三类分歧分开报，不合并（`AGENTS §6`）。

## 硬约束（含「这条约束为什么存在」）

| id | kind | target | rationale |
|---|---|---|---|
| `C-cites` | custom | `output/brief.md` | 每一条实质结论后面都要标它的来源行号，写法是 `L<数字>`（例如 `L23`）。全文至少 12 处，且至少落在 6 个【不同的】行号上；行号必须落在材料真实行数之内。 |
| `C-length` | custom | `output/brief.md` | 体量目标：去掉空白与 Markdown 记号后，正文有效字符不少于 4000。 |
| `C-provenance` | provenance | `output/brief.md` | 溯源覆盖率不少于 0.6：每条实质结论都要能指回材料里的具体一行。 |
| `C-write` | custom | `output/brief.md` | 产物写在 output/brief.md，且必须是成篇的文字：至少 8 个段落，每段不少于 80 字。只写标题、提纲、或一句话结论都不算。 |

## 已声明盲区

- 本文只覆盖单机单实例的压测，多机张量并行没有数据

## 分歧

- 盲区 BlindSpot: 0
- 冲突 Conflict: 0
- 口径不一致 DefinitionMismatch: 0

## 收敛

- 决策: `arbitrate`
- 说明: no block produced a candidate

## 归因

- failure: assembly failed on 1 block(s)
- blame: `model`
- confidence: `likely`
- experiment: `trace`
- actions: `switch_worker`, `replenish_semantics`, `rearrange_context`
  - evidence: `produce_failure(T-01)`
  - evidence: `produce_failure(T-01)`
