# Project Darkrai

A private web page that turns an amount into three investment choices, using the rules in the Three Choices Rule Book (v1.0). A job on GitHub runs every weekday night, fetches prices, checks them, and publishes a new `site/data.json` only if every check passes.

## What's in this folder

| Path | What it is |
| --- | --- |
| `site/index.html` | The page. It loads `data.json` automatically, or shows demo data until the first run succeeds |
| `site/engine.js` | The rule book engine. The page and the nightly check both use this file, so they can't disagree |
| `scripts/build_data.py` | The nightly job |
| `scripts/check_data.js` | Runs the engine on a new data file before it is published |
| `config/funds.json` | The funds for each slot and their facts. You update this monthly |
| `config/scenarios.json` | The eight scenarios from the rule book |
| `config/settings.json` | Limits, assumptions and pauses between requests |
| `state/` | Stored history, filled by the job. Don't edit by hand |
| `tests/` | Offline tests with made-up data. They run before every nightly build |
| `.github/workflows/nightly.yml` | The schedule: 23:37 UTC, Monday to Friday |

## What happens each night

1. Once a month, the S&P 500 list and market values are refreshed, and the two largest companies in each sector become candidates.
2. The latest daily price of every candidate is checked. A missing or stale price leaves that holding out. A move over 25% in a day stops the run until you confirm it.
3. The stored monthly history is updated, fetching only the last few months. If Yahoo has re-based a ticker's history, usually after a split, its full history is fetched again.
4. Monthly returns are built. Before a young fund existed, its proxy is used minus the fee difference.
5. Scenario values marked `compute` are calculated once from Yahoo and FRED, then kept fixed.
6. The engine checks the new file. Only then does it replace `site/data.json`. If anything fails, yesterday's data stays published and GitHub emails you.

## Try it on your computer first

You need Python 3.11 or newer and Node.js. In Terminal, from this folder:

```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_data.py
```

The tests use made-up data and should end with `OK`. The last command fetches real data and prints the run summary. To see the result, open `site/index.html` in your browser, click **Load a data file**, and choose `site/data.json`.

## Set it up once

1. **Make a private GitHub repository** and upload everything in this folder, including the `.github` folder.
2. **Fill in `config/funds.json`** from each fund's page on its provider's site:
   - Required: `duration` and `convexity` for the inflation-protected funds (SCHP and TIP). Without them, the TIPS slot is empty and the run stops.
   - Recommended: `expenseRatio`, `yield`, `spread` (the 30-day median bid-ask spread), `assets` in billions, `inception` as YYYY-MM-DD, and the proxy fees for GLD, DBC and DJP.
   - Then set `checked` to today's date.
   - Anything left blank is filled from Yahoo and listed on the page as unverified.
3. **Optional: add a cross-check.** Get a free Tiingo key and save it as a repository secret named `TIINGO_TOKEN`. Each month, five prices are then compared against Tiingo.
4. **Run it once.** Go to Actions, then Nightly data, then Run workflow. The first run fetches the full history and the S&P 500 list, so it takes a few minutes. Read the summary at the end of the run.
5. **Put the site online.** In Cloudflare Pages, connect the repository, leave the build command empty, and set the output directory to `site`. Each nightly commit then redeploys the site.
6. **Add a login.** In Cloudflare Zero Trust, under Access, add an application for your site's address with a policy that allows only your email. It's free for up to 50 users.
7. **Open the site.** The banner should say "Nightly data" with the date of the latest monthly close.

## Every month, about 10 minutes

- Update `config/funds.json` from the fund pages and set `checked`. The run summary warns once the facts are more than 45 days old.
- Open "Fund facts taken from Yahoo" on the page and replace each one with the fund page's figure.

## When a run fails

Yesterday's data stays published, so the page keeps working. The run summary says why it stopped.

| Message | What to do |
| --- | --- |
| A ticker moved more than 25% in a day | Check the news. If the move is real, such as a split or merger, add it to `confirmedEvents` in `config/settings.json` |
| No daily S&P 500 prices came back | Usually a Yahoo rate limit. The next run tries again. If it lasts a week, check the yfinance issue tracker and update the pinned version |
| No eligible fund for a slot | A fund's facts are missing or it failed a rule, such as $1 billion in assets. Fill the facts in `config/funds.json` |
| The engine check failed | Read the engine output in the summary. Usually a missing fact |

Scheduled runs can start late when GitHub is busy. If GitHub ever pauses the schedule, turn it back on in the Actions tab.

## Changing things on purpose

- **Rules:** change a limit or formula in `site/engine.js`, then raise `RULES_VERSION` in the engine and `rulesVersion` in `config/settings.json`, and add a dated entry to the rule book's change log. Scenario values recompute for the new version.
- **Packages:** versions are pinned in `requirements.txt`. To upgrade, change a version and run the workflow by hand; the offline tests run first.

## Costs and data terms

- Free: a run takes a few minutes, well inside GitHub's 2,000 free monthly minutes for private repositories, and Cloudflare Pages and Access have free plans.
- Yahoo data through yfinance is for personal use only, so keep the repository and the site private.
