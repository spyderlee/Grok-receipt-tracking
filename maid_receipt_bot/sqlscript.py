"""Split a .sql file into statements, honouring the mysql client's DELIMITER directive."""
from __future__ import annotations

from typing import List


def split_sql(text: str) -> List[str]:
    stmts: List[str] = []
    delim = ";"
    buf: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            delim = stripped.split(None, 1)[1]
            continue
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(delim):
            stmt = "\n".join(buf).rstrip()
            stmt = stmt[: len(stmt) - len(delim)].strip()
            if stmt:
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts
