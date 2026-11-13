#!/usr/bin/env python3
"""Push a branch through the GitHub API when git cannot reach github.com.

**Why this exists.** The machine that this was written on can reach
`api.github.com` but not `github.com`, and `git push` speaks to the
latter. The fact is not a guess: a direct `curl https://github.com/` from
that machine hangs until it times out, and the same `curl` against
`https://api.github.com` answers in under a second. So the port 443 of
`github.com` is closed there and the port 443 of `api.github.com` is
open, and the only way to push from that machine is to speak to the API.

**What it does.** The API can do the same job as `git push`: upload the
blobs, build the trees, create the commits and move the reference of the
branch.

Only the blobs that a commit changed are uploaded - the tree of the
commit is built on top of the tree of its parent, and the API applies
the difference - so a push costs one request per changed file rather
than one per file of the tree.

The objects that are sent are the ones that `git` already has, so the
commits that land on the server are the same commits that are in the
local repository, with the same hashes. The script verifies that at
every step: the hash the API returns is compared with the hash `git`
has, and a mismatch stops the push. The check is not a formality: the
tree of a commit is built from the mode of the file in the tree of that
commit and not from the mode of the diff, because the diff gives the
mode of the *old* side, which is `000000` for a file that is new, and the
API refuses such a mode.

**This is a tool of the environment and not of the database.** The
database is built and measured by the other tools of `tools/`; this one
only moves commits around. A reader who clones the repository on a
machine that can reach `github.com` does not need it at all.

    python3 tools/push_over_api.py --repo AndyCuihaoyi/seekdb-2026 \\
        --branch 2026-competition-round2

The token is read from the remote of the repository, unless it is given
in the environment as `MINIVECDB_GITHUB_TOKEN`.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"


def git(*args):
    """Run git and return its output as bytes."""
    out = subprocess.run(
        ["git"] + list(args), check=True, stdout=subprocess.PIPE)
    return out.stdout


def call(path, token, payload=None, method=None):
    """Call the API and return the parsed answer."""
    url = path if path.startswith("http") else API + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(
            "the API answered %d for %s:\n%s" % (error.code, path, detail))
    if not body:
        return None
    return json.loads(body)


def read_token():
    """The token of the push, from the remote or from the environment."""
    if os.environ.get("MINIVECDB_GITHUB_TOKEN"):
        return os.environ["MINIVECDB_GITHUB_TOKEN"]
    url = git("config", "--get", "remote.origin.url").decode("utf-8").strip()
    if "@" not in url:
        raise SystemExit("the remote has no token in it")
    credentials = url.split("://", 1)[1].split("@", 1)[0]
    if ":" not in credentials:
        raise SystemExit("the remote has no password in it")
    return credentials.split(":", 1)[1]


# ---------------------------------------------------------------------------
# The objects.
# ---------------------------------------------------------------------------

def push_blob(repo, token, sha):
    """Upload one blob and return its hash."""
    content = git("cat-file", "blob", sha)
    answer = call("/repos/%s/git/blobs" % repo, token, {
        "content": content.decode("utf-8", "surrogateescape"),
        "encoding": "utf-8",
    })
    if answer["sha"] != sha:
        raise SystemExit(
            "the API made the blob %s out of %s" % (answer["sha"], sha))
    return sha


def changed_blobs(commit, parent):
    """The blobs that this commit changed, with the path of each.

    A file that was deleted is reported with a null hash, and a file that
    was renamed is reported as a deletion and an addition.

    The mode is the one of the file in the tree of the commit, not the one
    of the diff: the diff gives the mode of the old side, which is
    `000000` for a file that is new.
    """
    modes = {}
    for line in git("ls-tree", "-r", commit).decode("utf-8").splitlines():
        head, _, name = line.partition("\t")
        mode, _, sha = head.split()
        modes[name] = (mode, sha)

    out = []
    text = git(
        "diff-tree", "-r", "--no-commit-id", "--raw", "-z",
        "--no-renames", parent, commit).decode("utf-8")
    fields = text.split("\0")
    index = 0
    while index < len(fields):
        head = fields[index]
        if not head.startswith(":"):
            index += 1
            continue
        parts = head[1:].split()
        new_sha = parts[3]
        path = fields[index + 1]
        index += 2
        if new_sha == "0" * 40:
            # The file was deleted. The tree is built on the tree of the
            # parent, so the entry has to be removed from it.
            out.append({"path": path, "mode": None, "type": None,
                        "sha": None})
            continue
        mode = modes.get(path, (parts[0], None))[0]
        out.append({"path": path, "mode": mode, "type": "blob",
                    "sha": new_sha})
    return out


def push_tree(repo, token, tree_sha, base, entries):
    """Build one tree on the server and return its hash.

    The tree of the parent is the base, and only the entries that this
    commit changed are sent, so the API applies the difference and the
    tree that comes out is the tree of the commit.
    """
    payload = {"base_tree": base}
    for entry in entries:
        if entry["sha"] is None:
            payload.setdefault("tree", []).append({
                "path": entry["path"],
                "mode": "040000",
                "type": "tree",
                "sha": "0" * 40,
            })
            continue
        push_blob(repo, token, entry["sha"])
        payload.setdefault("tree", []).append({
            "path": entry["path"],
            "mode": entry["mode"],
            "type": entry["type"],
            "sha": entry["sha"],
        })
    answer = call("/repos/%s/git/trees" % repo, token, payload)
    if answer["sha"] != tree_sha:
        raise SystemExit(
            "the API made the tree %s out of %s" % (answer["sha"], tree_sha))
    return tree_sha


def header_line(header, name):
    """One header of a commit, in the shape the API wants it.

    The header is `name <email> 1794476628 +0800`, and the API wants the
    name, the address and an ISO 8601 instant, so the timestamp is turned
    into one with the same offset.
    """
    for line in header.split("\n"):
        if line.startswith(name + " "):
            raw = line[len(name) + 1:]
            before_offset, _, offset = raw.rpartition(" ")
            who, _, stamp = before_offset.rpartition(" ")
            address, _, full = who.partition("<")
            return {
                "name": address.strip(),
                "email": full.rstrip(">").strip(),
                "date": iso_date(stamp + " " + offset),
            }
    raise SystemExit("the commit has no %s header" % name)


def iso_date(when):
    """`1794476628 +0800` as `2026-11-12T17:43:48+08:00`.

    The offset is kept as it is, so the instant is the same one that the
    commit names, and the commit that the API writes is the commit that
    `git` has.
    """
    stamp, _, offset = when.strip().rpartition(" ")
    if len(offset) != 5 or offset[0] not in "+-":
        raise SystemExit("the date %r is not understood" % when)
    minutes = int(offset[1:3]) * 60 + int(offset[3:5])
    if offset[0] == "-":
        minutes = -minutes
    zone = timezone(timedelta(minutes=minutes))
    instant = datetime.fromtimestamp(int(stamp), zone)
    return instant.isoformat()


def push_commit(repo, token, commit, parent, base_tree):
    """Send one commit, with its tree, and return its hash and its tree."""
    text = git("cat-file", "commit", commit).decode("utf-8")
    header, _, message = text.partition("\n\n")

    tree_sha = git("rev-parse", commit + "^{tree}").decode("utf-8").strip()
    entries = changed_blobs(commit, parent)
    push_tree(repo, token, tree_sha, base_tree, entries)

    body = {
        "message": message,
        "tree": tree_sha,
        "parents": [parent],
        "author": header_line(header, "author"),
        "committer": header_line(header, "committer"),
    }
    answer = call("/repos/%s/git/commits" % repo, token, body)
    if answer["sha"] != commit:
        raise SystemExit(
            "the API made the commit %s out of %s" % (answer["sha"], commit))
    return answer["sha"], tree_sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--token")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = args.token or read_token()
    head = git("rev-parse", "HEAD").decode("utf-8").strip()

    # The commit of the branch on the server, if there is one.
    reference = call(
        "/repos/%s/git/ref/heads/%s" % (args.repo, args.branch), token)
    parent = reference["object"]["sha"]
    base_tree = git("rev-parse", parent + "^{tree}").decode("utf-8").strip()
    print("the branch is on the server at %s" % parent[:10])

    # The commits that are not on the server yet, oldest first.
    commits = git(
        "rev-list", "--reverse", "%s..HEAD" % parent).decode("utf-8").split()
    print("%d commits to send" % len(commits))
    if args.dry_run:
        for commit in commits:
            files = len(changed_blobs(commit, commit + "^"))
            print("  %s %2d files  %s" % (
                commit[:10], files,
                git("log", "-1", "--format=%s", commit)
                .decode("utf-8").strip()))
        return

    for commit in commits:
        parent, base_tree = push_commit(
            args.repo, token, commit, parent, base_tree)
        print("pushed %s" % commit[:10], flush=True)

    call(
        "/repos/%s/git/refs/heads/%s" % (args.repo, args.branch), token,
        {"sha": parent, "force": False}, method="PATCH")
    print("the branch is now at %s" % parent)
    if parent != head:
        raise SystemExit(
            "the server has %s but the local head is %s" % (parent, head))


if __name__ == "__main__":
    main()
