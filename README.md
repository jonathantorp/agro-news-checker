# AGRO News Checker

A lightweight media monitor for the Department of Agroecology at Aarhus University. It searches the GDELT DOC API once a day, keeps a permanent JSON archive of unique mentions, and publishes a Danish-language dashboard through GitHub Pages.

## Project structure

```text
.
├── .github/workflows/update-and-deploy.yml  Daily automation and Pages deployment
├── assets/                                  Dashboard JavaScript and CSS
├── config/search_terms.json                 Editable searches and API limits
├── data/articles.json                       Permanent article archive
├── src/collector.py                         GDELT client, cleanup, and merge logic
├── tests/test_collector.py                  Unit tests
└── index.html                               Static dashboard
```

The project deliberately has no runtime dependencies outside Python's standard library and a modern browser.

## How it works

For every configured phrase, the collector requests up to 250 recent results from the [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/). Each result is reduced to its headline, canonical URL, publisher, publication date, first discovery time, and matching phrase.

Before comparison, URLs are normalised by:

- lowercasing the scheme and host;
- removing URL fragments, default ports, trailing slashes, and common tracking parameters;
- sorting meaningful query parameters.

The normalised URL is the unique identifier. Previously collected articles remain in `data/articles.json` indefinitely. Writes are atomic. If every API search fails, the command exits unsuccessfully without writing the data file. If only some searches fail, successful results are still retained and the failures are logged.

The browser loads `data/articles.json` directly and renders the newest publication date first. Because the dashboard is static, it needs no server, database, framework, or build step.

## Run locally

Python 3.11 or newer is recommended.

```bash
python -m unittest discover -v
python -m src.collector
python -m http.server 8000
```

Then open `http://localhost:8000`. Running the collector makes live GDELT requests and updates `data/articles.json`.

Optional collector controls:

```bash
python -m src.collector --timeout 30 --retries 4
python -m src.collector --config path/to/config.json --data path/to/articles.json
```

## Add or change search terms

Edit `config/search_terms.json` and add a phrase to the `search_terms` array. Keep the JSON valid and use plain phrases; the collector supplies the quotation marks required for exact GDELT matching.

`lookback` controls how far each daily query looks back. The default one-month overlap makes a temporary outage unlikely to lose an article. `max_records_per_term` is capped by GDELT and defaults to 250.

Run the tests and collector locally after changing the configuration.

## GitHub Actions

`.github/workflows/update-and-deploy.yml` runs every day at 05:17 UTC and can also be started from **Actions → Update news and deploy → Run workflow**.

Each run:

1. checks out the repository and runs the unit tests;
2. queries GDELT with retries and a 20-second timeout per attempt;
3. atomically merges new unique articles into the archive;
4. commits and pushes `data/articles.json` only when at least one new article was found;
5. uploads and deploys the static dashboard to GitHub Pages.

The workflow has a single concurrency group, so scheduled and manual runs cannot overlap. A successful zero-result run still deploys the existing dashboard but creates no commit. Consequently, the dashboard's “last updated” time advances in repository history only when new articles are committed.

## Deploy

1. Push this repository to GitHub.
2. In **Settings → Pages → Build and deployment → Source**, select **GitHub Actions**.
3. In **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions** so the workflow can commit article updates.
4. Start the workflow manually once. Its deploy job creates or updates the `github-pages` environment and displays the public URL.

If the default branch is protected, permit GitHub Actions to push to it, or adjust the protection rule so the bot can update `data/articles.json`. No API key or repository secret is required for GDELT.

## Verify the first run

Open the repository's **Actions** tab, select **Update news and deploy**, and inspect the latest run. Confirm:

- **Run tests** passes;
- **Collect news** logs each configured phrase and prints `new_articles=N`;
- **Commit new articles** runs only when `N` is greater than zero;
- **Deploy to GitHub Pages** finishes successfully.

Then open the Pages URL shown by the deploy job. Check the total, the update time, and several article links. If new results were found, also inspect the bot commit and `data/articles.json`.

GitHub schedules use UTC and may start a few minutes after the exact cron time. The first automatic run after enabling the workflow should appear shortly after 05:17 UTC.

## Troubleshooting

- **All searches fail:** Open the collector logs. GDELT may be temporarily unavailable or throttling requests. The existing JSON archive is intentionally left untouched; retry the workflow later.
- **Some searches fail:** The run continues with the successful terms and logs each failure. The one-month lookback lets a later run catch up.
- **No commit appears:** This is expected when `new_articles=0`. Check the collector step rather than treating a skipped commit as an error.
- **Push is rejected:** Enable write workflow permissions and review branch-protection rules.
- **Pages deployment fails:** Ensure Pages uses **GitHub Actions** as its source and that Pages is available for the repository visibility and organisation policy.
- **Dashboard shows an error locally:** Serve the directory over HTTP; browsers commonly block `fetch()` when opening `index.html` directly as a file.
- **An apparent duplicate remains:** The publisher may use genuinely different article URLs. Add another known tracking parameter to `TRACKING_PARAMETERS` in `src/collector.py`, with a test.

## Version 2 ideas

Potential later improvements, deliberately not included in Version 1:

- relevance scoring and exclusion phrases to reduce false positives;
- a small review/approval queue before public display;
- RSS or another news API as a fallback source;
- publisher and date filters, search, CSV export, and simple trends;
- alerts by email or Teams when a new mention is found;
- monitoring for stale runs and notifications after repeated API failures;
- canonical-link or content-based deduplication for publishers that change URLs.
