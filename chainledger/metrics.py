from prometheus_client import Counter, Gauge, Histogram

BLOCKS_INDEXED = Counter(
    "chainledger_blocks_indexed_total",
    "Total blocks indexed",
    ["token"],
)

TRANSFERS_INDEXED = Counter(
    "chainledger_transfers_indexed_total",
    "Total ERC-20 Transfer events indexed",
    ["token"],
)

RPC_ERRORS = Counter(
    "chainledger_rpc_errors_total",
    "Total RPC errors encountered (each retry attempt counts)",
)

BLOCKS_BEHIND = Gauge(
    "chainledger_blocks_behind_head",
    "Indexer lag behind the chain head in blocks",
)

API_REQUEST_LATENCY = Histogram(
    "chainledger_request_latency_seconds",
    "API request latency in seconds",
    ["method", "path"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
