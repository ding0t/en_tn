#!/usr/bin/env python3
"""Validate TN TSV files and manifest consistency."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


EXPECTED_HEADER = [
    "Reference",
    "ID",
    "Tags",
    "SupportReference",
    "Quote",
    "Occurrence",
    "Note",
]

ID_RE = re.compile(r"^[a-z][a-z0-9]{3}$")
REFERENCE_RE = re.compile(
    r"^(?:front:intro|\d+:intro|\d+:front|\d+:\d+(?:[,-][\d,:-]*\d+)*)$"
)
SUPPORT_REFERENCE_RE = re.compile(r"^rc://[^/]+/[^/]+/[^/]+/[^ \\]+$")
OCCURRENCE_RE = re.compile(r"^(?:-1|[0-9]+)$")
ALT_TRANSLATION_LABEL_RE = re.compile(r"Alternat(e|ive)( *)([Tt])ranslation")
DUPLICATE_ALT_TRANSLATION_LABEL_RE = re.compile(r"Alternate translation.{0,2} [Aa]lternat")

CHECK_VALID_MANIFEST = "1. Valid Manifest File Check"
CHECK_PROJECT_TSV_FILES_EXIST = "2. Project TSV Files Exist Check"
CHECK_NUM_COLUMNS = "3. Number of Columns Check"
CHECK_TSV_HEADER = "4. TSV Header Check"
CHECK_ID = "5. ID Check"
CHECK_REFERENCE = "6. Reference Check"
CHECK_SUPPORT_REFERENCE = "7. SupportReference Check"
CHECK_LITERAL_BACKSLASH_N = r"8. Literal \n Check"
CHECK_OCCURRENCE = "9. Occurrence Check"
CHECK_NOTE_ENDING = "10. Note Ending Check"
CHECK_REFERENCE_ORDER = "11. Reference Order Check"
CHECK_ALT_TRANSLATION_LABEL = "12. Alternate translation Label Check"
CHECK_PAIRED_SQUARE_BRACKET = "13. Paired Square Bracket Check"
CHECK_DUPLICATE_ID = "14. Duplicate ID Check"
CHECK_STRAIGHT_QUOTES = "15. Straight Quotes Check"
CHECK_NOTE = "16. Note Check"
CHECK_NUMBER_TO_NAME = {
    1: CHECK_VALID_MANIFEST,
    2: CHECK_PROJECT_TSV_FILES_EXIST,
    3: CHECK_NUM_COLUMNS,
    4: CHECK_TSV_HEADER,
    5: CHECK_ID,
    6: CHECK_REFERENCE,
    7: CHECK_SUPPORT_REFERENCE,
    8: CHECK_LITERAL_BACKSLASH_N,
    9: CHECK_OCCURRENCE,
    10: CHECK_NOTE_ENDING,
    11: CHECK_REFERENCE_ORDER,
    12: CHECK_ALT_TRANSLATION_LABEL,
    13: CHECK_PAIRED_SQUARE_BRACKET,
    14: CHECK_DUPLICATE_ID,
    15: CHECK_STRAIGHT_QUOTES,
    16: CHECK_NOTE,
}
CHECK_DISPLAY_ORDER = [CHECK_NUMBER_TO_NAME[i] for i in sorted(CHECK_NUMBER_TO_NAME.keys())]


@dataclass
class ValidationError:
    rule: str
    message: str
    file: str | None = None
    line: int | None = None
    row_id: str | None = None
    reference: str | None = None
    # "error" fails the run; "warning" is reported but never blocks a merge.
    severity: str = "error"

    def display(self) -> str:
        parts = [f"[{self.rule}]"]
        if self.file is not None:
            parts.append(f"File: {self.file}")
        if self.line is not None:
            parts.append(f"Line: {self.line}")
        if self.row_id is not None:
            parts.append(f"ID: {self.row_id if self.row_id != '' else '(blank)'}")
        if self.reference is not None:
            parts.append(f"Ref: {self.reference if self.reference != '' else '(blank)'}")
        parts.append(self.message)
        return " | ".join(parts)


class ErrorCollector:
    def __init__(self, max_errors: int, enabled_checks: set[str]) -> None:
        self.max_errors = max_errors
        self.enabled_checks = enabled_checks
        self.errors: list[ValidationError] = []
        self.truncated = False

    def add(
        self,
        *,
        rule: str,
        message: str,
        file: str | None = None,
        line: int | None = None,
        row_id: str | None = None,
        reference: str | None = None,
        severity: str = "error",
    ) -> None:
        if not self.is_enabled(rule):
            return
        if len(self.errors) >= self.max_errors:
            self.truncated = True
            return
        self.errors.append(
            ValidationError(
                rule=rule,
                message=message,
                file=file,
                line=line,
                row_id=row_id,
                reference=reference,
                severity=severity,
            )
        )

    def is_enabled(self, check_name: str) -> bool:
        return check_name in self.enabled_checks

    @property
    def failures(self) -> list[ValidationError]:
        return [err for err in self.errors if err.severity != "warning"]

    @property
    def warnings(self) -> list[ValidationError]:
        return [err for err in self.errors if err.severity == "warning"]

    def has_failures(self) -> bool:
        # Only hard errors decide the exit code. Warnings are advisory: they are
        # printed and annotated, but must not stop a book from merging.
        return bool(self.failures)

    def has_errors(self) -> bool:
        return bool(self.errors) or self.truncated


@dataclass
class ReferenceOrderKey:
    sort_key: tuple[int, ...]
    original: str
    line_number: int
    chapter: int = 0
    rank: int = 2
    first_verse: int = 0
    last_verse: int = 0


def normalize_repo_path(path_value: str) -> str:
    normalized = path_value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.startswith("/"):
        normalized = normalized[1:]
    return normalized


def load_manifest(manifest_path: Path, errors: ErrorCollector) -> dict[str, Any] | None:
    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # YAML parser errors are explicit and human-readable.
        errors.add(rule=CHECK_VALID_MANIFEST, message=f"{manifest_path} is not valid YAML: {exc}")
        return None

    if not isinstance(data, dict):
        errors.add(
            rule=CHECK_VALID_MANIFEST,
            message=f"{manifest_path} must parse to a YAML mapping object.",
        )
        return None
    return data


def parse_manifest_project_paths(
    manifest_data: dict[str, Any], errors: ErrorCollector
) -> tuple[set[str], list[str]]:
    projects = manifest_data.get("projects")
    manifest_paths: list[str] = []
    if not isinstance(projects, list):
        errors.add(rule=CHECK_PROJECT_TSV_FILES_EXIST, message="manifest.yaml key 'projects' must be a list.")
        return set(), manifest_paths

    for idx, project in enumerate(projects, start=1):
        if not isinstance(project, dict):
            errors.add(rule=CHECK_PROJECT_TSV_FILES_EXIST, message=f"manifest project #{idx} is not an object.")
            continue
        raw_path = project.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.add(rule=CHECK_PROJECT_TSV_FILES_EXIST, message=f"manifest project #{idx} has no valid 'path'.")
            continue
        path = normalize_repo_path(raw_path)
        if not path.lower().endswith(".tsv"):
            errors.add(
                rule=CHECK_PROJECT_TSV_FILES_EXIST,
                message=f"manifest project path '{raw_path}' is not a .tsv file path.",
            )
            continue
        manifest_paths.append(path)

    deduped = set(manifest_paths)
    if len(deduped) != len(manifest_paths):
        seen: set[str] = set()
        duplicates = []
        for path in manifest_paths:
            if path in seen and path not in duplicates:
                duplicates.append(path)
            seen.add(path)
        errors.add(
            rule=CHECK_PROJECT_TSV_FILES_EXIST,
            message=f"manifest has duplicate project path(s): {', '.join(sorted(duplicates))}",
        )
    return deduped, manifest_paths


def validate_manifest_vs_repo(
    manifest_tsv_paths: set[str], errors: ErrorCollector
) -> list[Path]:
    tsv_paths = sorted(Path(".").glob("tn_*.tsv"))
    missing_in_repo = sorted(path for path in manifest_tsv_paths if not Path(path).is_file())
    if missing_in_repo:
        errors.add(
            rule=CHECK_PROJECT_TSV_FILES_EXIST,
            message=(
                "Files listed in manifest projects but missing in repo: "
                + ", ".join(missing_in_repo)
            ),
        )
    return tsv_paths


def parse_tsv_row(raw_line: str, file_name: str, line_number: int, errors: ErrorCollector) -> list[str] | None:
    try:
        row = next(csv.reader([raw_line], delimiter="\t"))
    except Exception as exc:
        errors.add(
            rule=CHECK_NUM_COLUMNS,
            file=file_name,
            line=line_number,
            message=f"Line is not parseable as TSV: {exc}",
        )
        return None
    return row


def parse_reference_order_key(reference: str, line_number: int) -> ReferenceOrderKey | None:
    if ":" not in reference:
        return None

    chapter_text, verse_raw = reference.split(":", 1)
    if chapter_text == "front":
        chapter_value = -1
    elif chapter_text.isdigit():
        chapter_value = int(chapter_text)
    else:
        return None

    # Order within a chapter: intro (rank 0), then front (rank 1), then verses (rank 2).
    if verse_raw == "intro":
        rank, first_verse, last_verse = 0, 0, 0
    elif verse_raw == "front":
        rank, first_verse, last_verse = 1, 0, 0
    else:
        # Only the leading "a-b" segment defines the span; verses listed after a
        # comma do not affect ordering.
        first_segment = verse_raw.split(",", 1)[0]
        if "-" in first_segment:
            start_text, end_text = first_segment.split("-", 1)
            start_match = re.match(r"\d+", start_text)
            end_match = re.match(r"\d+", end_text)
            if not start_match:
                return None
            first_verse = int(start_match.group(0))
            last_verse = int(end_match.group(0)) if end_match else first_verse
        else:
            start_match = re.match(r"\d+", first_segment)
            if not start_match:
                return None
            first_verse = int(start_match.group(0))
            last_verse = first_verse
        rank = 2
        if last_verse < first_verse:
            last_verse = first_verse

    return ReferenceOrderKey(
        sort_key=(chapter_value, rank, first_verse, last_verse),
        original=reference,
        line_number=line_number,
        chapter=chapter_value,
        rank=rank,
        first_verse=first_verse,
        last_verse=last_verse,
    )


def is_reference_in_order(prev_key: ReferenceOrderKey, curr_key: ReferenceOrderKey) -> bool:
    # Retained for callers that want the simple monotonic test. The real
    # ordering policy lives in evaluate_reference_order.
    return curr_key.sort_key >= prev_key.sort_key


def _is_bridge(key: ReferenceOrderKey) -> bool:
    return key.rank == 2 and key.last_verse > key.first_verse


def evaluate_reference_order(
    rows: list[tuple[ReferenceOrderKey, dict[str, Any]]], errors: ErrorCollector
) -> None:
    """Report reference-ordering problems, separating hard errors from warnings.

    Single verses must never move backward: 10 before 9 is an error. A verse
    bridge (say 9-10) is different -- it describes a transition, so it may sit
    anywhere inside its own span: before the 9s, or between the 9s and the 10s.
    It is only an error when it escapes that span, i.e. it lands before verse-8
    material or after verse-11 material. Sitting after every one of its end
    verses, immediately before the next verse, is legal but usually not what was
    intended, so that case warns rather than fails.
    """
    for index, (key, context) in enumerate(rows):
        if index == 0:
            continue
        prev_key, _ = rows[index - 1]

        # Chapter order and intro/front placement stay strict.
        if key.chapter != prev_key.chapter:
            if key.chapter < prev_key.chapter:
                errors.add(
                    rule=CHECK_REFERENCE_ORDER,
                    **context,
                    message=(
                        f"Reference '{key.original}' is out of order. It appears after "
                        f"'{prev_key.original}' on line {prev_key.line_number}, but the chapter "
                        "goes backward."
                    ),
                )
            continue
        if key.rank < prev_key.rank:
            errors.add(
                rule=CHECK_REFERENCE_ORDER,
                **context,
                message=(
                    f"Reference '{key.original}' is out of order. It appears after "
                    f"'{prev_key.original}' on line {prev_key.line_number}; intro and front rows "
                    "must come before verse rows."
                ),
            )
            continue
        if key.rank != 2 or prev_key.rank != 2:
            continue

        next_key = rows[index + 1][0] if index + 1 < len(rows) else None
        if next_key is not None and (next_key.chapter != key.chapter or next_key.rank != 2):
            next_key = None

        if _is_bridge(key):
            # Escaped its span on the left: something before its start follows it.
            if next_key is not None and next_key.first_verse < key.first_verse:
                errors.add(
                    rule=CHECK_REFERENCE_ORDER,
                    **context,
                    message=(
                        f"Reference '{key.original}' is out of order. It appears before "
                        f"'{next_key.original}' on line {next_key.line_number}, which is earlier "
                        f"than the start of its own range (verse {key.first_verse})."
                    ),
                )
                continue
            # Escaped its span on the right: it follows something past its end.
            if prev_key.first_verse > key.last_verse:
                errors.add(
                    rule=CHECK_REFERENCE_ORDER,
                    **context,
                    message=(
                        f"Reference '{key.original}' is out of order. It appears after "
                        f"'{prev_key.original}' on line {prev_key.line_number}, which is past the "
                        f"end of its own range (verse {key.last_verse})."
                    ),
                )
                continue
            # Legal but suspect: parked after everything in its own range.
            if prev_key.first_verse == key.last_verse and (
                next_key is None or next_key.first_verse > key.last_verse
            ):
                trailing = (
                    f" and before '{next_key.original}'" if next_key is not None else ""
                )
                errors.add(
                    rule=CHECK_REFERENCE_ORDER,
                    severity="warning",
                    **context,
                    message=(
                        f"Reference '{key.original}' sits after all verse {key.last_verse} "
                        f"rows{trailing}. A bridged row usually belongs at the transition it "
                        "describes; check whether it should come earlier in its range."
                    ),
                )
            continue

        # Single verse: never allowed to move back past the previous row's start.
        if key.first_verse < prev_key.first_verse:
            errors.add(
                rule=CHECK_REFERENCE_ORDER,
                **context,
                message=(
                    f"Reference '{key.original}' is out of order. It appears after "
                    f"'{prev_key.original}' on line {prev_key.line_number}, but should not go "
                    "backward in verse order."
                ),
            )


def validate_alternate_translation_label(
    note: str,
    errors: ErrorCollector,
    row_context: dict[str, Any],
) -> None:
    for match in ALT_TRANSLATION_LABEL_RE.finditer(note):
        label_start = match.start()
        label_end = match.end()
        raw_match = match.group(0)

        # 1) Preceding context must be start-of-string or punctuation + space.
        if label_start != 0:
            preceding_two = note[max(0, label_start - 2):label_start]
            if preceding_two == "  ":
                errors.add(
                    rule=CHECK_ALT_TRANSLATION_LABEL,
                    severity="warning",
                    **row_context,
                    message=(
                        f"Matched label '{raw_match}': too many spaces before the "
                        "Alternate translation label."
                    ),
                )
            elif not re.fullmatch(r"[^a-z] ", preceding_two or ""):
                errors.add(
                    rule=CHECK_ALT_TRANSLATION_LABEL,
                    severity="warning",
                    **row_context,
                    message=(
                        f"Matched label '{raw_match}': the previous sentence does not "
                        "have punctuation at the end."
                    ),
                )

        # 3) Must be 'Alternate', not 'Alternative'
        if match.group(1) != "e":
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
                severity="warning",
                **row_context,
                message=f"Matched label '{raw_match}': use 'Alternate', not 'Alternative'.",
            )

        # 4) Must have exactly one space between words
        if match.group(2) != " ":
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
                severity="warning",
                **row_context,
                message=(
                    f"Matched label '{raw_match}': use exactly one space between "
                    "'Alternate' and 'translation'."
                ),
            )

        # 5) Must use lowercase 't'
        if match.group(3) != "t":
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
                severity="warning",
                **row_context,
                message=(
                    f"Matched label '{raw_match}': use lowercase 't' in "
                    "'Alternate translation'."
                ),
            )

        # 7) Must be followed by ':', ',' or space.
        if label_end >= len(note):
            next_char = ""
        else:
            next_char = note[label_end]
        if next_char not in {":", ",", " ", "s"}:
            pretty_next = next_char if next_char else "(end of note)"
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
                severity="warning",
                **row_context,
                message=(
                    f"Matched label '{raw_match}': Alternate translation label must be "
                    "followed by ':', ',' or a space "
                    f"(found '{pretty_next}')."
                ),
            )

    # 8) Duplicate label check.
    duplicate_match = DUPLICATE_ALT_TRANSLATION_LABEL_RE.search(note)
    if duplicate_match:
        errors.add(
            rule=CHECK_ALT_TRANSLATION_LABEL,
            severity="warning",
            **row_context,
            message=(
                f"Matched text '{duplicate_match.group(0)}': Duplicate Alternate "
                "translation labels."
            ),
        )


def validate_paired_square_brackets(
    note: str,
    errors: ErrorCollector,
    row_context: dict[str, Any],
) -> None:
    stack: list[tuple[int, int]] = []
    i = 0
    while i < len(note):
        char = note[i]
        if char not in "[]":
            i += 1
            continue

        j = i
        while j < len(note) and note[j] == char:
            j += 1
        run_len = j - i
        token = char * run_len

        if char == "[":
            stack.append((run_len, i))
        else:
            if not stack:
                errors.add(
                    rule=CHECK_PAIRED_SQUARE_BRACKET,
                    severity="warning",
                    **row_context,
                    message=(
                        f"Closing bracket '{token}' at character {i + 1} does not have a "
                        "matching opening bracket."
                    ),
                )
            else:
                open_len, open_pos = stack.pop()
                if open_len != run_len:
                    errors.add(
                        rule=CHECK_PAIRED_SQUARE_BRACKET,
                        severity="warning",
                        **row_context,
                        message=(
                            f"Opening bracket '{'[' * open_len}' at character {open_pos + 1} "
                            f"is closed by '{token}' at character {i + 1}. "
                            "Bracket sizes must match (for example [[ ... ]] or [ ... ])."
                        ),
                    )

        i = j

    while stack:
        open_len, open_pos = stack.pop()
        errors.add(
            rule=CHECK_PAIRED_SQUARE_BRACKET,
            severity="warning",
            **row_context,
            message=(
                f"Opening bracket '{'[' * open_len}' at character {open_pos + 1} does not "
                "have a matching closing bracket."
            ),
        )


def validate_tsv_file(tsv_path: Path, errors: ErrorCollector) -> None:
    file_name = tsv_path.name
    try:
        content = tsv_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        errors.add(
            rule=CHECK_NUM_COLUMNS,
            file=file_name,
            message=f"File is not valid UTF-8 text: {exc}",
        )
        return

    # Normalize line endings so row numbering remains stable across platforms.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")

    if len(lines) == 0 or (len(lines) == 1 and lines[0] == ""):
        errors.add(rule=CHECK_NUM_COLUMNS, file=file_name, line=1, message="TSV file is empty.")
        return

    header_line = lines[0]
    header_row = parse_tsv_row(header_line, file_name, 1, errors)
    if header_row is None:
        return
    if header_row != EXPECTED_HEADER:
        errors.add(
            rule=CHECK_TSV_HEADER,
            file=file_name,
            line=1,
            message=(
                "Header must be exactly: "
                + "\\t".join(EXPECTED_HEADER)
                + f" (found: {' | '.join(header_row)})"
            ),
        )

    seen_blank_line = False
    seen_ids: set[str] = set()
    reference_order_rows: list[tuple[ReferenceOrderKey, dict[str, Any]]] = []

    for zero_based_index, raw_line in enumerate(lines[1:], start=1):
        line_number = zero_based_index + 1

        if raw_line == "":
            seen_blank_line = True
            continue

        if seen_blank_line:
            errors.add(
                rule=CHECK_NUM_COLUMNS,
                file=file_name,
                line=line_number,
                message="Blank lines are only allowed at the very end of the file.",
            )
            seen_blank_line = False

        row = parse_tsv_row(raw_line, file_name, line_number, errors)
        if row is None:
            if "'" in raw_line or '"' in raw_line:
                found = []
                if "'" in raw_line:
                    found.append("single quote (')")
                if '"' in raw_line:
                    found.append('double quote (")')
                errors.add(
                    rule=CHECK_STRAIGHT_QUOTES,
                    file=file_name,
                    line=line_number,
                    message=(
                        "This row contains straight "
                        + " and ".join(found)
                        + ". Use only curly quotes in TSV content."
                    ),
                )
            continue

        if len(row) != 7:
            row_reference = row[0] if len(row) > 0 else None
            row_id = row[1] if len(row) > 1 else None
            errors.add(
                rule=CHECK_NUM_COLUMNS,
                file=file_name,
                line=line_number,
                row_id=row_id,
                reference=row_reference,
                message=f"Expected 7 columns but found {len(row)}.",
            )
            if "'" in raw_line or '"' in raw_line:
                found = []
                if "'" in raw_line:
                    found.append("single quote (')")
                if '"' in raw_line:
                    found.append('double quote (")')
                errors.add(
                    rule=CHECK_STRAIGHT_QUOTES,
                    file=file_name,
                    line=line_number,
                    row_id=row_id,
                    reference=row_reference,
                    message=(
                        "This row contains straight "
                        + " and ".join(found)
                        + ". Use only curly quotes in TSV content."
                    ),
                )
            continue

        reference, row_id, _tags, support_reference, _quote, occurrence, note = row
        row_context = {
            "file": file_name,
            "line": line_number,
            "row_id": row_id,
            "reference": reference,
        }
        if "'" in raw_line or '"' in raw_line:
            found = []
            if "'" in raw_line:
                found.append("single quote (')")
            if '"' in raw_line:
                found.append('double quote (")')
            errors.add(
                rule=CHECK_STRAIGHT_QUOTES,
                **row_context,
                message=(
                    "This row contains straight "
                    + " and ".join(found)
                    + ". Use only curly quotes in TSV content."
                ),
            )

        if "\\n" in reference:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="Reference column contains literal \\n, which is not allowed.",
            )
        if "\\n" in row_id:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="ID column contains literal \\n, which is not allowed.",
            )
        if "\\n" in _tags:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="Tags column contains literal \\n, which is not allowed.",
            )
        if "\\n" in support_reference:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="SupportReference column contains literal \\n, which is not allowed.",
            )
        if "\\n" in _quote:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="Quote column contains literal \\n, which is not allowed.",
            )
        if "\\n" in occurrence:
            errors.add(
                rule=CHECK_LITERAL_BACKSLASH_N,
                **row_context,
                message="Occurrence column contains literal \\n, which is not allowed.",
            )

        if not row_id:
            errors.add(rule=CHECK_ID, **row_context, message="ID cannot be blank.")
        elif not ID_RE.fullmatch(row_id):
            errors.add(
                rule=CHECK_ID,
                **row_context,
                message=f"ID '{row_id}' must match: first char [a-z], then 3 chars [a-z0-9].",
            )
        elif row_id in seen_ids:
            errors.add(
                rule=CHECK_DUPLICATE_ID,
                **row_context,
                message=f"ID '{row_id}' is duplicated in this file.",
            )
        else:
            seen_ids.add(row_id)

        if not reference:
            errors.add(
                rule=CHECK_REFERENCE,
                **row_context,
                message="Reference column cannot be blank.",
            )
        elif not REFERENCE_RE.fullmatch(reference):
            errors.add(
                rule=CHECK_REFERENCE,
                **row_context,
                message=(
                    f"Reference '{reference}' is invalid. "
                    "Valid formats are: 'front:intro', '<chapter>:intro', "
                    "'<chapter>:front', or '<chapter>:<verse(s)>' (for example 3:16, 3:16-18, 3:16,18)."
                ),
            )
        else:
            current_reference_key = parse_reference_order_key(reference, line_number)
            if current_reference_key is not None:
                # Ordering needs a lookahead, so collect now and judge once the
                # whole file has been read (see evaluate_reference_order).
                reference_order_rows.append((current_reference_key, dict(row_context)))

        if support_reference and not SUPPORT_REFERENCE_RE.fullmatch(support_reference):
            errors.add(
                rule=CHECK_SUPPORT_REFERENCE,
                **row_context,
                message=(
                    f"SupportReference '{support_reference}' is not a valid RC link. "
                    "If present, it must start with 'rc://' and include a complete path "
                    "(for example: rc://*/ta/man/translate/figs-metaphor)."
                ),
            )

        occurrence_is_allowed_blank = (occurrence == "" and _quote == "")
        if not occurrence_is_allowed_blank and not OCCURRENCE_RE.fullmatch(occurrence):
            errors.add(
                rule=CHECK_OCCURRENCE,
                **row_context,
                message=(
                    f"Occurrence '{occurrence}' must be a non-negative integer or -1. "
                    "It may be blank only when Quote is also blank."
                ),
            )

        if not note.strip():
            errors.add(
                rule=CHECK_NOTE,
                severity="warning",
                **row_context,
                message="Note column cannot be blank.",
            )

        if note.endswith("\\n"):
            errors.add(
                rule=CHECK_NOTE_ENDING,
                severity="warning",
                **row_context,
                message="Note must not end with literal \\n.",
            )

        if note:
            validate_alternate_translation_label(note=note, errors=errors, row_context=row_context)
            validate_paired_square_brackets(note=note, errors=errors, row_context=row_context)

    evaluate_reference_order(reference_order_rows, errors)


def _escape_annotation_value(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _should_emit_annotations() -> bool:
    value = os.environ.get("TN_EMIT_ANNOTATIONS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _check_number(check_label: str) -> int:
    prefix = check_label.split(".", 1)[0].strip()
    try:
        return int(prefix)
    except ValueError:
        return 9999


def resolve_active_checks(check_numbers: list[int] | None) -> list[str]:
    if not check_numbers:
        return CHECK_DISPLAY_ORDER.copy()

    unique_numbers = sorted(set(check_numbers))
    invalid = [num for num in unique_numbers if num not in CHECK_NUMBER_TO_NAME]
    if invalid:
        valid = ", ".join(str(n) for n in sorted(CHECK_NUMBER_TO_NAME.keys()))
        invalid_txt = ", ".join(str(n) for n in invalid)
        raise ValueError(f"Unknown check number(s): {invalid_txt}. Valid check numbers are: {valid}.")

    return [CHECK_NUMBER_TO_NAME[num] for num in unique_numbers]


def _error_sort_key(err: ValidationError) -> tuple[int, str, int, str]:
    file_name = err.file or ""
    line = err.line if err.line is not None else 0
    return (_check_number(err.rule), file_name, line, err.message)


def _group_errors_by_check(
    errors: list[ValidationError], active_checks: list[str]
) -> dict[str, list[ValidationError]]:
    grouped: dict[str, list[ValidationError]] = {check: [] for check in active_checks}
    for err in errors:
        if err.rule not in grouped:
            grouped[err.rule] = []
        grouped[err.rule].append(err)
    return grouped


def _json_output_by_check(
    grouped_errors: dict[str, list[ValidationError]], active_checks: list[str]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for check_name in active_checks:
        check_number = str(_check_number(check_name))
        output[check_number] = {
            "name": check_name,
            "errors": [
                {
                    "file": err.file,
                    "line": err.line,
                    "id": err.row_id,
                    "ref": err.reference,
                    "message": err.message,
                }
                for err in grouped_errors.get(check_name, [])
            ],
        }
    return output


def emit_results(errors: ErrorCollector, active_checks: list[str], output_json: bool = False) -> int:
    ordered_errors = sorted(errors.failures, key=_error_sort_key)
    ordered_warnings = sorted(errors.warnings, key=_error_sort_key)
    grouped_errors = _group_errors_by_check(ordered_errors, active_checks)
    grouped_warnings = _group_errors_by_check(ordered_warnings, active_checks)
    failed = errors.has_failures()

    def _annotate(err: ValidationError, level: str) -> None:
        if not _should_emit_annotations():
            return
        annotation = _escape_annotation_value(err.display())
        if err.file and err.line is not None:
            print(f"::{level} file={err.file},line={err.line}::{annotation}")
        elif err.file:
            print(f"::{level} file={err.file}::{annotation}")
        else:
            print(f"::{level}::{annotation}")

    if output_json:
        payload = {
            "errors": _json_output_by_check(grouped_errors, active_checks),
            "warnings": _json_output_by_check(grouped_warnings, active_checks),
        }
        print(json.dumps(payload, ensure_ascii=False))
        write_step_summary(
            success=not failed,
            errors=ordered_errors,
            truncated=errors.truncated,
            warnings=ordered_warnings,
        )
        return 1 if failed else 0

    if failed:
        print(f"Validation failed with {len(ordered_errors)} error(s).", end="")
    else:
        print("Validation passed: manifest.yaml and TSV checks succeeded.", end="")
    if ordered_warnings:
        print(f" {len(ordered_warnings)} warning(s) reported (not blocking).")
    else:
        print("")

    all_checks = list(active_checks) + [
        name for name in list(grouped_errors) + list(grouped_warnings)
        if name not in active_checks
    ]
    seen: set[str] = set()
    for check_name in all_checks:
        if check_name in seen:
            continue
        seen.add(check_name)
        print(check_name)
        print("-" * len(check_name))
        section_errors = grouped_errors.get(check_name, [])
        section_warnings = grouped_warnings.get(check_name, [])
        if not section_errors and not section_warnings:
            print("No validation errors found.")
            print("")
            continue
        for err in section_errors:
            print(f"- {err.display()}")
            _annotate(err, "error")
        for err in section_warnings:
            print(f"- WARNING: {err.display()}")
            _annotate(err, "warning")
        print("")

    if errors.truncated:
        truncation = (
            f"Error output truncated after {errors.max_errors} entries. "
            "Re-run with --max-errors for a larger cap."
        )
        print(truncation)
        if _should_emit_annotations():
            print(f"::{'error' if failed else 'warning'}::{_escape_annotation_value(truncation)}")

    write_step_summary(
        success=not failed,
        errors=ordered_errors,
        truncated=errors.truncated,
        warnings=ordered_warnings,
    )
    return 1 if failed else 0


def write_step_summary(
    *,
    success: bool,
    errors: list[ValidationError],
    truncated: bool = False,
    warnings: list[ValidationError] | None = None,
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    warning_list = warnings or []
    lines: list[str] = []
    if success:
        lines.append("## TN Validation")
        lines.append("")
        if warning_list:
            lines.append(
                f"All checks passed. **{len(warning_list)}** warning(s) reported "
                "(advisory only -- they do not block merging)."
            )
        else:
            lines.append("All checks passed.")
    else:
        lines.append("## TN Validation")
        lines.append("")
        lines.append(f"Found **{len(errors)}** error(s).")
        lines.append("")
        lines.append("| Check | Location | Message |")
        lines.append("|---|---|---|")
        for err in errors[:200]:
            location = err.file or "(repo)"
            if err.file and err.line is not None:
                location = f"{err.file}:{err.line}"
            message = err.message.replace("|", "\\|")
            lines.append(f"| {err.rule} | {location} | {message} |")
        if len(errors) > 200:
            lines.append("")
            lines.append(f"_Summary limited to first 200 errors (total: {len(errors)})._")
        if truncated:
            lines.append("")
            lines.append("_Validation output was truncated by --max-errors._")

    if warning_list:
        lines.append("")
        lines.append(f"### Warnings ({len(warning_list)}) -- not blocking")
        lines.append("")
        lines.append("| Check | Location | Message |")
        lines.append("|---|---|---|")
        for err in warning_list[:200]:
            location = err.file or "(repo)"
            if err.file and err.line is not None:
                location = f"{err.file}:{err.line}"
            message = err.message.replace("|", "\\|")
            lines.append(f"| {err.rule} | {location} | {message} |")
        if len(warning_list) > 200:
            lines.append("")
            lines.append(f"_Warning list limited to first 200 (total: {len(warning_list)})._")

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate tn_*.tsv files and manifest.yaml.")
    parser.add_argument("--manifest", default="manifest.yaml", help="Path to manifest.yaml")
    parser.add_argument(
        "--base-branch",
        default=os.environ.get("GITHUB_BASE_REF", ""),
        help="PR base branch name (e.g. master).",
    )
    parser.add_argument(
        "--max-errors",
        type=int,
        default=1000,
        help="Maximum number of errors to print before truncating output.",
    )
    parser.add_argument(
        "--check",
        dest="checks",
        action="append",
        type=int,
        help="Check number to run (repeatable). Example: --check 1 --check 4",
    )
    parser.add_argument(
        "--book",
        dest="books",
        action="append",
        help="Book identifier to validate TSV content checks for (repeatable, case-insensitive).",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output validation results as JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    try:
        active_checks = resolve_active_checks(args.checks)
    except ValueError as exc:
        print(f"Argument error: {exc}")
        return 2

    enabled_checks = set(active_checks)
    selected_books = sorted(
        {book.strip().lower() for book in (args.books or []) if isinstance(book, str) and book.strip()}
    )
    errors = ErrorCollector(max_errors=max(1, args.max_errors), enabled_checks=enabled_checks)
    manifest_path = Path(args.manifest)
    manifest_data: dict[str, Any] | None = None

    need_manifest = (
        CHECK_VALID_MANIFEST in enabled_checks
        or CHECK_PROJECT_TSV_FILES_EXIST in enabled_checks
        or bool(selected_books)
    )
    if need_manifest:
        manifest_data = load_manifest(manifest_path, errors)

    tsv_files: list[Path] = sorted(Path(".").glob("tn_*.tsv"))

    if CHECK_PROJECT_TSV_FILES_EXIST in enabled_checks:
        if manifest_data is None:
            errors.add(
                rule=CHECK_PROJECT_TSV_FILES_EXIST,
                message="Cannot run Project TSV Files Exist Check because manifest.yaml could not be parsed.",
            )
        else:
            manifest_tsv_paths, _manifest_paths_ordered = parse_manifest_project_paths(manifest_data, errors)
            tsv_files = validate_manifest_vs_repo(
                manifest_tsv_paths=manifest_tsv_paths,
                errors=errors,
            )

    if selected_books:
        if manifest_data is None:
            errors.add(
                rule=CHECK_PROJECT_TSV_FILES_EXIST,
                message="Cannot resolve --book values because manifest.yaml could not be parsed.",
            )
            tsv_files = []
        else:
            projects = manifest_data.get("projects")
            if not isinstance(projects, list):
                errors.add(
                    rule=CHECK_PROJECT_TSV_FILES_EXIST,
                    message="Cannot resolve --book values because manifest.yaml key 'projects' is not a list.",
                )
                tsv_files = []
            else:
                identifier_to_path: dict[str, str] = {}
                for project in projects:
                    if not isinstance(project, dict):
                        continue
                    identifier = project.get("identifier")
                    raw_path = project.get("path")
                    if isinstance(identifier, str) and isinstance(raw_path, str) and raw_path.strip():
                        identifier_to_path[identifier.lower()] = normalize_repo_path(raw_path)

                unknown_books = sorted(book for book in selected_books if book not in identifier_to_path)
                if unknown_books:
                    errors.add(
                        rule=CHECK_PROJECT_TSV_FILES_EXIST,
                        message=(
                            "--book value(s) not found in manifest project identifiers: "
                            + ", ".join(unknown_books)
                        ),
                    )

                selected_paths = [identifier_to_path[book] for book in selected_books if book in identifier_to_path]
                missing_selected_paths = [path for path in selected_paths if not Path(path).is_file()]
                if missing_selected_paths:
                    errors.add(
                        rule=CHECK_PROJECT_TSV_FILES_EXIST,
                        message=(
                            "Selected --book project path(s) are missing in repo: "
                            + ", ".join(sorted(missing_selected_paths))
                        ),
                    )

                tsv_files = [Path(path) for path in selected_paths if Path(path).is_file()]

    need_tsv_content_checks = any(
        check in enabled_checks
        for check in [
            CHECK_NUM_COLUMNS,
            CHECK_TSV_HEADER,
            CHECK_ID,
            CHECK_REFERENCE,
            CHECK_SUPPORT_REFERENCE,
            CHECK_LITERAL_BACKSLASH_N,
            CHECK_OCCURRENCE,
            CHECK_NOTE_ENDING,
            CHECK_REFERENCE_ORDER,
            CHECK_ALT_TRANSLATION_LABEL,
            CHECK_PAIRED_SQUARE_BRACKET,
            CHECK_DUPLICATE_ID,
            CHECK_STRAIGHT_QUOTES,
            CHECK_NOTE,
        ]
    )
    if need_tsv_content_checks:
        for tsv_path in tsv_files:
            validate_tsv_file(tsv_path, errors)

    return emit_results(errors, active_checks, output_json=args.json_output)


if __name__ == "__main__":
    sys.exit(main())
