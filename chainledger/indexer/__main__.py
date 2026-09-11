from prometheus_client import start_http_server

from chainledger.config import get_settings
from chainledger.indexer.rpc import make_client
from chainledger.indexer.service import IndexerService


def main() -> None:
    settings = get_settings()
    w3 = make_client(settings.rpc_url)
    start_http_server(settings.metrics_port, addr="0.0.0.0")
    IndexerService(settings, w3).run_forever()


if __name__ == "__main__":
    main()
