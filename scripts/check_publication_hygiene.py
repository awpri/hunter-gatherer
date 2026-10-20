#!/usr/bin/env python3
"""Bounded release triage; prints categories/counts, never matched values.

Scan an explicit clean export directory or wheel. Nonzero means review required.
This cannot clear privacy, copyright, credentials or Git history for publication.
"""
import argparse
import re
import zipfile
from collections import Counter
from pathlib import Path

PATTERNS = {
    "home_path": rb"/Users/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+",
    "email_shape": rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "provider_key_shape": rb"(?:sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{30,})",
}
PRIVATE = {".git", ".mimosa", ".zcode", ".env", "node_modules", "__pycache__"}
MAX_FILES = 5000
MAX_FILE = 4_000_000
MAX_TOTAL = 100_000_000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    counts = Counter()
    scanned = total = 0
    def inspect(name, data):
        nonlocal scanned, total
        parts = Path(name).parts
        if any(p in PRIVATE for p in parts) or Path(name).suffix in {".db", ".sqlite", ".sqlite3", ".source"}:
            counts["private_or_runtime_artifact"] += 1
        if data is None:
            counts["unreviewed_oversize_or_symlink"] += 1
            return
        scanned += 1
        total += len(data)
        if scanned > MAX_FILES or total > MAX_TOTAL:
            raise ValueError("scan budget exceeded; use a smaller explicit export")
        for category, pattern in PATTERNS.items():
            if re.search(pattern, data):
                counts[category] += 1
    try:
        if args.artifact.suffix == ".whl":
            with zipfile.ZipFile(args.artifact) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_FILES:
                    raise ValueError("entry budget exceeded")
                for entry in entries:
                    if not entry.is_dir():
                        inspect(entry.filename, archive.read(entry) if entry.file_size <= MAX_FILE else None)
        elif args.artifact.is_dir():
            for visited, path in enumerate(args.artifact.rglob("*"), start=1):
                if visited > MAX_FILES:
                    raise ValueError("entry budget exceeded")
                if path.is_symlink():
                    inspect(str(path.relative_to(args.artifact)), None)
                elif path.is_file():
                    inspect(str(path.relative_to(args.artifact)), path.read_bytes() if path.stat().st_size <= MAX_FILE else None)
        else:
            raise ValueError("artifact must be a wheel or explicit clean export directory")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print("Incomplete scan:", type(error).__name__)
        return 2
    print("Files scanned:", scanned)
    print("Review categories:", dict(sorted(counts.items())))
    print("Pattern triage only; human review and history/export review remain required.")
    return 1 if counts else 0


if __name__ == "__main__":
    raise SystemExit(main())
