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
    """The texts the agent wrote in answer to a request for a prompt.

    The answers are returned as they are, and the turn of the student
    that carries the text is returned with them: the material keeps a
    cut of the prompt, so the cut cannot be looked for in the answer of
    the agent directly. What is looked for is the turn of the student
    whose text the agent wrote.
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
                found.append((norm(turn["user"]), norm(answer)))
    return found


def was_drafted(content, drafts):
    """Whether the text of a prompt was written by the agent.

    The material keeps a cut of the prompt, so the cut of the material
    is not what is compared. The comparison is made the other way round:
    the text of the turn of the student is looked for in the answer of
    the agent, because that is the text that was written first.
    """
    for question, answer in drafts:
        if question and question in answer:
            return True
    return False


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
        # The cut of the material is compared with the cut of the record:
        # the first line of the prompt is enough to tell whose it is.
        head = content[:HEAD]
        drafted = any(head and head in answer for _, answer in drafts)
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
