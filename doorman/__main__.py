"""Entry point: python -m doorman"""
import logging

import uvicorn

from .config import settings


def main():
    # Uvicorn configures only its own loggers, so without this every
    # `log.info` in doorman/ is swallowed at the root's WARNING default --
    # including the scheduler letting somebody into the building at 4:50am,
    # which is precisely the line you want in `journalctl -u doorman`.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    accounts = __import__("doorman.store", fromlist=["store"]).list_accounts()
    who = f"{len(accounts)} account(s)" if accounts else "no accounts yet — sign up"
    print(f"Doorman -> http://{settings.host}:{settings.port}   ({who})")
    uvicorn.run("doorman.web.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
