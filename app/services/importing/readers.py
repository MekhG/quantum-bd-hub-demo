"""Reading uploaded CSV/XLSX files, and rows pasted from Sheets/Excel/AI chats, safely."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import BytesIO, StringIO

import pandas as pd

from app.services.importing.columns import header_score

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_ROWS = 60_000
MAX_PASTE_CHARS = 3_000_000
MAX_PASTE_ROWS = 5_000
ALLOWED_EXTENSIONS = (".csv", ".xlsx")


class UploadError(ValueError):
    """A problem with the file itself (wrong type, too big, unreadable)."""


@dataclass
class Sheet:
    name: str
    frame: pd.DataFrame  # all cells as text; columns are the sheet's header row
    first_data_row: int  # spreadsheet row number of the first data row (for messages like "row 14")


def _find_header_row(raw: pd.DataFrame) -> int:
    """Google Sheets exports sometimes start with a title row; pick the row that looks most like headers."""
    best_row, best_score = 0, -1
    for index in range(min(10, len(raw))):
        cells = [str(c) for c in raw.iloc[index].tolist() if str(c).strip()]
        score = header_score(cells)
        if score > best_score:
            best_row, best_score = index, score
    return best_row


def _to_sheet(name: str, raw: pd.DataFrame) -> Sheet:
    raw = raw.fillna("").astype(str)
    raw = raw.loc[:, (raw != "").any(axis=0)]  # drop empty columns
    if raw.empty:
        return Sheet(name, pd.DataFrame(), 2)
    header_row = _find_header_row(raw)
    headers = [str(h).strip() for h in raw.iloc[header_row].tolist()]
    # Blank or repeated header cells would collide; give them unique placeholder names.
    seen: dict[str, int] = {}
    unique_headers = []
    for i, header in enumerate(headers):
        header = header or f"(column {i + 1})"
        seen[header] = seen.get(header, 0) + 1
        unique_headers.append(header if seen[header] == 1 else f"{header} ({seen[header]})")
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = unique_headers
    first_data_row = header_row + 2
    body.index = range(first_data_row, first_data_row + len(body))
    body = body[(body.apply(lambda r: r.str.strip()) != "").any(axis=1)]  # drop blank rows
    if len(body) > MAX_ROWS:
        raise UploadError(f"Sheet '{name}' has {len(body):,} rows; the limit is {MAX_ROWS:,}. Split the file.")
    return Sheet(name, body, first_data_row)


def read_upload(data: bytes, file_name: str) -> list[Sheet]:
    lowered = file_name.lower()
    if not lowered.endswith(ALLOWED_EXTENSIONS):
        raise UploadError("Please upload a .xlsx or .csv file (in Google Sheets: File → Download).")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise UploadError("The file is empty.")

    try:
        if lowered.endswith(".csv"):
            for encoding in ("utf-8-sig", "cp1252"):
                try:
                    raw = pd.read_csv(BytesIO(data), header=None, dtype=str, keep_default_na=False,
                                      encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise UploadError("Couldn't read the CSV's text encoding. Try downloading it as .xlsx instead.")
            sheets = [_to_sheet(file_name, raw)]
        else:
            # .xlsx files are zip archives; anything else with that name is rejected here.
            if data[:2] != b"PK":
                raise UploadError("This doesn't look like a real .xlsx file.")
            book = pd.read_excel(BytesIO(data), sheet_name=None, header=None, dtype=str, engine="openpyxl")
            sheets = [_to_sheet(name, raw) for name, raw in book.items()]
    except UploadError:
        raise
    except Exception as error:  # pandas/openpyxl raise many different types for broken files
        raise UploadError(f"Couldn't read the file: {error}") from error

    sheets = [s for s in sheets if not s.frame.empty]
    if not sheets:
        raise UploadError("No data found in the file.")
    return sheets


_MD_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_MD_DECORATION = re.compile(r"^(\*\*|__|`)(.*)(\*\*|__|`)$")


def _markdown_rows(lines: list[str]) -> list[list[str]]:
    """A table as Claude/ChatGPT print it: | a | b |, with a |---|---| line under the headers."""
    rows = []
    for line in lines:
        if "|" not in line or _MD_SEPARATOR.match(line):
            continue
        cells = line.strip()
        cells = cells[1:] if cells.startswith("|") else cells
        cells = cells[:-1] if cells.endswith("|") else cells
        row = []
        for cell in cells.split("|"):
            cell = cell.strip().replace("<br>", "; ").replace("<br/>", "; ")
            match = _MD_DECORATION.match(cell)
            row.append(match.group(2).strip() if match else cell)
        rows.append(row)
    return rows


def read_pasted(text: str) -> Sheet:
    """Rows copied from Google Sheets / Excel (tab-separated), a CSV, or a Markdown table from an AI chat.

    The first row must be the column headers, exactly as with an uploaded sheet.
    """
    if not text or not text.strip():
        raise UploadError("Nothing pasted yet.")
    if len(text) > MAX_PASTE_CHARS:
        raise UploadError("That's too much to paste at once. Split it, or upload it as a file instead.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # AI answers often wrap tables in ``` fences; drop those lines.
    lines = [line for line in text.split("\n") if not line.strip().startswith("```")]
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        raise UploadError("Nothing pasted yet.")

    pipe_lines = sum(1 for line in non_empty if line.strip().startswith("|") or line.count("|") >= 2)
    if pipe_lines >= max(2, len(non_empty) // 2):
        rows = _markdown_rows(non_empty)
    else:
        body = "\n".join(lines)
        first = non_empty[0]
        delimiter = "\t" if "\t" in first else (";" if first.count(";") > first.count(",") else ",")
        # csv handles quoted cells with line breaks, which is how Sheets copies multi-line cells.
        rows = [row for row in csv.reader(StringIO(body), delimiter=delimiter) if any(c.strip() for c in row)]

    if len(rows) < 2:
        raise UploadError("Paste the header row plus at least one row of data.")
    if len(rows) - 1 > MAX_PASTE_ROWS:
        raise UploadError(f"{len(rows) - 1:,} rows pasted; the limit is {MAX_PASTE_ROWS:,}. Upload a file instead.")
    width = max(len(r) for r in rows)
    raw = pd.DataFrame([r + [""] * (width - len(r)) for r in rows], dtype=str)
    sheet = _to_sheet("Pasted rows", raw)
    if sheet.frame.empty:
        raise UploadError("No data rows found under the headers.")
    return sheet


def same_columns(a: Sheet, b: Sheet) -> bool:
    return [str(c).strip().lower() for c in a.frame.columns] == [str(c).strip().lower() for c in b.frame.columns]


def combine_sheets(sheets: list[Sheet]) -> Sheet:
    """Stacks tabs that share the same columns (e.g. 'Colleges 1' … 'Colleges 94') into one import.

    Row labels become 'Tab › row' so every message still points at the right place.
    """
    frames = []
    for sheet in sheets:
        frame = sheet.frame.copy()
        frame.columns = list(sheets[0].frame.columns)
        frame.index = [f"{sheet.name} › {row}" for row in frame.index]
        frames.append(frame)
    combined = pd.concat(frames)
    if len(combined) > MAX_ROWS:
        raise UploadError(f"These tabs hold {len(combined):,} rows together; the limit is {MAX_ROWS:,}.")
    return Sheet(f"{len(sheets)} tabs", combined, sheets[0].first_data_row)
