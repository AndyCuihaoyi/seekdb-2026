# Agent Prompt 项被判无效：诊断与改法

日期：2026-11-15
适用提交：`d0eed67d3c1cc51097e8daa15c0a9aa8d323cf6d`（分支 `2026-competition-round2`）

## 1. 现象

平台反馈（与得分版 `ae6514687` 那一轮**逐字相同**）：

```
MySQL 正式用例：通过；此项为准入条件，不计排名分
性能原始分：2617.947084	性能评测完成；原始指标 E = 2617.947084
Agent Prompt 原始分：Prompt结果无效、待复核、超出平台0–100范围或缺少材料摘要，
                     本项不参与排名；未截断或换算本地分数。
```

一句话里有四个可能的原因，平台没有指明是哪一个。**注意：得分版 `ae6514687`
（材料 81 974 B、21 个实例）报的是同一条错误**，所以「Agent Prompt 项从未被判过有效」，
上一轮把 schema 改成 `agent-learning-evidence-v3-unbounded`、补上 `material_summary`、
给每个实例补 `dimension`/`hypothesis_author`，**三条都不是根因**。

## 2. 排除法：把四个原因逐个查掉

| 候选原因 | 检查方式 | 结论 |
| --- | --- | --- |
| 超出平台 0–100 范围 | 材料的 `schema` 已是 `agent-learning-evidence-v3-unbounded`（题面 §2.4 指定的规则名）；`agent_prompt_score` 本身不设上限 | **不是** |
| 缺少材料摘要 | `material_summary`（what/how/outcome/evidence_note/known_gaps）在 `6fc24a492` 之后已存在 | **不是** |
| 文件读不出来 | 合法 UTF-8 JSON、顶层为对象、无重复键、无 NaN/Infinity、129 994 B < 131 072 B；两个校验器全绿 | **不是** |
| **待复核 / 结果无效** | 见下 | **是** |

题面 §3.4.4 规定，出现下列任一情况状态为 `NEEDS_REVIEW`，原始分暂为 `null`：

> + 任一维度出现证据不足，分数为 `null`。
> + 某一维度三轮最高分与最低分的差值达到或超过 `max(1.5, 0.5 × 该维度中位数)`。
> + 三轮对材料类型的判断不一致。

这三种都会产生「结果无效、待复核」这一条消息。**评审是三次独立进行的**，
所以「三轮分歧」和「某个维度被判证据不足」都会落到同一条消息上。

## 3. 根因

### 3.1 主因：把 Agent 代笔的提示词当成了学生的发言

材料里 `user_prompts[].source` 全部写死 `"student"`，`limitations.authorship` 写着
「`content` 是学生本人的输入，**逐字**来自会话记录，未作删改」，
`limitations.scope` 写着「所有 prompt 均为学生真实输入」。

但记录里，这 7 条长提示词是**监督会话的 Agent 起草、学生转发**的：

| 标记 | 长度 | Agent 起草时的原话 |
| --- | ---: | --- |
| `admission-gate` | 3 397 | 「## 给另一个 agent 的收口提示词（只做性能准入验证，不碰 prompt 部分）」 |
| `fix-complexity` | 10 088 | 「## 完整提示词」 |
| `deliver-fix` | 6 184 | 「## 给 agent 的提示词」 |
| `shape-no-where` / `whole-case-budget` | 8 532 | 「I now have all the facts. Here is the new prompt.」 |
| `remove-inverted` | 8 663 | 「I've verified everything. Here is the prompt.」 |

会话序列是「学生：给出提示词 → Agent：Here is the prompt → 学生把这段原样贴进来」。
Agent 自己在材料构建会话里写过一句：

> 「Also: up-01 to up-03 are from MY prompts (in this very conversation) —
> "1.今天还有两次额度…" "当前做的性能优化有哪些？对比得分的版本有优化吗？"
> "上一次成功得分的commit为什么没有内存OOM问题？…" These are questions I asked…」

题面 §3.4.1 写得很直接：

> 只给学生自身有证据支持的贡献计分：Agent 自动给出的优秀规划、技术解释、代码或修复，
> 不直接证明学生理解；日志本身也不证明学生能够解释和使用结果。

§4.4 同样要求「明确区分学生、Agent 和工具结果的来源」。**评审一旦抽查到一条
「student」标记的提示词实为 Agent 起草，就会判定材料对来源做了错误标注**，
进而怀疑全部实例的归属；三次评审里只要有一次这样判，就与另两次产生分歧，
落到 §3.4.4 的「三轮对材料类型的判断不一致」，原始分为 `null`。

这正是「货不对版」在评审眼里的形态：**不是代码与材料对不上，而是材料声称的作者与
记录里的作者对不上。**

### 3.2 并因：最近一轮的三个实例没有学生原话

`e-56` / `e-57` / `e-58` 标着 `hypothesis_author: "student"`，但是：

```
student_quote   = ""
student_evidence = null
```

原因是生成器里 `TURN_MARKERS` 没有收录最近一轮的三条学生发言
（`student-timeout-reading`、`student-drop-inverted`、`student-converge-gate`），
`index_of.get(marker)` 返回 `None`，于是证据块被写成 `null`。

§3.4.3 要求：

> + `student_evidence`：学生自身贡献的原文位置与片段。
> + `result_evidence`：实际结果的原文位置与片段。高于 0.5 分必须提供。

实例只挂 `result_evidence` 而没有 `student_evidence`，评审要么判它不构成实例，
要么判这一维度的证据不足——两种都指向 `null`。

### 3.3 次因：材料不自包含

材料里有 8 处「`[… 该条消息的后续部分见开发记录 …]`」，8 条 prompt 被截断，
被截掉的后半部分在**仓库外的开发记录**里。题面 §3.4.1：

> 评审只读取本次提交的单个 `conversation.json`……不会自动读取仓库其他文件、外部链接或
> 平台运行日志来补充学习证据。

被截断的恰好是 `admission-gate`、`fix-complexity`、`localise-timeout`、
`deliver-fix`、`shape-no-where` 等**信息量最大的几轮**。评审读不到被截掉的部分，
只能判「材料截断、缺失或完整性无法确认」，而 §3.4.3 对这种情况的规定是
「不武断判 0，应标记证据不足并复核」——又是 `NEEDS_REVIEW`。

## 4. 改法

### P0-1 标注真实作者

给每条 prompt 标真实作者，Agent 代笔的标 `"source": "agent_draft"`，并保留
「学生转发了它」这一事实。代笔的提示词**不能**作为 `student_evidence`。

### P0-2 补齐 e-56/57/58 的学生证据

把最近一轮的三条学生发言收进 `TURN_MARKERS`，让三个实例拿到真正的
`student_evidence`（学生原话 + JSON pointer）。

### P1-1 去掉外部依赖

不再截断 prompt：把「见开发记录」的尾巴换成材料内部的说明，或者把被截断的
内容压缩成**材料内自带的**摘要，保证材料自包含。

### P1-2 对齐摘要与代码

`material_summary` 的 `outcome` 还停在「性能原始分 1829.577226」和第八轮，
与本轮实际提测（E = 2617.947084、第九轮去掉倒排索引之后）不一致。
按题面「材料须真实反映该版本的开发学习过程」，摘要要写到当前 commit。

### P1-3 扩充校验器

`check_conversation_json.py` 增加：

* 每条 prompt 的 `source` 必须是 `student` 或 `agent_draft`，且 `agent_draft`
  的条目不得被任何实例当作 `student_evidence`；
* 每个 `hypothesis_author: "student"` 的实例必须有 `student_evidence`；
* 材料里不得出现指向仓库外部的引用（`见开发记录`）。

## 5. 预期

改完之后，五个维度都只由**学生自己的发言**支撑，每个实例都同时有
`student_evidence` 和 `result_evidence`，材料自包含。这样：

* 三次评审面对的是同一份「作者标注准确、证据齐全、不截断」的材料，
  产生「材料类型判断不一致」的概率显著下降；
* 任一维度都不会因为「证据不足」被判 `null`；
* 材料与当前代码（`d0eed67d3`）一致，不会再出现「货不对版」。

## 6. 已实施（提交 `1ef7f26d8`）

| 项 | 落地 |
| --- | --- |
| P0-1 作者归属 | `AGENT_DRAFTED_PROMPTS` 列出七条 Agent 起草、学生转发的提示词；`user_prompts[].source` 区分 `student` / `agent_draft`，后者带 `author_note`。材料里保留了 4 条代笔 prompt（其余被剪枝），**代笔文本一律不进 `student_evidence`** |
| P0-2 缺失证据 | `TURN_MARKERS` 补上 `student-timeout-reading` / `student-drop-inverted` / `student-converge-gate`；21 个计分实例**全部**同时有 `student_evidence` 与 `result_evidence`（原先 e-56/57/58 的 `student_evidence` 是 `null`） |
| P1-1 自包含 | 截断标记由「后续部分见开发记录」改为「在此处截断，后续内容未收入本材料」；九条被截断的 prompt 各配一条 `cut_summary`，把截掉的内容在材料内说清；材料中已无任何指向仓库外部的指针 |
| P1-2 摘要对齐 | `material_summary` 改为第九轮、E = 2617.947084、提测提交 `d0eed67d3`，并如实写出这次修正了什么；旧值 1829.577226 不再出现在 `outcome` 里 |
| P1-3 校验器 | `check_conversation_json.py` 新增四条断言；新增 `tools/check_prompt_authorship.py`，对着真实 transcript 双向核对每条 prompt 的归属（已用人为改错验证过它确实会抓） |
| 附带 | transcript 里有几处字节解码失败留下的替换字符（U+FFFD），现在统一命名为「[此处一个字节无法解码]」；材料里已无该字符 |

### 改后的材料

* 大小 128 986 B（限额 131 072 B），`conversation.json` 与 `agent/conversation.json` 逐字节一致；
* 维度覆盖：独立验证与质疑 7、取舍与反思 5、问题理解与自主拆解 4、技术理解与迁移 3、实验与反馈迭代 2；
* 构建可重现：重跑构建器后工作区无差异；
* **引擎代码与得分提交 `d0eed67d3` 逐字节相同**——本轮只动材料与工具。

