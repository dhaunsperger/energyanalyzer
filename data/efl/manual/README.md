# Hand-supplied EFLs

Drop EFL PDFs here for retailers automation cannot reach, and they will be
parsed on every refresh exactly like downloaded ones.

Files in this directory are **never deleted**. A refresh wipes `data/efl/` and
re-downloads everything, which is only safe for files a fetcher can restore --
a PDF you saved by hand is gone for good. That is not hypothetical: Ambit's
EFLs were saved here by hand after its WAF began refusing every client, and the
next refresh deleted them.

Use this for:

- **Ambit** -- `shopping.ambitenergy.com` returns "Blocked by WAF" to every
  client, httpx and a real Chromium alike. Open the plan's EFL in your own
  browser and save the PDF here.
- Any REP behind a captcha, or whose EFL only appears inside a logged-in funnel.

Name the file however you like; the parser reads the retailer and plan name out
of the document itself.
