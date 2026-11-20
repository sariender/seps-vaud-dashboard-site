# GitHub Pages

- Public website source: `sariender/seps-vaud-dashboard-site`. The original repository stays private.
- `scripts/prepare_pages_repository.py --output <empty-directory>` copies only the reviewed website source and workflow into a fresh checkout. It copies no Git history, archive, notebooks, research data, local caches or credentials.
- The public workflow runs at minute 32 of each hour (UTC), on pushes to `main`, or through **Actions → Dashboard → Run workflow**.
- Each run checks the code, fetches the three live data sources and publishes `_site/` as a Pages artifact. Data updates create no Git commits.
- A failed source publishes an unavailable snapshot for that source. The others can still update. Existing expiry checks stay in effect; delayed jobs can cause short periods without fresh readings.
- GitHub may disable scheduled workflows in public repositories after 60 days without repository activity. Re-enable **Dashboard** in Actions if this happens. [GitHub schedule rules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- To edit the dashboard, work in the original checkout, run the tests, prepare the public copy, review its changes and push as `sariender`.
- To move to the original private repository after upgrading: copy `pages.yml` into `.github/workflows/pages.yml`, change its repository condition to the target repository, and enable **Settings → Pages → GitHub Actions**. Relative URLs work under either repository path.
