import io
import zipfile
import zlib

from app.analyzer import analyze_statement, extract_pdf_text, parse_statement


def fake_categorizer(transaction):
    return {**transaction, "category": "test", "confidence": 1.0}


def test_analyze_csv_statement():
    content = b"Date,Description,Amount\n2026-01-05,Coffee Shop,-12.50\n2026-01-06,Payroll,2200.00\n"

    result = analyze_statement("statement.csv", content, categorizer=fake_categorizer)

    assert result["transactionCount"] == 2
    assert result["totals"] == {"credits": 2200.0, "debits": 12.5, "net": 2187.5}
    assert result["transactions"][0]["description"] == "Coffee Shop"
    assert result["transactions"][1]["category"] == "test"


def test_parse_xlsx_statement():
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>
                <row r="1">
                  <c r="A1" t="inlineStr"><is><t>Date</t></is></c>
                  <c r="B1" t="inlineStr"><is><t>Description</t></is></c>
                  <c r="C1" t="inlineStr"><is><t>Amount</t></is></c>
                </row>
                <row r="2">
                  <c r="A2" t="inlineStr"><is><t>2026-01-08</t></is></c>
                  <c r="B2" t="inlineStr"><is><t>Whole Foods</t></is></c>
                  <c r="C2"><v>-55.10</v></c>
                </row>
              </sheetData>
            </worksheet>""",
        )

    transactions = parse_statement(workbook.getvalue(), ".xlsx")

    assert len(transactions) == 1
    assert transactions[0]["description"] == "Whole Foods"
    assert transactions[0]["amount"] == -55.10


def test_parse_pdf_statement_lines():
    pdf_stream = zlib.compress(b"BT (01/05 Coffee Shop -12.50) Tj ET\nBT (01/06 Payroll 2200.00) Tj ET")
    pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nstream\n" + pdf_stream + b"\nendstream\nendobj\n%%EOF"

    text = extract_pdf_text(pdf_bytes)
    transactions = parse_statement(pdf_bytes, ".pdf")

    assert "Coffee Shop" in text
    assert [entry["description"] for entry in transactions] == ["Coffee Shop", "Payroll"]
