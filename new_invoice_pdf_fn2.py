def _invoice_pdf_bytes(co, start, end, number, inv, bank,
                       fit_one_page=False):
    """Build the invoice PDF matching the Hanseong sheet format (Letter
    portrait, compact so a 20-worker invoice fits on one page): PRISM
    logo + blue Invoice title, contact block (email on the same line as
    the phone, to its right), dark-navy VENDOR / SHIP TO bars, P.O.
    table, items table (NO/DESCRIPTION/UNIT PRICE/UOM/QTY/TOTAL,
    Regular (RH) then Overtime (OT) per employee), Bank Account Details
    box + SUB TOTAL / FREIGHT / SHIPPING / HANDLING / TOTAL stack,
    Approved by with signature line. The (RH)/(OT) legend sits side by
    side on one line. The DPO reference footer is intentionally left
    out. The bottom section is ALWAYS pinned to the bottom of the page
    (its position is computed from the bottom margin upward), so a short
    invoice has the bank box + totals + approval at the very bottom and
    a long invoice never overlaps them; very long invoices flow to
    page 2. Top/bottom margins come from the company record
    (pdf_top_margin / pdf_bottom_margin, mm; defaults 5 / 5).
    fit_one_page: shrink the item rows just enough for the whole
    invoice to fit on one page (never taller than the default, never
    below 3.0mm); if even the minimum row height cannot fit it, the
    invoice still flows to page 2."""
    from fpdf import FPDF

    NAVY = (10, 48, 65)
    BLUE = (51, 51, 153)
    LEFT = 17.8
    RIGHT = 197.8
    W = RIGHT - LEFT  # 180.0
    # Compact type: 8pt body, 4.5mm header rows, 3.6mm item rows, tight
    # section gaps — so a 20-worker invoice still fits on one page.
    BODY = 8
    ROW = 4.5
    DATA_ROW = 3.6
    PAGE_H = 279.4
    TOP = _pdf_margin(co.get("pdf_top_margin"), 5, 0, 20)
    BOTTOM_MARGIN = _pdf_margin(co.get("pdf_bottom_margin"), 5, 5, 30)
    if fit_one_page:
        # The items table runs from TOP+65 down to the bottom
        # section's top (yb2, computed the same way as below: the
        # 7-line bank box + approval block). Shrink the item rows
        # just enough to fit; only shrinks, never grows past the
        # default 3.6mm, and never below 3.0mm (the 8pt text still
        # needs to fit).
        box_h = 4 + 3.4 * 7  # 7 bank lines, same as the bottom section
        yb2 = PAGE_H - BOTTOM_MARGIN - 7 - 6 - (box_h + 1)
        available = yb2 - (TOP + 65)
        distinct = {(ln.get("company") or "") for ln in inv["lines"]
                    if ln.get("company")}
        n_section = len(distinct) if len(distinct) > 1 else 0
        n_rows = (len(inv["lines"]) + len(inv["expenses"]) + n_section)
        excluded_h = 4 if inv.get("excluded") else 0
        if n_rows > 0:
            fit_row = (available - ROW - excluded_h) / n_rows
            DATA_ROW = max(3.0, min(DATA_ROW, fit_row))

    def money(v):
        # Negative amounts (discounts / deductions) read as -$2,500.00.
        if v < 0:
            return f"-${abs(v):,.2f}"
        return f"${v:,.2f}"

    def qty(v):
        return f"{v:.2f}"

    def md(d):
        return f"{d.month}/{d.day}"

    def mdy(d):
        return f"{d.month}/{d.day}/{d.year}"

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=BOTTOM_MARGIN)
    pdf.set_margins(LEFT, TOP, 17.8)
    pdf.add_page()
    # Thin grid lines (0.1mm instead of the 0.2mm default).
    pdf.set_line_width(0.1)

    # --- Top row: logo (left) + Invoice title (blue, right) ---
    logo_path = os.path.join(BASE_DIR, "static", "prism-logo.png")
    if os.path.exists(logo_path):
        pdf.image(logo_path, x=LEFT, y=TOP, w=46)
    pdf.set_xy(124, TOP + 0.5)
    pdf.set_font("helvetica", "B", 20)
    pdf.set_text_color(*BLUE)
    pdf.cell(60, 9, "Invoice", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    # --- Contact block (left) / NUMBER + CATEGORY + duration (right) ---
    # 4 mm line height. The email sits on the SAME line as the phone,
    # to its right — so the block is 4 lines, not 5.
    y0 = TOP + 13
    pdf.set_xy(LEFT, y0)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(95, 4, INVOICE_COMPANY_NAME + ".", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", BODY)
    pdf.cell(95, 4, INVOICE_SITE_ADDRESS, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(95, 4, INVOICE_CONTACT_NAME, new_x="LMARGIN", new_y="NEXT")
    # Phone + email on one line: phone left, email right after it.
    phone_txt = f"Mobile: {INVOICE_CONTACT_PHONE}"
    pw = pdf.get_string_width(phone_txt)
    pdf.cell(pw, 4, phone_txt, new_x="RIGHT", new_y="TOP")
    pdf.cell(4, 4, "", new_x="RIGHT", new_y="TOP")
    pdf.set_text_color(*BLUE)
    pdf.set_font("helvetica", "U", BODY)
    pdf.cell(95 - pw - 4, 4, INVOICE_CONTACT_EMAIL,
            link=f"mailto:{INVOICE_CONTACT_EMAIL}",
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", BODY)
    pdf.set_text_color(0, 0, 0)

    pdf.set_xy(115, y0)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(83, 4, f"NUMBER : {number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(115, y0 + 4)
    pdf.set_font("helvetica", "", BODY)
    cat = (co.get("category") or "").strip().replace(" / ", " ")
    pdf.cell(83, 4, f"CATEGORY : {cat}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(115, y0 + 13)
    pdf.cell(83, 4, f"Payroll duration. ({md(start)}~{md(end)})",
            new_x="LMARGIN", new_y="NEXT")

    # --- VENDOR / SHIP TO dark bars (tight: right after the contact block) ---
    yb = TOP + 31
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    pdf.set_xy(LEFT, yb)
    pdf.cell(84, 6, " VENDOR", fill=True, new_x="RIGHT", new_y="TOP")
    pdf.set_xy(122, yb)
    pdf.cell(76, 6, " SHIP TO", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    # TO / COMPANY / ADDRESS (left) + ship-to value (right)
    pdf.set_xy(LEFT, yb + 6.5)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(84, 4, "TO :", new_x="RIGHT", new_y="TOP")
    pdf.set_xy(122, yb + 6.5)
    pdf.set_font("helvetica", "", BODY)
    pdf.cell(76, 4, (co.get("ship_to") or "").strip(),
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(LEFT, yb + 10.5)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(84, 4, f"COMPANY : {(co.get('vendor') or '').strip()}",
            new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(LEFT, yb + 14.5)
    pdf.cell(84, 4, "ADDRESS :", new_x="LMARGIN", new_y="NEXT")

    # --- TEL NUM + RH/OT legend (side by side on ONE line) ---
    # TEL NUM is the vendor's phone: the company's own phone when set
    # (a lone "-" is a deliberate "no number yet" and shows as "-"),
    # otherwise the default number.
    tel_num = (co.get("phone") or "").strip() or INVOICE_TEL_NUM
    yt = TOP + 50
    pdf.set_xy(LEFT, yt)
    pdf.set_font("helvetica", "B", BODY)
    pdf.cell(60, 4, f"TEL NUM : {tel_num}",
            new_x="LMARGIN", new_y="NEXT")
    # (RH) and (OT) next to each other on the same line.
    pdf.set_xy(124, yt)
    pdf.set_font("helvetica", "", BODY)
    rh_txt = "(RH) REGULAR HR."
    rh_w = pdf.get_string_width(rh_txt)
    pdf.cell(rh_w, 4, rh_txt, new_x="RIGHT", new_y="TOP")
    pdf.cell(4, 4, "", new_x="RIGHT", new_y="TOP")
    pdf.cell(60, 4, "(OT) OVER TIME HR", new_x="LMARGIN", new_y="NEXT")

    # --- P.O. table (tight: right after TEL NUM / legend) ---
    yp = TOP + 55
    po_cols = [("P.O. DATE", 45), ("REQUISITIONER", 45),
               ("SHIPPED VIA", 30), ("F.O.B. POINT", 30),
               ("PROJECT NAME", 30)]
    pdf.set_xy(LEFT, yp)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    for i, (label, cw) in enumerate(po_cols):
        last = i == len(po_cols) - 1
        pdf.cell(cw, ROW, label, border=1, align="C", fill=True,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "", BODY)
    po_vals = [(f"{mdy(start)}~{mdy(end)}", 45, "C"),
               (INVOICE_CONTACT_NAME, 45, "C"), ("-", 30, "C"),
               ("", 30, "C"), ("", 30, "C")]
    pdf.set_x(LEFT)
    for i, (txt, cw, al) in enumerate(po_vals):
        last = i == len(po_vals) - 1
        pdf.cell(cw, ROW, txt, border=1, align=al,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")

    # --- Items table (dark header; rows flow across pages if long) ---
    yi = TOP + 65
    cols = [("NO", 13.8, "C"), ("DESCRIPTION", 60.4, "C"),
            ("UNIT PRICE", 31.9, "C"), ("UOM", 14.8, "C"),
            ("QTY", 16.2, "C"), ("TOTAL", 42.9, "C")]
    pdf.set_xy(LEFT, yi)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", BODY)
    for i, (label, cw, al) in enumerate(cols):
        last = i == len(cols) - 1
        pdf.cell(cw, ROW, label, border=1, align=al, fill=True,
                new_x="LMARGIN" if last else "RIGHT",
                new_y="NEXT" if last else "TOP")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "", BODY)
    emp_no = 0
    cur_company = None
    # Section rows only when the invoice actually spans two companies;
    # a single-company invoice carries its company on every line but
    # shows no section header.
    combined_pdf = len({(ln.get("company") or "")
                        for ln in inv["lines"]
                        if ln.get("company")}) > 1
    for line in inv["lines"]:
        # Combined invoice: a bold section row when the company
        # changes, so the client can see which division each worker
        # belongs to. Single-company invoices carry no company on
        # their lines, so no header appears.
        line_company = line.get("company") or ""
        if combined_pdf and line_company and line_company != cur_company:
            cur_company = line_company
            pdf.set_font("helvetica", "B", BODY)
            pdf.set_x(LEFT)
            pdf.cell(13.8, DATA_ROW, "", border=1, align="C",
                    new_x="RIGHT", new_y="TOP")
            pdf.cell(60.4, DATA_ROW, f" {line_company}", border=1,
                    align="L", new_x="RIGHT", new_y="TOP")
            pdf.cell(31.9, DATA_ROW, "", border=1, align="R",
                    new_x="RIGHT", new_y="TOP")
            pdf.cell(14.8, DATA_ROW, "", border=1, align="C",
                    new_x="RIGHT", new_y="TOP")
            pdf.cell(16.2, DATA_ROW, "", border=1, align="R",
                    new_x="RIGHT", new_y="TOP")
            pdf.cell(42.9, DATA_ROW, "", border=1, align="R",
                    new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("helvetica", "", BODY)
        if line["type"] == "Regular":
            emp_no += 1
            no = f"A{emp_no}"
            desc = line["employee"]
            tag = "(RH)"
            # RH row: top border only (no bottom) -- it groups with the
            # OT row below, so the two rows of one worker have no line
            # between them.
            bdr = "LTR"
        else:
            no = ""
            desc = line["employee"]
            tag = "(OT)"
            # OT row: bottom border only (no top).
            bdr = "LBR"
        # NO
        pdf.set_x(LEFT)
        pdf.cell(13.8, DATA_ROW, no, border=bdr, align="C",
                new_x="RIGHT", new_y="TOP")
        # DESCRIPTION: name left, (RH)/(OT) right
        x_desc = pdf.get_x()
        y_row = pdf.get_y()
        pdf.cell(60.4, DATA_ROW, f" {desc}", border=bdr, align="L",
                new_x="RIGHT", new_y="TOP")
        # overlay the tag right-aligned inside the description column
        pdf.set_xy(x_desc + 60.4 - 14, y_row)
        pdf.cell(14, DATA_ROW, tag, align="R", new_x="RIGHT", new_y="TOP")
        pdf.cell(31.9, DATA_ROW, money(line["unit"]), border=bdr, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(14.8, DATA_ROW, "1", border=bdr, align="C",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(16.2, DATA_ROW, qty(line["qty"]), border=bdr, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(42.9, DATA_ROW, money(line["total"]), border=bdr, align="R",
                new_x="LMARGIN", new_y="NEXT")
    for ex in inv["expenses"]:
        pdf.set_x(LEFT)
        pdf.cell(13.8, DATA_ROW, "", border=1, new_x="RIGHT", new_y="TOP")
        pdf.cell(60.4, DATA_ROW, f" {ex['description']}", border=1, align="L",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(31.9, DATA_ROW, money(ex["amount"]), border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(14.8, DATA_ROW, "1", border=1, align="C",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(16.2, DATA_ROW, "1.00", border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(42.9, DATA_ROW, money(ex["amount"]), border=1, align="R",
                new_x="LMARGIN", new_y="NEXT")
    if inv["excluded"]:
        pdf.set_font("helvetica", "", 8)
        pdf.set_x(LEFT)
        pdf.cell(W, 4, "Not billed: " + ", ".join(inv["excluded"]) +
                " - no rate.", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", BODY)

    # --- Bottom section: ALWAYS pinned to the page bottom. ---
    # Its top (yb2) is computed from the bottom margin upward:
    # approval underline at PAGE_H - BOTTOM_MARGIN, approval text 7mm
    # above it, a 6mm gap, then the bank box. The table is filled down
    # to yb2, so a short invoice has the bottom at the very bottom and
    # a long invoice never overlaps it. If the table runs past yb2 on
    # this page, the bottom moves to a fresh page (also pinned).
    bank_lines = [
        ("Bank Name: ", bank["bank"]),
        ("Branch Name: ", bank["branch"]),
        ("Bank Telephone No.: ", bank.get("telephone") or ""),
        ("Account Name: ", bank["account_name"]),
        ("Routing #: ", bank["routing"]),
        ("Account #: ", bank["account"]),
        ("Zelle: ", bank.get("zelle") or ""),
    ]
    box_h = 4 + 3.4 * len(bank_lines)
    approval_underline_y = PAGE_H - BOTTOM_MARGIN
    ya = approval_underline_y - 7
    yb2 = ya - 6 - (box_h + 1)
    y_after = pdf.get_y()
    if y_after > yb2:
        # Table runs past where the bottom must start: fresh page.
        pdf.add_page()
        y_after = pdf.get_y()
    # (No border is drawn in the empty space between the table and
    # the bottom section: it stays blank white, so the bottom sits at
    # the very bottom of the page with no stray grid lines.)
    # Bank box (left)
    pdf.set_xy(LEFT + 2, yb2)
    pdf.set_font("helvetica", "BU", BODY)
    pdf.cell(76, 4, "Bank Account Details:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 8)
    for label, val in bank_lines:
        pdf.set_x(LEFT + 2)
        pdf.set_font("helvetica", "B", 8)
        lw = pdf.get_string_width(label)
        pdf.cell(lw, 3.4, label, new_x="RIGHT", new_y="TOP")
        pdf.set_font("helvetica", "", 8)
        pdf.cell(76 - lw, 3.4, val, new_x="LMARGIN", new_y="NEXT")
    # box border around the bank block
    pdf.rect(LEFT, yb2, 80, box_h + 1, style="D")
    # Totals stack (right)
    pdf.set_xy(118, yb2)
    pdf.set_font("helvetica", "B", BODY)
    totals_rows = [
        ("SUB TOTAL", money(inv["sub_total"]), True),
        ("FREIGHT", "", False),
        ("SHIPPING", "", False),
        ("HANDLING", "", False),
        ("TOTAL", money(inv["total"]), True),
    ]
    for label, amt, bold in totals_rows:
        pdf.set_font("helvetica", "B" if bold else "", BODY)
        pdf.set_x(118)
        pdf.cell(37, ROW, label, border=1, align="R",
                new_x="RIGHT", new_y="TOP")
        pdf.cell(43, ROW, amt, border=1, align="R",
                new_x="LMARGIN", new_y="NEXT")

    # --- Approved by + signature line (pinned: underline at the
    # bottom margin) ---
    pdf.set_xy(LEFT, ya)
    pdf.set_font("helvetica", "B", BODY)
    appr = (f"Approved by Prism ME LLC. Chief Executive Officer "
            f"{APPROVED_BY}")
    tw = pdf.get_string_width(appr)
    cx = (LEFT + RIGHT - tw) / 2
    pdf.set_x(cx)
    pdf.cell(tw, 5, appr, new_x="LMARGIN", new_y="NEXT")
    pdf.line(cx, approval_underline_y, cx + tw, approval_underline_y)

    return bytes(pdf.output())
