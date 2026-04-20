# Form → PR automation

A GitHub Actions workflow (`.github/workflows/sync-form.yml`) runs on a cron
schedule, reads new rows from the suggestion-form's response sheet, and opens
a PR for each submission that Claude judges to be a genuine piece by Muzan
Alneel.

## Flow

```
cron ──► fetch CSV ──► skip rows in state.json ──► for each new row:
                                                   ├─ no URL?          → mark processed
                                                   ├─ URL already in   → mark processed
                                                   │   index.html
                                                   ├─ URL unreachable? → include status in prompt
                                                   ├─ Claude says not  → mark processed
                                                   │   relevant
                                                   └─ Claude says yes  → branch, insert into
                                                                         ITEMS, push, open PR
                                                                         → mark processed
                                                                         → commit state.json to main
```

State of processed rows lives in `.automation/state.json`. It is updated on
`main` at the end of every run so that subsequent runs skip already-seen
submissions even when they were rejected.

## One-time setup

### 1. Publish the response sheet as CSV

The workflow fetches the sheet over HTTPS with no auth. "Anyone with the link"
sharing is **not** sufficient — you need "Publish to the web":

1. Open the Google Sheet attached to the form.
2. `File → Share → Publish to web`.
3. Choose the responses sheet/tab, format **Comma-separated values (.csv)**.
4. Click **Publish**. Copy the resulting URL — it looks like
   `https://docs.google.com/spreadsheets/d/e/…/pub?output=csv`.

### 2. Add repo secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Name                | Value                                                 |
|---------------------|-------------------------------------------------------|
| `SHEET_CSV_URL`     | The published-to-web CSV URL from step 1.             |
| `ANTHROPIC_API_KEY` | Your Anthropic API key.                               |

Optionally add a repo **variable** (not a secret) named `CLAUDE_MODEL` to
override the default model (`claude-sonnet-4-6`).

### 3. Allow the workflow to push to `main`

The script commits `state.json` updates directly to `main`. If `main` is
branch-protected, either:

- add `github-actions[bot]` to the list of pushers allowed to bypass
  protection, **or**
- disable protection for this repo (it's an archive; PR gate on content
  changes is preserved because content PRs still go through review).

### 4. Form fields

The script is field-name-agnostic — it picks any cell whose value starts with
`http(s)://` as the submitted URL and hands the entire row to Claude. Any
reasonable column layout (e.g., *Link*, *Title*, *Notes*, *Your name*) will
work.

## Triggering a run manually

Actions → **Sync suggestions from Google Form** → **Run workflow**.

## Local dry-run

```bash
export SHEET_CSV_URL="https://docs.google.com/.../pub?output=csv"
export ANTHROPIC_API_KEY="sk-ant-..."
pip install anthropic
python .automation/sync_form.py
```

You will need `gh` installed and authenticated if you let it reach the
PR-creation step; otherwise comment that call out for testing.
