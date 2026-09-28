# Worker process entrypoint. The loop from design section 5 ("Worker flow") is added in
# milestone 4; for now the process connects, applies the schema, and idles.
import logging
import signal
import threading

from app.config import Config
from app.db import apply_schema_url, create_pool

log = logging.getLogger("worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env()
    config.validate_worker()
    apply_schema_url(config.database_url)
    pool = create_pool(config.database_url, config.db_pool_size)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("worker started")
    while not stop.wait(config.worker_poll_seconds):
        pass
    pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
