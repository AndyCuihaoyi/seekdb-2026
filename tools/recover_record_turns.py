#!/usr/bin/env python3
"""Recover the turns of the record that the transcript no longer holds.

On 2026-11-13 the transcript of the working session was overwritten: the
supervising session and this one were appended to the same file, and the
turns of the working session that were in it before them are no longer
there. The file `agent/conversation.json` of the commit `a02c51604` still
holds the material that was built from those turns, and every line of it
was copied out of the record word for word.

This script reads that material and writes the turns back into the shape
that the builder reads, so that the builder keeps working from the record
rather than from a summary of it. Nothing is written that was not already
in the material, and nothing is attributed to the student that the student
did not write.

The turns are written to `tools/record_snapshot.json`, which the builder
reads when a turn of the record cannot be found in the transcript. The
snapshot is checked against the material every time it is used, so that a
snapshot that does not match the material is reported rather than trusted.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "tools", "record_snapshot.json")

# The commit whose material still holds the turns of the record.
SOURCE_COMMIT = "a02c5160441b69662a548d469b87b7531346f010"

# The marker of a turn of the record is a piece of its text, the same way
# the builder finds a turn in the transcript. The markers of the turns
# that the material holds are taken from the source of the conversations,
# which names them.
MARKERS = {
    "read-docs": "@docs/06-加赛赛题-MiniVectorDB.md",
    "placeholder": "题包目前是占位符",
    "s0": "先完成S0",
    "docker-question": "赛题文档要求一定要用docker吗",
    "mirror": "试试从其他镜像源拉取",
    "sudo": "我可以给你sudo权限",
    "terminal-paste": "cuihaoyi@desktop5",
    "inventory": "现在题包还没有公布，还有哪些遗留的可以完成的项目",
    "prioritise": "可以将没有题包前提下能完成的项目按照优先级规划",
    "upstream-check-1": "查看官方仓库有没有更新题包或者测试套件",
    "what-next": "根据现有的题包和信息，我们还有哪些优化和步骤能提前完成",
    "material-how": "conversation.json 材料准备 需要怎么做",
    "material-later": "那我们还是先将优化内容完成再来完成json的内容",
    "ann-hash": "ANN改为hash查找，要估算hash表的内存占用",
    "plan-p0": "MiniVectorDB · P0 计划（不依赖官方题包）",
    "upstream-check-2": "再次查看上游仓库有没有更新题包或者测试套件",
    "upstream-check-3": "再次查看上游仓库是否更新了新的文件",
    "two-items": "按顺序分别处理两项工作",
    "plan-abcd": "MiniVectorDB 性能与合规加固（A–D 批次）",
    "submit-info": "提交测试的信息应该填写什么呢",
    "after-submit": "我已经提交了测试，本地还有什么可以同步进行的吗",
    "rerun-and-syntax": "这次修复的测试可以覆盖上一次测试",
    "platform-feedback": "MySQL 正式用例：通过；此项为准入条件",
    "profile-first": "先精确定位1090 ms 的构成",
    "seal-and-push": "我已经将kernel的其他无关编译工作停止了",
    "platform-fail-2": "先停一下：",
    "converge-commit": "快速收敛结论，再提交一次commit",
    "hidden-cases": "测试还是没有通过，查看上游仓库的基准和要求文档",
    "diff-against-scoring": "得分的commit：ae6514687970ab74c0bec648d1e47bb0b4df41dc",
    "supervise-quota": "1.今天还有两次额度",
    "supervise-optimizations": "当前做的性能优化有哪些",
    "supervise-oom": "上一次成功得分的commit为什么没有内存OOM问题",
}

# The prompts that the student sent, in the order they were written. The
# material lists them with a day, and the day is what orders them.
PROMPT_ORDER = (
    "read-docs", "placeholder", "s0", "docker-question", "mirror", "sudo",
    "terminal-paste", "inventory", "prioritise", "upstream-check-1",
    "what-next", "material-how", "material-later", "ann-hash", "plan-p0",
    "upstream-check-2", "two-items", "plan-abcd", "submit-info", "after-submit",
    "rerun-and-syntax", "platform-feedback", "profile-first", "seal-and-push",
    "platform-fail-2", "converge-commit", "hidden-cases",
    "diff-against-scoring", "supervise-quota", "supervise-optimizations",
    "supervise-oom",
)


def load_material():
    """Read the material of the source commit."""
    import subprocess
    out = subprocess.run(
        ["git", "-C", ROOT, "show", "%s:agent/conversation.json" % SOURCE_COMMIT],
        check=True, stdout=subprocess.PIPE)
    return json.loads(out.stdout.decode("utf-8"))


def turns_of(material):
    """The turns of the record that the material holds.

    A turn is a message of the student followed by the answers of the
    agent. The material keeps the message of the student in
    `user_prompts` and in the conversations, and it keeps the answer of the
    agent in the conversations.
    """
    prompts = {}
    for prompt in material.get("user_prompts", []):
        prompts.setdefault(prompt["content"], prompt)

    # The answer of the agent of a turn is the one of the conversation
    # that carries the same source.
    answers = {}
    for conversation in material.get("conversations", []):
        for turn in conversation.get("turns", []):
            if turn.get("role") != "agent":
                continue
            source = turn.get("source", "")
            if source.startswith("record:"):
                answers.setdefault(source[len("record:"):], []).append(
                    turn.get("content", ""))

    out = {}
    for name, marker in MARKERS.items():
        # The message of the student is the one that holds the marker.
        student = None
        for text, prompt in prompts.items():
            if marker in text:
                student = text
                break
        if student is None:
            # The turn is only in the conversations.
            for conversation in material.get("conversations", []):
                for turn in conversation.get("turns", []):
                    if turn.get("role") != "student":
                        continue
                    source = turn.get("source", "")
                    if source == "record:%s" % name:
                        student = turn.get("content", "")
                        break
                if student is not None:
                    break
        if student is None:
            continue
        out[name] = {
            "user": student,
            "agent_parts": answers.get(name, []),
        }
    return out


def main():
    material = load_material()
    turns = turns_of(material)
    missing = [name for name in MARKERS if name not in turns]
    if missing:
        print("FAIL  the material does not hold the turns %s"
              % ", ".join(sorted(missing)))
        return 1

    snapshot = {
        "note": (
            "The turns of the record, kept word for word, that the transcript "
            "of the sessions no longer holds. They were copied out of the "
            "transcript into the material of the commit %s, and this file "
            "puts them back where the builder reads them. Nothing here was "
            "written by hand."
        ) % SOURCE_COMMIT,
        "source_commit": SOURCE_COMMIT,
        "turns": turns,
    }
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("%s: %d turns" % (SNAPSHOT, len(turns)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
