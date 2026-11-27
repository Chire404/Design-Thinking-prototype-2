"""Zero-dependency CSV (UTF-8 BOM) & genuine OpenXML XLSX ledger exporters."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape as xml_escape

_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")

LEDGER_HEADERS = [
    "Mã giao dịch",
    "Người dùng",
    "Username",
    "Loại giao dịch",
    "Cơ chế duyệt",
    "Biến động (VNĐ)",
    "Số tiền hiển thị",
    "Số dư trước (VNĐ)",
    "Số dư sau (VNĐ)",
    "Người xử lý",
    "Mô tả chi tiết",
    "Thời gian (UTC)",
]

LEDGER_COL_WIDTHS = [24, 24, 18, 20, 14, 18, 18, 18, 18, 24, 46, 26]


def _clean_xml_text(value: object) -> str:
    text = "" if value is None else str(value)
    return _ILLEGAL_XML_RE.sub("", text)


def _csv_safe_text(value: object) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "@", "\t", "\r"):
        return "'" + text
    return text


def _signed_amount(row: dict) -> int:
    amount = int(row.get("amount") or 0)
    is_debit = row.get("transaction_type") in ("USAGE", "PASS_PURCHASE", "ADJUSTMENT_DEBIT")
    return -amount if is_debit else amount


def _col_letter(idx_1based: int) -> str:
    res = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        res = chr(65 + rem) + res
    return res


def export_transactions_csv(rows: list[dict]) -> bytes:
    """Export filtered ledger transactions as UTF-8 BOM CSV for seamless Vietnamese display in Excel."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(LEDGER_HEADERS)

    for r in rows:
        signed_val = _signed_amount(r)
        writer.writerow(
            [
                _csv_safe_text(r.get("transaction_code", "")),
                _csv_safe_text(r.get("user_name", "")),
                _csv_safe_text(r.get("username", "")),
                _csv_safe_text(r.get("transaction_type", "")),
                _csv_safe_text(r.get("approval_badge_text", "")),
                signed_val,
                _csv_safe_text(r.get("signed_amount_formatted", "")),
                int(r.get("balance_before") or 0),
                int(r.get("balance_after") or 0),
                _csv_safe_text(r.get("admin_name") or "Hệ thống tự động"),
                _csv_safe_text(r.get("description", "")),
                _csv_safe_text(r.get("created_at", "")),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def _cell_str(ref: str, value: object, style_idx: int = 0) -> str:
    txt = xml_escape(_clean_xml_text(value))
    return f'<c r="{ref}" t="inlineStr" s="{style_idx}"><is><t xml:space="preserve">{txt}</t></is></c>'


def _cell_num(ref: str, value: int, style_idx: int = 2) -> str:
    return f'<c r="{ref}" t="n" s="{style_idx}"><v>{int(value)}</v></c>'


def export_transactions_xlsx(rows: list[dict]) -> bytes:
    """Build a valid OpenXML .xlsx spreadsheet archive with styled headers, auto-filter & VND numeric cells."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="SoCaiGiaoDich" sheetId="1" r:id="rId1"/>'
        "</sheets>"
        "</workbook>"
    )

    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="#,##0"/>'
        '<numFmt numFmtId="165" formatCode="+#,##0;-#,##0;0"/>'
        "</numFmts>"
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0B192C"/><bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="2">'
        "<border><left/><right/><top/><bottom/><diagonal/></border>"
        '<border>'
        '<left style="thin"><color rgb="FFE2E8F0"/></left>'
        '<right style="thin"><color rgb="FFE2E8F0"/></right>'
        '<top style="thin"><color rgb="FFE2E8F0"/></top>'
        '<bottom style="thin"><color rgb="FFE2E8F0"/></bottom>'
        "<diagonal/>"
        "</border>"
        "</borders>"
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center" wrapText="1"/>'
        "</xf>"
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
        "</cellXfs>"
        "</styleSheet>"
    )

    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{w}" customWidth="1"/>'
        for idx, w in enumerate(LEDGER_COL_WIDTHS, start=1)
    )

    sheet_rows: list[str] = []
    # Header row (row 1)
    header_cells = "".join(
        _cell_str(f"{_col_letter(c)}1", title, style_idx=1)
        for c, title in enumerate(LEDGER_HEADERS, start=1)
    )
    sheet_rows.append(f'<row r="1" ht="24" customHeight="1">{header_cells}</row>')

    # Data rows
    for idx, r in enumerate(rows, start=2):
        signed_val = _signed_amount(r)
        cells = [
            _cell_str(f"A{idx}", r.get("transaction_code", ""), 0),
            _cell_str(f"B{idx}", r.get("user_name", ""), 0),
            _cell_str(f"C{idx}", r.get("username", ""), 0),
            _cell_str(f"D{idx}", r.get("transaction_type", ""), 0),
            _cell_str(f"E{idx}", r.get("approval_badge_text", ""), 0),
            _cell_num(f"F{idx}", signed_val, 3),
            _cell_str(f"G{idx}", r.get("signed_amount_formatted", ""), 0),
            _cell_num(f"H{idx}", int(r.get("balance_before") or 0), 2),
            _cell_num(f"I{idx}", int(r.get("balance_after") or 0), 2),
            _cell_str(f"J{idx}", r.get("admin_name") or "Hệ thống tự động", 0),
            _cell_str(f"K{idx}", r.get("description", ""), 0),
            _cell_str(f"L{idx}", r.get("created_at", ""), 0),
        ]
        sheet_rows.append(f'<row r="{idx}">{"".join(cells)}</row>')

    last_row = max(1, len(rows) + 1)
    last_col = _col_letter(len(LEDGER_HEADERS))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols_xml}</cols>"
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        f'<autoFilter ref="A1:{last_col}{last_row}"/>'
        "</worksheet>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types.encode("utf-8"))
        zf.writestr("_rels/.rels", root_rels.encode("utf-8"))
        zf.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels.encode("utf-8"))
        zf.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
    return out.getvalue()
