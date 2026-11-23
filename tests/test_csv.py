import pytest

from archive.core import SubmissionError
from archive.csv_import import parse_edit_csv, parse_edit_csv_checked

HEADER = "镜头号,原卷号,起帧,帧数\n"


def test_parse_basic_csv():
    rows = parse_edit_csv((HEADER + "S01,R001,0,120\nS02,R002,50,30\n").encode("utf-8"))
    assert rows == [
        {"row_index": 1, "shot_no": "S01", "orig_reel": "R001", "orig_start": 0, "frame_count": 120},
        {"row_index": 2, "shot_no": "S02", "orig_reel": "R002", "orig_start": 50, "frame_count": 30},
    ]


def test_parse_utf8_bom_and_gb18030():
    for encoding in ("utf-8-sig", "gb18030"):
        rows = parse_edit_csv((HEADER + "S01,R001,7,8\n").encode(encoding))
        assert rows[0]["orig_start"] == 7


def test_header_aliases_and_spaces():
    text = "shot,reel,start,frames\nA1,R1,10,5\n"
    assert parse_edit_csv(text)[0]["frame_count"] == 5


def test_blank_lines_ignored_and_row_index_compacted():
    text = HEADER + "\nS01,R001,0,10\n\nS02,R001,10,10\n  \n"
    rows = parse_edit_csv(text)
    assert [r["row_index"] for r in rows] == [1, 2]


@pytest.mark.parametrize(
    "text,needle",
    [
        ("", "为空"),
        ("镜头号,原卷号\nS01,R001\n", "缺少列"),
        (HEADER + "S01,R001,abc,10\n", "不是整数"),
        (HEADER + "S01,R001,-1,10\n", "不能为负数"),
        (HEADER + "S01,R001,0,0\n", "帧数必须大于 0"),
        (HEADER + "\n", "没有有效"),
        (HEADER + "S01,R001,0\n", "列数不足"),
    ],
)
def test_invalid_csv_rejected_with_row_context(text, needle):
    with pytest.raises(SubmissionError) as exc:
        parse_edit_csv(text)
    assert any(needle in m for m in exc.value.messages)


def test_all_errors_reported_at_once():
    text = HEADER + "S01,R001,x,10\nS02,,0,0\nS03,R001,0,5\n"
    with pytest.raises(SubmissionError) as exc:
        parse_edit_csv(text)
    assert len(exc.value.messages) == 2


def test_checked_validation_returns_edit_rows():
    rows = parse_edit_csv_checked(HEADER + "S01,R001,0,10\n")
    assert rows[0].orig_end == 10


def test_crlf_and_quoted_fields():
    text = '镜头号,原卷号,起帧,帧数\r\n"S01","R,001",2,6\r\n'
    rows = parse_edit_csv(text)
    assert rows[0]["orig_reel"] == "R,001"
