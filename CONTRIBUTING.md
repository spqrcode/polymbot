# Contributing

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Practical rules

- Keep `DRY_RUN=true` and `PAPER_TRADING=true` during development unless you are explicitly running a live test.
- Do not commit `.env`, `runtime/`, `reports/`, or other local artifacts.
- Update `README.md` and `.env.example` whenever you introduce new configuration variables or operational flows.
- If you change quoting, hedging, risk, or fill-tracking logic, add or update tests in `tests/test_regressions.py`.

## Minimum checks before a PR

```bash
python3 -m unittest discover -s tests -v
zsh -n start_polymarketbot status_polymarketbot stop_polymarketbot scripts/run_background.sh scripts/run_launchd.sh
```
