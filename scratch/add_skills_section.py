# -*- coding: utf-8 -*-
"""
scratch/add_skills_section.py — one-off: insert section 6.4 (the skills system)
into SemanticGateway_Project_Overview.docx.

The document carries no styles for its callouts/tables — every element is
manually formatted — so this script reproduces the exact formatting already in
use rather than inventing new looks:

  Heading 2   pStyle Heading2, spacing before 300 after 120, bold run in 1F6F63
  lead-in     italic, 11 pt, 4A5558
  body        spacing after 130, line 280 auto
  callout     left border C88214, fill FFF6E5, bold lead-in in 8A5A00
  inline code Consolas in 9C2B2B
  table       cloned tblPr from an existing table: header fill 1F6F63,
              white bold 9.5 pt; body 9.5 pt; first column Consolas
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import docx
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

DOC = Path(__file__).resolve().parent.parent / "SemanticGateway_Project_Overview.docx"
W = nsdecls("w")

TEAL = "1F6F63"
LEAD_GREY = "4A5558"
CODE_RED = "9C2B2B"
CALLOUT_BORDER = "C88214"
CALLOUT_FILL = "FFF6E5"
CALLOUT_LEAD = "8A5A00"


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def runs(markup: str, *, size: str | None = None, base_color: str | None = None,
         lead_color: str | None = None) -> str:
    """Render mini-markup into <w:r> elements.

    **bold** -> bold run (in *lead_color* when given), `code` -> Consolas run.
    """
    size_xml = f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>' if size else ""
    out: list[str] = []
    # Split on **bold** and `code`, keeping the delimiters.
    for token in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", markup):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            colour = lead_color or base_color
            col_xml = f'<w:color w:val="{colour}"/>' if colour else ""
            body = esc(token[2:-2])
            rpr = f"<w:b/><w:bCs/>{col_xml}{size_xml}"
        elif token.startswith("`") and token.endswith("`"):
            body = esc(token[1:-1])
            rpr = (
                '<w:rFonts w:ascii="Consolas" w:eastAsia="Consolas" '
                'w:hAnsi="Consolas" w:cs="Consolas"/>'
                f'<w:color w:val="{CODE_RED}"/>{size_xml}'
            )
        else:
            body = esc(token)
            col_xml = f'<w:color w:val="{base_color}"/>' if base_color else ""
            rpr = f"{col_xml}{size_xml}"
        out.append(
            f"<w:r><w:rPr>{rpr}</w:rPr>"
            f'<w:t xml:space="preserve">{body}</w:t></w:r>'
        )
    return "".join(out)


def heading2(text: str):
    return parse_xml(
        f"<w:p {W}><w:pPr><w:pStyle w:val=\"Heading2\"/>"
        f'<w:spacing w:before="300" w:after="120"/></w:pPr>'
        f'<w:r><w:rPr><w:b/><w:bCs/><w:color w:val="{TEAL}"/></w:rPr>'
        f"<w:t>{esc(text)}</w:t></w:r></w:p>"
    )


def lead(text: str):
    return parse_xml(
        f"<w:p {W}><w:pPr>"
        f'<w:spacing w:after="180" w:line="280" w:lineRule="auto"/></w:pPr>'
        f'<w:r><w:rPr><w:i/><w:iCs/><w:color w:val="{LEAD_GREY}"/>'
        f'<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>'
        f"<w:t>{esc(text)}</w:t></w:r></w:p>"
    )


def body(markup: str):
    return parse_xml(
        f"<w:p {W}><w:pPr>"
        f'<w:spacing w:after="130" w:line="280" w:lineRule="auto"/></w:pPr>'
        f"{runs(markup)}</w:p>"
    )


def callout(markup: str):
    return parse_xml(
        f"<w:p {W}><w:pPr>"
        f'<w:pBdr><w:left w:val="single" w:sz="20" w:space="8" '
        f'w:color="{CALLOUT_BORDER}"/></w:pBdr>'
        f'<w:shd w:val="clear" w:color="auto" w:fill="{CALLOUT_FILL}"/>'
        f'<w:spacing w:before="140" w:after="180" w:line="280" w:lineRule="auto"/>'
        f'<w:ind w:left="200" w:right="140"/></w:pPr>'
        f"{runs(markup, lead_color=CALLOUT_LEAD)}</w:p>"
    )


CELL_MAR = (
    '<w:tcMar><w:top w:w="70" w:type="dxa"/><w:left w:w="110" w:type="dxa"/>'
    '<w:bottom w:w="70" w:type="dxa"/><w:right w:w="110" w:type="dxa"/></w:tcMar>'
)
TBL_PR_EX = (
    '<w:tblPrEx><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
    '<w:bottom w:w="0" w:type="dxa"/></w:tblCellMar></w:tblPrEx>'
)


def cell(width: int, markup: str, *, header: bool = False, code: bool = False) -> str:
    shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{TEAL}"/>' if header else ""
    if header:
        inner = (
            f'<w:r><w:rPr><w:b/><w:bCs/><w:color w:val="FFFFFF"/>'
            f'<w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr>'
            f"<w:t>{esc(markup)}</w:t></w:r>"
        )
    elif code:
        inner = (
            '<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:eastAsia="Consolas" '
            'w:hAnsi="Consolas" w:cs="Consolas"/>'
            '<w:sz w:val="19"/><w:szCs w:val="19"/></w:rPr>'
            f"<w:t>{esc(markup)}</w:t></w:r>"
        )
    else:
        inner = runs(markup, size="19")
    return (
        f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shd}{CELL_MAR}</w:tcPr>'
        f'<w:p><w:pPr><w:spacing w:line="250" w:lineRule="auto"/></w:pPr>'
        f"{inner}</w:p></w:tc>"
    )


def table(widths: list[int], header: list[str], rows: list[list[str]],
          code_cols: set[int] = frozenset({0})):
    tbl_pr = (
        f'<w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>'
        f'<w:top w:val="single" w:sz="8" w:space="0" w:color="{TEAL}"/>'
        '<w:left w:val="none" w:sz="0" w:space="0" w:color="FFFFFF"/>'
        f'<w:bottom w:val="single" w:sz="8" w:space="0" w:color="{TEAL}"/>'
        '<w:right w:val="none" w:sz="0" w:space="0" w:color="FFFFFF"/>'
        '<w:insideH w:val="single" w:sz="2" w:space="0" w:color="C9D6D3"/>'
        '<w:insideV w:val="none" w:sz="0" w:space="0" w:color="FFFFFF"/>'
        '</w:tblBorders><w:tblCellMar><w:left w:w="10" w:type="dxa"/>'
        '<w:right w:w="10" w:type="dxa"/></w:tblCellMar>'
        '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="1" '
        'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>'
    )
    grid = "<w:tblGrid>" + "".join(
        f'<w:gridCol w:w="{w}"/>' for w in widths
    ) + "</w:tblGrid>"

    head_cells = "".join(cell(w, t, header=True) for w, t in zip(widths, header))
    trs = [f"<w:tr>{TBL_PR_EX}<w:trPr><w:tblHeader/></w:trPr>{head_cells}</w:tr>"]
    for row in rows:
        cells = "".join(
            cell(w, t, code=(i in code_cols))
            for i, (w, t) in enumerate(zip(widths, row))
        )
        trs.append(f"<w:tr>{TBL_PR_EX}{cells}</w:tr>")

    return parse_xml(f"<w:tbl {W}>{tbl_pr}{grid}{''.join(trs)}</w:tbl>")


def spacer():
    return parse_xml(f'<w:p {W}><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>')


# ── The section ───────────────────────────────────────────────────────────────
def build_elements():
    els = [
        heading2("6.4 The skill files that ground the prompts"),
        lead("Two markdown files are the only description of the physical database "
             "that the language model ever sees."),

        body(
            "The semantic layer knows about metrics. It does not tell a model that "
            "`watch_time_minutes` must be averaged rather than summed, that a null "
            "country means unknown origin, or that `dim_subscribers` has no "
            "`created_at` column. That knowledge lives in two files under "
            "`backend/skills/` — `streaming_analytics.md` (128 lines) and "
            "`sql_reviewer.md` (223 lines) — and nowhere else in the system."
        ),
        body(
            "They are ordinary markdown, injected verbatim into prompts at runtime "
            "rather than compiled into anything at build time. "
            "`backend/core/skill_loader.py` reads them from disk on each call and "
            "offers exactly two functions: one returns a whole file, the other "
            "returns a single section located by its heading. Editing a file "
            "changes model behaviour on the next request — no restart, no rebuild, "
            "no deployment step."
        ),

        table(
            widths=[2150, 2350, 4860],
            header=["File", "Read by", "What it contributes"],
            rows=[
                [
                    "streaming_analytics.md",
                    "IntentExtractor — two sections only",
                    "Table grain, join keys, and the rules that decide which metric "
                    "answers a question.",
                ],
                [
                    "sql_reviewer.md",
                    "SQLGenerator — whole file, as a system prompt",
                    "Every physical column name in the warehouse, plus a five-point "
                    "adversarial review checklist.",
                ],
            ],
        ),
        spacer(),

        body(
            "**The analytics skill is injected in part, not whole.** The extractor "
            "pulls exactly two sections — Table Reference and Gotchas — and places "
            "them in the prompt under its own headings. Table Reference gives the "
            "grain and join key of the three tables a question can touch. Gotchas "
            "is the part that changes answers: MRR is a monthly snapshot and must "
            "never be summed across months; watch time is averaged, never summed; "
            "`churned_mrr` is already negative, so negating it flips the sign; rows "
            "with a null country are excluded unless the question asks otherwise; "
            "`plan_type` has exactly three valid values; and a bare mention of "
            "churn always means the rate, never the count. The file's other "
            "sections — its metric list, worked SQL patterns and provenance footer "
            "— are never read by the gateway."
        ),
        body(
            "**The reviewer skill is loaded whole**, as the system prompt for an "
            "adversarial review pass. Most of its length is a Physical Column "
            "Reference covering six tables — every column, its type, and how it may "
            "be filtered. It is written defensively, stating outright what does not "
            "exist: no `created_at`, no `snapshot_date`, no `is_deleted` on any "
            "table, and `session_start` rather than `session_start_at`. That "
            "negative phrasing is deliberate, because the failure it exists to "
            "prevent is a model inventing a plausible column name. After the "
            "reference comes a fixed checklist to be worked in order — grain "
            "mismatch, fan-out risk, missing hygiene filters, wrong aggregation, "
            "date filter gaps — and a mandated reply of either "
            "“PASS — no issues found” or an issue list followed by "
            "revised SQL. If the query cannot be corrected without inventing a "
            "column, the reviewer is instructed to say so rather than guess."
        ),
        body(
            "**The reviewer almost never runs, by design.** Native MetricFlow SQL "
            "is trusted and skips it. A template cache hit skips it. The governed "
            "fallback builder skips it too, because its SQL is already "
            "deterministic and column-validated, and a review pass would add a "
            "second or two plus the risk of an unnecessary rewrite. It runs on one "
            "path only: when MetricFlow has failed and hand-assembled SQL would "
            "otherwise execute. On a healthy deployment that path is never taken, "
            "so the reviewer costs nothing. When it does run and returns a "
            "revision, that revision is itself safety-checked before use, and the "
            "original SQL is kept if the check fails."
        ),

        callout(
            "**Maintenance obligation:  **Add or rename a column in a dbt model and "
            "both files must be edited by hand. Nothing detects the drift — the "
            "four tests covering the loader check file reading and section "
            "extraction, and no test compares the Physical Column Reference against "
            "the actual warehouse. A stale reference does not fail loudly; it "
            "quietly instructs the reviewer to assert that a column which now "
            "exists does not."
        ),
        callout(
            "**Why backend/ cannot be pruned:  **the gateway locates these files by "
            "walking three directories up from `gateway/core/` into "
            "`backend/skills/`. The directory is never served as a service, but if "
            "it is missing from the checkout both features disable themselves "
            "silently — a warning is logged, the extractor prompt loses its data "
            "reference, and the reviewer fails open and approves whatever it was "
            "given. That is why the deployment keeps a directory it never runs."
        ),
    ]
    return els


def main() -> int:
    d = docx.Document(str(DOC))

    anchor = None
    for p in d.paragraphs:
        if p.style.name == "Heading 1" and p.text.strip().startswith("7. The frontend"):
            anchor = p._p
            break
    if anchor is None:
        print("ERROR: could not find the '7. The frontend' heading to anchor to.")
        return 1

    for el in build_elements():
        anchor.addprevious(el)

    # Point the existing backend/ inventory row at the new section.
    for t in d.tables:
        for row in t.rows:
            cells = row.cells
            if cells and cells[0].text.strip() == "skills/":
                para = cells[1].paragraphs[-1]
                if "6.4" not in cells[1].text:
                    r = para.add_run(" See section 6.4.")
                    r.font.size = para.runs[0].font.size if para.runs else None

    d.save(str(DOC))
    print("Inserted section 6.4 before '7. The frontend'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
