"""回填历史记录的成本。

用法：
    python -m app.backfill
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .db import Store
from .pricing import load_prices

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
log = logging.getLogger("backfill")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    cfg = load_config()
    prices = load_prices(
        root / "pricing.yaml",
        auto_dirs={"opencode": root / "zen_prices.json"},
    )

    store = Store(cfg.data_dir / "usage.db")
    conn = store.conn

    rows = conn.execute(
        """
        SELECT id, ts, source, model, input_tokens, output_tokens,
               cache_read, cache_write
        FROM usage_records
        WHERE cost_usd IS NULL
        """
    ).fetchall()

    log.info("found %d records without cost", len(rows))

    updated = 0
    skipped = 0
    for r in rows:
        cost = prices.compute(
            upstream=r["source"],
            model=r["model"],
            input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0,
            cache_read=r["cache_read"] or 0,
            cache_write=r["cache_write"] or 0,
            at=datetime.fromtimestamp(r["ts"], tz=timezone.utc),
        )
        if cost is None:
            skipped += 1
            continue
        conn.execute(
            "UPDATE usage_records SET cost_usd = ? WHERE id = ?", (cost, r["id"])
        )
        updated += 1

    conn.commit()
    log.info("updated %d records, skipped %d (no price)", updated, skipped)

    total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM usage_records"
    ).fetchone()[0]
    log.info("total cost now: $%.6f", total)

    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
