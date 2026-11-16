#!/usr/bin/env python3
"""Check the attribution of the prompts against the real record.

The rules of the statement count only what the student came up with, and
they ask for the source of every piece of the material to be marked. A
prompt that is marked as the student's own while the record shows that
the agent wrote it makes the reviewer doubt every attribution in the
file, and the case is then marked as "evidence insufficient".

The attribution is checked here against the transcripts. The pattern of
the supervising session is this:

    the student asks for a prompt   ->  "给出提示词"
    the agent writes one            ->  "Here is the prompt" / "# 给 agent 的提示词"
    the student pastes it into the working session

A prompt whose text is found in the answer of the agent to a turn where
the student asked for a prompt was written by the agent, whatever the
student did with it afterwards.

The check walks the transcripts of every session, finds the turns that
follow such an ask, and reports every prompt of the material whose
attribution does not match.

    10|* every prompt that the agent drafted is marked `agent_draft`;
* every prompt that the student wrote is marked `student`;
* no instance is scored on a prompt that the agent drafted.
"""

import glob
import json
import os
import re
import sys

# The folder that holds the transcripts of the sessions.
RECORD = os.path.expanduser(
    "~/.cursor/projects/home-cuihaoyi-OceanBase/agent-transcripts"
)

# The words the student uses when he asks the agent for a prompt.
ASK = "提示词"

# A prompt has to be long enough for the comparison to mean something: a
# question of the student and a sentence the agent wrote can look alike.
MINIMAL = 12

# How much of the prompt is compared. The material keeps a cut of the
# prompt, and the cut lands wherever the budget allowed, so the head of
# the prompt is what is compared: it is the part that says whose the
# prompt is.
HEAD = 120


def norm(text):
    """The text with the line breaks collapsed.

    The transcripts and the material differ in their line breaks, because
    the material keeps the text of a turn as it was written and the
    transcripts wrap it.
    """
    return re.sub(r"\s+", " ", text).strip()


def turns_of(path):
    """The turns of one session: a message of the student and the answers
    of the agent that belong to it."""
    out = []
    current = None
    for line in open(path, encoding="utf-8", errors="replace"):
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
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            text = ""
        if role == "user":
            if current is not None:
                out.append(current)
            current = {"user": text, "agent": []}
        elif current is not None and text.strip():
            current["agent"].append(text)
    if current is not None:
        out.append(current)
    return out


def drafted_texts():
    """The answers of the agent to a request for a prompt.

    Only the answers that the agent gave in answer to a turn in which
    the student asked for a prompt are returned. Those are the texts
    that the agent wrote for the student to paste into the working
    session.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(RECORD, "*", "*.jsonl"))):
        for turn in turns_of(path):
            if ASK not in norm(turn["user"]):
                # The student did not ask for a prompt in this turn, so
                # nothing in the answer of the agent was written for him
                # to paste.
                continue
            for answer in turn["agent"]:
                found.append(norm(answer))
    return found


def was_drafted(content, drafts):
    """Whether the text of a prompt was written by the agent.

    The material keeps a cut of the prompt, and the cut lands wherever
    the budget allowed, so the whole of the cut is not what can be
    compared. The lines of the cut are compared one by one instead.

    A prompt the student wrote that the agent then quoted back does not
    make the prompt the agent's: the student wrote it first. The test
    that tells the two apart is how much of the prompt the answer of the
    agent holds: the agent's own prompt is held whole, while a quoted
    piece of the student's message is held in a few lines of an answer
    that is mostly something else.

    The threshold is three quarters: the prompts of the agent that were
    measured hold between seven eighths and all of their lines in the
    answer, while the one prompt of the student that the agent quoted
    back holds two thirds of its lines.
    """
    lines = [ln for ln in content.split(" ") if len(ln) >= 24]
    if not lines:
        return False
    hits = 0
    for line in lines:
        for answer in drafts:
            if line in answer:
                hits += 1
                break
    return hits * 4 >= len(lines) * 3


def fail(problems, message):
    problems.append(message)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "conversation.json")
    if not os.path.exists(path):
        print("FAIL  conversation.json does not exist")
        return 1

    doc = json.load(open(path, encoding="utf-8"))
    problems = []

    drafts = drafted_texts()

    for i, prompt in enumerate(doc.get("user_prompts", [])):
        where = "/user_prompts/%d" % i
        content = norm(prompt.get("content", ""))
        if len(content) < MINIMAL:
            continue
        # The material keeps a cut of the prompt, and the cut lands
        # wherever the budget allowed, so the whole of the cut is not
        # what can be compared. The lines of the cut are compared one by
        # one instead: a prompt the agent wrote has lines that appear in
        # the answer of the agent and nowhere else, and a prompt the
        # student wrote does not.
        drafted = was_drafted(content, drafts)
        source = prompt.get("source")
        if drafted and source != "agent_draft":
            fail(problems, "%s is marked as %r, but the record shows that "
                           "the agent wrote it" % (where, source))
        if not drafted and source == "agent_draft":
            fail(problems, "%s is marked as an agent draft, but the record "
                           "does not show that the agent wrote it" % where)

    for i, experiment in enumerate(doc.get("experiments", [])):
        where = "/experiments/%d" % i
        block = experiment.get("student_evidence")
        if not isinstance(block, dict):
            continue
        pointer = block.get("prompt_pointer")
        if not isinstance(pointer, str):
            continue
        index = int(pointer.rsplit("/", 1)[-1])
        prompt = doc["user_prompts"][index]
        if prompt.get("source") != "student":
            fail(problems, "%s is scored on the prompt %s, which the agent "
                           "drafted" % (where, pointer))

    if problems:
        for p in problems:
            print("FAIL  %s" % p)
        return 1
    print("ok    every prompt is attributed as the record shows it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
