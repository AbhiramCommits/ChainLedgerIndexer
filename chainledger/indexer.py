import structlog

from chainledger.config import get_settings

logger = structlog.get_logger()


def main() -> None:
    settings = get_settings()
    logger.info(
        "indexer starting",
        rpc_url=settings.rpc_url,
        chain_id=settings.chain_id,
        tokens=settings.token_addresses,
        start_block=settings.start_block,
        confirmations=settings.confirmations,
    )


if __name__ == "__main__":
    main()
