from __future__ import annotations

import argparse
import sys

from .config import load_config
from .executor import TradingBot


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Path to YAML config file")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    bot = TradingBot(cfg)
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
