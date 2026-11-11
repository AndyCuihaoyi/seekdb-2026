#!/usr/bin/env python3
"""Check the Agent prompt material before it is committed.

The rules of the competition ask for evidence that can be located inside the
file: every reference has to be a JSON pointer into a string field, and the
text of that field has to be the evidence.

The checks are:

* the file is valid UTF-8 JSON, an object at the top level;
* no key is repeated, and no value is NaN or Infinity;
* the size is below the limit of the statement;
* every `ref` resolves, and it resolves to a string;
* every excerpt is a verbatim part of the turn it belongs to;
* no excerpt is used twice, so that one piece of evidence can not be
  scored twice;
* the student contribution and the result are not the same fragment;
* no evidence excerpt is shared by two instances, in any dimension,
  so that one piece of evidence can not be scored twice;
* every dimension of the rubric carries at least one instance whose
  hypothesis the student raised, so that no dimension is left without
  evidence.
"""

import json
import os
import sys

# The limit of the statement is 128 KiB.
MAX_BYTES = 128 * 1024

# The limit of a cited excerpt.
MAX_EXCERPT = 240

# The five dimensions of the rubric, and the number of instances that each
# of them has to carry. A dimension without an instance is scored as zero
# by the reviewer, which is different from a dimension whose evidence was
# not found, so a thin dimension is reported here.
DIMENSIONS = (
    "问题理解与自主拆解",
    "技术理解与迁移",
    "独立验证与质疑",
    "实验与反馈迭代",
    "取舍与反思",
)

# The smallest number of instances that a dimension has to carry, so that
# the reviewer has something to score in every dimension.
MIN_INSTANCES_PER_DIMENSION = 2


def fail(problems, message):
    problems.append(message)


def resolve(doc, pointer):
    """Resolve a JSON pointer into the document.

    Returns the value, or None when the pointer does not resolve.
    """
    if not pointer.startswith("/"):
        return None
    node = doc
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def walk_refs(node, path, out):
    """Collect every (pointer, value) pair of the `ref` keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ref":
                out.append((path + "/" + key, value))
            else:
                walk_refs(value, path + "/" + key, out)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk_refs(value, path + "/" + str(i), out)


def reject_constant(name):
    raise ValueError("the value %s is not a standard JSON number" % name)


def check_no_duplicates(text, problems):
    """Report a repeated key, which the reader of the file cannot resolve."""

    def hook(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                fail(problems, "the key %r is repeated" % key)
            seen.add(key)
        return dict(pairs)

    json.loads(text, object_pairs_hook=hook)


def check_refs(doc, problems):
    """Check that every reference resolves, and is used once."""
    refs = []
    walk_refs(doc, "", refs)

    used = {}
    for pointer, target in refs:
        if not isinstance(target, str):
            fail(problems, "the reference %s is not a string" % pointer)
            continue
        value = resolve(doc, target)
        if value is None:
            fail(problems, "the reference %s points at %s, which does not exist"
                 % (pointer, target))
            continue
        if not isinstance(value, str):
            fail(problems, "the reference %s points at %s, which is not a string"
                 % (pointer, target))
            continue
        if len(value) > MAX_EXCERPT:
            fail(problems, "the excerpt at %s is %d characters long, above %d"
                 % (target, len(value), MAX_EXCERPT))
        if not value.strip():
            fail(problems, "the excerpt at %s is empty" % target)
        if target in used:
            fail(problems, "the excerpt %s is used twice" % target)
        used[target] = True

        # The excerpt has to be a verbatim part of the turn it belongs to.
        owner = target.rsplit("/", 1)[0]
        owner_value = resolve(doc, owner)
        if isinstance(owner_value, str) and value not in owner_value:
            fail(problems, "the excerpt at %s is not a verbatim part of %s"
                 % (target, owner))


def check_excerpts(doc, problems):
    """Check that every turn carries an excerpt that belongs to it."""
    for i, conversation in enumerate(doc.get("conversations", [])):
        for j, item in enumerate(conversation.get("turns", [])):
            content = item.get("content")
            excerpt = item.get("excerpt")
            where = "/conversations/%d/turns/%d" % (i, j)
            if not isinstance(content, str) or not content.strip():
                fail(problems, "%s has no content" % where)
                continue
            if not isinstance(excerpt, str) or not excerpt.strip():
                fail(problems, "%s has no excerpt" % where)
                continue
            if excerpt not in content:
                fail(problems, "the excerpt of %s is not a verbatim part of it"
                     % where)
            if len(excerpt) > MAX_EXCERPT:
                fail(problems, "the excerpt of %s is %d characters long, above %d"
                     % (where, len(excerpt), MAX_EXCERPT))


def check_experiments(doc, problems):
    """Check that every experiment names a command and a real result."""
    for i, experiment in enumerate(doc.get("experiments", [])):
        where = "/experiments/%d" % i
        for field in ("id", "name", "hypothesis", "command", "result",
                      "interpretation"):
            value = experiment.get(field)
            if not isinstance(value, str) or not value.strip():
                fail(problems, "%s has no %s" % (where, field))
        excerpt = experiment.get("result_excerpt")
        result = experiment.get("result")
        if isinstance(excerpt, str) and isinstance(result, str):
            if excerpt not in result:
                fail(problems, "the result excerpt of %s is not a verbatim "
                               "part of its result" % where)
            if len(excerpt) > MAX_EXCERPT:
                fail(problems, "the result excerpt of %s is %d characters long, "
                               "above %d" % (where, len(excerpt), MAX_EXCERPT))


def check_prompts(doc, problems):
    """Check that every prompt is marked with its author."""
    for i, prompt in enumerate(doc.get("user_prompts", [])):
        where = "/user_prompts/%d" % i
        if prompt.get("source") != "student":
            fail(problems, "%s is not marked as the work of the student" % where)
        content = prompt.get("content")
        excerpt = prompt.get("excerpt")
        if not isinstance(content, str) or not content.strip():
            fail(problems, "%s has no content" % where)
            continue
        if not isinstance(excerpt, str) or not excerpt.strip():
            fail(problems, "%s has no excerpt" % where)
            continue
        if excerpt not in content:
            fail(problems, "the excerpt of %s is not a verbatim part of it"
                 % where)


def check_evidence_is_not_shared(doc, problems):
    """Check that no excerpt of evidence is used by two instances.

    The rules say that one piece of evidence must not be scored twice,
    neither across two instances nor across two dimensions. A repeated
    excerpt is therefore a defect of the material, not of the reviewer.
    """
    seen = {}
    for i, experiment in enumerate(doc.get("experiments", [])):
        where = "/experiments/%d" % i
        for field in ("student_evidence", "result_evidence"):
            block = experiment.get(field)
            if not isinstance(block, dict):
                continue
            excerpt = block.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip():
                fail(problems, "%s/%s has no excerpt" % (where, field))
                continue
            if excerpt in seen:
                fail(problems,
                     "the excerpt of %s/%s is the same as the one of %s"
                     % (where, field, seen[excerpt]))
                continue
            seen[excerpt] = "%s/%s" % (where, field)

    # The excerpt of a conversation turn is evidence as well, so it is
    # checked against the same pool.
    for i, conversation in enumerate(doc.get("conversations", [])):
        for j, item in enumerate(conversation.get("turns", [])):
            excerpt = item.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip():
                continue
            where = "/conversations/%d/turns/%d" % (i, j)
            if excerpt in seen:
                fail(problems, "the excerpt of %s is the same as the one of %s"
                     % (where, seen[excerpt]))
                continue
            seen[excerpt] = where


def check_instances_are_independent(doc, problems):
    """Check that no two instances are scored on the same prompt.

    The rules say that one piece of evidence must not be scored twice,
    neither across two instances nor across two dimensions. Two instances
    that point at the same prompt are the same piece of evidence, even
    when their excerpts are worded differently, so this is checked on the
    prompt the instance is built on and not only on its excerpt.
    """
    owners = {}
    for i, experiment in enumerate(doc.get("experiments", [])):
        where = "/experiments/%d" % i
        block = experiment.get("student_evidence")
        if not isinstance(block, dict):
            continue
        pointer = block.get("prompt_pointer")
        if not isinstance(pointer, str):
            fail(problems, "%s/student_evidence has no prompt pointer" % where)
            continue
        if pointer in owners:
            fail(problems,
                 "%s is scored on the prompt %s, which %s is scored on as well"
                 % (where, pointer, owners[pointer]))
            continue
        owners[pointer] = where


def check_dimensions(doc, problems):
    """Check that every dimension of the rubric carries enough instances.

    The reviewer scores each dimension on its own and adds the five
    scores up. A dimension with no instance is scored as zero, and one
    with a single thin instance is scored with a wide spread over the
    three reviews. Both make the whole material unreviewable, so they
    are reported here.
    """
    total = {}
    student = {}
    for experiment in doc.get("experiments", []):
        dimension = experiment.get("dimension")
        if dimension not in DIMENSIONS:
            fail(problems, "the dimension %r is not one of the five of the "
                           "rubric" % dimension)
            continue
        total[dimension] = total.get(dimension, 0) + 1
        if experiment.get("hypothesis_author") == "student":
            student[dimension] = student.get(dimension, 0) + 1

    for dimension in DIMENSIONS:
        if total.get(dimension, 0) < MIN_INSTANCES_PER_DIMENSION:
            fail(problems,
                 "the dimension %s has %d instances, fewer than the %d it "
                 "needs" % (dimension, total.get(dimension, 0),
                            MIN_INSTANCES_PER_DIMENSION))
        if not student.get(dimension):
            fail(problems,
                 "the dimension %s has no instance whose hypothesis the "
                 "student raised" % dimension)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    problems = []

    for name in ("conversation.json", "agent/conversation.json"):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            fail(problems, "%s does not exist" % name)
            continue

        raw = open(path, "rb").read()
        if len(raw) > MAX_BYTES:
            fail(problems, "%s is %d bytes, above the limit" % (name, len(raw)))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            fail(problems, "%s is not valid UTF-8: %s" % (name, exc))
            continue
        try:
            doc = json.loads(text, parse_constant=reject_constant)
        except ValueError as exc:
            fail(problems, "%s is not valid JSON: %s" % (name, exc))
            continue
        if not isinstance(doc, dict):
            fail(problems, "%s is not a JSON object at the top level" % name)
            continue

        if name == "conversation.json":
            check_no_duplicates(text, problems)
            check_refs(doc, problems)
            check_excerpts(doc, problems)
            check_experiments(doc, problems)
            check_prompts(doc, problems)
            check_evidence_is_not_shared(doc, problems)
            check_instances_are_independent(doc, problems)
            check_dimensions(doc, problems)

    # The two copies have to be the same, so that the reader finds the same
    # material in either place.
    first = os.path.join(root, "conversation.json")
    second = os.path.join(root, "agent", "conversation.json")
    if os.path.exists(first) and os.path.exists(second):
        if open(first, "rb").read() != open(second, "rb").read():
            fail(problems, "the two copies of the material differ")

    if problems:
        for p in problems:
            print("FAIL  %s" % p)
        return 1
    print("ok    the material is valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
