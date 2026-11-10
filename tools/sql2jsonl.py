#!/usr/bin/env python3
"""Turn a file of SQL statements into the JSON Lines requests of the protocol.

The crash test feeds the database with a file of statements and kills it
while it works. The protocol expects JSON objects, so the file is converted
first.

Usage:
    tools/sql2jsonl.py <input.sql> <output.jsonl>
"""

import json
import sys


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = argv[1], argv[2]
    with open(source, "r", encoding="utf-8") as reader:
        statements = [
            line.strip() for line in reader if line.strip()
        ]
    with open(target, "w", encoding="utf-8") as writer:
        for index, sql in enumerate(statements, 1):
            request = {
                "id": index,
                "op": "execute",
                "sql": sql.rstrip(";"),
                "search_mode": "exact",
            }
            writer.write(json.dumps(request, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
