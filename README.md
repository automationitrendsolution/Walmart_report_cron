# Sellerboard Walmart export

Downloads the Sellerboard **Walmart dashboard** as one file per day (or week /
month), **newest first**, back to `BACKFILL_START_DATE` (default 2025-01-01).
It is built for comparing last year, this year and next year: every scheduled
run picks up the new day automatically, so the archive keeps growing into the
next year with no changes.

```
periods, newest -> oldest  (skip what the manifest already has)
        |  Playwright + Chromium, persistent logged-in profile
dashboard?periodsPreset=custom-range:c|<start>-<end>   (period opened by URL)
        |  Export -> real download event -> validated
data/downloads/walmart/<year>/sellerboard_walmart_<YYYY-MM-DD>.xlsx
        |
data/state/manifest_walmart_day.json   (resume point + status per period)
        |  parse (header detection, $/%/comma cleaning)
MySQL sellerboard_walmart_daily        (DELETE + INSERT per period, one transaction)
```

The project layout follows `out-of-budget-us` (AdFlow): environment-driven
config, a selector catalogue in `config/selectors.json`, retries for transient
failures only, a run lock, a redacting log, and failure screenshots.

## How the dates work

Sellerboard puts the range in the URL as Unix seconds for **UTC midnight** of
the first and last day, both inclusive. `1788220800-1793404800` is
2026-09-01 .. 2026-10-31. Each period is opened directly by URL, so the date
picker is never touched.

- **Newest first.** The backfill walks backwards from yesterday, so recent data
  lands first and an interrupted backfill always has a complete recent block.
- **Resumable.** The manifest is saved after every period. Stop it at any time.
  The next run continues where it stopped.
- **Recent days are refreshed.** Sellerboard keeps revising recent figures
  (refunds, ad attribution, fees), so periods ending in the last
  `REFRESH_RECENT_DAYS` (default 30) are downloaded again on every run. A refresh
  writes to a temp file first, so a failed refresh never destroys the good file.
- **Today is skipped** (it is still changing) unless `INCLUDE_TODAY=true`.
- "Today" is computed in `REPORT_TIMEZONE`, never in the server's zone. Set it to
  the timezone of the Sellerboard account.

## The database

Every downloaded period is loaded into MySQL straight after it is saved. The
tool creates and owns the table: `sellerboard_walmart_daily` by default, or
`_weekly`/`_monthly` for those granularities, or `DB_TABLE`.

| Column | Meaning |
| --- | --- |
| `id` | auto key |
| `marketplace`, `granularity` | `walmart`, `day` |
| `period_start`, `period_end` | the period the row belongs to (DATE) |
| `sheet_name`, `row_num` | where in the export the row came from |
| `source_file`, `loaded_at` | lineage |
| *one column per export header* | e.g. `Gross profit, $` becomes `gross_profit` |

- **Columns come from the export.** Headers become snake_case column names.
  Numbers are stored as `DECIMAL(20,4)`, dates as `DATETIME`, and anything
  else as `TEXT`. `$1,234.56` becomes 1234.56, `12%` becomes 12,
  `(45)` becomes -45, and `-`/`N/A` become NULL. A header that appears later
  is added as a column. A numeric column that starts receiving text is widened
  to `TEXT`.
- **A re-download replaces the period.** `DELETE` of that period and `INSERT`
  run in one transaction. If the insert fails, the old rows stay.
- **Loads are tracked in the manifest.** A file that could not be loaded is
  retried on the next run without downloading it again.
  `run.py --load-only` loads pending files without opening a browser, and
  `--load-only --force` reloads everything.

For year-over-year analysis, use `period_start` as the date column, e.g.
`YEAR(period_start)`, `MONTH(period_start)`.

## Setup

```bash
uv sync
uv run playwright install chromium
cp .env.example .env          # then set SELLERBOARD_PASSWORD and the DB_* values
```

Then, once, by hand:

```bash
uv run python run.py --login     # visible browser; finish any verification, press Enter
uv run python run.py --inspect   # lists the dashboard's buttons + saves a screenshot
uv run python run.py --limit 1   # download just the newest day, check the file
```

**What one day looks like:** the export is the dashboard's **Products** table, one
row per product with `units`, `refunds`, `sales`, `ads`, `tacos`,
`shipping_costs`, `fees`, `cost_of_goods`, `grossprofit`, `margin`,
`expenses`, `net_profit`, `roi` and `aov`. This was checked on 2026-11-26:
2026-11-25 loaded 27 products, and the sums matched the dashboard tile exactly.

## Commands

| Command | What it does |
| --- | --- |
| `run.py` | Scheduled run: refresh recent periods, then continue the backfill. |
| `run.py --plan` | List outstanding periods and why. No browser. |
| `run.py --limit N` | Download at most N periods this run. |
| `run.py --day 2025-07-04` | Re-download one specific period. |
| `run.py --from 2025-01-01 --to 2025-12-31` | Restrict this run to a window. |
| `run.py --force` | Re-download periods already marked done. |
| `run.py --load-only [--force]` | Load downloaded files into MySQL; no browser. |
| `run.py --no-db` | Download files only, this run. |
| `run.py --login` | Interactive sign-in. |
| `run.py --inspect [--headed]` | Show dashboard controls, for selector setup. |
| `--headed` | Visible browser for any mode. |

### Exit codes

`0` success · `2` config · `3` sign-in needs a person (`--login`) · `4`
navigation · `6` download · `7` bad file · `8` unreadable export · `9`
database (transaction rolled back) · `11` another run is active · `12`
some periods failed (retried automatically next run).

## Scheduling

The first backfill is ~690 daily downloads. Let it run in one go, or spread it
with `MAX_PERIODS_PER_RUN=100`. After that, each run downloads only the new day
plus the refresh window.

**Linux (cron)**: once a day at 06:00 server time:

```cron
0 6 * * * cd /opt/sellerboard-walmart && /usr/local/bin/uv run --no-sync python run.py >> logs/cron.log 2>&1
```

**Windows (Task Scheduler)**: daily at 06:00, using the included wrapper:

```bat
schtasks /Create /TN "Sellerboard Walmart export" /SC DAILY /ST 06:00 ^
  /TR "\"C:\projects\walmart_report_cron\run_scheduled.cmd\""
```

Overlapping runs are refused (exit 11) by an OS lock, which is released
automatically if a run crashes, so a long backfill and the daily run cannot
collide.

## Settings

See `.env.example` for the annotated list. The ones you are most likely to
change:

| Variable | Default | Notes |
| --- | --- | --- |
| `SELLERBOARD_EMAIL` / `SELLERBOARD_PASSWORD` | — | Set both or neither. |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | — | Required unless `DB_ENABLED=false`. |
| `DB_TABLE` | `sellerboard_walmart_daily` | |
| `BACKFILL_START_DATE` | `2025-01-01` | Oldest day downloaded. |
| `PERIOD_GRANULARITY` | `day` | `day`, `week` (Mon–Sun) or `month`. Each has its own manifest. |
| `REFRESH_RECENT_DAYS` | `30` | Recent periods re-downloaded every run (Walmart refunds/fees land 14–28 days late). |
| `MAX_PERIODS_PER_RUN` | `0` | 0 = unlimited. |
| `REPORT_TIMEZONE` | `America/Los_Angeles` | Defines "yesterday". |
| `HEADLESS` | `true` | |
| `DOWNLOAD_TIMEOUT_MS` | `180000` | Raise if exports of long ranges time out. |

## Troubleshooting

- **Exit 3 / `AuthenticationError`**: the session expired and either
  credentials were rejected or Sellerboard asked for a verification step. Run
  `run.py --login` once. A rejected password is never retried, to avoid locking
  the account.
- **"No export button" / "never produced a file"**: Sellerboard changed the
  page. Run `run.py --inspect`, put the new locator at the top of
  `export_trigger` in `config/selectors.json`, and check `data/screenshots/`.
- **A period keeps failing**: `run.py --plan` shows it as `retry`, and the
  manifest records `last_error`. Re-run it alone with `--day YYYY-MM-DD --headed`.

## Security

`.env`, `data/session/` (live login cookies), downloads, state and logs are all
git-ignored. The password never reaches logs (`repr` excluded, redaction
filter). Page HTML is never dumped, and logged URLs have their query strings
stripped.

## Tests

```bash
uv run pytest     # 70 unit tests, no network (MySQL is replaced by SQLite)
```
#   W a l m a r t _ r e p o r t _ c r o n  
 