#!/usr/bin/env python3
"""Put the turns of the current round into the snapshot of the record.

The transcript of the working session is written at the end of a turn, so
the turns of the round that is in progress are not in it yet. The
material is built from the record, and the record has to hold the turns
of this round for the material to cite them.

The text of a turn is copied word for word: it is taken out of the
transcript of the working session, or, for the turn that the working
session has not written yet, out of the message of the supervising
session that carries it. Nothing is written by hand.

The snapshot is checked against the transcript every time it is used, and
the transcript wins where it holds the turn: `merge_snapshot` skips a
turn of the snapshot whose marker the transcript already carries.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "tools", "record_snapshot.json")

RECORD = os.path.expanduser(
    "~/.cursor/projects/home-cuihaoyi-OceanBase/agent-transcripts"
)

# The turns of this round, and the marker that names each of them.
#
# The first three are in the transcript of the working session. The
# fourth is the turn that the working session has not written yet: its
# text is taken out of the message of the supervising session that
# carries it.
#
# The turns of the round that fixed the timeout are the ones that make the
# material and the code agree. They are named by a piece of their text:
#
# * `platform-fail-4` is the report of the platform about the commit
#   `b19769271`, the one that failed with `JSON response timeout`;
# * `localise-timeout` is the instruction to localise the failure;
# * `prioritise-suspects` is the instruction to look at the suspects in
#   the order of their likelihood;
# * `experiments-ab` is the instruction to run the two decisive
#   experiments, the block size and the cache size;
# * `deliver-fix` is the instruction that opened the round of the fix;
# * `supervise-close` is the question of the supervising session that
#   found the gap between the material and the code.
TURNS = (
    ("remeasure-axes", "有一项性能数据需要重测"),
    ("admission-gate", "# 任务：为提测做准入验证"),
    ("platform-fail-3", "Agent Prompt 原始分:正确性门槛未通过（失败用例数：1）。原因：JSON response timeout（未及时收到JSON响应，请检查换行、flush和程序阻塞） × 1。未执行Agent Prompt评测。"),
    ("fix-complexity", "# 任务：完成复杂度回归的修复收尾"),
    ("platform-fail-4", "commit：b19769271ef0e54db9f5e49385dc37a9c804bb65还是没有通过"),
    ("localise-timeout", "【本轮要交付的东西：一份定位报告，不是一个补丁】"),
    ("prioritise-suspects", "按照怀疑的优先级进行排查和测试"),
    ("experiments-ab", "先做一个 30 分钟的决定性实验"),
    ("deliver-fix", "【本轮要交付的东西：一个可提测的修复 + 一份闭环的证据链】"),
    ("supervise-close", "agent-transcripts/299dedb6-ef18-49d7-bff5-86e687e2d7f5/299dedb6-ef18-49d7-bff5-86e687e2d7f5.jsonl 问题似乎已经闭环"),
    # The turns of the round that removed the inverted indexes.
    #
    # * `shape-no-where` is the turn that changed the object of the
    #   diagnosis: the official case has no WHERE clause at all, so the
    #   inverted index bought nothing on the case that is judged;
    # * `whole-case-budget` is the same turn, read at the point where it
    #   separates the budget of the case from the budget of one statement;
    # * `remove-inverted` is the instruction to remove the inverted index
    #   and to do nothing else.
    #
    # The two last turns are the same message of the supervising session,
    # so they are read from it with two different markers of the block.
    ("shape-no-where", "【平台在修复生效的情况下仍然失败。上一轮的诊断对象错了，本轮换对象】"),
    ("whole-case-budget", "第 2 步（必做，40 分钟）：在整例 600 秒的尺度上量"),
    ("remove-inverted", "【去掉倒排索引。目标是收敛到能过门槛，不要再做新实验】"),
    # The two turns of the student about this round, in their own words.
    ("student-timeout-reading", "按照官方文档的timeout报错口径，哪些优化会导致超时呢"),
    ("student-drop-inverted", "如果舍弃倒排索引，其他的优化是不是无效了"),
    ("student-converge-gate", "我觉得先去掉倒排索引，通过门槛比较重要"),
)

# The prompt that the supervising session wrote and the working session
# has not received yet is inside a block of markdown in the message.
#
# The fence of the block is three or four backticks, and the name of the
# language is optional: the messages of the supervising session use both
# forms, and the block is taken as it is, word for word, whichever form it
# is written with.
RELAY = re.compile(r"`{3,4}(?:markdown)?\n(.*?)\n`{3,4}", re.S)


def read_events():
    """Read every transcript into a list of (role, text) pairs."""
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
                events.append((role, text))
    return events


def find_user_text(events, marker):
    """The text of the message of the student that carries the marker."""
    hits = []
    for role, text in events:
        if role != "user":
            continue
        if "<user_query>" in text:
            text = text.split("<user_query>", 1)[1].split("</user_query>")[0]
        text = text.strip()
        if marker in text:
            hits.append(text)
    if not hits:
        return None
    if len(set(hits)) > 1:
        raise SystemExit("the marker %r matches %d different messages"
                         % (marker, len(set(hits))))
    return hits[0]


def find_relayed_text(events, marker):
    """The text of the prompt that the supervising session wrote.

    The prompt is inside a block of markdown in the message of the
    supervising session. The block is taken as it is, word for word.
    """
    for role, text in events:
        if role != "assistant":
            continue
        for body in RELAY.findall(text):
            if marker in body:
                return body
    return None


def main():
    events = read_events()
    snapshot = {
        "note": (
            "The turns of the record, kept word for word, that the transcript "
            "of the sessions no longer holds. They were copied out of the "
            "transcript into the material of the commit a02c5160441b69662a548d469b87b7531346f010, "
            "and this file puts them back where the builder reads them. Nothing here was "
            "written by hand. The turns of the round that is in progress are kept "
            "here as well, because the transcript of the working session is written "
            "at the end of a turn; the transcript wins over this file wherever it "
            "holds the turn."
        ),
        "source_commit": "a02c5160441b69662a548d469b87b7531346f010",
        "turns": {},
    }

    with open(SNAPSHOT, "r", encoding="utf-8") as fh:
        existing = json.load(fh)
    snapshot["turns"].update(existing.get("turns", {}))

    for name, marker in TURNS:
        text = find_user_text(events, marker)
        origin = "the transcript of the working session"
        if text is None:
            text = find_relayed_text(events, marker)
            origin = "the message of the supervising session that carries it"
        if text is None:
            print("FAIL  the turn %r is not in the record" % name)
            return 1
        # The answer of the agent of the turn is not copied: the turns of
        # the snapshot that came from the material of an earlier commit
        # carry it, and the turns of this round are cited for what the
        # student said, not for the answer.
        snapshot["turns"][name] = {
            "user": text,
            "agent_parts": [],
            "_origin": origin,
        }
        print("%s: %d characters, from %s" % (name, len(text), origin))

    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("%s: %d turns" % (SNAPSHOT, len(snapshot["turns"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
