#!/usr/bin/env python3
"""Reorder rows of TN/TQ/TWL TSV files into canonical Reference order.

The Reference (first column) ordering rules, matching the repo validators:

  * ``front:intro`` comes first of all.
  * Within a chapter: ``<chapter>:intro`` first, then ``<chapter>:front``,
    then verse rows.
  * Verse rows are ordered by their first verse ascending. Among rows that
    share the same first verse, the largest range comes first (largest last
    verse, where the last verse is taken from the first ``a-b`` segment only;
    verses after a comma are ignored when computing the range). After that,
    rows that carry extra comma-listed verses come before rows that do not.

The sort is STABLE: rows whose Reference values sort equally — in particular
rows with the exact same Reference — keep their original relative order. The
header row (the first line) is always kept in place. Only whole lines are
moved; field content is never rewritten, so quoting and escaping are preserved
byte-for-byte.

Usage:
    # Reorder every tn_*/tq_*/twl_*.tsv under the en_tn, en_tq, en_twl repos
    # that sit next to this script:
    python reorder_tsv_references.py

    # Preview without writing:
    python reorder_tsv_references.py --dry-run

    # Reorder specific files or directories:
    python reorder_tsv_references.py en_tn/tn_GEN.tsv en_tq
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DEFAULT_REPOS = ("en_tn", "en_tq", "en_twl")
TSV_GLOBS = ("tn_*.tsv", "tq_*.tsv", "twl_*.tsv")

# Sorts after every valid reference, so unparseable references (a data error
# the validators flag separately) sink to the end rather than landing somewhere
# arbitrary in the middle.
UNPARSEABLE_SORT_KEY: tuple[float, ...] = (float("inf"),)


def reference_sort_key(reference: str) -> tuple[int, ...] | tuple[float, ...] | None:
    """Return the canonical sort key for a Reference value, or None if blank."""
    if ":" not in reference:
        return None

    chapter_text, verse_raw = reference.split(":", 1)
    if chapter_text == "front":
        chapter_value = -1
    elif chapter_text.isdigit():
        chapter_value = int(chapter_text)
    else:
        return UNPARSEABLE_SORT_KEY

    # Order within a chapter: intro (rank 0), then front (rank 1), then verses (rank 2).
    if verse_raw == "intro":
        return (chapter_value, 0, 0, 0, 0)
    if verse_raw == "front":
        return (chapter_value, 1, 0, 0, 0)

    first_segment = verse_raw.split(",", 1)[0]
    has_extra_verses = "," in verse_raw
    if "-" in first_segment:
        start_text, end_text = first_segment.split("-", 1)
        start_match = re.match(r"\d+", start_text)
        end_match = re.match(r"\d+", end_text)
        if not start_match:
            return UNPARSEABLE_SORT_KEY
        first_verse = int(start_match.group(0))
        last_verse = int(end_match.group(0)) if end_match else first_verse
    else:
        start_match = re.match(r"\d+", first_segment)
        if not start_match:
            return UNPARSEABLE_SORT_KEY
        first_verse = int(start_match.group(0))
        last_verse = first_verse

    extra_rank = 0 if has_extra_verses else 1
    # Negate last_verse so a larger range sorts first within the same first verse.
    return (chapter_value, 2, first_verse, -last_verse, extra_rank)


def first_field(line: str) -> str:
    """Return the Reference (first tab-separated field) of a raw TSV line."""
    return line.split("\t", 1)[0]


def reorder_file(path: Path, *, dry_run: bool) -> tuple[bool, str]:
    """Reorder one TSV file. Returns (changed, message)."""
    raw = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    if not lines or lines[0] == "":
        return (False, "skipped: empty file")

    header = lines[0]
    # Keep only non-empty body rows; blank lines (only valid at EOF) are dropped.
    body = [line for line in lines[1:] if line != ""]

    decorated = []
    unparseable = 0
    for index, line in enumerate(body):
        key = reference_sort_key(first_field(line))
        if key is None or key is UNPARSEABLE_SORT_KEY:
            unparseable += 1
            sort_key: tuple[int, ...] | tuple[float, ...] = UNPARSEABLE_SORT_KEY
        else:
            sort_key = key
        decorated.append((sort_key, index, line))

    # (sort_key, original_index) — the index tiebreak makes the sort stable, so
    # rows with the same Reference (or otherwise equal keys) keep their order.
    decorated.sort(key=lambda item: (item[0], item[1]))
    new_body = [line for _key, _index, line in decorated]

    # Safety: the reordered body must contain exactly the same lines.
    if sorted(new_body) != sorted(body):
        return (False, "ERROR: row multiset changed; refusing to write")

    moved = sum(1 for old, new in zip(body, new_body) if old != new)
    if moved == 0:
        suffix = f" ({unparseable} unparseable reference(s))" if unparseable else ""
        return (False, f"already in order{suffix}")

    if not dry_run:
        new_text = newline.join([header, *new_body]) + newline
        path.write_text(new_text, encoding="utf-8")

    verb = "would reorder" if dry_run else "reordered"
    suffix = f", {unparseable} unparseable reference(s) moved to end" if unparseable else ""
    return (True, f"{verb} {moved} row(s){suffix}")


def collect_tsv_files(paths: list[str], script_dir: Path) -> list[Path]:
    targets = [Path(p) for p in paths] if paths else [script_dir / repo for repo in DEFAULT_REPOS]

    files: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        if target.is_dir():
            for glob in TSV_GLOBS:
                for found in sorted(target.glob(glob)):
                    resolved = found.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        files.append(found)
        elif target.is_file():
            resolved = target.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(target)
        else:
            print(f"warning: path not found, skipping: {target}", file=sys.stderr)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Reorder TN/TQ/TWL TSV rows into canonical Reference order.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="TSV files or directories to process. Default: en_tn, en_tq, en_twl next to this script.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing any files.",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    files = collect_tsv_files(args.paths, script_dir)
    if not files:
        print("No TSV files found to process.")
        return 0

    changed_count = 0
    error_count = 0
    for path in files:
        try:
            changed, message = reorder_file(path, dry_run=args.dry_run)
        except Exception as exc:  # Surface and continue; one bad file shouldn't stop the run.
            print(f"{path}: ERROR: {exc}")
            error_count += 1
            continue
        if message.startswith("ERROR"):
            error_count += 1
        if changed:
            changed_count += 1
        if changed or message.startswith("ERROR") or "unparseable" in message:
            print(f"{path}: {message}")

    action = "would change" if args.dry_run else "changed"
    print(f"\nDone. {changed_count} file(s) {action}, {error_count} error(s), {len(files)} file(s) scanned.")
    return 1 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
