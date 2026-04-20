#!/usr/bin/env python3
"""Sync Google Form submissions into the Muzan archive as PRs.

Flow per cron run:
  1. Fetch form-responses sheet as CSV (SHEET_CSV_URL).
  2. Load state (.automation/state.json) to skip already-processed rows.
  3. For each new row:
       - Find the submitted URL; skip if already present in index.html.
       - HEAD/GET the URL to confirm it resolves.
       - Ask Claude whether it is a genuine piece by Muzan Alneel and,
         if so, to produce a normalized entry matching the ITEMS schema.
       - If accepted: create branch, insert entry into index.html, push,
         open a PR.
       - Mark row as processed regardless of accept/reject.
  4. Commit updated state.json directly to main.
"""

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
STATE_PATH = REPO_ROOT / ".automation" / "state.json"

VALID_TYPES = {"article", "interview", "report", "book", "video"}


def fetch_csv(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "muzan-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def row_hash(row: dict) -> str:
    h = hashlib.sha256()
    for k in sorted(row.keys()):
        h.update(f"{k}={row[k]}\n".encode("utf-8"))
    return h.hexdigest()[:12]


def find_submitted_url(row: dict) -> str | None:
    for v in row.values():
        if v and re.match(r"https?://", v.strip()):
            return v.strip()
    return None


def extract_existing_urls() -> set[str]:
    text = INDEX_HTML.read_text(encoding="utf-8")
    return {m.group(1).strip() for m in re.finditer(r'url:\s*"([^"]+)"', text)}


def check_url_reachable(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; muzan-archive/1.0)"}
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return f"HEAD {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 501):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return f"GET {resp.status}"
            except Exception as e2:
                return f"unreachable ({type(e2).__name__}: {e2})"
        return f"HTTP {e.code}"
    except Exception as e:
        return f"unreachable ({type(e).__name__}: {e})"


def assess_with_claude(row: dict, url_status: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""You are validating a submission to an archive of Muzan Alneel's published work.
Muzan Alneel (1986-2026) was a Sudanese engineer, researcher, and activist. The archive
catalogues her articles, interviews, videos, reports, and book chapters.

Form submission fields:
{json.dumps(row, ensure_ascii=False, indent=2)}

URL fetch status: {url_status}

Decide:
  (a) Is this plausibly a genuine piece BY Muzan Alneel herself (authored,
      interviewed, or substantially featured — not merely mentioning her)?
  (b) If yes, normalize the submission into an entry for our ITEMS array.

Respond with EXACTLY one JSON object, no prose, no code fences.

If acceptable:
{{
  "relevant": true,
  "reasoning": "<one or two sentences>",
  "entry": {{
    "year": <integer>,
    "month": "<Arabic month name or empty string>",
    "titleEn": "<English title>",
    "titleAr": "<Arabic title>",
    "type": "<article|interview|report|book|video>",
    "outlet": "<publication or venue>",
    "url": "<canonical url>",
    "descEn": "<1-2 sentence English description>",
    "descAr": "<1-2 sentence Arabic description>",
    "sourceLang": "<en or ar>",
    "collabEn": "<optional: 'With X, Y' if co-authored>",
    "collabAr": "<optional: 'مع X و Y' if co-authored>"
  }}
}}

If not acceptable:
{{"relevant": false, "reasoning": "<why — e.g., not by Muzan, URL dead, obvious spam>"}}

Valid Arabic months: يناير، فبراير، مارس، أبريل، مايو، يونيو، يوليو، أغسطس، سبتمبر، أكتوبر، نوفمبر، ديسمبر.
Leave month as empty string if unknown. Omit collabEn/collabAr if not applicable.
"""
    resp = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def js_str(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{s}"'


def format_entry(entry: dict) -> str:
    lines = ["  {"]
    year = int(entry["year"])
    month = entry.get("month") or ""
    lines.append(f"    year: {year}, month: {js_str(month)},")
    for key in ("titleEn", "titleAr", "type", "outlet", "url", "descEn", "descAr"):
        val = entry.get(key)
        if val:
            lines.append(f"    {key}: {js_str(str(val))},")
    for key in ("collabEn", "collabAr"):
        val = entry.get(key)
        if val:
            lines.append(f"    {key}: {js_str(str(val))},")
    source_lang = entry.get("sourceLang") or "en"
    lines.append(f"    sourceLang: {js_str(source_lang)}")
    lines.append("  }")
    return "\n".join(lines)


def validate_entry(entry: dict) -> str | None:
    required = ("year", "titleEn", "titleAr", "type", "outlet", "url", "descEn", "descAr", "sourceLang")
    for k in required:
        if not entry.get(k):
            return f"missing required field: {k}"
    if entry["type"] not in VALID_TYPES:
        return f"invalid type: {entry['type']!r}"
    if entry["sourceLang"] not in ("en", "ar"):
        return f"invalid sourceLang: {entry['sourceLang']!r}"
    try:
        int(entry["year"])
    except (TypeError, ValueError):
        return f"invalid year: {entry['year']!r}"
    return None


def insert_entry_into_html(entry: dict) -> None:
    text = INDEX_HTML.read_text(encoding="utf-8")
    marker = "const ITEMS = [\n"
    idx = text.find(marker)
    if idx < 0:
        raise RuntimeError("Could not find 'const ITEMS = [' in index.html")
    insert_at = idx + len(marker)
    block = format_entry(entry) + ",\n"
    INDEX_HTML.write_text(text[:insert_at] + block + text[insert_at:], encoding="utf-8")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True)


def branch_exists_on_origin(branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip())


def create_pr(entry: dict, row_id: str, reasoning: str, row: dict) -> bool:
    branch = f"auto/form-{row_id}"
    if branch_exists_on_origin(branch):
        print(f"Branch {branch} already exists on origin — skipping PR creation.")
        return True

    title_source = entry.get("titleEn") or entry.get("titleAr") or "new archive entry"
    title = f"Add: {title_source}"
    if len(title) > 70:
        title = title[:67] + "..."

    body_lines = [
        "Auto-generated from a suggestion-form submission.",
        "",
        f"- **Title:** {entry.get('titleEn') or entry.get('titleAr')}",
        f"- **Outlet:** {entry.get('outlet')}",
        f"- **Type:** {entry.get('type')}",
        f"- **Year / Month:** {entry.get('year')} / {entry.get('month') or '—'}",
        f"- **URL:** {entry.get('url')}",
        f"- **Source language:** {entry.get('sourceLang')}",
        "",
        f"**Assessment:** {reasoning}",
        "",
        "<details><summary>Raw submission</summary>",
        "",
        "```json",
        json.dumps(row, ensure_ascii=False, indent=2),
        "```",
        "",
        "</details>",
        "",
        "*Review the entry for accuracy before merging.*",
    ]
    body = "\n".join(body_lines)

    run(["git", "checkout", "-b", branch])
    run(["git", "add", str(INDEX_HTML)])
    run(["git", "commit", "-m", f"Add entry: {title_source}"])
    run(["git", "push", "-u", "origin", branch])
    run(["gh", "pr", "create", "--base", "main", "--head", branch,
         "--title", title, "--body", body])
    return True


def commit_state_to_main() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", str(STATE_PATH)],
        capture_output=True, text=True, check=True,
    )
    if not result.stdout.strip():
        print("State unchanged; no commit needed.")
        return
    run(["git", "add", str(STATE_PATH)])
    run(["git", "commit", "-m", "chore(automation): update sync state"])
    run(["git", "push", "origin", "main"])


def main() -> int:
    csv_url = os.environ.get("SHEET_CSV_URL")
    if not csv_url:
        print("ERROR: SHEET_CSV_URL env var not set", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY env var not set", file=sys.stderr)
        return 2

    rows = fetch_csv(csv_url)
    state = load_state()
    processed = set(state.get("processed", []))
    existing_urls = extract_existing_urls()

    print(f"Fetched {len(rows)} rows; {len(processed)} previously processed.")

    created = 0
    for row in rows:
        rid = row_hash(row)
        if rid in processed:
            continue

        print(f"\n=== Row {rid} ===")
        print(json.dumps(row, ensure_ascii=False, indent=2))

        submitted_url = find_submitted_url(row)
        if not submitted_url:
            print(f"SKIP {rid}: no URL in row")
            processed.add(rid)
            continue

        if submitted_url in existing_urls:
            print(f"SKIP {rid}: URL already in archive")
            processed.add(rid)
            continue

        url_status = check_url_reachable(submitted_url)
        print(f"URL check: {url_status}")

        try:
            result = assess_with_claude(row, url_status)
        except Exception as e:
            print(f"ERROR assessing row {rid}: {e}", file=sys.stderr)
            continue

        if not result.get("relevant"):
            print(f"REJECTED {rid}: {result.get('reasoning')}")
            processed.add(rid)
            continue

        entry = result.get("entry") or {}
        err = validate_entry(entry)
        if err:
            print(f"REJECTED {rid}: Claude returned invalid entry — {err}")
            processed.add(rid)
            continue

        if entry["url"] in existing_urls:
            print(f"SKIP {rid}: URL in entry already in archive")
            processed.add(rid)
            continue

        print(f"ACCEPTED {rid}: creating PR")
        insert_entry_into_html(entry)
        try:
            create_pr(entry, rid, result.get("reasoning", ""), row)
            created += 1
            existing_urls.add(entry["url"])
            processed.add(rid)
        except subprocess.CalledProcessError as e:
            print(f"ERROR creating PR for {rid}: {e}", file=sys.stderr)
        finally:
            run(["git", "checkout", "main"])
            run(["git", "reset", "--hard", "origin/main"])

    state["processed"] = sorted(processed)
    save_state(state)
    commit_state_to_main()

    print(f"\nDone. Created {created} PR(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
