"""Entry point: python -m doorman"""
import uvicorn

from .config import settings


def main():
    accounts = __import__("doorman.store", fromlist=["store"]).list_accounts()
    settings.account(None)                     # triggers first-run seeding
    who = f"{len(accounts)} account(s)" if accounts else "no accounts yet — sign up"
    print(f"Doorman -> http://{settings.host}:{settings.port}   ({who})")
    uvicorn.run("doorman.web.app:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
