#!/usr/bin/env python3
"""Check the material against the real development record.

The material quotes the development of the project. Every excerpt has to
come from the record itself, otherwise the material would claim evidence
that does not exist.

The record is made of two parts:

    * the transcript of the sessions that were held on this project, and
    * the development log of the topic (`topics/<topic>/03-impl-log.md` and
      `04-test-report.md`), which is written down while the work is done.

Both parts are contemporaneous: neither is written from the material, so
an excerpt that is found in them was really said or really happened.
The check walks every excerpt of the material and looks for it in
the text of the record. An excerpt that is not in the record is reported.
"""

import glob
import json
import os
import sys

# The folder that holds the transcripts of the sessions.
RECORD = os.path.expanduser(
    "~/.cursor/projects/home-cuihaoyi-OceanBase/agent-transcripts"
)

# The development log of the topic, next to the material.
LOG = os.path.expanduser(
    "~/OceanBase/topics/mvdb-01-minivectordb"
)

# The files of the log that are read as part of the record.
LOG_FILES = ("03-impl-log.md", "04-test-report.md")


def load_record():
    """Read every transcript into one text."""
    parts = []
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
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            text = item.get("text")
                            if isinstance(text, str):
                                parts.append(text)
    return "\n".join(parts)


def load_log():
    """Read the development log of the topic into one text."""
    parts = []
    for name in LOG_FILES:
        path = os.path.join(LOG, name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            parts.append(fh.read())
    return "\n".join(parts)


def collect_excerpts(node, path, out):
    """Collect every (pointer, excerpt, owner content) triple."""
    if isinstance(node, dict):
        content = node.get("content")
        excerpt = node.get("excerpt")
        if isinstance(excerpt, str) and isinstance(content, str):
            out.append((path, excerpt, content))
        for key, value in node.items():
            collect_excerpts(value, path + "/" + key, out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            collect_excerpts(value, path + "/" + str(i), out)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "conversation.json")
    if not os.path.exists(path):
        print("FAIL  conversation.json does not exist")
        return 1

    doc = json.load(open(path, encoding="utf-8"))
    record = load_record()
    log = load_log()
    if not record and not log:
        print("FAIL  the development record is empty")
        return 1

    excerpts = []
    collect_excerpts(doc, "", excerpts)

    missing = []
    for pointer, excerpt, content in excerpts:
        if excerpt not in record and excerpt not in log:
            missing.append((pointer, excerpt))

    total = len(excerpts)
    if missing:
        for pointer, excerpt in missing:
            print("FAIL  %s is not in the record: %r" % (pointer, excerpt[:80]))
        print("     %d of %d excerpts are not in the record" % (len(missing), total))
        return 1

    print("ok    all %d excerpts are in the development record" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
