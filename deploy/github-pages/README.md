# GitHub Pages

- Public website: https://sariender.github.io/seps-vaud-dashboard-site/. The original project repository stays private.
- The current website is frozen for design review. Its clock, weather, WBGT, map temperatures and water points use `data/dashboard-frozen.json`.
- Scheduled data updates and automatic category rotation are off. Category buttons still work.
- Build the saved version without contacting data providers: `python scripts/build_pages.py --frozen data/dashboard-frozen.json --output _site`.
- Pushes to `main` and **Actions → Dashboard → Run workflow** publish the same saved data; they do not refresh it.
- To deliberately replace the snapshot, run `python scripts/build_pages.py --capture-frozen data/dashboard-frozen.json`. Capture succeeds only when all three sources return valid current data.
- `python scripts/prepare_pages_repository.py --output <empty-directory>` prepares the reviewed public source. It copies no private Git history, archive, research files or local caches.
- Work in the original project, prepare the public copy, review its changes and push as `sariender`.
- To resume live updates later, use the build command without `--frozen`, restore a schedule and verify an actual scheduled run. The previous hourly GitHub trigger did not run reliably.
