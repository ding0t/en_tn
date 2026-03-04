#!/usr/bin/env python3
"""Validate TN TSV files and manifest consistency."""

from __future__ import annotations

import argparse
import csv
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
            )
        )

    def is_enabled(self, check_name: str) -> bool:
        return check_name in self.enabled_checks

    def has_errors(self) -> bool:
        return bool(self.errors) or self.truncated


@dataclass
class ReferenceOrderKey:
    chapter_value: int
    verse_rank: int
    verse_number: int
    chapter_text: str
    verse_text: str
    original: str
    line_number: int


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

    if verse_raw == "intro":
        verse_rank = 0
        verse_number = -1
        verse_text = "intro"
    elif verse_raw == "front":
        verse_rank = 1
        verse_number = -1
        verse_text = "front"
    else:
        match = re.match(r"^(\d+)", verse_raw)
        if not match:
            return None
        verse_rank = 2
        verse_number = int(match.group(1))
        verse_text = match.group(1)

    return ReferenceOrderKey(
        chapter_value=chapter_value,
        verse_rank=verse_rank,
        verse_number=verse_number,
        chapter_text=chapter_text,
        verse_text=verse_text,
        original=reference,
        line_number=line_number,
    )


def is_reference_in_order(prev_key: ReferenceOrderKey, curr_key: ReferenceOrderKey) -> bool:
    if curr_key.chapter_value < prev_key.chapter_value:
        return False

    if curr_key.chapter_value > prev_key.chapter_value:
        return True

    # Same chapter order: intro -> front -> verses, and verse numbers never decrease.
    if curr_key.verse_rank < prev_key.verse_rank:
        return False
    if curr_key.verse_rank > prev_key.verse_rank:
        return True

    if curr_key.verse_rank == 2 and curr_key.verse_number < prev_key.verse_number:
        return False
    return True


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
                    **row_context,
                    message=(
                        f"Matched label '{raw_match}': too many spaces before the "
                        "Alternate translation label."
                    ),
                )
            elif not re.fullmatch(r"[^a-z] ", preceding_two or ""):
                errors.add(
                    rule=CHECK_ALT_TRANSLATION_LABEL,
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
                **row_context,
                message=f"Matched label '{raw_match}': use 'Alternate', not 'Alternative'.",
            )

        # 4) Must have exactly one space between words
        if match.group(2) != " ":
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
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
        if next_char not in {":", ",", " "}:
            pretty_next = next_char if next_char else "(end of note)"
            errors.add(
                rule=CHECK_ALT_TRANSLATION_LABEL,
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
    previous_reference_key: ReferenceOrderKey | None = None

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
            continue

        reference, row_id, _tags, support_reference, _quote, occurrence, note = row
        row_context = {
            "file": file_name,
            "line": line_number,
            "row_id": row_id,
            "reference": reference,
        }

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
                rule=CHECK_ID,
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
                if previous_reference_key is not None and not is_reference_in_order(
                    previous_reference_key, current_reference_key
                ):
                    errors.add(
                        rule=CHECK_REFERENCE_ORDER,
                        **row_context,
                        message=(
                            f"Reference '{reference}' is out of order. It appears after "
                            f"'{previous_reference_key.original}' on line "
                            f"{previous_reference_key.line_number}, but should not go backward "
                            "in chapter or verse order."
                        ),
                    )
                previous_reference_key = current_reference_key

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

        if note.endswith("\\n"):
            errors.add(
                rule=CHECK_NOTE_ENDING,
                **row_context,
                message="Note must not end with literal \\n.",
            )

        if note:
            validate_alternate_translation_label(note=note, errors=errors, row_context=row_context)
            validate_paired_square_brackets(note=note, errors=errors, row_context=row_context)


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


def emit_results(errors: ErrorCollector, active_checks: list[str]) -> int:
    ordered_errors = sorted(errors.errors, key=_error_sort_key)
    grouped_errors = _group_errors_by_check(ordered_errors, active_checks)

    if errors.has_errors():
        print(f"Validation failed with {len(ordered_errors)} error(s).")
    else:
        print("Validation passed: manifest.yaml and TSV checks succeeded.")

    for check_name in active_checks:
        print(check_name)
        print("-" * len(check_name))
        section_errors = grouped_errors.get(check_name, [])
        if not section_errors:
            print("No validation errors found.")
            print("")
            continue

        for err in section_errors:
            line = err.display()
            print(f"- {line}")
            if _should_emit_annotations():
                if err.file:
                    annotation = _escape_annotation_value(line)
                    if err.line is not None:
                        print(f"::error file={err.file},line={err.line}::{annotation}")
                    else:
                        print(f"::error file={err.file}::{annotation}")
                else:
                    annotation = _escape_annotation_value(line)
                    print(f"::error::{annotation}")
        print("")

    for check_name in grouped_errors:
        if check_name in active_checks:
            continue
        print(check_name)
        print("-" * len(check_name))
        section_errors = grouped_errors[check_name]
        if not section_errors:
            print("No validation errors found.")
        else:
            for err in section_errors:
                line = err.display()
                print(f"- {line}")
                if _should_emit_annotations():
                    if err.file:
                        annotation = _escape_annotation_value(line)
                        if err.line is not None:
                            print(f"::error file={err.file},line={err.line}::{annotation}")
                        else:
                            print(f"::error file={err.file}::{annotation}")
                    else:
                        annotation = _escape_annotation_value(line)
                        print(f"::error::{annotation}")
        print("")

    if errors.truncated:
        truncation = (
            f"Error output truncated after {errors.max_errors} entries. "
            "Re-run with --max-errors for a larger cap."
        )
        print(truncation)
        if _should_emit_annotations():
            print(f"::error::{_escape_annotation_value(truncation)}")

    if errors.has_errors():
        write_step_summary(success=False, errors=ordered_errors, truncated=errors.truncated)
        return 1

    write_step_summary(success=True, errors=[])
    return 0


def write_step_summary(
    *, success: bool, errors: list[ValidationError], truncated: bool = False
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines: list[str] = []
    if success:
        lines.append("## TN Validation")
        lines.append("")
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
    args = parser.parse_args()

    try:
        active_checks = resolve_active_checks(args.checks)
    except ValueError as exc:
        print(f"Argument error: {exc}")
        return 2

    enabled_checks = set(active_checks)
    errors = ErrorCollector(max_errors=max(1, args.max_errors), enabled_checks=enabled_checks)
    manifest_path = Path(args.manifest)
    manifest_data: dict[str, Any] | None = None

    need_manifest = (
        CHECK_VALID_MANIFEST in enabled_checks or CHECK_PROJECT_TSV_FILES_EXIST in enabled_checks
    )
    if need_manifest:
        manifest_data = load_manifest(manifest_path, errors)

    tsv_files: list[Path] = sorted(Path(".").glob("tn_*.tsv"))

    if CHECK_PROJECT_TSV_FILES_EXIST in enabled_checks:
        if manifest_data is None:
            errors.add(
                rule=CHECK_PROJECT_TSV_FILES_EXIST,
                message="Cannot run 66 TSV File Check because manifest.yaml could not be parsed.",
            )
        else:
            manifest_tsv_paths, _manifest_paths_ordered = parse_manifest_project_paths(manifest_data, errors)
            tsv_files = validate_manifest_vs_repo(
                manifest_tsv_paths=manifest_tsv_paths,
                errors=errors,
            )

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
        ]
    )
    if need_tsv_content_checks:
        for tsv_path in tsv_files:
            validate_tsv_file(tsv_path, errors)

    return emit_results(errors, active_checks)


if __name__ == "__main__":
    sys.exit(main())
