"""Run the supported Steam adapters through the shared durable collection ledger."""
import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe configured Steam sources using one accounted collection run.")
    parser.add_argument("--config", help="JSON configuration path (or GAME_CENSUS_CONFIG)")
    parser.add_argument("--app-id", type=int, action="append", help="Configured app ID; repeat for a bounded subset")
    args = parser.parse_args(argv)
    from game_census.cli import main as command
    options = (["--config", args.config] if args.config else []) + ["collect", "--once"]
    for app_id in args.app_id or []:
        options.extend(["--app-id", str(app_id)])
    return command(options)


if __name__ == "__main__":
    sys.exit(main())
