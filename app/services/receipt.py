"""PDF payment receipts for the Billing Center.

Generates a simple, downloadable receipt PDF for a confirmed NOWPayments
transaction using fpdf2. Korean text is supported via the bundled Malgun
Gothic font (app/assets/fonts/malgun.ttf / malgunbd.ttf).

No secrets (payment keys, hashes) are embedded — only order/payment ids which
the tenant already owns and can see in their own history.
"""

import os
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from app.core.logging import get_logger
from app.models.nowpayments import NowPaymentsTransaction
from app.models.tenant import Tenant

logger = get_logger(__name__)

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_REGULAR = _FONT_DIR / "malgun.ttf"
_FONT_BOLD = _FONT_DIR / "malgunbd.ttf"


def _font_available() -> bool:
    return _FONT_REGULAR.exists() and _FONT_BOLD.exists()


def _format_amount(amount: float | None) -> str:
    if amount is None:
        return "-"
    return f"${amount:,.2f}"


def _format_date(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M")


def generate_payment_receipt(tenant: Tenant, txn: NowPaymentsTransaction) -> bytes:
    """Build a PDF receipt (as bytes) for a confirmed transaction."""
    plan_name = (tenant.plan or "free").upper()
    status = (txn.payment_status or "unknown").upper()

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    if _font_available():
        pdf.add_font("Malgun", "", str(_FONT_REGULAR))
        pdf.add_font("Malgun", "B", str(_FONT_BOLD))
        font_name = "Malgun"
    else:
        font_name = "Helvetica"

    # Header
    pdf.set_font(font_name, "B", 22)
    pdf.set_text_color(40, 40, 40)
    pdf.cell(0, 12, "TeleMon", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font_name, "", 10)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, "Payment Receipt / 결제 영수증", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Horizontal rule
    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.4)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Receipt number
    pdf.set_font(font_name, "B", 12)
    pdf.set_text_color(40, 40, 40)
    pdf.cell(0, 8, f"Receipt No. {txn.payment_id}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Details table
    pdf.set_font(font_name, "B", 10)
    pdf.set_text_color(60, 60, 60)
    pdf.set_fill_color(245, 245, 245)
    rows = [
        ("Plan", f"{plan_name} ({tenant.plan})"),
        ("Status", status),
        ("Amount", _format_amount(txn.amount)),
        ("Paid", _format_amount(txn.paid_amount)),
        ("Currency", (txn.pay_currency or "-").upper()),
        ("Order ID", txn.order_id or "-"),
        ("Payment ID", txn.payment_id),
        ("Created", _format_date(txn.created_at)),
        ("Fulfilled", _format_date(txn.fulfilled_at)),
    ]
    col_w = 45
    for label, value in rows:
        pdf.cell(col_w, 8, label, fill=True, border=1)
        pdf.set_font(font_name, "", 10)
        pdf.cell(0, 8, value, border=1, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(font_name, "B", 10)

    pdf.ln(8)
    pdf.set_font(font_name, "", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.multi_cell(
        0,
        5,
        "Thank you for using TeleMon. For questions about this receipt, contact support "
        "with the Receipt No. above.",
    )

    return bytes(pdf.output())
