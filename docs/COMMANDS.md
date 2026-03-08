# Useful commands

## Enter the project directory

```bash
cd polymarket
```

## Start the bot with the mini interface

```bash
./start_polymarketbot
```

It asks for:
- mode: `paper` or `live`
- max entry orders: `0` = no limit
- profit target in USD: `0` = no limit
- stop loss in USD: positive number, for example `5`
- max duration in minutes: `0` = unlimited

Note:
- after startup, the terminal becomes available again immediately
- the bot keeps running in the background

## Check whether the bot is running

```bash
./status_polymarketbot
```

## Stop the bot manually

```bash
./stop_polymarketbot
```

## Tail the live log

```bash
tail -f runtime/polymarketbot.log
```

To exit `tail`:

```bash
Ctrl+C
```

## Show the latest saved run configuration

```bash
cat runtime/session.env
```

## Show run history

```bash
cat reports/run_history.jsonl
```

To only see the latest lines:

```bash
tail -n 20 reports/run_history.jsonl
```

## Direct start without the mini interface

```bash
./polymarketbot
```

Direct paper trading example:

```bash
DRY_RUN=true PAPER_TRADING=true USE_WEBSOCKET=true ./polymarketbot
```

Direct live example:

```bash
DRY_RUN=false PAPER_TRADING=false USE_WEBSOCKET=true ./polymarketbot
```

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## Recommended first test

Run:

```bash
./start_polymarketbot
```

Then enter values such as:

```text
Mode [paper/live] [paper]: paper
Max entry orders (0 = no limit) [0]: 100
Profit target USD (0 = no limit) [0]: 2
Stop loss USD [5]: 5
Max duration minutes (0 = unlimited) [0]: 15
```

## Common mistake to avoid

This is wrong:

```bash
/status_polymarketbot
```

This is correct:

```bash
./status_polymarketbot
```
