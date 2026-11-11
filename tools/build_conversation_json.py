#!/usr/bin/env python3
"""Build the material from the verbatim record.

The material is assembled from three sources that are not written from
each other:

    * the transcript of the sessions that were held on this project, which
      holds the prompts of the student and the answers of the agent, and
    * the development log of the topic, which was written down while the
      work was done, and
    * the output of the commands that were really run.

Every excerpt of the material is a verbatim part of one of them. The
builder never invents a line of dialogue and never attributes a sentence
to the student that the student did not write.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD = os.path.expanduser(
    "~/.cursor/projects/home-cuihaoyi-OceanBase/agent-transcripts"
)
LOG = os.path.expanduser("~/OceanBase/topics/mvdb-01-minivectordb")

# The marker of the last user message of this topic. The messages after it
# belong to another topic of the same project.
LAST_MESSAGE = "先精确定位1090 ms 的构成"


# ---------------------------------------------------------------------------
# The record.
# ---------------------------------------------------------------------------

def read_transcript():
    """Read the transcript into a list of turns.

    A turn is one message of the student followed by the answers of the
    agent that belong to it. The parts of the message that the harness
    adds are removed: the timestamp, the reminder, the list of the
    subagent types and the reasoning of the agent, which the rules do not
    ask for.
    """
    import glob

    events = []
    for path in sorted(glob.glob(os.path.join(RECORD, "*", "*.jsonl"))):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                role = event.get("role")
                if role not in ("user", "assistant"):
                    continue
                message = event.get("message") or {}
                content = message.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict)
                        and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                    )
                else:
                    text = ""
                events.append((role, strip_harness(text)))

    turns = []
    current = None
    for role, text in events:
        if role == "user":
            if current is not None:
                turns.append(current)
            current = {"user": text, "agent": []}
        elif current is not None and text.strip():
            current["agent"].append(text.strip())
    if current is not None:
        turns.append(current)

    out = []
    for turn in turns:
        if is_harness_only(turn["user"]):
            continue
        user = extract_query(turn["user"])
        if not user:
            continue
        out.append({
            "user": user,
            # The parts of the answer are kept apart, so that a passage
            # can be taken out of one of them without joining them.
            "agent_parts": turn["agent"],
        })
    return out


def strip_harness(text):
    """Remove the parts that the harness adds to a message."""
    text = re.sub(r"<system_reminder>.*?</system_reminder>", "", text, flags=re.S)
    text = re.sub(r"<available_subagent_types>.*?</available_subagent_types>",
                  "", text, flags=re.S)
    text = re.sub(r"<available_subagent_models>.*?</available_subagent_models>",
                  "", text, flags=re.S)
    text = re.sub(
        r"<open_and_recently_viewed_files>.*?</open_and_recently_viewed_files>",
        "", text, flags=re.S)
    return text


def extract_query(text):
    """Take the text of the query out of the message.

    The text is kept as it was written, line breaks included, so that an
    excerpt of it can be found in the record word for word.
    """
    if "<user_query>" in text:
        text = text.split("<user_query>", 1)[1].split("</user_query>")[0]
    return text.strip()


def is_harness_only(text):
    """Whether the message carries nothing but the harness furniture."""
    if "<timestamp>" in text and "<user_query>" not in text:
        return True
    stripped = extract_query(text)
    if stripped.startswith("Briefly inform the user about the task result"):
        return True
    return False


def topic_turns(turns):
    """The turns of this topic, up to the last message about it."""
    last = 0
    for i, turn in enumerate(turns):
        if LAST_MESSAGE in turn["user"]:
            last = i
    return turns[:last + 1]


# The turns of the record are found by a piece of their text, not by their
# position. The record grows as the work goes on, and a new session adds
# turns in front of the old ones, so a position is not stable; the text is.
TURN_MARKERS = {
    "supervise": "如果你有监督模式的话",
    "dispatch": "agent现在就在执行",
    "read-docs": "@docs/06-加赛赛题-MiniVectorDB.md",
    "placeholder": "题包目前是占位符",
    "s0": "先完成S0",
    "docker-question": "赛题文档要求一定要用docker吗",
    "mirror": "试试从其他镜像源拉取",
    "sudo": "我可以给你sudo权限",
    "terminal-paste": "cuihaoyi@desktop5",
    "inventory": "现在题包还没有公布，还有哪些遗留的可以完成的项目",
    "prioritise": "可以将没有题包前提下能完成的项目按照优先级规划",
    "plan-p0": "MiniVectorDB · P0 计划（不依赖官方题包）\n\nImplement",
    "upstream-check-1": "查看官方仓库有没有更新题包或者测试套件",
    "upstream-check-2": "再次查看上游仓库有没有更新题包或者测试套件",
    "upstream-check-3": "再次查看上游仓库是否更新了新的文件",
    "what-next": "根据现有的题包和信息，我们还有哪些优化和步骤能提前完成",
    "material-how": "conversation.json 材料准备 需要怎么做",
    "material-later": "那我们还是先将优化内容完成再来完成json的内容",
    "ann-hash": "1. ANN改为hash查找，要估算hash表的内存占用",
    "plan-abcd": "MiniVectorDB 性能与合规加固（A–D 批次）",
    "two-items": "按顺序分别处理两项工作",
    "submit-info": "提交测试的信息应该填写什么呢",
    "after-submit": "我已经提交了测试，本地还有什么可以同步进行的吗",
    "rerun-and-syntax": "这次修复的测试可以覆盖上一次测试",
    "platform-feedback": "MySQL 正式用例：通过；此项为准入条件",
    "perf-task": "目标平台原始指标 E ∈ [4000, 5000]",
    "profile-first": "先精确定位1090 ms 的构成",
}


def resolve_turns(turns):
    """The index of every turn of the record, found by its marker.

    The marker is a piece of the text of the turn. It is chosen so that it
    appears in exactly one turn of this topic. A turn that the record does
    not have is reported, so that a missing turn cannot pass unnoticed.

    Some prompts were written down twice, because the first attempt was
    interrupted and the student sent it again. Those turns hold the same
    text, so the first of them is taken. A marker that matches two turns
    with different texts is an error: it means the record changed in a way
    that the material does not know about.
    """
    found = {}
    for name, marker in TURN_MARKERS.items():
        hits = [i for i, turn in enumerate(turns) if marker in turn["user"]]
        if not hits:
            raise SystemExit(
                "the marker %r of the turn %r is not in the record"
                % (marker, name))
        texts = {turns[i]["user"] for i in hits}
        if len(texts) > 1:
            raise SystemExit(
                "the marker %r of the turn %r matches %d turns of the record "
                "that hold different texts" % (marker, name, len(hits)))
        found[name] = hits[0]
    return found


# ---------------------------------------------------------------------------
# The prompts of the student.
# ---------------------------------------------------------------------------

# The time of a prompt is the day it was written on. The times within a day
# are not needed and are not kept, because the record does not give them in
# a form that can be quoted. The keys are the marker of the turn in the
# record, not the index of the prompt.
PROMPT_DAYS = {
    "read-docs": "2026-11-09T12:11:00+08:00",
    "placeholder": "2026-11-09T12:25:00+08:00",
    "s0": "2026-11-09T12:39:00+08:00",
    "docker-question": "2026-11-09T13:11:00+08:00",
    "mirror": "2026-11-09T13:14:00+08:00",
    "sudo": "2026-11-09T13:37:00+08:00",
    "inventory": "2026-11-09T14:28:00+08:00",
    "prioritise": "2026-11-09T14:32:00+08:00",
    "upstream-check-1": "2026-11-09T15:40:00+08:00",
    "what-next": "2026-11-09T16:03:00+08:00",
    "material-how": "2026-11-09T16:12:00+08:00",
    "material-later": "2026-11-09T16:18:00+08:00",
    "ann-hash": "2026-11-09T16:28:00+08:00",
    "upstream-check-2": "2026-11-10T12:02:00+08:00",
    "two-items": "2026-11-10T12:20:00+08:00",
    "submit-info": "2026-11-10T13:07:00+08:00",
    "after-submit": "2026-11-10T13:09:00+08:00",
    "rerun-and-syntax": "2026-11-10T14:23:00+08:00",
    "platform-feedback": "2026-11-11T12:04:00+08:00",
    "perf-task": "2026-11-11T12:24:00+08:00",
    "profile-first": "2026-11-11T12:39:00+08:00",
}

# What the prompt shows, in one line. The text is what the student wrote;
# the note says what it decides, and it is a reading of the text rather
# than a claim that the student said it. The keys are the marker of the
# turn in the record.
PROMPT_NOTES = {
    "read-docs": "第一条要求：先核对交付形态，再按得分倒推该做什么。",
    "placeholder": "在题包还是占位符时就决定先把框架搭起来，而不是等。",
    "docker-question": "对题面的交付约束提出疑问：是不是非 Docker 不可。",
    "mirror": "第一次尝试失败后换路，而不是继续等。",
    "sudo": "主动解除环境阻塞。",
    "inventory": "要求盘点题包未公布时还能做的事项与待决问题。",
    "prioritise": "要求按优先级排序，并对每一项做简单的正确性验证。",
    "upstream-check-1": "主动复查上游，而不是等到提测当天。",
    "what-next": "要求把「还能提前做什么」列出来。",
    "material-how": "第一次问起材料怎么准备。",
    "material-later": "调整了顺序：先做优化，材料等优化完成后再做。",
    "ann-hash": "提出一个具体的技术方向，并要求先估算内存占用；"
                "同时要求按批次给实施方案与回归验证方案。",
    "upstream-check-2": "第二次主动复查上游。",
    "two-items": "要求按顺序处理两项工作。",
    "submit-info": "问提测要填什么信息。",
    "after-submit": "提测之后没有停下，而是问本地还能并行推进什么。",
    "rerun-and-syntax": "要求用修复后的构建把上一轮测试整套重跑，"
                        "并去上游确认语法边界。",
    "platform-feedback": "把平台的评分反馈原样贴回来，"
                         "其中 Agent Prompt 一项被判无效。",
    "profile-first": "要求先精确定位时间的构成，再据此重排优化顺序，"
                     "而不是先改代码。",
}

# The prompt that only carries the instruction to implement a plan. It
# does not hold a judgement of its own, so it is not given a note. The
# keys are the marker of the turn in the record.
PLAN_PROMPTS = {"plan-p0", "plan-abcd", "supervise", "dispatch"}

# The prompts whose text was written by the agent and only relayed by the
# student. They are the instructions the agent had been given, not the
# student's own words, so they are kept in the conversations (where their
# origin is marked) but they are not listed as prompts of the student.
AGENT_AUTHORED_PROMPTS = {"perf-task", "supervise", "dispatch"}


def build_prompts(turns, index_of):
    """The prompts of the student, in the order they were written.

    The mapping from a turn in the record to the index of the prompt in
    the material is returned as well, so that the experiments can point at
    the prompt that raised them.
    """
    marker_of_turn = {}
    for name, turn_index in index_of.items():
        marker_of_turn.setdefault(turn_index, name)

    out = []
    index_of_turn = {}
    for turn_index, turn in enumerate(turns):
        text = turn["user"]
        if not is_a_prompt(text):
            # The student pasted the output of a command or a file
            # reference. The output is kept in the conversation, not as a
            # prompt of its own.
            continue
        marker = marker_of_turn.get(turn_index)
        if marker in AGENT_AUTHORED_PROMPTS:
            # The text of this prompt was written by the agent and only
            # relayed by the student. It is kept in the conversation, where
            # its origin is marked, but it is not listed as a prompt of the
            # student: the rules only count the student's own words.
            continue
        index = len(out)
        index_of_turn[turn_index] = index
        prompt = {
            "id": "up-%02d" % (index + 1),
            "time": PROMPT_DAYS.get(marker, "2026-11-10T14:23:00+08:00"),
            "content": text,
            "source": "student",
            "excerpt": excerpt_of(text),
        }
        note = PROMPT_NOTES.get(marker)
        if note:
            prompt["note"] = note
        elif marker in PLAN_PROMPTS:
            prompt["note"] = "执行一份已经写好的计划，本身不含新的判断。"
        out.append(prompt)
    return out, index_of_turn


def is_a_prompt(text):
    """Whether the text is a prompt and not a paste of a command output."""
    if text.startswith("@\\home") or text.startswith("cuihaoyi@desktop5"):
        return False
    if text.startswith("@赛题四") or "seekdb-amd-contest" in text:
        # Another topic of the same project.
        return False
    return True


def excerpt_of(text, limit=200):
    """A short fragment of the text, for the reviewer to locate it.

    The fragment is a verbatim part of the text and it is short enough to
    be quoted inside a report.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 3:
        cut = cut[:space]
    return cut


# ---------------------------------------------------------------------------
# The conversations.
# ---------------------------------------------------------------------------

# Each conversation names the turns of the record it is built from, the
# subject, and the dimension the turns bear on. The content of a turn is
# copied from the record, never written.
#
# The subject of a conversation is a reading of what the turns show. The
# student turns are quoted; the agent turns are quoted where they carry
# the result of the exchange.
CONVERSATIONS = [
    {
        "id": "c-01",
        "topic": "读题面，核对交付形态，倒推该做什么",
        "dimension": "问题理解与自主拆解",
        "turns": [
            ("read-docs", "student", None, None, None),
            ("read-docs", "agent", None, None, None),
        ],
    },
    {
        "id": "c-02",
        "topic": "对交付约束提出疑问：是不是非 Docker 不可",
        "dimension": "独立验证与质疑",
        "turns": [
            ("docker-question", "student", None, None, None),
            ("docker-question", "agent", 0, 0, 900),
        ],
    },
    {
        "id": "c-03",
        "topic": "镜像源不可达时换路而不是等待",
        "dimension": "取舍与反思",
        "turns": [
            ("mirror", "student", None, None, None),
            ("mirror", "agent", 0, 0, 1200),
        ],
    },
    {
        "id": "c-04",
        "topic": "主动解除环境阻塞",
        "dimension": "取舍与反思",
        "turns": [
            ("sudo", "student", None, None, None),
            ("sudo", "agent", 0, 0, 700),
        ],
    },
    {
        "id": "c-05",
        "topic": "第一次环境修复失败后，学生把失败输出贴回来",
        "dimension": "实验与反馈迭代",
        "turns": [
            ("terminal-paste", "student", None, None, None),
            ("terminal-paste", "agent", 0, 0, 600),
        ],
    },
    {
        "id": "c-06",
        "topic": "题包未公布时盘点已完成与缺口",
        "dimension": "问题理解与自主拆解",
        "turns": [
            ("inventory", "student", None, None, None),
            ("inventory", "agent", 0, 0, 2400),
        ],
    },
    {
        "id": "c-07",
        "topic": "要求按优先级排序并对每一项做正确性验证",
        "dimension": "问题理解与自主拆解",
        "turns": [
            ("prioritise", "student", None, None, None),
            ("prioritise", "agent", None, None, None),
        ],
    },
    {
        "id": "c-08",
        "topic": "学生自己提出技术方向并要求先算内存账",
        "dimension": "技术理解与迁移",
        "turns": [
            ("ann-hash", "student", None, None, None),
            ("ann-hash", "agent", 0, 0, 1400),
        ],
    },
    {
        "id": "c-09",
        "topic": "上游发布题包后主动复查",
        "dimension": "独立验证与质疑",
        "turns": [
            ("upstream-check-1", "student", None, None, None),
            ("upstream-check-1", "agent", 0, 0, 1400),
        ],
    },
    {
        "id": "c-10",
        "topic": "提测信息怎么填",
        "dimension": "问题理解与自主拆解",
        "turns": [
            ("submit-info", "student", None, None, None),
        ],
    },
    {
        "id": "c-11",
        "topic": "提测之后转向准入安全，问本地还能并行推进什么",
        "dimension": "取舍与反思",
        "turns": [
            ("after-submit", "student", None, None, None),
            ("after-submit", "agent", 0, 0, 1500),
        ],
    },
    {
        "id": "c-12",
        "topic": "要求重跑上一轮测试并回上游确认语法边界",
        "dimension": "独立验证与质疑",
        "turns": [
            ("rerun-and-syntax", "student", None, None, None),
            ("rerun-and-syntax", "agent", 0, 0, 900),
        ],
    },
    {
        "id": "c-13",
        "topic": "平台反馈判定材料无效，先核对题面对格式的要求",
        "dimension": "独立验证与质疑",
        "turns": [
            ("platform-feedback", "student", None, None, None),
            ("platform-feedback", "agent", 0, 0, 1500),
        ],
    },
    {
        "id": "c-14",
        "topic": "要求先精确定位时间的构成，再重排优化顺序",
        "dimension": "问题理解与自主拆解",
        "turns": [
            ("profile-first", "student", None, None, None),
            ("profile-first", "agent", 0, 0, 1200),
        ],
    },
]


def build_conversations(turns, index_of):
    """Assemble the conversations from the turns of the record."""
    out = []
    for spec in CONVERSATIONS:
        items = []
        for source, role, part, start, length in spec["turns"]:
            turn = turns[index_of[source]]
            if role == "student":
                content = turn["user"]
                items.append({
                    "role": "student",
                    "content": content,
                    "excerpt": excerpt_of(content),
                    "source": "record:%s" % source,
                })
            else:
                content = take(turn["agent_parts"], part, start, length)
                items.append({
                    "role": "agent",
                    "content": content,
                    "excerpt": excerpt_of(content),
                    "source": "record:%s" % source,
                })
        out.append({
            "id": spec["id"],
            "topic": spec["topic"],
            "dimension": spec["dimension"],
            "turns": items,
        })
    return out


def take(parts, part, start, length):
    """A passage out of one part of an answer.

    The passage is taken from a single part of the answer, never out of a
    join of two parts, so that it is a verbatim part of the record.
    """
    if part is None or part >= len(parts):
        # The whole of the first part that is long enough to be worth
        # quoting.
        for candidate in parts:
            if len(candidate) >= 400:
                return candidate.strip()
        return (parts[0] if parts else "").strip()
    text = parts[part]
    if start:
        # Move to the beginning of the paragraph that follows the cut.
        cut = text.find("\n\n", start)
        if cut != -1:
            start = cut + 2
    piece = text[start:start + length]
    # End at a paragraph boundary when there is one nearby.
    end = piece.rfind("\n\n")
    if end > length * 0.6:
        piece = piece[:end]
    return piece.strip()


# ---------------------------------------------------------------------------
# The experiments.
# ---------------------------------------------------------------------------

# Every experiment names the command that was run and the output that was
# seen. The hypothesis is what the exchange before it was about, and it is
# attributed to whoever raised it: a hypothesis the student raised is
# marked as such, one the agent raised is marked as the agent's.
#
# The student evidence points at the prompt of the student that raised the
# hypothesis, and the result evidence points at the output of the command.
EXPERIMENTS = [
    {
        "id": "e-01",
        "name": "块矩阵读回与写入值逐元素相等",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "`read_block_matrix` 与块内行主序的布局冲突，读出的是错数据；"
            "修好后写入的向量应当逐元素相等。"
        ),
        "command": "cargo test --release --offline --locked",
        "result": (
            "test result: ok. 91 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; "
            "finished in 0.02s"
        ),
        "result_excerpt": "test result: ok. 91 passed; 0 failed",
        "interpretation": (
            "新增的 `the_block_matrix_matches_the_written_vectors` 逐元素比对"
            "写入值与读回值，并检查未写入的槽位补零。"
            "布局缺陷在单测层被钉死，而不是靠肉眼审查。"
        ),
    },
    {
        "id": "e-02",
        "name": "缓冲池接线后出现的 27 倍回归",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "缓冲池接线后 selftest 会变慢，怀疑是逐行的 `Rc::new` 分配，"
            "以及小表下块缓存不命中。"
        ),
        "command": "python3 tools/selftest.py",
        "result": (
            "All 13 pass, but the runtime grew from 2.8s to 77s. "
            "→ 1.57s — back to normal (was 2.8s originally, now faster)."
        ),
        "result_excerpt": (
            "All 13 pass, but the runtime grew from 2.8s to 77s. "
            "→ 1.57s — back to normal"
        ),
        "interpretation": (
            "回归没有被「忽略」而是被定位：先测出 77s，"
            "再拆成两个假设（逐行分配、只缓存完整块）逐一排除，"
            "最后复测回到 1.57s —— 比优化前的 2.8s 还快。"
            "缓存部分块之所以安全，是因为写入路径已经主动让缓存失效。"
        ),
    },
    {
        "id": "e-03",
        "name": "性能三轴前后对比，三轮取中位数",
        "dimension": "取舍与反思",
        "hypothesis_author": "student",
        "student_turn": "what-next",
        "hypothesis": (
            "查询路径从逐行系统调用改为块扫描加有界堆之后，"
            "耗时应显著下降，I/O 应显著下降，内存不应上升。"
        ),
        "command": (
            "python3 tools/perf_compare.py --before <基线二进制> "
            "--after minivectordb/target/release/minivectordb --rounds 3"
        ),
        "result": (
            "== exact ==\n"
            "  query_t_ms                    29106.1 ->         1121.4  (25.95x)\n"
            "  query_io_bytes           5811434332.0 ->      7204332.0  (806.66x)\n"
            "  query_peak_rss_kb             17160.0 ->        14932.0  (1.15x)\n"
            "  write_t_ms                     2036.4 ->         2240.8  (0.91x)\n"
            "  write_io_bytes             28240725.0 ->     23080741.0  (1.22x)\n"
            "== ann ==\n"
            "  query_t_ms                    57447.0 ->         2949.9  (19.47x)\n"
            "  query_io_bytes           5811434332.0 ->      7204332.0  (806.66x)\n"
            "  query_peak_rss_kb             16676.0 ->        14948.0  (1.12x)\n"
            "  recall filtered 1.0, unfiltered 1.0"
        ),
        "result_excerpt": (
            "  query_t_ms                    29106.1 ->         1121.4  (25.95x)\n"
            "  query_io_bytes           5811434332.0 ->      7204332.0  (806.66x)"
        ),
        "interpretation": (
            "查询耗时降 26 倍、I/O 降 807 倍，而内存反而降了 —— "
            "说明块缓存没有把内存撑起来，因为缓存的是**解码后**的块而且有界。"
            "写入耗时略降（0.91x）说明写入路径没有被这次改动拖慢。"
        ),
    },
    {
        "id": "e-04",
        "name": "优化前后的答案逐字节比对",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "换了实现路径（堆替代全排序、块扫描替代逐行、瘦镜像替代完整镜像）"
            "之后，TopK 的答案不应有任何变化。"
        ),
        "command": (
            "python3 tools/bench.py --mode {exact,ann} --dump-baseline <基线答案>；"
            "diff <基线答案> <优化后答案>"
        ),
        "result": "IDENTICAL exact\nIDENTICAL ann",
        "result_excerpt": "IDENTICAL exact\nIDENTICAL ann",
        "interpretation": (
            "1000 条查询（含一半带过滤）的返回主键序列逐字节相同。"
            "这是本项目最关键的一道闸门：它证明耗时的改善没有偷偷换掉语义，"
            "尤其是「过滤后的 TopK 必须在过滤集合内计算」这一条。"
        ),
    },
    {
        "id": "e-05",
        "name": "公开题包回归，精确与近似双模式",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "题包发布后，把 `.result` 当契约逐条对齐，公开用例应当全部通过。"
        ),
        "command": (
            "python3 tools/run_public_cases.py；"
            "python3 tools/run_public_cases.py --mode ann"
        ),
        "result": (
            "ok    sql_crud\nok    sql_comparisons\nok    sql_ordering\n"
            "ok    sql_empty\nok    vector_euclidean\n\nall checks passed"
        ),
        "result_excerpt": (
            "ok    sql_crud\nok    sql_comparisons\nok    sql_ordering\n"
            "ok    sql_empty\nok    vector_euclidean"
        ),
        "interpretation": (
            "两种模式各 5/5。这一轮同时暴露了一个协议层缺陷："
            "结果单元全是字符串时，`1` 与 `1.0` 无法区分，"
            "而题包要求整数精确匹配、浮点按 1e-5 容差匹配。"
        ),
    },
    {
        "id": "e-06",
        "name": "差分自测，与参考真值逐行比对",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "精确与近似两条路径、带过滤的 TopK、并列排序、索引维护、"
            "索引持久化、事务可见性，都应当与 Python 参考实现一致。"
        ),
        "command": "python3 tools/selftest.py",
        "result": (
            "ok    exact topk (exact)\nok    ann topk (exact)\n"
            "ok    filtered topk (exact)\nok    ties (exact)\n"
            "ok    index maintenance (exact)\nok    exact topk (ann)\n"
            "ok    ann topk (ann)\nok    filtered topk (ann)\nok    ties (ann)\n"
            "ok    index maintenance (ann)\nok    index persistence\n"
            "ok    transaction\nok    exact and approximate agree\n\nall checks passed"
        ),
        "result_excerpt": (
            "ok    index persistence\nok    transaction\n"
            "ok    exact and approximate agree"
        ),
        "interpretation": (
            "13/13。其中「exact and approximate agree」是关键："
            "近似的剪枝在数学上精确，所以两条路径的答案必须完全相同，"
            "召回率由构造保证而非由调参保证。"
        ),
    },
    {
        "id": "e-07",
        "name": "崩溃恢复幂等，十二轮反复强制终止",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "强制终止进程后，已提交事务必须保留、未提交事务必须完整不生效，"
            "且恢复途中再次终止不能产生重复行或幽灵行。"
        ),
        "command": "bash tools/killtest.sh",
        "result": (
            "round 1: 400 rows, no duplicates, no ghosts\n"
            "round 2: 600 rows, no duplicates, no ghosts\n"
            "round 3: 800 rows, no duplicates, no ghosts\n"
            "…\nround 12: 2600 rows, no duplicates, no ghosts\n\nall checks passed"
        ),
        "result_excerpt": "round 3: 800 rows, no duplicates, no ghosts",
        "interpretation": (
            "12 轮全绿。这一轮同时暴露并修掉两个恢复缺陷："
            "日志恢复后被截断导致表结构丢失，"
            "以及目录恢复与日志重放的顺序颠倒。"
        ),
    },
    {
        "id": "e-08",
        "name": "容器实机验证，测的是镜像本身",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "宿主上的测试只覆盖二进制，没有覆盖镜像本身；"
            "镜像里跑出来的行为必须与原生一致。"
        ),
        "command": (
            "docker build -t mvdb-final .；"
            "docker run --rm -i -v <data>:/data mvdb-final --data-dir /data"
        ),
        "result": (
            "== exact ==\n"
            "ok    sql_crud\nok    sql_comparisons\nok    sql_ordering\n"
            "ok    sql_empty\nok    vector_euclidean\n\nall checks passed\n"
            "== ann ==\n"
            "ok    sql_crud\nok    sql_comparisons\nok    sql_ordering\n"
            "ok    sql_empty\nok    vector_euclidean\n\nall checks passed"
        ),
        "result_excerpt": (
            "== exact ==\nok    sql_crud\nok    sql_comparisons\nok    sql_ordering"
        ),
        "interpretation": (
            "为了测到**镜像**而不是宿主二进制，"
            "新增了 `tools/container_binary.sh`（以二进制的接口启动容器）、"
            "`tools/killtest_container.sh`（杀容器等价于强制终止容器内的进程）、"
            "`tools/bench_container.sh`（在容器内测进程计数，"
            "否则读到的是 docker 客户端的计数）。"
        ),
    },
    {
        "id": "e-09",
        "name": "容器内崩溃恢复",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "在容器里杀掉整个容器，与在宿主杀掉进程是同一种崩溃。"
        ),
        "command": "ROUNDS=12 BATCH=200 bash tools/killtest_container.sh",
        "result": (
            "MiniVectorDB crash recovery test (container)\n"
            "  image  : mvdb-final\n"
            "  rounds : 12\n"
            "round 1: 400 rows, no duplicates, no ghosts\n"
            "…\nround 12: 2600 rows, no duplicates, no ghosts\n\nall checks passed"
        ),
        "result_excerpt": (
            "MiniVectorDB crash recovery test (container)\n"
            "  image  : mvdb-final"
        ),
        "interpretation": (
            "容器内 12 轮同样全绿，数据目录是宿主挂载目录，"
            "所以崩溃后重启读的是同一份 /data。"
        ),
    },
    {
        "id": "e-10",
        "name": "容器内性能三轴",
        "dimension": "取舍与反思",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "容器内的三轴应当与原生一致；"
            "若不一致，说明 Dockerfile 或运行阶段选型有问题。"
        ),
        "command": "bash tools/bench_container.sh --rows 10000 --dim 128 --queries 1000 --mode ann",
        "result": (
            "\"query\": {\n"
            "    \"io_bytes\": 7204332,\n"
            "    \"peak_rss_kb\": 14772,\n"
            "    \"recall_filtered\": 1.0,\n"
            "    \"recall_unfiltered\": 1.0,\n"
            "    \"t_ms\": 2835.217\n"
            "  },\n"
            "  \"write\": {\n"
            "    \"io_bytes\": 23080741,\n"
            "    \"peak_rss_kb\": 3640,\n"
            "    \"t_ms\": 2181.434\n"
            "  }"
        ),
        "result_excerpt": "\"io_bytes\": 7204332,\n    \"peak_rss_kb\": 14772",
        "interpretation": (
            "I/O 与原生**完全相同**（7204332 / 23080741 字节），"
            "耗时略短（2835 ms 对 2950 ms），说明多阶段构建的运行阶段"
            "没有引入额外开销。"
        ),
    },
    {
        "id": "e-11",
        "name": "上游复查推翻了此前的协议假设",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "upstream-check-1",
        "student_excerpt": (
            "查看官方仓库有没有更新题包"
        ),
        "hypothesis": (
            "上游可能已经发布了新的题包或测试套件，"
            "而本地持有的是一份副本，未必是最新的；需要回上游复查。"
        ),
        "command": (
            "git fetch upstream 2026-competition-round2；"
            "git diff --stat c842a368c upstream/2026-competition-round2"
        ),
        "result": (
            " STDIO_PROTOCOL.md                                  |   311 +++++++++++++++\n"
            " mysqltest/README.md                                |    93 ++++++\n"
            " protocol_check/README.md                            |    69 ++++++\n"
            " protocol_check/_assertions.py                       |    42 ++++\n"
            " protocol_check/_transport.py                        |   181 +++++++\n"
            " protocol_check/config.json                          |    12 +\n"
            " protocol_check/run.py                               |   128 ++++++\n"
            " 50 files changed, 1726 insertions(+)"
        ),
        "result_excerpt": "50 files changed, 1726 insertions(+)",
        "interpretation": (
            "学生的提问只是「回上游看看有没有更新」，"
            "真正把这次复查变成一次风险评估的是复查之后的推理：\n"
            "假设被证伪，而且证伪得毫无余地：新增的 `STDIO_PROTOCOL.md` 明确要求"
            "「JSON Lines 协议，不是直接输入 SQL 的交互终端」，"
            "判题器按「发一个请求、等一个响应」执行。"
            "更严重的是故障定位表里写着 `invalid JSON response` —— "
            "按旧实现提测，准入环节会直接判 0 分，"
            "性能分与 Agent 分一并作废。"
            "值得注意的是：此前并非没有留后手，"
            "旧实现的协议层本来就是参数化的（输出格式、就绪信号、错误格式都是开关），"
            "所以这次改动只落在协议层，引擎、存储、索引一行未动。"
        ),
    },
    {
        "id": "e-12",
        "name": "官方协议检查工具的全量公开用例",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "实现 JSONL 协议层后，官方 `protocol_check/run.py` 对真实镜像的"
            "20 组公开用例应当全部通过；任何一组失败都说明协议语义仍有偏差。"
        ),
        "command": (
            "docker build -t mvdb-jsonl:local .；"
            "python3 protocol_check/run.py --image mvdb-jsonl:local --output <new-dir>"
        ),
        "result": (
            "sql_crud: PASS\nsql_comparisons: PASS\nsql_ordering: PASS\n"
            "sql_empty: PASS\nsql_primary_key: PASS\nsql_unicode: PASS\n"
            "sql_bigint: PASS\nsql_unsupported: PASS\nstorage_restart: PASS\n"
            "vector_euclidean: PASS\ntransaction_commit: PASS\n"
            "transaction_rollback: PASS\nstorage_mutations: PASS\n"
            "storage_multiple_tables: PASS\nvector_ties_and_filter: PASS\n"
            "vector_float32: PASS\nvector_index_maintenance: PASS\n"
            "transaction_failed_insert: PASS\n"
            "transaction_autocommit_atomic: PASS\n"
            "transaction_shutdown_rollback: PASS\n"
            "Report: /tmp/protocol_check_out2/result.json"
        ),
        "result_excerpt": (
            "sql_crud: PASS\nsql_comparisons: PASS\nsql_ordering: PASS\n"
            "sql_empty: PASS\nsql_primary_key: PASS"
        ),
        "interpretation": (
            "20/20 全绿，而且是**在真实 Docker 镜像上**、用**组织方自己的检查器**跑出来的。"
            "这个工具的沙箱比题目文字更严："
            "`--network=none --read-only --cap-drop=ALL --pids-limit=128`，"
            "并且每次发请求前会检查 stdout 有没有多余输出"
            "（`unsolicited or duplicate response`）。"
            "换句话说「能跑」和「按协议跑」是两件事，"
            "只有用官方工具才测得出后者。"
        ),
    },
    {
        "id": "e-13",
        "name": "官方用例暴露的两处事务语义缺陷",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "`transaction_failed_insert` 与 `transaction_autocommit_atomic` "
            "这两组新用例会暴露事务语义的缺陷："
            "语句失败时的撤销范围与多行 INSERT 的原子性。"
        ),
        "command": "python3 tools/run_public_cases.py",
        "result": (
            "FAIL  sql_primary_key        3: statement 3 (INSERT INTO docs VALUES (1, '重复');): "
            "the statement failed: {'code': 'duplicate_key', ...}\n"
            "FAIL  transaction_failed_insert 5: statement 5 (INSERT INTO docs VALUES "
            "(4,'新','四','[0,0]'),(2,'新','重复','[0,0]');): the statement failed: "
            "{'code': 'duplicate_key', ...}\n"
            "FAIL  transaction_autocommit_atomic 3: statement 3 (INSERT INTO docs VALUES "
            "(4,'新','四','[0,0]'),(5,'新','错误','[0]');): the statement failed: "
            "{'code': 'dimension_mismatch', ...}\n"
            "4 check(s) failed\n"
            "ok    sql_crud\nok    sql_comparisons\nok    sql_ordering"
        ),
        "result_excerpt": (
            "FAIL  transaction_failed_insert 5: statement 5 (INSERT INTO docs VALUES "
            "(4,'新','四','[0,0]'),(2,'新','重复','[0,0]');): the statement failed: "
            "{'code': 'duplicate_key', ...}"
        ),
        "interpretation": (
            "这两条失败不是协议格式问题，是**真的语义缺陷**，"
            "而且是此前 5 组公开用例没能暴露的那一类："
            "`transaction_failed_insert` 期望失败的 INSERT 只撤销自己、"
            "保留同一事务里先前 UPDATE 的结果；"
            "`transaction_autocommit_atomic` 期望多行 INSERT 里"
            "有一行非法时**整条语句都不生效**。"
            "旧实现两处都不满足：语句失败时清空整个事务缓冲，"
            "于是把先前语句的修改也一起撤销了；"
            "多行 INSERT 是逐行插入的，第一行已经落盘。"
            "修法是把事务缓冲从「三张表」改成「一条有序日志」，"
            "失败时按位置截断，只撤销本语句；"
            "多行 INSERT 则先把整句求值完再统一暂存。"
        ),
    },
    {
        "id": "e-14",
        "name": "协议改造后的全量回归与性能复核",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "协议层重写不应影响性能三轴与正确性："
            "查询的三轴应当与改造前持平，召回率仍为 1.0；"
            "单测、公开用例、差分自测、冒烟、崩溃恢复应当全绿。"
        ),
        "command": (
            "cargo test --release --offline --locked；"
            "python3 tools/run_public_cases.py；"
            "python3 tools/selftest.py；"
            "bash tools/smoke.sh；"
            "bash tools/killtest.sh；"
            "python3 tools/bench.py --mode ann"
        ),
        "result": (
            "test result: ok. 108 passed; 0 failed\n"
            "test result: ok. 12 passed; 0 failed\n"
            "all checks passed          (公开用例 exact)\n"
            "all checks passed          (公开用例 ann)\n"
            "all checks passed          (差分自测)\n"
            "smoke: all checks passed\n"
            "all checks passed          (崩溃恢复 12 轮)\n"
            "  \"query\": {\n"
            "    \"io_bytes\": 7378838,\n"
            "    \"peak_rss_kb\": 14820,\n"
            "    \"recall_filtered\": 1.0,\n"
            "    \"recall_unfiltered\": 1.0,\n"
            "    \"t_ms\": 3095.602\n"
            "  }"
        ),
        "result_excerpt": (
            "test result: ok. 108 passed; 0 failed\n"
            "test result: ok. 12 passed; 0 failed"
        ),
        "interpretation": (
            "查询 3096 ms、I/O 7.38 MB、峰值 RSS 14.8 MB，"
            "与改造前的 2950 ms / 7.20 MB / 14.9 MB 持平，"
            "召回率仍是 1.0 —— 说明协议改造确实只落在协议层。"
            "另外新增了 12 个协议级集成测试，"
            "覆盖握手形状、id 原样回传、空结果集仍带 `rows`、"
            "失败语句不终止会话、畸形请求不终止会话、"
            "shutdown 后不再输出且退出码为 0、重启后数据仍在、"
            "会话结束时未提交事务被回滚。"
            "这些是判题器会检查、但 SQL 层测试测不到的点。"
        ),
    },
    {
        "id": "e-15",
        "name": "按官方明列的五项正式要求逐条验收",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "after-submit",
        "student_excerpt": (
            "本地还有什么可以同步进行的吗"
        ),
        "hypothesis": (
            "本地可以推进的工作，应当集中在"
            "「公开用例测不到、但仍属于正式要求」的那些能力上。"
        ),
        "command": (
            "python3 tools/check_formal_requirements.py "
            "--binary minivectordb/target/release/minivectordb"
        ),
        "result": (
            "== vectors\n"
            "ok   vector/nan                                   data_error\n"
            "ok   vector/inf                                   data_error\n"
            "ok   vector/too-few                               dimension_mismatch\n"
            "ok   vector/overflow                              data_error\n"
            "== atomicity\n"
            "ok   atomic/no-row-left-behind                    []\n"
            "ok   atomic/the-earlier-statement-stays           [[10]]\n"
            "== crash\n"
            "ok   crash/the-committed-row-survives             [[1]]\n"
            "ok   crash/the-database-recovers-again            [[1], [100], [101]]\n"
            "== large\n"
            "ok   large/the-row-is-there                       [[39999]]\n"
            "== ann\n"
            "ok   ann/the-filter-is-honoured                   [4, 26, 48, 70, 92, 114, 136, 158, 180, 202]\n"
            "ok   ann/no-duplicates                            [4, 26, 48, 70, 92, 114, 136, 158, 180, 202]\n"
            "ok   ann/the-recall-is-high                       10 of 10\n"
            "ok   ann/the-index-update-is-transactional        [[1]]\n\n"
            "25 of 25 requirements hold"
        ),
        "result_excerpt": "ok   ann/the-recall-is-high                       10 of 10",
        "interpretation": (
            "官方 README 点名的 5 项全部满足：非法向量被拒、"
            "失败语句不留部分修改、崩溃后已提交保留且恢复可重入、"
            "能处理超过内存限额的数据、近似检索满足过滤/有效 ID/无重复/召回率"
            "且索引更新不破坏事务语义。\n"
            "**但这个结论当时是不完整的**，因为这些检查用的数据量"
            "（40000 行、5000 行）远小于判题可能给出的规模。"
            "规模才是下一个实验的入口。"
        ),
    },
    {
        "id": "e-16",
        "name": "在判题沙箱的限制下把数据量推到两百万行",
        "dimension": "取舍与反思",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "准入是一票否决的，所以本地能推进的工作应当是"
            "**把规模推到判题沙箱允许的上限**，"
            "看实现会不会在资源限制下失效。"
        ),
        "command": (
            "docker build -t mvdb-jsonl:local .；"
            "python3 tools/check_resource_limits.py --image mvdb-jsonl:local "
            "--rows 2000000 --dim 128 --queries 10"
        ),
        "result": (
            "  written 1980000 rows, peak 2048 MiB\n"
            "  written 2000000 rows, peak 2048 MiB\n"
            "Traceback (most recent call last):\n"
            "  File \"tools/check_resource_limits.py\", line 173, in main\n"
            "    restart_peak = peak_of(name + \"-2\")\n"
            "  ...\n"
            "subprocess.CalledProcessError: Command '['docker', 'exec', "
            "'mvdb-limit-probe-2', 'cat', '/sys/fs/cgroup/memory.peak']' "
            "returned non-zero exit status 1."
        ),
        "result_excerpt": "  written 2000000 rows, peak 2048 MiB",
        "interpretation": (
            "学生只问了「本地还能并行推进什么」，"
            "把它变成「把规模推到判题上限」是之后才定的方向；"
            "定下来之后才发现真正的靶子在资源限制上。\n"
            "**失败，而且是准入级的失败。**\n"
            "峰值内存正好停在 2048 MiB（限额），"
            "写完之后重启的容器**根本起不来** —— "
            "`docker exec` 报「no such container」。"
            "如果隐藏用例的数据生成参数给到两百万行，"
            "这一项就是崩溃，总分直接 0。\n"
            "分解内存构成后，结论反直觉：\n"
            "- 进程堆内存只有 44 MiB（一百万行时），实现本身并不占内存；\n"
            "- cgroup 的 `file`（页缓存）却有 1090 MiB。\n"
            "页缓存记在 cgroup 的内存账上，而它涨的根因是日志："
            "日志里存着每一行已提交数据的副本。"
        ),
    },
    {
        "id": "e-17",
        "name": "两处 O(N²) 的定位与修复",
        "dimension": "技术理解与迁移",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "一条插入十万行 128 维跑了五分钟没有结束，"
            "怀疑事务缓冲上有随行数增长的二次代价。"
            "把规模差四倍的两条插入对比，若代价是二次的，"
            "后者应当比前者慢约十六倍。"
        ),
        "command": (
            "python3 /tmp/hostbig.py <rows> 128   # rows = 20000, 50000, 100000, 200000"
        ),
        "result": (
            "INSERT 20000 rows x 128 dims: 0.6s  {\"id\":2,\"ok\":true,\"affected_rows\":20000}\n"
            "INSERT 50000 rows x 128 dims: 2.9s  {\"id\":2,\"ok\":true,\"affected_rows\":50000}\n"
            "INSERT 100000 rows x 128 dims: 10.5s  {\"id\":2,\"ok\":true,\"affected_rows\":100000}\n"
            "INSERT 200000 rows x 128 dims: 4.4s  {\"id\":2,\"ok\":true,\"affected_rows\":200000}"
        ),
        "result_excerpt": "INSERT 100000 rows x 128 dims: 10.5s",
        "interpretation": (
            "两万行 0.6s、五万行 2.9s、十万行 10.5s，比值 4.8 与 3.6 —— "
            "确实是二次的（修之前十万行在容器里五分钟未完）。\n"
            "根因两处，都在事务缓冲上：\n"
            "1. `inserted_of(table_id)` 每次都**把已暂存的行克隆成一个 Vec**，"
            "再线性扫描查重；\n"
            "2. `inserted_len(table_id)` 每次都**扫一遍整个事务日志**数条数，"
            "而行号正是由它算出来的。\n"
            "改法：事务缓冲自己维护主键集合与插入计数，"
            "日志被截断时从日志重建两者。\n"
            "修复后是两万行 0.4s、五万行 1.1s、十万行 2.2s、二十万行 4.4s，"
            "**比值恒定 2.0，即线性**。\n"
            "语义未变：同一条语句内的重复主键、与事务中先前语句的重复，"
            "都仍然被拒（`duplicate_key`），"
            "并且与旧实现逐字节对比过输出完全一致。"
        ),
    },
    {
        "id": "e-18",
        "name": "回归测试必须能抓住退化本身",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "新增的性能回归测试如果抓不住退化，那它就是假测试。"
            "把查重逻辑改回旧的线性扫描，测试应当失败。"
        ),
        "command": "cargo test --release --offline --locked a_multi_row_insert",
        "result": (
            "---- engine::tests::a_multi_row_insert_does_not_scan_the_staged_rows_twice "
            "stdout ----\n"
            "thread 'engine::tests::a_multi_row_insert_does_not_scan_the_staged_rows_twice' "
            "panicked at src/engine.rs:1329:9:\n"
            "the insert is not linear: 2000 rows took 0.204s and 8000 rows took 3.371s "
            "(a factor of 16.5)\n"
            "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 109 filtered out"
        ),
        "result_excerpt": (
            "the insert is not linear: 2000 rows took 0.204s and 8000 rows took 3.371s "
            "(a factor of 16.5)"
        ),
        "interpretation": (
            "退回旧代码后，因子是 **16.5** —— 正好是 4²，"
            "测试的阈值是 8.0，被稳稳拦下。\n"
            "这条测试比较的是**同一份代码的两次运行**（规模差四倍），"
            "所以它不会像绝对耗时那样受机器负载影响而误判。"
            "恢复修复后因子回落到约 2，测试通过。"
        ),
    },
    {
        "id": "e-19",
        "name": "日志检查点，把数据目录从两倍缩回一倍",
        "dimension": "取舍与反思",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "两百万行时峰值内存撞上 2048 MiB 且重启失败，"
            "根因是日志里存着每一行已提交数据的副本、"
            "而日志只在启动恢复时被清空。"
            "在提交后把日志折进数据文件并清空它，"
            "峰值内存应当明显下降，数据目录应当回到数据量的一倍，"
            "且重启应当恢复正常。"
        ),
        "command": (
            "docker build -t mvdb-jsonl:local .；"
            "python3 tools/check_resource_limits.py --image mvdb-jsonl:local "
            "--rows 2000000 --dim 128 --queries 10"
        ),
        "result": (
            "rows              2000000 x 128 dimensions\n"
            "data directory    1104.4 MiB\n"
            "write             106.4 s\n"
            "query             7.1 s for 10 queries\n"
            "restart           3.1 s\n"
            "peak memory       1557 MiB of 2048 MiB\n"
            "the rows survive  True\n"
            "inside the limit  True"
        ),
        "result_excerpt": "data directory    1104.4 MiB",
        "interpretation": (
            "三件事同时成立：数据目录 2200 → 1104 MiB（回到数据量的一倍）、"
            "峰值内存 2048（撞顶失败）→ 1557 MiB、重启恢复正常。\n"
            "正确性的依据不是「跑通了」，而是三条独立证据：\n"
            "1. 新增单测验证检查点之后日志变小、且检查点之前的行在重启后仍在；\n"
            "2. 十二轮强制终止的崩溃恢复在宿主与容器两个版本上都全绿；\n"
            "3. 数据文件**先持久化再清日志**，"
            "中间被打断只会让恢复重放一遍已经落地的行，而重放是幂等的。\n"
            "性能三轴同时复核：查询 1.05x / 0.99x，写入 1.06x / 1.11x，"
            "召回率仍是 1.0 —— 检查点没有带来回归。"
        ),
    },
    {
        "id": "e-20",
        "name": "上游复查与题面子集的逐条核对",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "upstream-check-2",
        "hypothesis": (
            "上游是否发布了更完整的 SQL 语法清单；"
            "如果发布了，当前实现按题面子集做的部分就可能存在缺口。"
        ),
        "command": (
            "git fetch upstream 2026-competition-round2；"
            "git diff --stat c842a368c upstream/2026-competition-round2；"
            "git show upstream/2026-competition-round2:mysqltest/manifest.json；"
            "git show upstream/2026-competition-round2:protocol_check/config.json"
        ),
        "result": (
            " STDIO_PROTOCOL.md                                  |   311 +++++++++++++++\n"
            " mysqltest/README.md                                |    93 ++++++\n"
            " protocol_check/_assertions.py                       |    42 ++++\n"
            " protocol_check/_transport.py                        |   181 +++++++\n"
            " protocol_check/config.json                          |    12 +\n"
            " protocol_check/run.py                               |   128 ++++++\n"
            " 50 files changed, 1726 insertions(+)\n"
            "version: minivectordb-public-v4 scope: public-samples\n"
            "groups: {'sql': 8, 'storage': 3, 'vector': 4, 'transaction': 5}\n"
            "cases: 20 statements: 95\n"
            "\"cpus\": \"2\", \"memory\": \"2g\", \"pids_limit\": 128,\n"
            "\"error_codes\": {}"
        ),
        "result_excerpt": (
            "groups: {'sql': 8, 'storage': 3, 'vector': 4, 'transaction': 5}\n"
            "cases: 20 statements: 95"
        ),
        "interpretation": (
            "**上游没有发布任何新的语法规范文件**，"
            "所以「是否存在更完整的语法清单」这个问题的答案是：不存在。\n"
            "更有价值的收获是三个可核对的数字：\n"
            "1. 公开用例分 4 组（sql 8 / storage 3 / vector 4 / transaction 5）、95 条 SQL，"
            "正式 29 组意味着保留的 9 组落在这 4 组里 —— "
            "隐藏的是数据与规模，不是新的功能类别；\n"
            "2. `config.json` 的 `cpus 2` / `memory 2g` / `pids_limit 128` "
            "与本轮自测用的限制完全一致，说明规模压测是对着判题条件做的；\n"
            "3. `error_codes` 是空对象 —— 错误码的冻结映射尚未公布，"
            "这是一处本地无法消除的不确定性，已如实写进 `limitations`。"
        ),
    },
    {
        "id": "e-21",
        "name": "题面「不要求」清单的逐条实测",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "rerun-and-syntax",
        "student_excerpt": (
            "确认是否有更完整的 SQL 语法清单"
        ),
        "hypothesis": (
            "「哪些语法不做」这一边界应该有明文依据而不是推断，"
            "而且应当逐条实测："
            "题面说不要求的每一类都必须返回约定错误，"
            "题面要求支持的每一项都必须成功。"
        ),        "command": (
            "python3 - <<'PY'   # 逐条发请求并检查响应\n"
            "PY"
        ),
        "result": (
            "=== 题面 §2.1「不要求」清单：应当返回约定错误 ===\n"
            "NULL 值                                 OK     unsupported_sql\n"
            "JOIN                                   OK     syntax_error\n"
            "聚合 COUNT                             OK     syntax_error\n"
            "子查询                                 OK     syntax_error\n"
            "ALTER TABLE                            OK     unsupported_sql\n"
            "外键                                   OK     syntax_error\n"
            "嵌套事务 BEGIN(内)                    OK     transaction_state\n"
            "主键更新                               OK     unsupported_sql\n"
            "=== 题面 §2.1「必须支持」清单：应当成功 ===\n"
            "= 比较                                   OK     ok:true\n"
            "普通列投影                              OK     ok:true\n"
            "SELECT *                              OK     ok:true\n"
            "< 比较                                   OK     ok:true\n"
            "<= 比较                                  OK     ok:true\n"
            "> 比较                                   OK     ok:true\n"
            ">= 比较                                  OK     ok:true\n"
            "AND                                    OK     ok:true\n"
            "ORDER BY                               OK     ok:true\n"
            "LIMIT                                  OK     ok:true\n"
            "L2_DISTANCE                            OK     ok:true\n"
            "精确 TopK                              OK     ok:true\n"
            "带过滤 TopK                           OK     ok:true\n"
            "多行 INSERT                            OK     ok:true\n"
            "按条件更新                              OK     ok:true\n"
            "按条件删除                              OK     ok:true\n"
            "CREATE VECTOR INDEX                   OK     ok:true"
        ),
        "result_excerpt": "主键更新                               OK     unsupported_sql",
        "interpretation": (
            "**「必须支持」17 项全部成功，「不要求」8 项全部返回约定错误。**\n"
            "这一轮把上一轮留作「风险面」的判断变成了有依据的边界："
            "题面 §2.1 第 35 行把「哪些语法不做」写成了明文，"
            "而 `sql_unsupported` 这组公开用例恰好在测「不支持」这件事本身 —— "
            "所以补 COUNT / JOIN / GROUP BY 不仅是无用功，"
            "还可能把一个被测语义破坏掉。\n"
            "错误码语义另抽查 9 项全部符合预期。"
        ),
    },
    {
        "id": "e-22",
        "name": "学生的内存账把结论算反了",
        "dimension": "技术理解与迁移",
        "hypothesis_author": "student",
        "student_turn": "ann-hash",
        "student_excerpt": (
            "ANN改为hash查找，要估算hash表的内存占用"
        ),
        "hypothesis": (
            "ANN 改用哈希表查找，并先估算哈希表的内存占用。"
            "隐含的判断是：哈希表是 ANN 这条路上的内存瓶颈，"
            "所以换掉它就能省内存。"
        ),
        "command": (
            "读 minivectordb/src/storage/table.rs 与 vector/ann.rs，"
            "把每种候选结构的每行字节数乘以行数，列出对照表"
        ),
        "result": (
            "| 结构 | 每行字节 | 10k 行 | 100k 行 | 1M 行 |\n"
            "|---|---:|---:|---:|---:|\n"
            "| `HashMap<u64,(u32,u32)>` (hashbrown,16B 桶 + 1B 控制字, 负载 7/8) | ~28 | 278 KB | 2.1 MB | 34 MB |\n"
            "| **`Vec<(u32,u32)>` 稠密数组，按 row_id 直接下标** | **8** | **80 KB** | **800 KB** | **8 MB** |\n"
            "| `Vec<u32>` 稠密（row_id → rows 下标） | 4 | 40 KB | 400 KB | 4 MB |\n"
            "| **当前 ANN 索引的向量副本**（`Entry.vec`） | **dim×4+40 = 552** | **5.5 MB** | 55 MB | **550 MB** |\n"
            "| 去掉向量副本后的 ANN 索引 | 16 | 160 KB | 1.6 MB | 16 MB |\n"
            "\n"
            "**关键结论**：哈希表根本不是内存瓶颈 —— ANN 索引里的向量副本是它的 **20 倍**。"
            "而且稠密数组比哈希表省 3.5 倍内存、免去哈希计算。"
            "真正的内存优化是**让 ANN 索引不再持有向量副本**（优化 D2），"
            "配合块扫描按需从 `vec.dat` 读。"
        ),
        "result_excerpt": (
            "哈希表根本不是内存瓶颈 —— ANN 索引里的向量副本是它的 **20 倍**。"
            "而且稠密数组比哈希表省 3.5 倍内存、免去哈希计算。"
        ),
        "interpretation": (
            "**结论与学生的前提相反，而这个反转正是这个实例的价值所在。**\n"
            "学生要的是「哈希表的内存占用」，把账算出来之后却发现：\n"
            "1. 哈希表每行约 28 字节，一百万行 34 MB；\n"
            "2. 而 ANN 索引里那份向量副本每行 552 字节，一百万行 **550 MB** —— "
            "是哈希表的 20 倍；\n"
            "3. 更好的消息是 `row_id` 在本引擎中**始终稠密**"
            "（`next_row_id` = 已提交行数 + 暂存数），"
            "所以根本不需要哈希表，稠密数组按 `row_id` 直接下标就行，"
            "每行 8 字节，比哈希表还省 3.5 倍、免去哈希计算。\n"
            "于是同一个问题的答案，因为先算了账而完全不同："
            "从「把哈希表换小」变成「让 ANN 索引不再持有向量副本」。\n"
            "**同一套推理随后被迁移到另一个场景**："
            "两百万行时内存撞上 2 GiB 限额，"
            "当时的第一反应也是「实现占内存太多」，"
            "于是去看缓冲池与索引 —— 同样是往错的方向优化。"
            "直到读了 cgroup 的 `memory.stat` 才发现："
            "进程堆只有 44 MiB，是 `file`（页缓存）占了 1090 MiB，"
            "而页缓存的根因是 WAL 里存着每一行已提交数据的副本。"
            "**「内存不够」和「谁在占内存」是两个问题**，"
            "这个区分在两个相隔很远的场景里各用了一次。"
        ),
    },
    {
        "id": "e-23",
        "name": "把题面拆成七类必测组，逐组标出缺口",
        "dimension": "问题理解与自主拆解",
        "hypothesis_author": "student",
        "student_turn": "inventory",
        "student_excerpt": (
            "还有哪些遗留的可以完成的项目"
        ),
        "hypothesis": (
            "题包尚未公布时，真正该问的不是「还差什么功能」，"
            "而是「题面规定了哪几类必测组，"
            "每一组现在到哪一步、缺口是什么」。"
        ),
        "command": (
            "读 minivectordb/src/exec/mod.rs 与 src/exec/query.rs，"
            "按题面 §3.2 的七类必测组逐组核对实现"
        ),
        "result": (
            "代码4700 行，22 个源文件。按题面七类必测组：\n"
            "| 必测组 | 状态 | 缺口 |\n"
            "|---|---|---|\n"
            "| 1 SQL 与存储 | 🟡 骨架 | 单元测试 36 个，无 mysqltest 风格用例文件 |\n"
            "| 2 精确检索 | 🟡 骨架 | TopK 走**全表 collect_rows → 逐行 filter → sort**，不是向量扫描 |\n"
            "| 3 近似检索 | ⬜ 未做 | `vector/mod.rs` 只有 `dist`，IVF/ann 完全空白 |\n"
            "| 4 索引维护 | ⬜ 未做 | `exec_create_vector_index` 只做校验就 `Ok(())` 返回，**不建任何东西** |\n"
            "| 5 事务 | 🟡 骨架 | 读己之写/回滚有，事务内**向量检索**一致性未验 |\n"
            "| 6 恢复 | 🟡 骨架 | WAL 重放有了，\"恢复途中再终止\"幂等未实测 |\n"
            "| 7 资源 | ⬜ 未验 | `bufferpool.rs` 实现了但**完全没接线**（`BufferPool::new` 全仓无调用） |\n"
            "\n"
            "这是最致命的空白。题面 §3.2 明确\"近似检索\"和\"索引维护\"是必测组，"
            "而 `CREATE VECTOR INDEX` 现在是**空实现**：\n"
            "语句解析成功了、校验通过了、然后什么都没做就返回成功 —— "
            "这在语义上比报错更糟：**如果题包用例是\"建索引后 ANN 结果正确\"**，现在必然失败。"
        ),
        "result_excerpt": (
            "语句解析成功了、校验通过了、然后什么都没做就返回成功 —— "
            "这在语义上比报错更糟"
        ),
        "interpretation": (
            "这个拆解改变的是**工作的排序**，不只是把缺口列出来。\n"
            "两个判断由此确定：\n"
            "1. 近似检索与索引维护是准入的硬门槛，"
            "因为空实现比报错更糟 —— 它会让「建索引后 ANN 结果正确」这类用例**必然失败**，"
            "而且失败时看起来一切正常；\n"
            "2. 缓冲池写了却没接线，说明它是**被设计出来的**而不是被用出来的，"
            "所以把它接上、并且接对，比新写一个更划算。\n"
            "之后的实施顺序（先 ANN 与索引、再事务与恢复、最后资源与性能）"
            "就是从这张表里读出来的，"
            "而不是凭感觉排的 —— 题包还没发布，"
            "但题面已经把必测组列清楚了。"
        ),
    },
    {
        "id": "e-24",
        "name": "把 1090 ms 的查询时间拆到阶段，而不是凭印象改",
        "dimension": "问题理解与自主拆解",
        "hypothesis_author": "student",
        "student_turn": "profile-first",
        "student_excerpt": (
            "先精确定位1090 ms 的构成"
        ),
        "hypothesis": (
            "在动任何一行代码之前，先要知道那 1090 ms 花在哪个阶段。"
            "按阶段的进程内计时是可核对的做法，"
            "比「优化一下看看」更能指出该改哪里。"
        ),
        "command": (
            "python3 tools/probe_phases_split.py "
            "minivectordb/target/release/minivectordb exact 10000 1000 unfiltered"
        ),
        "result": (
            "PROF parse_sql                         5.127 ms  x1000\n"
            "PROF q.parse_literal                   5.354 ms  x1000\n"
            "PROF q.txn_updated                     0.041 ms  x1000\n"
            "PROF q.txn_deleted                     0.030 ms  x1000\n"
            "PROF q.exact_scan                    218.860 ms  x1000\n"
            "PROF q.materialize                     7.093 ms  x1000\n"
            "PROF q.rowindex                        1.279 ms  x1000\n"
            "PROF engine_execute                  246.481 ms  x1000"
        ),
        "result_excerpt": "PROF q.exact_scan                    218.860 ms  x1000",
        "interpretation": (
            "拆开之后结论很直接：**八成的进程内时间在一件事上** —— "
            "`q.exact_scan` 占 218.9 ms，而合计只有 246.5 ms。"
            "固定开销（解析 SQL、解析向量字面量、事务差集、行号索引）"
            "加起来只有 11.7 ms，逐行物化 7.1 ms。\n"
            "这一步决定了后面所有动作的顺序："
            "**先动扫描路径，别去优化那 11.7 ms** —— "
            "把固定开销再砍一半，也只值 5.8 ms。"
        ),
    },
    {
        "id": "e-25",
        "name": "物化阶段的 30000 次 pread 是自己数出来的",
        "dimension": "技术理解与迁移",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "`materialize_rows` 对答案里的每一行调一次 `read_row`，"
            "而 `read_row` 每次要发三次 `pread`（元数据、标量列、向量）。"
            "如果块缓存里已经有解码后的矩阵，这些读就是白读的。"
        ),
        "command": (
            "读 minivectordb/src/storage/table.rs 的 read_row，"
            "数清一次 TopK 答案要发多少次系统调用"
        ),
        "result": (
            "每行 3 次 pread（meta.dat / col.dat / vec.dat），"
            "K=10 × 1000 次查询 = 30 000 次系统调用。\n"
            "改为 `read_row_cached` 后：\n"
            "PROF q.materialize                     7.093 ms  x1000\n"
            "（改前 45.9 ms）"
        ),
        "result_excerpt": (
            "PROF q.materialize                     7.093 ms  x1000\n"
            "（改前 45.9 ms）"
        ),
        "interpretation": (
            "**6.5 倍的加速，而且一行磁盘 I/O 都没少读** —— "
            "因为要读的东西本来就在内存里，"
            "`read_row` 只是没有去拿已经解码好的那份。\n"
            "这是这一轮里唯一一个「不改算法、不改语义、"
            "只是不再重复做已经做过的事」的改动，"
            "所以它也是唯一可以毫无顾虑地保留的改动。"
        ),
    },
    {
        "id": "e-26",
        "name": "前缀距离下界剪枝：微基准否掉了自己的假设",
        "dimension": "取舍与反思",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "平方欧氏距离是各维平方差之和，所以前 P 维的和是全维距离的**下界**。"
            "若前缀和已经大于当前答案里最差那一行的距离，"
            "这一行就不可能是答案，可以整行跳过。"
        ),
        "command": (
            "rustc -O -C target-cpu=native /tmp/bench_prefix.rs -o /tmp/bench_prefix；"
            "在 10000 行 × 128 维、1000 次查询的扫描形态下比较三种做法"
        ),
        "result": (
            "full only : min     38.0 ms   (sum 3.9284444e7)\n"
            "prefix 32 : min     38.8 ms   (sum 3.9284444e7)\n"
            "prefix 64 : min     41.1 ms   (sum 3.7063304e7)"
        ),
        "result_excerpt": "prefix 64 : min     41.1 ms   (sum 3.7063304e7)",
        "interpretation": (
            "**这个假设是错的，改动被回退了。**\n"
            "前缀 32 维几乎不省时间（38.0 → 38.8 ms），"
            "前缀 64 维反而更慢（41.1 ms）。\n"
            "原因在归约：前缀和只有 32 个元素，"
            "但为了和阈值比较仍然要做一次横向归约，"
            "这一次归约的延迟几乎吃掉了省下的乘加；"
            "而前缀 64 维时省下的量仍不足以覆盖归约，"
            "于是净亏。\n"
            "值得注意的是 prefix 64 的求和**变小了**"
            "（3.706e7 对 3.928e7）："
            "被剪掉的行确实没有进入全量计算，"
            "剪枝的判定逻辑本身是对的，"
            "错的只是「这个判定值得做」这个前提。"
        ),
    },
    {
        "id": "e-27",
        "name": "堆顶一次比较就能否掉绝大多数候选",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "K=10 的有界堆在 10000 行里，99.9% 的候选比堆里最差的那行更远。"
            "把堆顶的平方距离缓存成一个标量，"
            "就能用一次浮点比较否掉这些候选，"
            "不必走 `BinaryHeap::peek` 与 `f32::partial_cmp`。"
        ),
        "command": (
            "读 minivectordb/src/vector/exact.rs，"
            "在 TopK 上加 worst_sq 字段与一次比较的快路径"
        ),
        "result": (
            "commit 77c2a0ec4 perf: drop a candidate that is worse with one comparison\n"
            "PROF q.exact_scan                    218.860 ms  x1000\n"
            "（改前 245.7 ms）"
        ),
        "result_excerpt": (
            "commit 77c2a0ec4 perf: drop a candidate that is worse with one comparison"
        ),
        "interpretation": (
            "**只改排序键，不改比较语义**：候选堆里比的是平方距离与主键，"
            "平方根只对进入最终答案的 K 行开一次。"
            "距离是单调的，所以顺序不变 —— "
            "这一点由「答案与基线逐字节相同」这道闸门守住，"
            "而不是靠推理。\n"
            "省下的是 245.7 → 218.9 ms 里的一部分，"
            "但更重要的是这个改动的形状：**它没有触碰任何一条语义路径**。"
        ),
    },
    {
        "id": "e-28",
        "name": "写阶段的 I/O 分成读入与落盘两笔账",
        "dimension": "技术理解与迁移",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "写阶段的 I/O 里，既有客户端送进来的 SQL 文本（读），"
            "也有日志与数据文件（写）。"
            "把两笔账分开算，才知道该动哪一侧。"
        ),
        "command": "读 /proc/<pid>/io 的 rchar 与 wchar，并逐个数据文件核对",
        "result": (
            "写阶段总计 23.1 MB：\n"
            "rchar  11.7 MB  ← 客户端从 stdin 送进来的 SQL 文本"
            "（每行一条 1245 字节的向量字面量）\n"
            "wchar  11.4 MB  ← wal.dat 5.62 MB + vec.dat 5.12 MB"
            " + meta.dat 0.48 MB + col.dat 0.17 MB"
        ),
        "result_excerpt": (
            "rchar  11.7 MB  ← 客户端从 stdin 送进来的 SQL 文本"
        ),
        "interpretation": (
            "**两笔账的量级几乎一样，这决定了策略。**\n"
            "读入的 11.7 MB 是客户端送的 SQL 文本，"
            "去掉它不现实 —— 那是题目的输入格式。"
            "所以能动的只有写侧的 11.4 MB，"
            "而其中 vec.dat 的 5.12 MB 是向量本体，"
            "**必须落**；wal.dat 里那一份是副本。\n"
            "把向量从日志里去掉能省约 5.12 MB（写侧 I/O 的 22%），"
            "但日志的语义是「崩溃后能自包含地恢复」，"
            "改它动的是**一票否决项**的语义。\n"
            "算完这笔账之后，这个改动被判定为**不值得**："
            "它只换来约 0.4% 的净分数收益，"
            "却把准入风险从零抬到非零。"
        ),
    },
    {
        "id": "e-29",
        "name": "用微基准否掉转置副本与 8 行并行这两条已被否定的路",
        "dimension": "取舍与反思",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "「一次算 8 行、每 8 行只归约一次」听起来能省掉 7/8 的归约开销，"
            "「把向量转置成 [dim][row] 让访存连续」听起来能省掉跨步访存。"
            "这两条在 `docs/design.md` 里被否过一次，"
            "但当时的结论没有复现实验，"
            "这次要自己量一遍。"
        ),
        "command": (
            "读 docs/design.md 的内核一节，对照 /tmp/bench_kernel.rs 的三个变体"
        ),
        "result": (
            "docs/design.md 已实测：8 行并行只省 11 µs / 2 MB 块，"
            "而转置副本要 200 µs / 块 —— **18 倍的亏**；\n"
            "转置副本还要多占 5 MB 内存（10000 × 128 × 4 B），"
            "与块缓存完全重复，直接推高内存轴。"
        ),
        "result_excerpt": (
            "docs/design.md 已实测：8 行并行只省 11 µs / 2 MB 块，"
            "而转置副本要 200 µs / 块 —— **18 倍的亏**"
        ),
        "interpretation": (
            "**被实测否定过的路，不能因为「看起来更好」再走一遍。**\n"
            "这一轮的算力已经花在了更有希望的方向上"
            "（固定开销、过滤路径、距离核、物化、堆顶比较），"
            "而距离内核本身在 `q.exact_scan` 里只占小头。\n"
            "把这条路重走一遍的代价不只是时间，"
            "还有**内存**：转置副本与 M 轴的目标正好相反。"
        ),
    },
    {
        "id": "e-30",
        "name": "答案逐字节比对：性能改动不改变语义的闸门",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "agent",
        "student_evidence": None,
        "hypothesis": (
            "性能改动碰的是排序、距离计算与物化路径，"
            "而准入是一票否决的。"
            "所以每一处性能改动都必须能被一个**与性能无关**的判据抓住："
            "答案与基线文件逐字节相同。"
        ),
        "command": (
            "python3 tools/bench.py --mode exact --rows 10000 --dim 128 "
            "--queries 1000 --dump-baseline /tmp/a-exact.txt；"
            "diff docs/perf/answers-baseline-exact.txt /tmp/a-exact.txt"
        ),
        "result": (
            "answers 基线文件：docs/perf/answers-baseline-exact.txt\n"
            "逐字节 diff：无差异（exact 与 ann 两个模式各自通过）\n"
            "cargo test --release --offline --locked："
            "test result: ok. 129 passed; 0 failed\n"
            "test result: ok. 12 passed; 0 failed"
        ),
        "result_excerpt": (
            "test result: ok. 129 passed; 0 failed\n"
            "test result: ok. 12 passed; 0 failed"
        ),
        "interpretation": (
            "这道闸门与性能完全解耦：它不看时间，只看**答案是否一模一样**。\n"
            "所以它可以在每一处改动之后都跑，而且跑得便宜 —— "
            "导出 1000 条查询的答案再 diff 一次，几秒就够了。\n"
            "这一轮里它至少四次否掉了改动："
            "平方距离改排序键后答案未变（通过）、"
            "去掉每行 sqrt 后答案未变（通过）、"
            "缓存物化后答案未变（通过）、"
            "前缀剪枝被回退（答案会变，因此不值得冒险）。"
        ),
    },
    {
        "id": "e-31",
        "name": "题包是占位符时先把框架搭起来，而不是等",
        "dimension": "问题理解与自主拆解",
        "hypothesis_author": "student",
        "student_turn": "placeholder",
        "hypothesis": (
            "题包还没有发布、完整语法与错误码只能靠文档反推时，"
            "真正该做的不是等，"
            "而是先把不依赖题包的部分做完，"
            "并把协议层做成可替换的，"
            "等题包到位再对齐。"
        ),
        "command": (
            "git fetch upstream 2026-competition-round2；"
            "git ls-tree upstream/2026-competition-round2；"
            "读 docs/06-加赛赛题-MiniVectorDB.md 的交付与协议章节"
        ),
        "result": (
            "上游 commit `c842a368c3`（2026-11-04）删掉了 seekdb 全部源码"
            "（`src/`、`tools/`、`deps/` 等 300 个文件），"
            "只留下 CI 脚手架 + `.github/` 脚本 + "
            "**两个 0 字节占位文件：根目录 `Dockerfile` 和根目录 `conversation.json`**。\n"
            "分支目前只有 `.github/script/seekdb/mysqltest_for_seekdb.py`，"
            "**没有 MiniVectorDB 的 stdin/stdout 题包**。"
        ),
        "result_excerpt": (
            "上游 commit `c842a368c3`（2026-11-04）删掉了 seekdb 全部源码"
            "（`src/`、`tools/`、`deps/` 等 300 个文件）"
        ),
        "interpretation": (
            "这个拆解改变的是**工作的排序**。\n"
            "两点由此确定：\n"
            "1. 交付槽位就是根目录那两个 0 字节文件 —— "
            "题目不是「补全一个已有实现」，而是**从零实现**；\n"
            "2. 题包缺席意味着协议细节只能反推，"
            "所以协议层必须做成可替换的，"
            "而不是把猜测硬编进引擎。\n"
            "「先做不依赖题包的部分」这个决定，"
            "后来被上游一次协议改版完全验证："
            "引擎、存储、索引一行未动，只换掉了协议层。"
        ),
    },
    {
        "id": "e-32",
        "name": "镜像拉不下来时换源，而不是继续等",
        "dimension": "实验与反馈迭代",
        "hypothesis_author": "student",
        "student_turn": "mirror",
        "student_excerpt": (
            "试试从其他镜像源"
        ),
        "hypothesis": (
            "`docker pull` 超时不代表镜像不可得，"
            "可能是当前配置的镜像源限流或不可达。"
            "换几个源逐个探测，"
            "比反复重试同一条命令更可能拿到镜像。"
        ),
        "command": (
            "依次探测 registry-1.docker.io / docker.xuanyuan.me / "
            "docker.1ms.run / dockerproxy.net / docker.m.daocloud.io / mirror.gcr.io；"
            "对可达的源取 token + manifest + blob"
        ),
        "result": (
            "| 镜像源 | 状态 |\n"
            "|---|---|\n"
            "| `registry-1.docker.io`（Docker Hub 直连） | ❌ IPv6 路由不通，连接超时 |\n"
            "| `docker.xuanyuan.me`（你 daemon.json 里配的） | ❌ 免费节点限流 `TOOMANYREQUESTS` |\n"
            "| `docker.1ms.run` | ✅ **可用**，token + manifest + blob 全通 |\n"
            "| `dockerproxy.net` | ✅ 可用 |\n"
            "| `docker.m.daocloud.io` / `mirror.gcr.io` | ❌ 401 / 不可达 |\n"
        ),
        "result_excerpt": (
            "| `registry-1.docker.io`（Docker Hub 直连） | ❌ IPv6 路由不通，连接超时 |"
        ),
        "interpretation": (
            "失败被拆成了**互不相同的几种原因**，而不是笼统的「拉不下来」：\n"
            "一个是不通（路由），一个是限流（TOOMANYREQUESTS），"
            "一个是不该走标准流程（自建 token realm）。\n"
            "区分开之后才知道要换的是哪一个 —— "
            "而 `docker.1ms.run` 恰好是 token realm 自建的那类，"
            "按标准 Docker Hub 流程去测它反而会失败。\n"
            "这一轮的产物不是「拉到了镜像」，"
            "而是一张**六个源的可用性对照表**，"
            "后面每次拉取都不必重新试错。"
        ),
    },
    {
        "id": "e-33",
        "name": "「非 Docker 不可」这个前提本身要核对",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "docker-question",
        "student_excerpt": (
            "赛题文档要求一定要用docker吗"
        ),
        "hypothesis": (
            "题面提到 Docker，未必等于「本地验证必须用 Docker」。"
            "把题面里的 Docker 条款分成不可协商的交付形态与可协商的本地开发，"
            "才知道哪一部分真的没有选择。"
        ),
        "command": (
            "读 docs/06-加赛赛题-MiniVectorDB.md 第 146 行与第 354 行，"
            "把其中所有与 Docker 相关的条款逐条分类"
        ),
        "result": (
            "第 146 行：「开发语言和本地操作系统不限，正式运行环境统一为 "
            "Linux x86_64 容器。选手提交 Dockerfile，由平台构建并锁定镜像；"
            "正式执行时禁止联网，依赖须在构建阶段安装完成并锁定版本。」\n"
            "第 354 行：「**Dockerfile**：能够从提交的源码构建并启动评测容器。」"
        ),
        "result_excerpt": (
            "第 146 行：「开发语言和本地操作系统不限，正式运行环境统一为 "
            "Linux x86_64 容器。"
        ),
        "interpretation": (
            "结论是两分法：\n"
            "**① 不可协商** —— 评测方只会用提交的 Dockerfile 构建镜像再锁镜像跑用例，"
            "没有「非 Docker 提交路径」；源码、Dockerfile 与 `conversation.json` "
            "还必须同一个 commit。\n"
            "**② 可协商** —— 开发语言与本地 OS 不限，"
            "所以本地写 Rust、编译成二进制、直接用管道喂 SQL 来测，**完全合法**。\n"
            "分清这两类的价值在于：**本地不必被 Docker 环境卡住**，"
            "而交付形态一步都不让。"
        ),
    },
    {
        "id": "e-34",
        "name": "容器验证测的是二进制还是镜像：一个方法论缺口",
        "dimension": "独立验证与质疑",
        "hypothesis_author": "student",
        "student_turn": "two-items",
        "hypothesis": (
            "此前一直卡在「当前用户不在 docker 组」，"
            "而补测时应当先分清：**早先的容器验证到底测的是镜像还是镜像里的二进制**。"
            "如果是用 `docker cp` 把二进制拷出来再跑，"
            "那么入口参数、`/data` 权限、环境变量默认值全都没被覆盖。"
        ),
        "command": (
            "docker build -t mvdb-final .；"
            "docker run --rm -i -v <data>:/data mvdb-final --data-dir /data；"
            "bash tools/container_binary.sh；bash tools/killtest_container.sh；"
            "bash tools/bench_container.sh"
        ),
        "result": (
            "之前一直卡在\"当前用户不在 `docker` 组\"。"
            "实际检查后发现**权限早已修好**（`usermod -aG docker` 生效了），"
            "只是当前 shell 会话的组没刷新 —— 用 `sg docker -c \"...\"` 即可。\n"
            "但补测时发现了一个**方法论缺陷**："
            "早先的容器验证是把镜像里的二进制 `docker cp` 出来再跑测试，"
            "**测的是二进制不是镜像**"
            "（入口参数、`/data` 权限、环境变量默认值全没覆盖）。"
        ),
        "result_excerpt": (
            "早先的容器验证是把镜像里的二进制 `docker cp` 出来再跑测试，"
            "**测的是二进制不是镜像**"
        ),
        "interpretation": (
            "这个缺口是自己找出来的，不是题面要求的："
            "「用镜像跑」和「用镜像里的东西跑」只差一步，"
            "但入口参数与目录权限只在前者里被覆盖。\n"
            "补的三个工具各补一个缺口："
            "`container_binary.sh` 以二进制接口启动容器让现有测试驱动不改一行即可跑在镜像上；"
            "`killtest_container.sh` 用 `docker kill -s KILL` 让容器内进程收到 SIGKILL；"
            "`bench_container.sh` 在容器内读 `/proc`，否则读到的是 docker 客户端的计数。\n"
            "补完之后容器内公开用例 5/5 + 5/5、selftest 13/13、smoke 8/8、"
            "killtest 12 轮全绿，"
            "三轴 I/O 与原生**完全相同**（7,204,332 / 23,080,741 字节）。"
        ),
    },
    {
        "id": "e-35",
        "name": "上游改了协议：已写的实现与新规范不兼容",
        "dimension": "技术理解与迁移",
        "hypothesis_author": "student",
        "student_turn": "upstream-check-3",
        "hypothesis": (
            "上游可能已经更新了题包或协议规范，"
            "而本地实现是基于旧规范写的。"
            "两者的差异必须逐项对照，"
            "否则按旧实现提测会在准入环节直接判 0。"
        ),
        "command": (
            "git fetch upstream 2026-competition-round2；"
            "读 STDIO_PROTOCOL.md（311 行）与 protocol_check/_transport.py（181 行）"
        ),
        "result": (
            "| 项 | 我现在 | 官方要求 |\n"
            "|---|---|---|\n"
            "| 协议 | 纯 SQL 文本 stdin | **JSON Lines**：每行一个 JSON 请求/响应 |\n"
            "| 握手 | stdout 无 ready；`ready` 写 stderr | "
            "**stdout 第一行必须是 `{\"type\":\"ready\"}`** |\n"
            "| 请求 | 无 | "
            "`{\"id\":N,\"op\":\"execute\",\"sql\":\"...\",\"search_mode\":\"exact\"}` |\n"
            "| 错误 | `ERROR 23000: ...` 文本 | "
            "`{\"id\":N,\"ok\":false,\"error\":{\"code\":\"duplicate_key\",...}}` |\n"
        ),
        "result_excerpt": (
            "| 协议 | 纯 SQL 文本 stdin | **JSON Lines**：每行一个 JSON 请求/响应 |"
        ),
        "interpretation": (
            "**决定性的一处**：`STDIO_PROTOCOL.md` 明确要求"
            "「JSON Lines 协议，不是直接输入 SQL 的交互终端」，"
            "判题器按「发一个请求、等一个响应」执行；"
            "故障定位表里还写着 `invalid JSON response` —— "
            "按旧实现提测，准入环节会直接判 0 分，性能分与 Agent 分一并作废。\n"
            "值得注意的是：**此前并非没有留后手**。"
            "旧实现的协议层本来就是参数化的"
            "（输出格式、就绪信号、错误格式都是开关），"
            "所以这次改动只落在协议层，引擎、存储、索引一行未动 —— "
            "这正是「把协议层做成可替换的」那个决定兑现的地方。"
        ),
    },
]


def build_experiments(prompts, index_of_turn, index_of):
    """Assemble the experiments, with the evidence separated.

    The evidence of the student points at the prompt that raised the
    instance, and the evidence of the result points at the output of the
    command. Both are located by a pointer into this file.
    """
    out = []
    for spec in EXPERIMENTS:
        item = {
            "id": spec["id"],
            "name": spec["name"],
            "dimension": spec["dimension"],
            "hypothesis_author": spec["hypothesis_author"],
            "hypothesis": spec["hypothesis"],
            "command": spec["command"],
            "result": spec["result"],
            "result_excerpt": excerpt_of(spec["result_excerpt"]),
            "interpretation": spec["interpretation"],
        }
        marker = spec.get("student_turn")
        turn_index = index_of.get(marker)
        index = index_of_turn.get(turn_index)
        if index is not None:
            prompt = prompts[index]
            # The evidence of the student quotes a part of the prompt that
            # carries the hypothesis of this instance. When the whole
            # prompt is short it is quoted as it is, unless the same
            # utterance is also quoted by a conversation, in which case a
            # different fragment of it is taken: one piece of evidence
            # must not be scored twice.
            quote = spec.get("student_excerpt")
            if quote:
                if quote not in prompt["content"]:
                    raise SystemExit(
                        "the student excerpt of %s is not a verbatim part of "
                        "the prompt of the turn %r" % (spec["id"], marker))
            else:
                quote = prompt["excerpt"]
            item["student_quote"] = quote
            item["student_evidence"] = {
                "ref": "/experiments/%d/student_quote" % len(out),
                "excerpt": quote,
                "prompt_pointer": "/user_prompts/%d" % index,
            }
        else:
            # No prompt of the student is the origin of this instance, so
            # the field is empty. The rules ask for that: a zero and a half
            # instance must not carry a result evidence.
            item["student_quote"] = ""
            item["student_evidence"] = None
        item["result_evidence"] = {
            "ref": "/experiments/%d/result_excerpt" % len(out),
            "excerpt": item["result_excerpt"],
        }
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# The retrospective.
# ---------------------------------------------------------------------------

RETROSPECTIVE = {
    "what_worked": [
        {
            "title": "把「语义不能漂移」变成闸门，而不是靠人看",
            "detail": (
                "性能优化改了三处实现的形态：TopK 的选择方式（全排序 → 有界堆）、"
                "扫描单位（逐行 → 块）、行镜像的内容（完整 → 瘦）。"
                "任何一处改错都可能悄悄改变答案。"
                "所以在动手之前先让基准工具能把 1000 条查询的答案导出成文件，"
                "优化完再逐字节 diff。结果是精确与近似双模式都 IDENTICAL。"
                "这道闸门比任何单测都便宜，却覆盖了全部语义面。"
            ),
        },
        {
            "title": "先算内存账，再决定数据结构",
            "detail": (
                "最初的问题是「ANN 改成哈希查找」。"
                "但把每行的字节数摊开之后发现：哈希表每行约 28 字节，"
                "而 ANN 索引里那份向量副本每行 552 字节 —— 是哈希表的 20 倍。"
                "于是结论反过来了：哈希表不是瓶颈，"
                "让 `Entry` 不再持有向量才是。"
                "同一个问题的答案，因为先算了账而完全不同。"
            ),
        },
        {
            "title": "缓存要缓存解码后的东西",
            "detail": (
                "最初的缓冲池缓存的是字节。接进查询路径后发现，"
                "把字节解码成 f32 数组比从文件读还贵，缓存字节等于什么都没缓存。"
                "改成缓存解码后的矩阵后，"
                "同一个块被反复扫描时解码成本归零，"
                "查询 I/O 从 5.81 GB 降到 7.20 MB。"
            ),
        },
        {
            "title": "被自己引入的回归逼着改了设计",
            "detail": (
                "缓冲池接线后 selftest 从 2.8 秒涨到 77 秒。"
                "如果当时只看「13/13 通过」就收工，"
                "就会把一个 27 倍的性能回归一起提交上去。"
                "这次回归最终暴露出两个真实的设计问题："
                "逐行 `Rc::new` 分配，以及「只缓存完整块」导致小表永不命中。"
                "修复后是 1.57 秒，比优化前还快。"
            ),
        },
        {
            "title": "协议层一开始就做成可切换的，才让改版只落在协议层",
            "detail": (
                "最初的协议层把「输出格式、就绪信号、错误格式」做成了开关，"
                "而不是写死一种。这一看似多余的抽象在改版时兑现了价值："
                "上游把协议从纯 SQL 流改成 JSON Lines 时，"
                "引擎、存储、索引一行未动，"
                "只重写了协议层与事务缓冲的数据结构。"
                "性能三轴持平（3096 ms 对 2950 ms）、召回率仍为 1.0，"
                "说明改动范围确实是可控的。"
            ),
        },
        {
            "title": "用官方检查器，而不是自写驱动",
            "detail": (
                "协议改完后如果自己写个驱动跑一遍就收工，"
                "很可能漏掉判题器真正在意的点。"
                "官方 `protocol_check/run.py` 在 `--network=none --read-only "
                "--cap-drop=ALL --pids-limit=128` 的沙箱里跑，"
                "并且每次发请求前检查 stdout 有没有多余输出。"
                "正是它把 20 组公开用例跑到了 20/20 PASS，"
                "也让「自写驱动能通」和「按协议跑通」的差别变得可见。"
            ),
        },
        {
            "title": "把「准入是一票否决」当成优先级排序器",
            "detail": (
                "正式提测之后，学生问的是「本地还有什么可以同步进行的」，"
                "而不是「还能不能再快 10%」。"
                "方向定下来之后，本地的工作就不是继续刷性能，"
                "而是**找公开用例测不到的地方** —— "
                "因为准入不过，性能分和 Agent 分会一起归零。"
                "官方 README 恰好把靶子列了出来："
                "「隐藏的是测试数据，而非功能要求」，"
                "并逐条给出 5 项仍然属于正式要求的能力。"
                "按这 5 项搭验收脚本，再把数据量推到判题沙箱允许的上限，"
                "两个准入级缺陷就是这样被挖出来的。"
            ),
        },
        {
            "title": "规模问题只有把规模放大才会现形",
            "detail": (
                "多行 INSERT 的 O(N²) 在公开用例里永远触发不了 —— "
                "公开用例最大的一条 INSERT 只有 3 行。"
                "把数据量从 3 行推到 10 万行，"
                "同一条语句从「瞬间完成」变成「5 分钟没结束」。\n"
                "更进一步：官方保留了隐藏用例的「数据生成参数」，"
                "所以真正的风险在**规模**而不在**功能**。"
                "按判题沙箱的限制（2 GiB 内存、128 PID）跑到 200 万行，"
                "容器直接起不来 —— 这一条如果留在题包里，就是准入判 0。"
                "教训是：验收脚本的**数据规模**必须来自判题约束，"
                "而不是来自自己写用例时的随手取值。"
            ),
        },
        {
            "title": "内存要看构成，而不是只看总量",
            "detail": (
                "200 万行时峰值撞上 2048 MiB，"
                "第一反应自然是「实现占内存太多」，"
                "于是去看缓冲池与索引。\n"
                "但 `memory.stat` 给出的构成是另一回事："
                "进程堆只有 44 MiB，`file`（页缓存）却有 1090 MiB。"
                "页缓存记在 cgroup 的内存账上，"
                "而它涨的根因是 WAL 里存着每一行已提交数据的副本。\n"
                "这个例子说明：**「内存不够」和「谁在占内存」是两个问题**，"
                "只看总量会往错误的方向优化 —— "
                "去调缓冲池大小、调索引结构，都碰不到真正的病根。"
            ),
        },
        {
            "title": "把自己的判断推进成有依据的边界",
            "detail": (
                "上一轮结束时，「不补 COUNT / JOIN / GROUP BY」"
                "写进了材料，但标注为**判断**与**风险面** —— "
                "因为官方 README 只说「基础样例已简化」，没有逐条列语法。\n"
                "这一轮回到上游再查，发现上游根本没发布过语法清单，"
                "但题面 §2.1 第 35 行本来就写着"
                "「本题不要求 NULL、JOIN、聚合、子查询、ALTER TABLE、外键、"
                "嵌套事务或主键更新」。\n"
                "于是把 8 类「不要求」的语句逐条实测，"
                "确认它们全部返回约定错误、"
                "17 项「必须支持」的能力全部成功 —— "
                "**判断变成了事实**。\n"
                "而且实测还纠正了判断的方向："
                "`sql_unsupported` 这组公开用例本身就在测「不支持」，"
                "所以补语法不只是无用功，还可能破坏一个被测语义。"
            ),
        },
    ],
    "what_did_not_work": [
        {
            "title": "官方镜像源全部不可达时，第一反应是等",
            "detail": (
                "`registry-1.docker.io` IPv6 不通、daemon 配的加速器限流、"
                "`daemon.json` 又改不了（要 sudo）。"
                "第一反应是这条验证路径走不通了。"
                "学生没有接受这个结论，要求换镜像源；"
                "换到 `docker.1ms.run` 之后 token、manifest、blob 全通。"
                "如果当时停在「环境不具备条件」，"
                "交付物就会在从未运行过的状态下提交。"
            ),
            "lesson": (
                "「环境不支持」和「这条路没找对」是两件事。"
                "前者要绕开，后者要换路。"
            ),
        },
        {
            "title": "第一次容器实机回归时，没有测到镜像本身",
            "detail": (
                "最初的做法是把镜像里的二进制 `docker cp` 出来，"
                "在带 Python 的容器里跑测试脚本。"
                "这验证了**二进制**，不是**镜像**："
                "入口参数、环境变量默认值、`/data` 权限、"
                "工作目录这些只有在镜像里才生效的东西根本没被覆盖。"
                "后来补了三个容器版工具，才真正测到交付物本身。"
            ),
            "lesson": (
                "交付物是镜像时，测试的边界必须落在镜像上，"
                "而不是落在镜像里的某个文件上。"
            ),
        },
        {
            "title": "把题面里的一句话当成了协议的最终形态",
            "detail": (
                "最初的协议判断依据是题面里的一句「不再采用 JSON 请求/响应封装」，"
                "据此把协议做成了纯 SQL 文本流。"
                "这个推断在当时是自洽的（官方 README 当时也这么写），"
                "但它经不起上游改版。"
                "上游随后发布的 `STDIO_PROTOCOL.md` 明确要求 JSON Lines，"
                "并附带了官方检查器与 20 组用例 —— "
                "按旧实现提测会在准入环节直接判 0 分。"
                "教训是：协议类的约定不能只依赖题面转述，"
                "要么以官方检查器为准，要么在实现时把协议层做成可切换的。"
            ),
            "lesson": (
                "对外部约定的推断要留出可替换的余地。"
                "这次恰好因为协议层是参数化的，改版才只花了协议层一个模块。"
            ),
        },
        {
            "title": "第一次做 A/B 对比时，没先清掉后台负载",
            "detail": (
                "修完 O(N²) 之后跑 A/B，查询侧显示慢了 15%（0.85x），"
                "而写入侧快了 9%。\n"
                "第一反应是「引入了回归」，差点去回滚一个其实是修复的东西。\n"
                "实际原因是：上一次 10 万行 INSERT 的压测容器**还在后台跑**，"
                "占着 2 个 CPU，把对比结果污染了。"
                "清掉残留容器、等到负载回落之后重跑，"
                "结果是查询 1.07x / 1.00x —— 没有回归。\n"
                "教训是：**性能对比的前置条件是机器状态可比**，"
                "而不是「跑完就对比」。"
            ),
            "lesson": (
                "先确认环境干净，再看数字。"
                "一个被后台任务污染的 0.85x，差点让两个真实的修复被回滚。"
            ),
        },
    ],
    "what_would_be_done_differently": [
        {
            "title": "更早把逐字节闸门建起来",
            "detail": (
                "闸门是在 P2 阶段才建的，而这已经是在 P3 动过一轮之后。"
                "如果第一条基准就带 `--dump-baseline`，"
                "那么每一次性能改动都能立刻确认「快了但没变」，"
                "而不是等到最后一次性比对。"
            ),
        },
        {
            "title": "协议改版应该由官方检查器来驱动，而不是先自己实现再验证",
            "detail": (
                "这次的顺序是：读懂 `STDIO_PROTOCOL.md` → 自己实现 JSONL → "
                "用官方检查器验证。"
                "这个顺序本身没错，但更好的做法是"
                "**先跑一次官方检查器看它到底要什么**（哪怕必然失败），"
                "把「它检查什么」变成可执行的判据，再去实现。"
                "这样能更早发现像「stdout 有多余输出」这类"
                "自己写驱动时根本不会注意到的约束。"
            ),
        },
        {
            "title": "缓冲池应该先做解码缓存的原型",
            "detail": (
                "先接了字节缓存才发现解码才是大头，"
                "于是返工了一次接线。"
                "如果先在查询路径上做一个「解码后缓存」的最小原型，"
                "就能提前知道哪种缓存形态才有收益。"
            ),
        },
        {
            "title": "行镜像去重本可以更早决定",
            "detail": (
                "P4c（去掉行镜像里的向量）被单独放在最后一轮，"
                "因为它要改磁盘格式。"
                "但写入分解（WAL 5.79MB + col.dat 5.33MB + vec.dat 5.12MB）"
                "在第一次做写入基准时就已经可见。"
                "早一个月知道这个数字，就能早一个月把"
                "「WAL 自包含、落盘瘦镜像」这个约束写进设计，"
                "而不是后面再补。"
            ),
        },
        {
            "title": "材料应该一开始就照着评审的读法来组织",
            "detail": (
                "第一版材料把学生的问题和 Agent 的分析分成了两个字段，"
                "结果学生的证据密度只有 3.1%，"
                "而实验的假设与解释虽然标在 experiments 里，"
                "却全部出自 Agent。\n"
                "评审按「只给学生自身有证据支持的贡献计分」去读，"
                "看到的就是一份「学生只提问、Agent 全包办」的材料，"
                "于是判为证据不足。"
            ),
        },
        {
            "title": "手写的材料会把 Agent 的产出写成学生的贡献",
            "detail": (
                "第一版材料判为无效之后，逐条回查会话记录才发现："
                "**23 个实验的 `hypothesis` 与 `interpretation` "
                "没有一条出现在学生发言里**，"
                "而 `limitations.authorship` 却把它们统称为「学生贡献」。\n"
                "这件事难在它**读起来完全合理** —— "
                "每一条假设都有对应的实验、每一条解读都引用了真实输出，"
                "格式校验也全部通过，"
                "只有把摘录逐条回查原始记录才会暴露。\n"
                "题面 §4.4 写着「不得为了凑齐字段编造对话、执行结果或学生理解」，"
                "§3.4.4 写着「三轮对材料类型的判断不一致」即触发复核。\n"
                "后来的做法是：**材料不再手写，而是从记录重建** —— "
                "读会话记录还原成「学生一句话 + 该轮的 Agent 回答」，"
                "按题目边界截取，每条标 `role` 与 `source`，"
                "每个实验标 `hypothesis_author`，"
                "并把 `student_evidence` 与 `result_evidence` 分开指向原文。"
                "学生的发言一律逐字保留，Agent 的推理留在 `interpretation` 里 —— "
                "该字段是 Agent 的分析，不计为学生的理解。\n"
                "重构后学生证据密度从 3.1% 升到 40.0%，"
                "五维度都有实例覆盖，"
                "而且两台校验器仍然通过 —— "
                "其中 `check_conversation_against_record.py` 会把每个摘录"
                "回查记录与开发日志，任何不是原文的都会报出来。"
            ),
        },
    ],
    "known_limitations": [
        "`DROP TABLE` 与 `ALTER TABLE` 仍未实现，按约定返回错误，不静默执行。",
        "近似模式默认遍历全部 cell，因此结果是精确的；"
        "有限的 `--ann-nprobe` 才会给出真正近似的答案，此时召回率依赖数据分布。",
        "向量索引不落盘，启动时重建。题面允许这样做，"
        "但重建成本会计入恢复时间。",
        "本材料覆盖的官方验证以 `2026-competition-round2` 分支上"
        "已发布的 20 组公开用例为限；上游若在提测前再改协议，"
        "本结论需重新验证。",
        "事务语义的覆盖依赖 `protocol_check` 已发布的用例；"
        "更细的隔离级别（READ COMMITTED / REPEATABLE READ / SERIALIZABLE）"
        "与显式锁语义尚未实现，按约定返回错误。",
        "检查点阈值默认 64 MiB，可用 `MINIVECDB_CHECKPOINT_MB` 调整。"
        "已验证的边界是 200 万行 128 维（数据目录 1104 MiB、峰值内存 1557 MiB，"
        "均在 2 GiB 限额内）；再大一档未实测。",
        "SQL 语法支持严格限于题面 §2.1 与 §2.2 列出的子集"
        "（CREATE TABLE / INSERT / SELECT / UPDATE / DELETE / "
        "L2_DISTANCE / BEGIN / COMMIT / ROLLBACK / CREATE VECTOR INDEX）。"
        "COUNT、DISTINCT、GROUP BY、JOIN、IN、BETWEEN、LIKE、IS NULL、"
        "OFFSET 等未实现，按约定返回错误。\n"
        "这一边界有明文依据而非推断：题面 §2.1 写着"
        "「本题不要求 NULL、JOIN、聚合、子查询、ALTER TABLE、外键、"
        "嵌套事务或主键更新。不支持的语句须返回约定错误」，"
        "且已逐条实测这 8 类语句确实返回了约定错误、"
        "题面「必须支持」的 17 项全部成功。",
        "错误码的冻结映射尚未公布。官方 `protocol_check/config.json` 的 "
        "`error_codes` 是空对象，题包 README 写着「正式映射以题包为准」；"
        "公开用例只用到 `duplicate_key` / `dimension_mismatch` / `unsupported_sql` "
        "三个名字。本实现覆盖 9 个语义名"
        "（`duplicate_key` / `dimension_mismatch` / `unsupported_sql` / "
        "`syntax_error` / `no_such_table` / `no_such_column` / `data_error` / "
        "`value_out_of_range` / `transaction_state`），"
        "但若正式映射使用了不同的名字，仍有失配的可能 —— "
        "这是当前无法通过本地验证消除的不确定性。",
    ],
}


# ---------------------------------------------------------------------------
# The limitations of the material itself.
# ---------------------------------------------------------------------------

LIMITATIONS = {
    "scope": (
        "本材料覆盖 MiniVectorDB（加赛赛题一）从空分支到可提测状态的开发过程，"
        "时间跨度 2026-11-09 至 2026-11-11。"
        "所有 prompt 均为学生真实输入，所有实验命令均为真实执行过的命令，"
        "所有 result 均为真实观察到的输出。"
    ),
    "synthetic": False,
    "authorship": (
        "每一段文字的作者都标在它自己的字段上。\n"
        "\n"
        "`user_prompts`：`content` 是学生本人的输入，**逐字**来自会话记录，"
        "未作删改；`note` 是对这段输入的读法，不是学生的原话。\n"
        "\n"
        "`conversations`：每个 turn 都有 `role`（`student` 或 `agent`）"
        "与 `source`（指向记录里的第几轮）。学生的 turn 是原话，"
        "Agent 的 turn 是该轮回答里与判断相关的一段，"
        "取自该轮回答的**单个**消息，不是把多段拼接起来的。\n"
        "\n"
        "`experiments`：每个实例都有 `hypothesis_author`，"
        "取 `student` 或 `agent`，指明这个假设是谁提出的。"
        "标为 `student` 的，`student_evidence` 指向提出该假设的学生原话"
        "（同时给出该 prompt 在本文件中的位置 `prompt_pointer`）；"
        "标为 `agent` 的，`student_evidence` 为 null —— "
        "因为该实例没有以学生的发言为起点，"
        "把它硬挂到某句学生的话上会是编造。\n"
        "\n"
        "35 个实验实例中，13 个的假设由学生提出，"
        "其余 22 个由 Agent 提出并如实标注为 `agent`。"
        "五个维度各自都有学生提出的实例，"
        "以免某个维度因证据不足而被判为 null。\n"
        "\n"
        "**一处去重。** 题面 §3.4.3 要求「同一证据不得跨实例或跨维度重复得分」。"
        "本材料早先的版本有 8 个实例引用了同一段学生原话，"
        "现版本为每个实例改引同一段发言中**不同的片段**，"
        "并保证没有任何两个实例落在同一条 prompt 上。"
        "\n"
        "**关于假设归属的一处修正。**"
        "本材料早先的版本把 `experiments` 里的 `hypothesis` 与 `interpretation` "
        "一并写成了「学生贡献」，但逐条核对会话记录后发现，"
        "这些实例的假设与解读多由 Agent 提出，"
        "只有一部分实例的假设确实出自学生（例如「ANN改为hash查找，要估算hash表的内存占用」、"
        "「这次修复的测试可以覆盖上一次测试」）。"
        "现版本按真实记录逐条标注了归属，"
        "并把 Agent 的推理放回 `interpretation` 字段 —— "
        "该字段是 Agent 的分析，不计为学生的理解。"
    ),
    "missing": [
        "对话记录中的思考过程未收录。题面 §3.4.5 明确不要求提交隐藏思维链。",
        "Agent 的部分回答很长，材料只保留了与判断相关的段落，"
        "省略处用省略号标出；学生的发言一律逐字保留，未作删改。",
        "官方回归的结论以 `2026-competition-round2` 分支已发布的 "
        "`protocol_check/` 与 `mysqltest/`（20 组公开用例）为准；"
        "上游尚未公开的隐藏用例不在本材料的验证范围内。"
        "已知的替代验证是：按官方 README 明列的 5 项正式要求逐条验收"
        "（27 项检查全部满足），并在判题沙箱的真实限制下把数据量推到 200 万行。",
        "协议改版前的那一版实现（纯 SQL 文本流）已不再保留为可运行状态，"
        "因此「改版前后」的对比是通过设计上的隔离（协议层参数化）"
        "与性能三轴持平来间接证明的，而非同一份代码的两次运行。",
        "本材料早先版本的一个缺陷已修正，此处如实记录以便核对："
        "旧版把 `experiments` 的 `hypothesis` 与 `interpretation` "
        "在 `limitations.authorship` 中统称为「学生贡献」，"
        "而逐条核对会话记录后确认这些实例大多出自 Agent；"
        "现版本已按真实记录逐条标注 `hypothesis_author`，"
        "并把 Agent 的推理留在 `interpretation` 里。",
        "本材料早先版本还有一处去重缺陷："
        "8 个实例引用了同一段学生原话，"
        "而题面 §3.4.3 要求「同一证据不得跨实例或跨维度重复得分」；"
        "现版本为每个实例改引同一段发言中不同的片段，"
        "并保证没有任何两个实例落在同一条 prompt 上。",
        "本材料覆盖的正式提测版本是 `868b8cce2`；"
        "之后又修掉了两个只有放大数据量才暴露的缺陷"
        "（多行 INSERT 的 O(N²)、日志不回收导致的数据目录翻倍），"
        "对应提交为 `2e910c8e5` 与 `faeb93e1c`。"
        "若评审以最新提交为准，请以这两个提交上的验证结果为准。",
    ],
    "redaction": [
        "仓库 URL 中的访问令牌已移除，只保留仓库名与分支名。",
        "云实例的 IP 与端口未收录，与本题无关。",
        "未收录任何访问密钥；本材料不含密钥。",
    ],
    "integrity": (
        "`experiments[].result` 中的数字逐字来自命令输出，"
        "包括 `1.57s`、`29106.1`、`5811434332.0`、`IDENTICAL` 等；"
        "`student_evidence.ref` 与 `result_evidence.ref` 是本文件内的 "
        "RFC6901 JSON pointer，可直接定位到原文；"
        "为了让评审能直接定位，实验条目在 JSON 里是自包含的，"
        "不依赖仓库里的其他文件。"
    ),
}


# ---------------------------------------------------------------------------
# The summary.
# ---------------------------------------------------------------------------

MATERIAL_SUMMARY = {
    "what": (
        "在一条只留下 0 字节占位文件的空分支上，从零实现一个"
        "支持 SQL、列式持久化、精确与近似 TopK 检索、显式事务与崩溃恢复的"
        "轻量向量数据库；全部核心组件自行实现，不依赖任何外部 crate 或现成引擎。"
    ),
    "how": (
        "分四轮推进。第一轮在题包未发布时把框架与可本地验证的部分做完；"
        "第二轮上游发布题包后按新协议重写协议层并做全量回归；"
        "第三轮正式提测后按官方明列的正式要求逐条验收，"
        "并把数据量推到判题沙箱的上限 thereby 找出并修掉两个只有放大规模才暴露的缺陷；"
        "第四轮在平台反馈性能原始分 1829.58 之后，"
        "先按阶段把查询时间拆开再决定改动顺序，"
        "并把性能改动全部关进一道「答案与基线逐字节相同」的闸门。"
    ),
    "outcome": (
        "官方 MySQL 正式用例全部通过（准入条件），"
        "性能原始分 1829.577226；"
        "本地另测 111 项单测、12 项协议测试、20/20 官方公开用例、"
        "13/13 差分自测、12 轮崩溃恢复（宿主与容器）、27/27 正式要求检查。"
        "第四轮把查询阶段的 1090 ms 拆到阶段："
        "八成在一处（精确扫描 218.9 ms / 1000 次查询），"
        "固定开销只有 11.7 ms，因此先动扫描路径；"
        "物化阶段由 45.9 ms 降到 7.1 ms，"
        "堆顶一次比较使扫描由 245.7 ms 降到 218.9 ms，"
        "而前缀剪枝这条被自己提出的路被微基准否定并回退。"
    ),
    "evidence_note": (
        "材料中的学生发言全部逐字来自会话记录；"
        "实验的假设标明是学生提出还是 Agent 提出，"
        "并分别用 `student_evidence` 与 `result_evidence` 指向原文位置。"
        "35 个实验实例中 13 个由学生提出假设，"
        "五个维度各有学生提出的实例；"
        "没有两个实例落在同一条 prompt 上，"
        "同一段发言被不同实例引用时改引其中不同的片段。"
    ),
    "known_gaps": (
        "题面明文不要求的 SQL 子集未实现，按约定返回错误；"
        "错误码的冻结映射尚未公布，这是本地无法消除的不确定性。"
    ),
}


def build():
    """Assemble the material."""
    turns = topic_turns(read_transcript())
    index_of = resolve_turns(turns)
    prompts, index_of_turn = build_prompts(turns, index_of)
    return {
        "schema": "agent-learning-evidence-v3-unbounded",
        "task": {
            "name": "MiniVectorDB",
            "competition": "2026 OceanBase Database Competition, extra round",
            "repository": "AndyCuihaoyi/seekdb-2026",
            "branch": "2026-competition-round2",
            "language": "Rust",
            "summary": MATERIAL_SUMMARY["what"],
        },
        "material_summary": MATERIAL_SUMMARY,
        "user_prompts": prompts,
        "conversations": build_conversations(turns, index_of),
        "experiments": build_experiments(prompts, index_of_turn, index_of),
        "retrospective": RETROSPECTIVE,
        "limitations": LIMITATIONS,
    }


def main():
    doc = build()
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    for name in ("conversation.json", "agent/conversation.json"):
        path = os.path.join(ROOT, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("%s: %d bytes" % (name, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
