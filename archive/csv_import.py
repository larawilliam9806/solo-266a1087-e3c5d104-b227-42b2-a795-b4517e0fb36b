"""Edit-list (剪辑 CSV) parsing.

The uploaded CSV has one header row and then one row per shot:

    镜头号,原卷号,起帧,帧数
    S01,R001,0,120

Column order is fixed, but common header aliases (Chinese or English) are
accepted.  Blank lines are skipped; surrounding whitespace is trimmed.  The
whole file is validated before anything touches the database, so a malformed
row rejects the entire order.
"""

from __future__ import annotations

import csv
import io

from .core import MAX_FRAME, MAX_ROWS, SubmissionError, validate_rows

HEADER_ALIASES = {
    "shot_no": {"镜头号", "镜头", "shot", "shot_no", "shotno", "shot id"},
    "orig_reel": {"原卷号", "原卷", "reel", "reel_no", "orig_reel", "source reel"},
    "orig_start": {"起帧", "起始帧", "原卷起帧", "start", "in", "in_frame", "orig_start"},
    "frame_count": {"帧数", "长度", "帧长", "count", "frames", "duration", "frame_count"},
}

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _norm_header(value: str) -> str:
    return (
        (value or "")
        .strip()
        .strip("﻿")
        .replace(" ", "")
        .replace("_", "")
        .lower()
    )


def _header_map(header) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = _norm_header(cell)
        if not key:
            continue
        for field, aliases in HEADER_ALIASES.items():
            if key in {_norm_header(a) for a in aliases} and field not in mapping:
                mapping[field] = index
    missing = [f for f in HEADER_ALIASES if f not in mapping]
    if missing:
        labels = {
            "shot_no": "镜头号",
            "orig_reel": "原卷号",
            "orig_start": "起帧",
            "frame_count": "帧数",
        }
        raise SubmissionError(
            "CSV 表头缺少列: " + "、".join(labels[f] for f in missing)
        )
    return mapping


def _parse_int(value, line_no: int, label: str) -> int:
    text = (value or "").strip()
    if text == "":
        raise SubmissionError(f"第 {line_no} 行{label}为空")
    try:
        number = int(text, 10)
    except ValueError:
        raise SubmissionError(f"第 {line_no} 行{label}不是整数: {text!r}") from None
    if number < 0:
        raise SubmissionError(f"第 {line_no} 行{label}不能为负数")
    if number > MAX_FRAME:
        raise SubmissionError(f"第 {line_no} 行{label}超出允许范围")
    return number


def parse_edit_csv(data: bytes | str) -> list[dict]:
    """Parse edit-list bytes/text into validated row dicts (0-based frames)."""
    if isinstance(data, bytes):
        if len(data) > MAX_UPLOAD_BYTES:
            raise SubmissionError(
                f"CSV 文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）"
            )
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise SubmissionError("CSV 无法解码，请使用 UTF-8 或 GB18030 编码")
    else:
        text = data
    if not text.strip():
        raise SubmissionError("CSV 文件为空")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover - guarded by the empty check
        raise SubmissionError("CSV 文件为空") from None
    mapping = _header_map(header)

    rows: list[dict] = []
    problems: list[str] = []
    for line_no, record in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in record):
            continue
        if len(record) < len(header):
            problems.append(
                f"第 {line_no} 行列数不足（{len(record)} < {len(header)}）"
            )
            continue
        try:
            shot_no = record[mapping["shot_no"]].strip()
            orig_reel = record[mapping["orig_reel"]].strip()
            orig_start = _parse_int(
                record[mapping["orig_start"]], line_no, "起帧"
            )
            frame_count = _parse_int(
                record[mapping["frame_count"]], line_no, "帧数"
            )
        except SubmissionError as exc:
            problems.extend(exc.messages)
            continue
        if not shot_no:
            problems.append(f"第 {line_no} 行镜头号为空")
            continue
        if not orig_reel:
            problems.append(f"第 {line_no} 行原卷号为空")
            continue
        if frame_count <= 0:
            problems.append(f"第 {line_no} 行帧数必须大于 0")
            continue
        if orig_start + frame_count > MAX_FRAME:
            problems.append(f"第 {line_no} 行帧段超出允许范围")
            continue
        rows.append(
            {
                "row_index": len(rows) + 1,
                "shot_no": shot_no,
                "orig_reel": orig_reel,
                "orig_start": orig_start,
                "frame_count": frame_count,
            }
        )
        if len(rows) > MAX_ROWS:
            problems.append(f"CSV 行数过多（上限 {MAX_ROWS} 行）")
            break

    if problems:
        raise SubmissionError(problems)
    if not rows:
        raise SubmissionError("CSV 中没有有效的镜头记录")
    return rows


def parse_edit_csv_checked(data: bytes | str) -> list:
    return validate_rows(parse_edit_csv(data))
