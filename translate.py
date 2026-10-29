#!/usr/bin/env python3
"""Translates academic papers (PDF) into another language while preserving the page layout.

Usage:
    Put your Claude API key in API_KEY.TXT (or set the ANTHROPIC_API_KEY environment variable)
    python translate.py                           # all PDFs in articles/ -> translations/
    python translate.py paper.pdf                 # -> paper_<TARGET>.pdf
    python translate.py --source en --target de   # language pair (or set it in SETTINGS.TXT)
    python translate.py *.pdf --output out/       # several files

How it works:
  1. PyMuPDF extracts every text block with its position, font, size, colour and
     inline bold/italic/superscript/subscript formatting.
  2. The full paper is given to the model as context: first a paper-specific
     terminology glossary is built, then each page is translated with that glossary
     and the full-text context (for consistent terminology).
  3. The original text is removed while keeping images and drawings, and the
     translation is placed in the same box with the same style (scaled down if needed).
  4. Translations are cached in translations/.cache/; an interrupted run resumes
     where it stopped and already translated pages are not paid for again.
"""

import argparse
import hashlib
import html
import json
import os
import re
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
import pymupdf

MODEL = "claude-opus-5-5"
BASE = Path(__file__).resolve().parent
INPUT_DIR = BASE / "articles"
OUTPUT_DIR = BASE / "translations"
SEGMENTS_PER_REQUEST = 40
CACHE_VERSION = 2  # bump when the translation rules change so old caches are not reused

# Language code: (English name, native name, the word for "References" in that language)
LANGS = {
    "en": ("English", "English", "References"),
    "tr": ("Turkish", "Türkçe", "Kaynakça"),
    "de": ("German", "Deutsch", "Literatur"),
    "fr": ("French", "Français", "Références"),
    "es": ("Spanish", "Español", "Referencias"),
    "it": ("Italian", "Italiano", "Bibliografia"),
    "pt": ("Portuguese", "Português", "Referências"),
    "nl": ("Dutch", "Nederlands", "Literatuur"),
    "pl": ("Polish", "Polski", "Bibliografia"),
    "sv": ("Swedish", "Svenska", "Referenser"),
    "el": ("Greek", "Ελληνικά", "Βιβλιογραφία"),
    "ru": ("Russian", "Русский", "Список литературы"),
    "uk": ("Ukrainian", "Українська", "Список літератури"),
    "az": ("Azerbaijani", "Azərbaycanca", "Ədəbiyyat"),
    "ar": ("Arabic", "العربية", "المراجع"),
    "fa": ("Persian", "فارسی", "منابع"),
    "he": ("Hebrew", "עברית", "מקורות"),
    "hi": ("Hindi", "हिन्दी", "संदर्भ"),
    "id": ("Indonesian", "Bahasa Indonesia", "Daftar Pustaka"),
    "vi": ("Vietnamese", "Tiếng Việt", "Tài liệu tham khảo"),
    "zh": ("Chinese (Simplified)", "简体中文", "参考文献"),
    "ja": ("Japanese", "日本語", "参考文献"),
    "ko": ("Korean", "한국어", "참고문헌"),
}
RTL = {"ar", "fa", "he", "ur"}

# Blocks after a references heading are not sent to the API (until an appendix heading appears)
REF_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s*|[IVX]+\.\s*)?(references|literature cited|bibliography|works cited|reference list|"
    r"cited literature|literatur|literaturverzeichnis|bibliographie|références(?: bibliographiques)?|referencias|"
    r"bibliografía|bibliografia|riferimenti(?: bibliografici)?|referências(?: bibliográficas)?|literatuur|"
    r"referenser|kaynakça|kaynaklar|список литературы|литература|список літератури|βιβλιογραφία|"
    r"المراجع|منابع|מקורות|参考文献|참고문헌|daftar pustaka|tài liệu tham khảo)\s*[:：]?\s*$", re.I)
BACK_HEADING = re.compile(r"^\s*(?:[A-Z]\.?\s+|\d+\.?\s*)?(appendix|appendices|supplementary|supporting information|"
                          r"anhang|annexe|apéndice|anexo|appendice|bijlage|ekler?|приложение|附录|付録|부록|ملحق)",
                          re.I)

MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|Symbol|Math|rsfs|wasy|esint", re.I)
SERIF_FONT = re.compile(r"Times|Serif|Roman|CMR|CMB|CMTI|LMRoman|Minion|Georgia|Garamond|Cambria|"
                        r"Palatino|Book|Charter|Utopia|STIX|Mincho|Nimbus.?Rom|Baskerville|Caslon", re.I)
SANS_FONT = re.compile(r"Arial|Helvetica|Sans|Calibri|Verdana|Myriad|Frutiger|Univers|Gill|Futura|"
                       r"Segoe|Roboto|Lato|Open.?Sans|Source.?Sans|CMSS", re.I)
MONO_FONT = re.compile(r"Mono|Courier|CMTT|Consol|Menlo|Inconsolata", re.I)

SYSTEM_PROMPT = """You are an expert academic translator who translates {src} scholarly articles into {tgt} for publication-quality academic use. The full text of the article you are translating is provided below for context.{detect}

Translation standards:
- Write in the formal academic register of {tgt}, following its standard spelling and punctuation norms and the conventions of {tgt} scientific writing (voice, tense, impersonal constructions). Keep the authors' first person where they use it rhetorically, rendered naturally and consistently.
- Convey the exact meaning; do not summarise, add, or omit content. Preserve hedging and certainty levels ("may", "suggests", "clearly").
- Use established {tgt} terminology of the field. Follow the terminology glossary provided below consistently. If a term has no widely accepted {tgt} equivalent, keep the source term (in italics markup if it was italic in the source) rather than inventing one.
- Use the standard {tgt} academic names for section headings and labels (Abstract, Keywords, Introduction, Methods, Results, Discussion, Conclusion, Acknowledgements, References, Appendix, Figure, Table, Equation, Section, Theorem, Proof, Definition) and the {tgt} conventions for abbreviations such as "et al.", "i.e.", "e.g.".
- Do NOT translate: person names, institution names, journal/conference names, citation markers (keep names and years; render "et al."-type abbreviations in the {tgt} convention), mathematical expressions and variables, numbers (keep the source digits and decimal notation so they match figures and tables), units, URLs, DOIs, e-mail addresses, code, software/dataset/model names.
- Bibliography entries in the reference list must be returned exactly unchanged.
- Running headers/footers, page numbers, copyright lines: translate only natural-language parts.

Lessons from reviewing earlier translations (follow strictly):
- One source term = one {tgt} term for the whole article, including headings, running headers and table-of-contents entries (a section heading and its TOC entry must be worded identically). Never render two different source terms with the same {tgt} word. Do not repeat parenthetical glosses or source-language originals; add one at most at the first occurrence if truly needed.
- Keep the field-specific meaning; avoid word-for-word calques that lose it, and words that shift, load or make the meaning ambiguous in {tgt}.
- Preserve the strength of claims (e.g. "countered" means disputed, not refuted).
- Render comparisons such as "versus/vs." with the construction that is natural in {tgt} for the context, not a literal word.
- Write natural {tgt}, not translationese: avoid calqued phrases (e.g. "as a function of", "global self-esteem" rendered word for word) and unnecessary loanwords where established native terms exist; never offer slash alternatives — choose one word.
- Place percent signs and other symbols according to {tgt} typographic conventions, without changing the values.
- Translate common nouns and demographic/ethnic labels; keep only proper names of institutions, journals, programmes and instruments.
{extra}
Input format:
- You receive a JSON list of text segments in reading order (one or more consecutive pages). Each segment is one text block of the page layout.
- Segments may contain the inline markup tags <b>, <i>, <sup>, <sub> and line breaks <br>. Keep these tags in the translation around the corresponding words, keep them balanced, keep every <br> at the corresponding position (they separate headings and paragraphs), and do not add any other tags. Keep HTML entities (&amp; &lt; &gt;) as entities.
- A segment marked "continues_in": "<id>" ends mid-sentence and the sentence continues in that later segment (next column or page; running headers may sit in between). Translate the whole sentence first, then split the {tgt} sentence at a natural phrase boundary: the first part goes to this segment, the rest to the continuation segment. Together the two parts must contain the complete meaning exactly once — check that every clause and the main verb are present and that nothing is repeated at the start of the continuation. Each part should be roughly proportional in length to its source part.
- Otherwise keep each segment's content in that segment; never move content between unrelated segments.
- Space on the page is limited: prefer concise, natural {tgt} over wordy paraphrase.
- If a segment needs no translation (e.g. a reference entry, a formula, a name), return it unchanged.

Return a translation for every segment id."""

# Extra rules for specific target languages (learned from reviewing Turkish translations)
TARGET_RULES = {
    "tr": """
Additional rules for Turkish:
- Follow TDK spelling and punctuation. Use the impersonal/passive academic style natural to Turkish scientific writing (e.g. "...incelenmiştir", "...önerilmektedir", "Bu çalışmada..."); report findings and the literature with -mıştır / -maktadır, not the narrative past (-dı).
- Standard renderings: Abstract -> Özet, Keywords -> Anahtar Kelimeler, Introduction -> Giriş, Related Work -> İlgili Çalışmalar, Method(s)/Methodology -> Yöntem, Results -> Bulgular, Discussion -> Tartışma, Conclusion(s) -> Sonuç, Acknowledgements -> Teşekkür, References -> Kaynakça, Appendix -> Ek, Figure -> Şekil, Table -> Tablo, Equation/Eq. -> Denklem, Section -> Bölüm, Algorithm -> Algoritma, Theorem -> Teorem, Proof -> İspat, Definition -> Tanım, "et al." -> "vd.", "i.e." -> "yani", "e.g." -> "örneğin".
- Examples of the general lessons: adolescence = "ergenlik", so puberty = "puberte" (not "ergenlik (puberte)"); peer "crowds" (Brown) -> "geniş akran grupları" (not "kalabalıklar"); parental "differential treatment" -> "farklı muamele" (not "ayrımcı muamele"); "peer victimization" -> "akran mağduriyeti" (not "zorbalık"); "thinness" -> "incelik / zayıf olma" (not the ambiguous "zayıflık"); "countered" -> "karşı çıkmak" (not "çürütmek"); "we suspect" -> "...düşünüyoruz" (not "kuşkulanıyoruz"); "versus" -> "ile", "ve" or "ya da" (e.g. "nitel ve nicel yöntemler", "evli ya da boşanmış"), never "karşı"; "as a function of" -> "...bağlı olarak"; "istikrarlı/kararlı", not "stabil"; never "uyuşturucu/madde".
- Percentages with the sign first: "%75", "%5–10".
- Native American -> Yerli Amerikalı, Latino -> Latin kökenli, e-mail -> e-posta; "adolescents/teenagers" -> "ergenler", "youth/young people" -> "gençler".
""",
}

GLOSSARY_PROMPT = """Prepare a terminology glossary for translating this article from {src} into {tgt} academic language. List the field-specific technical terms, key concepts, recurring expressions, and research-method terms used in the article (e.g. operationalize, moderate/mediate, longitudinal, self-report, heritability) — typically 30-100 entries — with their established {tgt} academic equivalents. Prefer equivalents used in {tgt} academic literature of this field and in its standard terminology resources{resources}. If a term is normally kept untranslated in {tgt} academic writing, give the source term as the equivalent.
Rules: give exactly one {tgt} equivalent per entry (no alternatives, slashes or parenthetical glosses); never map two different source terms to the same {tgt} word; choose words that keep the technical meaning without shifting or loading it, and that are unambiguous in {tgt}."""

GLOSSARY_RESOURCES = {"tr": " (Türkçe Bilim Terimleri, TÜBA/TDK usage)"}


def lang_name(code):
    """Turns a language code ("de") or a free-form language name ("Swahili") into the name given to the model."""
    if code == "auto":
        return "the source language"
    return LANGS[code][0] if code in LANGS else code


def build_prompts(src, tgt):
    names = {"src": lang_name(src), "tgt": lang_name(tgt)}
    system = SYSTEM_PROMPT.format(**names, extra=TARGET_RULES.get(tgt, ""),
                                  detect=" Detect the source language of the article yourself." if src == "auto" else "")
    glossary = GLOSSARY_PROMPT.format(**names, resources=GLOSSARY_RESOURCES.get(tgt, ""))
    return system, glossary


TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "translation": {"type": "string"}},
                "required": ["id", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

GLOSSARY_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
                "required": ["source", "target"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["field", "terms"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- extraction

def _style(span, line_size, baseline):
    font = span["font"]
    flags = span["flags"]
    small = span["size"] < 0.85 * line_size
    if flags & 1 or (small and span["origin"][1] < baseline - 0.15 * line_size):
        script = "sup"
    elif small and span["origin"][1] > baseline + 0.1 * line_size:
        script = "sub"
    else:
        script = None
    return (
        bool(flags & 16 or re.search(r"bold|black|heavy|semibold|CMBX", font, re.I)),
        bool(flags & 2 or re.search(r"italic|oblique|CMTI|CMMI", font, re.I)),
        script,
    )


def _runs_to_html(runs):
    out = []
    for (bold, italic, script), text in runs:
        t = html.escape(text, quote=False).replace("\n", "<br>")
        if script:
            t = f"<{script}>{t}</{script}>"
        if italic:
            t = f"<i>{t}</i>"
        if bold:
            t = f"<b>{t}</b>"
        out.append(t)
    return "".join(out)


def _font_family(span):
    font = span["font"]
    if MONO_FONT.search(font):
        return "monospace"
    if SANS_FONT.search(font):
        return "sans-serif"
    if SERIF_FONT.search(font) or span["flags"] & 4:
        return "serif"
    return "sans-serif"


def _make_segment(lines, sid, pno, page_rect, obstacles):
    """Builds a translation unit (segment) from a group of lines; returns None if there is nothing to translate."""
    spans = [s for l in lines for s in l["spans"] if s["text"].strip()]
    if not spans:
        return None
    plain = " ".join(s["text"] for s in spans)
    if sum(c.isalpha() for c in plain) < 2:
        return None
    total = sum(len(s["text"]) for s in spans)
    math_chars = sum(len(s["text"]) for s in spans if MATH_FONT.search(s["font"]))
    if math_chars > 0.4 * total:
        return None

    bbox = pymupdf.Rect()
    for l in lines:
        bbox |= l["bbox"]
    runs = []
    prev_line = None
    for line in lines:
        line_spans = [s for s in line["spans"] if s["text"]]
        if not line_spans:
            continue
        if runs:
            prev_style, prev_text = runs[-1]
            first = line_spans[0]["text"].lstrip()
            # in justified text one visual line can arrive as several pieces
            same_row = abs(line["bbox"][1] - prev_line["bbox"][1]) < 0.5 * line_spans[0]["size"]
            # the previous line ended before the right edge: end of a heading or paragraph
            short = prev_line["bbox"][2] < bbox.x1 - max(0.15 * bbox.width, 3 * line_spans[0]["size"])
            if same_row:
                if not prev_text.endswith(" "):
                    runs[-1] = (prev_style, prev_text + " ")
            elif short:
                runs[-1] = (prev_style, prev_text.rstrip() + "\n")
            elif prev_text.endswith("-") and first[:1].islower():
                runs[-1] = (prev_style, prev_text[:-1])  # end-of-line hyphenation
            elif not prev_text.endswith(" "):
                runs[-1] = (prev_style, prev_text + " ")
        main = max(line_spans, key=lambda s: len(s["text"].strip()) * s["size"])
        for s in line_spans:
            st = _style(s, main["size"], main["origin"][1])
            if runs and runs[-1][0] == st:
                runs[-1] = (st, runs[-1][1] + s["text"])
            else:
                runs.append((st, s["text"]))
        prev_line = line
    src_html = re.sub(r"\s+", " ", _runs_to_html(runs)).strip()
    src_html = re.sub(r"\s*<br>\s*", "<br>", src_html)

    # dominant style: the size / font / colour that carries the most characters
    weight = {}
    for s in spans:
        key = (round(s["size"], 1), _font_family(s), s["color"])
        weight[key] = weight.get(key, 0) + len(s["text"])
    size, family, color = max(weight, key=weight.get)

    # the block itself and what it contains are not obstacles; neighbours that merely touch its edge are
    others = [o for o in obstacles
              if not o.intersects(bbox) or abs(o & bbox) < 0.5 * min(abs(o), abs(bbox) or 1)]
    pw = page_rect.width
    x0, x1 = bbox.x0, bbox.x1
    # visual rows (in justified text one row can consist of several pieces)
    rows = []
    for l in lines:
        if rows and abs(l["bbox"][1] - rows[-1].y0) < 0.5 * size:
            rows[-1] |= l["bbox"]
        else:
            rows.append(pymupdf.Rect(l["bbox"]))
    if len(rows) > 1:
        gaps = [b.y0 - a.y0 for a, b in zip(rows, rows[1:]) if b.y0 - a.y0 > 0.5 * size]
        line_height = min(max(statistics.median(gaps) / size, 1.0), 2.0) if gaps else 1.2
        tol = 0.6 * size
        lefts_aligned = max(r.x0 for r in rows) - min(r.x0 for r in rows[1:] or rows) < tol
        rights_aligned = max(r.x1 for r in rows[:-1]) - min(r.x1 for r in rows[:-1]) < tol
        centers = [(r.x0 + r.x1) / 2 for r in rows]
        if rights_aligned and (lefts_aligned or len(rows) > 2):
            align = "justify"  # the first line may be indented
        elif max(centers) - min(centers) < tol and not lefts_aligned:
            align = "center"
        elif max(r.x1 for r in rows) - min(r.x1 for r in rows) < tol and not lefts_aligned:
            align = "right"
        else:
            align = "left"
    else:
        line_height = 1.2
        centered = abs((bbox.x0 + bbox.x1) / 2 - pw / 2) < 0.04 * pw and bbox.width < 0.75 * pw
        align = "center" if centered else "left"
        # single-line heading/label: may widen up to the nearest object on the same row
        row = [o for o in others if o.y0 < bbox.y1 - 1 and o.y1 > bbox.y0 + 1]
        right = min([o.x0 for o in row if o.x0 >= bbox.x1 - 1] + [page_rect.x1 - 0.06 * pw]) - 0.5 * size
        left = max([o.x1 for o in row if o.x1 <= bbox.x0 + 1] + [page_rect.x0 + 0.06 * pw]) + 0.5 * size
        if align == "center":
            room = max(0, min(bbox.x0 - left, right - bbox.x1))
            x0, x1 = bbox.x0 - room, bbox.x1 + room
        else:
            x1 = max(bbox.x1, right)

    # translations are often longer: the block may grow down to the next object below it
    below = [max(o.y0, bbox.y1) for o in others
             if o.y0 >= bbox.y1 - 0.5 * size and o.y1 > bbox.y1 and o.x0 < x1 - 1 and o.x1 > x0 + 1]
    bottom_limit = page_rect.y1 - 0.04 * page_rect.height
    y1 = max(bbox.y1, min(below + [bottom_limit]) - 0.3 * size)
    if len(rows) == 1:
        # single line: the box must be at least one line high (text starts at the top, overflow is invisible)
        y1 = max(y1, bbox.y0 + line_height * size + 1)

    return {
        "id": sid,
        "page": pno,
        "bbox": list(bbox),
        "box": [x0, bbox.y0, x1, y1],
        "html": src_html,
        "size": size,
        "family": family,
        "color": f"#{color:06x}",
        "line_height": round(line_height, 2),
        "align": align,
    }


def extract_segments(doc):
    """Returns the translatable text blocks of every page with their style information."""
    segments = []
    for pno, page in enumerate(doc):
        data = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
        blocks = [b for b in data["blocks"] if b["type"] == 0 and b["lines"]]
        obstacles = [pymupdf.Rect(b["bbox"]) for b in data["blocks"]]
        obstacles += [pymupdf.Rect(l["bbox"]) for b in blocks for l in b["lines"]]  # for table cells
        obstacles += [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
        # blocks with wide gaps, such as table rows, are handled cell by cell
        groups = []
        for bno, block in enumerate(blocks):
            lines = block["lines"]
            # rotated text (e.g. the arXiv side stamp) is left as it is
            if any(abs(l["dir"][0] - 1) > 0.01 for l in lines):
                continue
            tabular = False
            for i, (a, b) in enumerate(zip(lines, lines[1:])):
                if not a["spans"] or abs(b["bbox"][1] - a["bbox"][1]) > 2:
                    continue
                if b["bbox"][0] - a["bbox"][2] > 1.5 * a["spans"][0]["size"]:
                    # list labels such as "[12]", "1.", "•" at the start of a row are not table cells
                    row_start = i == 0 or abs(lines[i - 1]["bbox"][1] - a["bbox"][1]) > 2
                    label = "".join(sp["text"] for sp in a["spans"]).strip()
                    if not (row_start and len(label) <= 5):
                        tabular = True
                        break
            if tabular:
                groups += [(f"p{pno + 1}b{bno}l{i}", [l]) for i, l in enumerate(lines)]
            else:
                groups.append((f"p{pno + 1}b{bno}", lines))
        for sid, lines in groups:
            seg = _make_segment(lines, sid, pno, page.rect, obstacles)
            if seg:
                segments.append(seg)
    return segments


def _plain(seg):
    return html.unescape(re.sub(r"<[^>]+>", " ", seg["html"])).strip()


def split_references(segments):
    """Separates the reference list. Returns (blocks to translate, reference headings, skipped blocks)."""
    body, headings, skipped = [], [], []
    in_refs = False
    for s in segments:
        text = _plain(s)
        if REF_HEADING.match(text):
            in_refs = True
            headings.append(s)
        elif in_refs and len(text) < 80 and BACK_HEADING.match(text):
            in_refs = False
            body.append(s)
        elif in_refs:
            skipped.append(s)
        else:
            body.append(s)
    return body, headings, skipped


def ref_heading_local(src, tgt):
    """Translates headings such as "References" / "LITERATURE CITED" into the target language, keeping any
    number and formatting. Returns None if the target language is not in the table (the model translates it)."""
    if tgt not in LANGS:
        return None
    m = REF_HEADING.match(html.unescape(re.sub(r"<[^>]+>", "", src)))
    word = LANGS[tgt][2].upper() if m.group(1).isupper() else LANGS[tgt][2]
    return re.sub(re.escape(m.group(1)).replace(r"\ ", r"\s+"), word, src, count=1, flags=re.I)


def document_text(segments):
    pages = {}
    for s in segments:
        pages.setdefault(s["page"], []).append(_plain(s))
    return "\n\n".join(f"[Page {p + 1}]\n" + "\n".join(t) for p, t in sorted(pages.items()))


def mark_continuations(segments):
    """Marks blocks whose sentence continues in the next body block (next column or page)."""
    for i, s in enumerate(segments):
        text = _plain(s)
        if len(text) < 60 or re.search(r"[.?!:;)\]\"”’。！？．」』]\s*$", text) or re.fullmatch(r"(<b>.*</b>)", s["html"]):
            continue
        for n in segments[i + 1:]:
            t = _plain(n)
            # short blocks such as running headers and page numbers are skipped
            if len(t) >= 40:
                # the continuation starts with a lowercase letter, a digit or a caseless script,
                # or the previous block breaks off after "&", "(", "and" and the like
                first = t.lstrip("“\"‘'«„")[:1]
                if first.islower() or first.isdigit() or (first.isalpha() and not first.isupper()) \
                        or re.search(r"(?:[&(,–-]|\b(?:and|or|the|of|in|by|to|a|an|with))\s*$", text):
                    s["continues_in"] = n["id"]
                break


# ---------------------------------------------------------------- translation

def base_file(name):
    """Finds a file in the program folder case-insensitively (on Linux API_KEY.TXT and api_key.txt are
    different files)."""
    for f in BASE.iterdir():
        if f.name.lower() == name.lower():
            return f
    return BASE / name


def load_key():
    """Reads the Claude API key from the environment or from API_KEY.TXT.
    The file may contain a "CLAUDE_API_KEY = sk-ant-..." line or just the key itself."""
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    key_file = base_file("API_KEY.TXT")
    if not key and key_file.exists():
        m = re.search(r"sk-ant-[\w-]+", key_file.read_text(encoding="utf-8-sig"))
        key = m and m.group(0)
    if not key:
        sys.exit("No API key found. Put your Claude API key in API_KEY.TXT.")
    return key


class Translator:
    def __init__(self, model, effort, context, cache_path, src, tgt):
        self.client = anthropic.Anthropic(api_key=load_key(), max_retries=5)
        self.system_prompt, self.glossary_prompt = build_prompts(src, tgt)
        self.tgt_name = lang_name(tgt)
        self.model = model
        self.effort = effort
        self.context = context
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.cache = {"glossary": None, "segments": {}}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _save(self):
        with self.lock:
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.cache_path)

    def _call(self, system, user, schema):
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError(f"the model declined the request: {msg.stop_details}")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("the output hit the max_tokens limit")
        text = next(b.text for b in msg.content if b.type == "text")
        u = msg.usage
        print(f"    [tokens] input={u.input_tokens} cache-read={u.cache_read_input_tokens or 0} "
              f"cache-write={u.cache_creation_input_tokens or 0} output={u.output_tokens}", flush=True)
        return json.loads(text)

    def build_glossary(self):
        if self.cache.get("glossary"):
            return
        print("  Building the terminology glossary...", flush=True)
        system = [{"type": "text", "text": f"<article>\n{self.context}\n</article>",
                   "cache_control": {"type": "ephemeral"}}]
        self.cache["glossary"] = self._call(system, self.glossary_prompt, GLOSSARY_SCHEMA)
        self._save()
        g = self.cache["glossary"]
        print(f"  Field: {g['field']} — {len(g['terms'])} terms", flush=True)

    def _system(self):
        g = self.cache["glossary"]
        glossary = "\n".join(f"- {t.get('source', t.get('en'))} → {t.get('target', t.get('tr'))}" for t in g["terms"])
        return [{
            "type": "text",
            "text": f"{self.system_prompt}\n\nField: {g['field']}\n\n<glossary>\n{glossary}\n</glossary>\n\n"
                    f"<article>\n{self.context}\n</article>",
            "cache_control": {"type": "ephemeral"},
        }]

    @staticmethod
    def key(seg):
        return hashlib.sha1(seg["html"].encode("utf-8")).hexdigest()[:16] + ":" + seg["id"]

    def translate_chunk(self, chunk):
        payload = [{"id": s["id"], "text": s["html"], **({"continues_in": s["continues_in"]}
                                                          if "continues_in" in s else {})} for s in chunk]
        first, last = chunk[0]["page"] + 1, chunk[-1]["page"] + 1
        pages = f"page {first}" if first == last else f"pages {first}-{last}"
        user = (f"Translate the following segments from {pages} into {self.tgt_name}.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=1))
        result = self._call(self._system(), user, TRANSLATION_SCHEMA)
        got = {t["id"]: t["translation"] for t in result["translations"]}
        # omission check: retry once if a long block came back much shorter than is typical for this request
        # (relative, because the length ratio depends on the language pair)
        ratio = {s["id"]: len(re.sub(r"<[^>]+>", "", got[s["id"]])) / len(_plain(s))
                 for s in chunk if s["id"] in got and "continues_in" not in s and len(_plain(s)) >= 80}
        typical = statistics.median(ratio.values()) if len(ratio) >= 3 else 1.0
        short = [i for i, r in ratio.items() if r < 0.75 * typical]
        if short:
            print(f"    warning: {', '.join(short)} came back short (possible omission), retrying", flush=True)
            note = ("\n\nNote: a previous attempt left these segments much shorter than the source, which suggests "
                    f"omitted content: {', '.join(short)}. Make sure every clause is translated.")
            retry = self._call(self._system(), user + note, TRANSLATION_SCHEMA)
            got.update({t["id"]: t["translation"] for t in retry["translations"]})
        missing = 0
        with self.lock:
            for s in chunk:
                if s["id"] in got:
                    self.cache["segments"][self.key(s)] = got[s["id"]]
                else:
                    missing += 1
        self._save()
        if missing:
            print(f"    warning: {pages}: {missing} blocks were not translated and keep the original text", flush=True)

    def translate_all(self, segments, workers):
        todo = [s for s in segments if self.key(s) not in self.cache["segments"]]
        if not todo:
            print("  Everything is cached; no API calls needed.")
            return
        # requests are split at page ends; if a sentence continues on the next page, the continuation goes
        # into the same request so the model sees the whole sentence
        chunks, cur = [], []
        for s in todo:
            if cur and cur[-1]["page"] != s["page"] and "continues_in" not in cur[-1]:
                chunks.append(cur)
                cur = []
            cur.append(s)
            if len(cur) >= SEGMENTS_PER_REQUEST and "continues_in" not in s:
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
        print(f"  Translating {len(todo)} blocks in {len(chunks)} requests...", flush=True)

        def run(i, chunk):
            print(f"  -> page {chunk[0]['page'] + 1} ({i}/{len(chunks)})", flush=True)
            try:
                self.translate_chunk(chunk)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError):
                raise  # key or credit problem: no point in trying the other pages
            except Exception as e:  # an error on one page should not stop the whole job
                print(f"    ERROR on page {chunk[0]['page'] + 1}: {e} — it will be retried on the next run",
                      flush=True)

        # the first request runs alone: it writes the context to the prompt cache, the others read it
        run(1, chunks[0])
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(run, i + 2, c) for i, c in enumerate(chunks[1:])]
            for f in as_completed(futures):
                f.result()

    def get(self, seg):
        return self.cache["segments"].get(self.key(seg))


# ---------------------------------------------------------------- rendering

FONT_DIRS = [Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"),
             Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/msttcorefonts")]
FONT_FILES = {  # family: regular, bold, italic, bold italic (macOS names, then Windows names)
    "serif": ["Times New Roman.ttf", "Times New Roman Bold.ttf", "Times New Roman Italic.ttf",
              "Times New Roman Bold Italic.ttf", "times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"],
    "sans-serif": ["Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf",
                   "arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"],
    "monospace": ["Courier New.ttf", "Courier New Bold.ttf", "Courier New Italic.ttf",
                  "Courier New Bold Italic.ttf", "cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"],
}


def font_setup():
    """Loads system fonts that resemble common academic fonts and cover extended Latin, Greek and Cyrillic.
    Falls back to PyMuPDF's built-in fonts (which also cover CJK, Arabic, Devanagari and more)."""
    for d in FONT_DIRS:
        if not d.is_dir():
            continue
        faces = []
        for family, files in FONT_FILES.items():
            for variants in (files[:4], files[4:]):
                if all((d / f).exists() for f in variants):
                    alias = "f-" + family
                    for f, (weight, style) in zip(variants, [("normal", "normal"), ("bold", "normal"),
                                                             ("normal", "italic"), ("bold", "italic")]):
                        faces.append(f"@font-face {{font-family:{alias}; src:url('{f}'); "
                                     f"font-weight:{weight}; font-style:{style};}}")
                    break
        if faces:
            return pymupdf.Archive(str(d)), " ".join(faces)
    return None, ""


def render(doc, segments, translations, out_path, force=False, rtl=False):
    by_page = {}
    for s in segments:
        tr = translations(s)
        if tr is None or (not force and tr.strip() == s["html"].strip()):
            continue  # untranslated or unchanged blocks keep the original text
        by_page.setdefault(s["page"], []).append((s, tr))

    archive, font_css = font_setup()
    shrunk = 0
    for pno, items in by_page.items():
        page = doc[pno]
        for s, _ in items:
            page.add_redact_annot(pymupdf.Rect(s["bbox"]))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        for s, tr in items:
            family = f"f-{s['family']}, {s['family']}" if f"f-{s['family']}" in font_css else s["family"]
            # with direction:rtl MuPDF treats "left" as the line start (right side), so no mapping is needed
            align = s["align"]
            css = (f"{font_css} body, p {{margin:0; padding:0;}} "
                   f"body {{font-family:{family}; font-size:{s['size']}pt; color:{s['color']}; "
                   f"line-height:{s['line_height']}; text-align:{align};"
                   f"{' direction:rtl;' if rtl else ''}}} "
                   f"sup, sub {{font-size:70%;}}")
            rect = pymupdf.Rect(s["box"])
            rect.y0 -= (s["line_height"] - 1) * s["size"] / 2  # the top half-leading added by CSS line-height
            rect.x1 += 1  # avoids needless line wraps caused by rounding
            _, scale = page.insert_htmlbox(rect, f"<p>{tr}</p>", css=css, scale_low=0, archive=archive)
            if scale < 0.999:
                shrunk += 1

    doc.subset_fonts()
    doc.save(out_path, garbage=3, deflate=True)
    return shrunk


# ---------------------------------------------------------------- main

def process(pdf_path, out_dir, args):
    out_dir = out_dir or pdf_path.parent
    out_path = out_dir / f"{pdf_path.stem}{output_suffix(args.target)}.pdf"
    (out_dir / ".cache").mkdir(exist_ok=True)
    cache_path = (out_dir / ".cache" /
                  f"{pdf_path.stem}.{args.model}.{args.source}-{args.target}.v{CACHE_VERSION}.json")
    print(f"\n== {pdf_path.name}")

    doc = pymupdf.open(pdf_path)
    segments = extract_segments(doc)
    if not segments:
        print("  No text found (probably a scanned PDF; it needs OCR first). Skipping.")
        return
    print(f"  {doc.page_count} page{'s' if doc.page_count != 1 else ''}, {len(segments)} text blocks")

    if args.dry_run:
        # re-typesets the original text to test the layout without using the API
        shrunk = render(doc, segments, lambda s: s["html"], out_path, force=True)
    else:
        body, ref_headings, skipped = split_references(segments)
        if ref_headings:
            pages = sorted({s["page"] + 1 for s in skipped})
            span = f", pages {pages[0]}–{pages[-1]}" if pages else ""
            print(f"  References start on page {ref_headings[0]['page'] + 1}: {len(skipped)} blocks{span} "
                  f"are left untranslated and not sent to the API")
        mark_continuations(body)
        context = document_text(body)
        if len(context) > 2_500_000:
            sys.exit("  The document is too long for the context window; please split it.")
        tr = Translator(args.model, args.effort, context, cache_path, args.source, args.target)
        for h in ref_headings:  # translated without the API; unknown target languages go to the model
            local = ref_heading_local(h["html"], args.target)
            if local:
                tr.cache["segments"][tr.key(h)] = local
            else:
                body.append(h)
        tr.build_glossary()
        tr.translate_all(body, args.parallel)
        # identical text already translated elsewhere (e.g. running headers on reference pages) is reused
        same = {s["html"]: tr.get(s) for s in body if tr.get(s)}
        shrunk = render(doc, segments, lambda s: tr.get(s) or same.get(s["html"]), out_path,
                        rtl=args.target in RTL)
    print(f"  Saved: {out_path}")
    if shrunk:
        print(f"  Note: text was scaled down to fit in {shrunk} blocks.")


def output_suffix(tgt):
    return "_" + (tgt.upper() if tgt in LANGS else re.sub(r"\W+", "", tgt)[:12])


def read_settings():
    """Reads the "source = en" / "target = tr" lines of SETTINGS.TXT."""
    f = base_file("SETTINGS.TXT")
    if not f.exists():
        return {}
    return {k.lower(): v.strip() for k, v in
            re.findall(r"^[ \t]*(source|target)[ \t]*[=:][ \t]*([^#\n]*?)[ \t]*(?:#.*)?$",
                       f.read_text(encoding="utf-8-sig"), re.M | re.I)}


def language_list():
    return "\n".join(f"  {code:4} {native} ({name})" for code, (name, native, _) in LANGS.items())


def main():
    ap = argparse.ArgumentParser(description="Translates academic PDFs into another language, preserving the layout.")
    ap.add_argument("pdf", nargs="*", type=Path, help="PDF file(s) to translate (default: everything in articles/)")
    ap.add_argument("--output", type=Path, help="output folder (default: translations/, or the PDF's own folder)")
    ap.add_argument("--source", help="language of the paper: a language code or 'auto' (default: SETTINGS.TXT, else auto)")
    ap.add_argument("--target", help="language to translate into: a language code (default: SETTINGS.TXT)")
    ap.add_argument("--languages", action="store_true", help="list the supported language codes")
    ap.add_argument("--model", default=MODEL, help=f"Claude model (default: {MODEL})")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"],
                    help="how thoroughly the model thinks (default: high)")
    ap.add_argument("--parallel", type=int, default=4, help="number of concurrent requests (default: 4)")
    ap.add_argument("--force", action="store_true", help="re-create PDFs that were already translated")
    ap.add_argument("--dry-run", action="store_true", help="test the layout without calling the API")
    args = ap.parse_args()

    if args.languages:
        print("Language codes (for a language not listed, its English name also works, e.g. Swahili):")
        print(language_list())
        return
    settings = read_settings()
    args.source = (args.source or settings.get("source") or "auto").strip()
    args.target = (args.target or settings.get("target") or "").strip()
    if not args.target:
        sys.exit("No target language set. Write it in SETTINGS.TXT (e.g. target = en) or use --target.")
    args.source = args.source.lower() if args.source.lower() in LANGS or args.source.lower() == "auto" else args.source
    args.target = args.target.lower() if args.target.lower() in LANGS else args.target
    if args.target == "auto":
        sys.exit("The target language cannot be 'auto'. Set it in SETTINGS.TXT.")
    if args.source == args.target:
        sys.exit(f"Source and target language are the same ({args.target}). Check SETTINGS.TXT.")
    for code in (args.source, args.target):
        if code not in LANGS and code != "auto":
            print(f"Note: '{code}' is not in the list; it is passed to the model by name. See --languages.")
    print(f"Translating: {lang_name(args.source) if args.source != 'auto' else 'auto-detected language'}"
          f" -> {lang_name(args.target)}")

    if not args.pdf:
        INPUT_DIR.mkdir(exist_ok=True)
        args.output = args.output or OUTPUT_DIR
        args.pdf = sorted(p for p in INPUT_DIR.iterdir() if p.suffix.lower() == ".pdf")
        if not args.pdf:
            sys.exit(f"No PDFs in {INPUT_DIR}. Put the papers you want to translate there.")
        out = lambda p: args.output / f"{p.stem}{output_suffix(args.target)}.pdf"
        done = [p for p in args.pdf if not args.force and out(p).exists()
                and out(p).stat().st_mtime > p.stat().st_mtime]
        if done:
            print(f"Already translated, skipping ({len(done)}): " + ", ".join(p.name for p in done))
        args.pdf = [p for p in args.pdf if p not in done]
        if not args.pdf:
            print("Nothing new to translate.")
        else:
            print(f"Papers to translate ({len(args.pdf)}): " + ", ".join(p.name for p in args.pdf))

    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    for p in args.pdf:
        if not p.exists():
            print(f"Not found: {p}")
            continue
        try:
            process(p, args.output, args)
        except anthropic.AuthenticationError:
            sys.exit("\nERROR: The API key is not valid. Delete API_KEY.TXT, run the program again "
                     "and paste the key again.")
        except anthropic.PermissionDeniedError as e:
            sys.exit(f"\nERROR: This API key is not allowed to do this: {e.message}")
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower():
                sys.exit("\nERROR: Your Claude account has no credit left. Add credit under Billing at "
                         "console.anthropic.com, then run the program again.")
            sys.exit(f"\nERROR: The API rejected the request: {e.message}")
        except anthropic.APIConnectionError:
            sys.exit("\nERROR: Could not connect to the internet. Check your connection and run the program "
                     "again; finished parts are kept and the job resumes where it stopped.")


if __name__ == "__main__":
    main()
