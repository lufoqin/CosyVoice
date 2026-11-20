import csv
import io
import re
from contextlib import contextmanager
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException


@contextmanager
def open_excel(file):
    try:
        workbook = load_workbook(file, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError, OSError, SyntaxError) as exc:
        raise ValueError("无法读取 .xlsx 文件") from exc
    try:
        yield workbook
    finally:
        workbook.close()


def cell_text(value):
    return "" if value is None else str(value)


def column(headers, name, location):
    if name not in headers:
        raise ValueError(f"{location} 缺少「{name}」列")
    return headers.index(name)


def sentence_rows(rows, location, *, test_sheet=False):
    headers = [cell_text(value).strip() for value in next(rows, ())]
    text_column = "测试语句" if test_sheet or ("文本" not in headers and "测试语句" in headers) else "文本"
    text_index = column(headers, text_column, location)
    id_index = headers.index("音频编号") if not test_sheet and "音频编号" in headers else None
    for row_number, row in enumerate(rows, start=2):
        text = cell_text(row[text_index]) if text_index < len(row) else ""
        number = cell_text(row[id_index]).strip() if id_index is not None and id_index < len(row) else ""
        if text.strip():
            yield text, number, f"{location} 第 {row_number} 行"
        elif number:
            raise ValueError(f"{location} 第 {row_number} 行有音频编号，但缺少文本")


def batch_entries(file, filename):
    if filename.lower().endswith(".xlsx"):
        try:
            with open_excel(io.BytesIO(file.read())) as workbook:
                rows = [
                    row
                    for sheet in workbook.worksheets
                    for row in sentence_rows(sheet.values, sheet.title)
                ]
        except (BadZipFile, KeyError, OSError, SyntaxError) as exc:
            raise ValueError("无法读取 .xlsx 工作表") from exc
    elif filename.lower().endswith(".csv"):
        try:
            with io.StringIO(file.read().decode("utf-8-sig"), newline="") as stream:
                rows = list(sentence_rows(csv.reader(stream, strict=True), "CSV"))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError("无法读取 CSV，请使用 UTF-8 编码和逗号分隔") from exc
    else:
        raise ValueError("批量文件须为 .xlsx 或 .csv")

    if not rows:
        raise ValueError("表格没有可合成的文本")

    entries = []
    names = set()
    for index, (text, number, location) in enumerate(rows, start=1):
        number = number or f"{index:06d}"
        if not re.fullmatch(r"[\w-]+", number):
            raise ValueError(f"{location} 的音频编号只能包含字母、数字、下划线或连字符")
        name = f"{number}.wav"
        if name.casefold() in names:
            raise ValueError(f"{location} 的音频文件名重复：{name}")
        names.add(name.casefold())
        entries.append((name, text))
    return entries
