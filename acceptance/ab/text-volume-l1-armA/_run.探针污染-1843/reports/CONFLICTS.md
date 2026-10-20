# CONFLICTS.md

约束、冲突与归因。三类分歧分开报，不合并（`AGENTS §6`）。

## 硬约束（含「这条约束为什么存在」）

| id | kind | target | rationale |
|---|---|---|---|
| `C-cites` | custom | `output/brief.md` | 结论要能指回材料里的具体行——用没用到读到的材料，这是判据 |
| `C-length` | custom | `output/brief.md` | 契约声明了体量目标：少于 4000 字不算写完 |
| `C-provenance` | provenance | `output/brief.md` | 每条值都要能指回一份快照，否则无法复核 |
| `C-write` | custom | `output/brief.md` | 产物必须真的落在交付位置，且必须是成篇的文字 |

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
