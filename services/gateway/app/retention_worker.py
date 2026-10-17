from __future__ import annotations

import logging
import os
import time
import asyncio

from .history_store import purge_expired


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("megmeet.retention")


def main() -> None:
    interval = max(60, int(os.getenv("RETENTION_INTERVAL_SECONDS", "300")))
    batch_size = max(1, min(5000, int(os.getenv("RETENTION_BATCH_SIZE", "500"))))
    while True:
        try:
            deleted = purge_expired(batch_size=batch_size)
            logger.info("retention cleanup completed: deleted=%s", deleted)
            from .cloud_retention import enabled as workspace_policy_enabled
            if workspace_policy_enabled():
                from .cloud_retention import maintenance_cycle
                counts=asyncio.run(maintenance_cycle(int(os.getenv('PERSONAL_CLOUD_HISTORY_BATCH','10'))))
                logger.info('cloud workspace maintenance: %s',counts)
            elif os.getenv('PERSONAL_CLOUD_HISTORY_ENABLED','false').lower()=='true':
                from .cloud_history import maintenance_cycle
                counts=asyncio.run(maintenance_cycle(int(os.getenv('PERSONAL_CLOUD_HISTORY_BATCH','10'))))
                logger.info('cloud history maintenance: %s',counts)
        except Exception:
            logger.error("retention cleanup failed; retrying next cycle")
        time.sleep(interval)


if __name__ == "__main__":
    main()
