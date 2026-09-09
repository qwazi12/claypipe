"""Human review pages and their verdict loaders (SPEC §5).

Two self-contained HTML pages and the loaders that read what a human decided.
Shared rendering primitives live here so `canary_page` and `flag_page` stay
about their own content.

THE HARD CONSTRAINT, and the reason this module builds HTML by hand rather than
reaching for a template engine or a CSS framework: a page must be ONE file with
ZERO external references. It is opened from `file://` on an operator's machine,
possibly offline, possibly on a laptop that has never seen this repo. Every
byte it needs — styles, images, the form — is in the file.

SUBMISSION, and why there is no POST handler (see memory.md D21):
SPEC §5 says "No web server", so there is nothing to POST to. The form is a
real HTML form with real `name=` attributes and `method="get"` pointed at the
page itself, so submitting it puts the whole decision in the address bar as a
form-encoded query string. The operator copies that one line back to the CLI.
That path needs no JavaScript, no server and no network, and works in a
text-mode browser or with a screen reader. JavaScript, when present, only adds
convenience (a copy button and a JSON download); persistence never depends on it.
"""

from __future__ import annotations

import base64
import html
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Field-name separator for form encoding. Frame names contain "." (f_00001.png)
# but never ":", so ":" is what splits a nested field name unambiguously.
FIELD_SEP = ":"

__all__ = [
    "FIELD_SEP",
    "Caveat",
    "data_uri",
    "esc",
    "find_decisions",
    "page_shell",
    "PAGE_CSS",
]


def esc(value: object) -> str:
    """HTML-escape anything, including quotes, for attribute-safe output."""
    return html.escape(str(value), quote=True)


def data_uri(path: Path) -> str:
    """Inline a file as a `data:` URI.

    base64 rather than a `blob:` URL because the page is opened over `file://`,
    where blob URLs are not addressable.
    """
    mime, _ = mimetypes.guess_type(path.name)
    if mime is None:
        mime = "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


@dataclass(frozen=True)
class Caveat:
    """A warning rendered at the top of a page, quoting a memory.md decision."""

    number: str
    headline: str
    body: str


def find_decisions(memory_text: str, numbers: Iterable[str]) -> list[Caveat]:
    """Pull decision entries out of memory.md by D-number.

    memory.md writes decisions as `- **D<n> (date) headline...**`, and older
    guidance assumed `### D<n>` headings. Both forms are accepted so the page
    keeps working whichever way the file is maintained (memory.md D23).
    """
    wanted = list(numbers)
    found: dict[str, Caveat] = {}
    lines = memory_text.splitlines()

    for index, line in enumerate(lines):
        stripped = line.strip()
        for number in wanted:
            if number in found:
                continue
            is_bullet = stripped.startswith(f"- **{number} ") or stripped.startswith(f"- **{number}(")
            is_heading = stripped.startswith(f"### {number} ") or stripped == f"### {number}"
            if not (is_bullet or is_heading):
                continue
            body: list[str] = [stripped.lstrip("- ").lstrip("#").strip()]
            for follow in lines[index + 1 :]:
                nxt = follow.strip()
                if not nxt or nxt.startswith("- **D") or nxt.startswith("###") or nxt.startswith("## "):
                    break
                body.append(nxt)
            text = " ".join(body).replace("**", "").strip()
            headline = text.split(".")[0].strip()
            found[number] = Caveat(number=number, headline=headline, body=text)
    return [found[n] for n in wanted if n in found]


# Deliberately plain. No web fonts, no CDN, no icon set — everything here is
# either a system font stack or a colour.
PAGE_CSS = """
:root{--ink:#16181d;--muted:#5b6270;--line:#d7dbe2;--bg:#f6f7f9;--card:#fff;
--warn-bg:#fff4d6;--warn-line:#e0a800;--ok:#1a7f4b;--mid:#b5710a;--bad:#b3261e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
header{background:var(--card);border-bottom:1px solid var(--line);padding:20px 24px;}
h1{margin:0 0 4px;font-size:20px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
main{padding:24px;max-width:1100px;margin:0 auto}
.caveat{background:var(--warn-bg);border:1px solid var(--warn-line);
border-left-width:5px;border-radius:6px;padding:14px 16px;margin:0 0 20px}
.caveat h2{margin:0 0 6px;font-size:14px;text-transform:uppercase;letter-spacing:.06em}
.caveat p{margin:6px 0 0;font-size:13px}
.caveat code{background:rgba(0,0,0,.06);padding:1px 5px;border-radius:3px}
.cards{display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.card img{display:block;width:100%;height:auto;background:#222}
.card .body{padding:12px 14px}
.card h3{margin:0 0 8px;font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.metrics{width:100%;border-collapse:collapse;font-size:12px;margin:0 0 10px}
.metrics td{padding:2px 0;border-bottom:1px solid var(--line)}
.metrics td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.miss{color:var(--bad);font-weight:600}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;
font-weight:700;letter-spacing:.04em}
.pill.PASS{background:#e4f5eb;color:var(--ok)}
.pill.BORDERLINE{background:#fdf0dc;color:var(--mid)}
.pill.FAIL{background:#fbe4e2;color:var(--bad)}
.pill.UNSCORED{background:#eceef2;color:var(--muted)}
fieldset{border:0;padding:0;margin:0 0 8px}
fieldset label{display:block;padding:4px 0;font-size:13px;cursor:pointer}
input[type=text]{width:100%;padding:7px 9px;border:1px solid var(--line);
border-radius:5px;font:inherit;font-size:13px}
textarea{width:100%;min-height:70px;padding:8px 10px;border:1px solid var(--line);
border-radius:5px;font:inherit;font-size:13px;resize:vertical}
.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:18px 20px;margin:0 0 20px}
.panel h2{margin:0 0 12px;font-size:15px}
.row{margin:0 0 14px}
.row > label{display:block;font-size:12px;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted);margin:0 0 5px}
button{background:var(--ink);color:#fff;border:0;border-radius:6px;
padding:11px 22px;font:inherit;font-weight:600;cursor:pointer}
.hint{font-size:12px;color:var(--muted);margin:10px 0 0}
"""


def page_shell(*, title: str, subtitle: str, caveats: list[Caveat], body: str) -> str:
    """Wrap page content in the shared, fully inlined document."""
    caveat_html = ""
    for caveat in caveats:
        caveat_html += (
            '<section class="caveat">'
            f"<h2>Caveat &mdash; {esc(caveat.number)}</h2>"
            f"<p><strong>{esc(caveat.headline)}</strong></p>"
            f"<p>{esc(caveat.body)}</p>"
            f"<p>Recorded in <code>memory.md</code> as <code>{esc(caveat.number)}</code>.</p>"
            "</section>"
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title>"
        f"<style>{PAGE_CSS}</style>"
        "</head><body>"
        f"<header><h1>{esc(title)}</h1><div class=\"sub\">{esc(subtitle)}</div></header>"
        f"<main>{caveat_html}{body}</main>"
        "</body></html>\n"
    )
