from chainledger.config import get_settings
from chainledger.indexer.rpc import make_client
from chainledger.indexer.service import IndexerService


def main() -> None:
    settings = get_settings()
    w3 = make_client(settings.rpc_url)
    IndexerService(settings, w3).run_forever()


if __name__ == "__main__":
    main()
