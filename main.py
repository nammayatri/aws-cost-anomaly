import argparse
import json
import logging
import sys
from datetime import date, timedelta

import config as config_loader
from detect import detect
from drilldown import top_movers
from fetch import fetch_by_service, load_csv
from slack import build_header_blocks, build_thread_attachments, post

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cost-anomaly")


def run(dry_run: bool = False, csv_path: str | None = None, target: date | None = None) -> int:
    cfg = config_loader.load()
    target = target or (date.today() - timedelta(days=1))

    if csv_path:
        log.info("Loading cost data from CSV %s", csv_path)
        df = load_csv(csv_path)
    else:
        log.info("Fetching cost data from AWS Cost Explorer")
        df = fetch_by_service(cfg, end=target + timedelta(days=1))

    if df.empty:
        log.error("No cost data available")
        return 1

    if target not in df.index:
        log.error("Target date %s not in data (have %s..%s)", target, df.index.min(), df.index.max())
        return 1

    increases, decreases, summary = detect(df, target, cfg)
    log.info(
        "Detected %d increases, %d decreases for %s (total $%.2f)",
        len(increases), len(decreases), target, summary["total"],
    )

    if not increases and not decreases:
        log.info("No threshold crossings — skipping Slack post.")
        return 0

    movers_by_service: dict[str, list[dict]] = {}
    if not csv_path:
        for a in increases + decreases:
            try:
                movers_by_service[a.service] = top_movers(cfg, a.service, target)
            except Exception as e:
                log.warning("Drilldown failed for %s: %s", a.service, e)
                movers_by_service[a.service] = []

    if dry_run:
        payload = {
            "header": build_header_blocks(summary, len(increases), len(decreases), cfg.get("mention", "")),
            "thread_attachments": build_thread_attachments(summary, increases, decreases, movers_by_service),
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    post(cfg, summary, increases, decreases, movers_by_service)
    log.info("Posted to Slack channel %s", cfg["slack_channel_id"])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print Slack payload to stdout instead of posting")
    ap.add_argument("--csv", help="Use a local CSV instead of CE API (skips drill-down)")
    ap.add_argument("--date", help="Target date (YYYY-MM-DD). Default: yesterday")
    args = ap.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    sys.exit(run(dry_run=args.dry_run, csv_path=args.csv, target=target))


if __name__ == "__main__":
    main()
