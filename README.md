# Academic PDF Translator

Translates academic papers (PDF) into another language while keeping the page layout:
columns, headings, figures, tables, footnotes and bold/italic/superscript/subscript
formatting stay where they were. Translation is done by **Claude Opus 5.5**
(`claude-opus-5-5`) with rules for academic register and consistent terminology.

- Any language pair: 23 languages built in, any other language by name
- Right-to-left (Arabic, Persian, Hebrew) and non-Latin scripts (Chinese, Japanese,
  Korean, Hindi, Cyrillic, Greek)
- The reference list is detected and **not** sent to the API, so it costs nothing
- Interrupted runs resume where they stopped; nothing is paid for twice

## Quick start (Windows)

1. Download and extract the folder (right-click the zip → *Extract All*).
   Do not run it from inside the zip.
2. Get a Claude API key: sign up at <https://console.anthropic.com>, add credit under
   **Billing**, then create a key under **API Keys**. The key starts with `sk-ant-`
   and is shown only once. Keep it private.
3. Put the PDFs you want to translate into the **articles** folder.
4. Double-click **TRANSLATE.bat**.
   - The first run installs everything it needs (a few minutes, no Python required)
     and asks for your API key once. Paste it with a right-click and press Enter.
   - If Windows shows *"Windows protected your PC"*, click *More info* → *Run anyway*.
5. When it finishes, the **translations** folder opens. Each translation is saved as
   `<name>_<TARGET>.pdf`, e.g. `paper_TR.pdf`.

## Quick start (macOS / Linux)

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r REQUIREMENTS.TXT
echo "CLAUDE_API_KEY = sk-ant-..." > API_KEY.TXT     # or: export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python translate.py                       # translates everything in articles/
```

## Choosing the languages

Edit the two lines in **SETTINGS.TXT**:

```
source = auto    # language of the papers; "auto" detects it
target = en      # language to translate into
```

or pass them on the command line: `translate.py --source en --target de`.

Built-in codes (`translate.py --languages`): en, tr, de, fr, es, it, pt, nl, pl, sv, el,
ru, uk, az, ar, fa, he, hi, id, vi, zh, ja, ko. For any other language, write its
English name (e.g. `target = Swahili`).

The general translation rules apply to every language; Turkish has extra rules
learned from reviewing real translations. Rules for other languages can be added to
`TARGET_RULES` in `translate.py`.

## Command-line options

| Option | Meaning |
|---|---|
| `translate.py paper.pdf` | translate a single file |
| `--source`, `--target` | language pair (overrides SETTINGS.TXT) |
| `--output DIR` | output folder |
| `--force` | re-create PDFs that were already translated |
| `--dry-run` | test the layout without calling the API |
| `--model claude-fable-5-1` | stronger model, about 2.5× the price |
| `--effort xhigh` / `max` | more careful, slower and more expensive |
| `--parallel N` | concurrent requests (default 4) |

## Cost

You pay Anthropic directly through your own API key. A 20-page paper (English →
Turkish, references excluded) cost about **$2** with the default settings. Longer
papers cost proportionally more.

## How it works

1. PyMuPDF extracts every text block with its position, font, size and inline formatting.
2. The model builds a terminology glossary for the paper, then translates it in
   page batches with the full text as context. Sentences that run across a page
   break are sent together so nothing is lost or repeated.
3. The original text is removed (images and drawings are kept) and the translation
   is typeset in the same box with the same style. If it is longer, it first uses the
   free space below the block and is scaled down only if needed.
4. Results are cached in `translations/.cache/`.

## Limitations

- Scanned PDFs (images of pages) need OCR first; the text must be selectable.
- Text embedded inside figure images is not translated.
- The layout follows the source paper; for right-to-left languages the column order
  is not mirrored.
