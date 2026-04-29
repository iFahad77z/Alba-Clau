# GitHub Actions Trader — Setup

This package runs your dual-strategy watcher (v1 LIVE Golden Cross + v2 SHADOW EMA Scalper) on GitHub's free cron runners every 5 minutes — no laptop required.

## One-time setup (~5 minutes)

1. **Create a new GitHub repo** (PRIVATE recommended — your secrets stay encrypted either way, but private avoids exposing the strategy code).
   - Go to https://github.com/new
   - Name it whatever you like (e.g. `alpaca-trader`)
   - Set it to Private
   - Click "Create repository"

2. **Push these files to the repo.** From this folder:
   ```bash
   cd C:\Users\iFaha\Desktop\github-actions-trader
   git init
   git add .
   git commit -m "initial trader"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```

3. **Add Alpaca credentials as repo secrets:**
   - Repo page → Settings → Secrets and variables → Actions → New repository secret
   - Add `ALPACA_KEY` = `PKOQBZRNDJZ3BRJH76DCCHEOLJ`
   - Add `ALPACA_SECRET` = `4u7rnC2GJNVhWFic6n1jNMa9RAshnFSF9fwWA2bprTi8`

4. **Enable GitHub Actions** (it's enabled by default for new repos, but if you ever forked or cloned an existing one, check Settings → Actions → "Allow all actions").

5. **Trigger the first run manually** to confirm it works:
   - Repo page → Actions tab → "Dual Strategy Trader" workflow → "Run workflow" button (top right) → Run

## After setup

- Workflow runs every 5 minutes automatically.
- Live v1 orders fire on the Alpaca paper account whenever a Golden/Death Cross hits.
- Shadow v2 simulates entries; state persists in `v2_state.json` (committed back automatically each run).
- Each run's full log is in the Actions tab under that run.

## Important caveats

1. **GitHub Actions cron is best-effort.** Runs can be delayed up to 15-30+ minutes during peak load. If a tick is missed and a cross happens, v1 will catch it on the next run — but if the cross *un-crosses* between firings, the signal is lost. For mission-critical timing use a real VM.
2. **Free private-repo Actions are limited to 2,000 minutes/month.** Each run takes ~30 seconds. 12/hour × 24h × 30 days × 0.5 min = **4,320 min/month — over the limit for private repos.** Two ways to stay free:
   - Make the repo **public** (Actions are unlimited for public repos; secrets are still encrypted).
   - Increase the cron to every 10 or 15 minutes (`*/10 * * * *` or `*/15 * * * *`).
3. **The repo will accumulate commits** from the bot updating `v2_state.json` — that's expected. They're tagged `[skip ci]` so they don't trigger extra runs.
4. **To pause:** Repo Actions tab → "..." menu on the workflow → "Disable workflow."

## File map

- `trader.py` — Python port of both strategies
- `.github/workflows/trade.yml` — cron schedule + run + state-commit step
- `v2_state.json` — shadow-strategy positions (auto-updated by the bot)
- `.gitignore` — local junk
