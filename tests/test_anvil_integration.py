import shutil
import socket
import subprocess
import time

import pytest
from eth_account import Account
from sqlalchemy import func, select

from chainledger.config import Settings
from chainledger.indexer.rpc import make_client
from chainledger.indexer.service import IndexerService
from chainledger.models import Transfer
from tests.conftest import load_fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("anvil") is None, reason="anvil binary not on PATH"),
]

# anvil's default funded account #0
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

RECIPIENT_A = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
RECIPIENT_B = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"

MINI_ERC20 = load_fixture("mini_erc20.json")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_rpc(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError("anvil did not start listening")


@pytest.fixture(scope="module")
def anvil_w3():
    port = _free_port()
    proc = subprocess.Popen(
        [shutil.which("anvil"), "--port", str(port), "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_rpc(port)
        yield make_client(f"http://127.0.0.1:{port}"), f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _deploy_mini_erc20(w3, deployer: str, nonce: int) -> tuple[str, str]:
    contract = w3.eth.contract(abi=MINI_ERC20["abi"], bytecode=MINI_ERC20["bytecode"])
    tx = contract.constructor(10**24).build_transaction(
        {"from": deployer, "nonce": nonce, "gas": 1_000_000, "gasPrice": w3.eth.gas_price}
    )
    signed = w3.eth.account.sign_transaction(tx, DEPLOYER_KEY)
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )
    return str(receipt.contractAddress).lower()


def _transfer(w3, token_address: str, deployer: str, nonce: int, to: str, amount: int) -> None:
    token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=MINI_ERC20["abi"])
    tx = token.functions.transfer(to, amount).build_transaction(
        {"from": deployer, "nonce": nonce, "gas": 100_000, "gasPrice": w3.eth.gas_price}
    )
    signed = w3.eth.account.sign_transaction(tx, DEPLOYER_KEY)
    w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.raw_transaction))


async def test_anvil_end_to_end(anvil_w3, db_session, session_factory, api_client):
    w3, rpc_url = anvil_w3
    deployer = Account.from_key(DEPLOYER_KEY).address

    token_address = _deploy_mini_erc20(w3, deployer, nonce=0)
    _transfer(w3, token_address, deployer, nonce=1, to=RECIPIENT_A, amount=10**18)
    _transfer(w3, token_address, deployer, nonce=2, to=RECIPIENT_B, amount=2 * 10**18)
    _transfer(w3, token_address, deployer, nonce=3, to=RECIPIENT_A, amount=3 * 10**18)

    settings = Settings(
        _env_file=None,
        rpc_url=rpc_url,
        db_url="postgresql://unused",
        tokens=token_address,
        start_block=0,
        confirmations=0,
    )
    IndexerService(settings, w3, session_factory=session_factory).run_once()

    assert db_session.scalar(select(func.count(Transfer.id))) == 3
    with session_factory() as session:
        rows = list(
            session.execute(
                select(Transfer).order_by(Transfer.block_number, Transfer.log_index)
            ).scalars()
        )
    assert [row.from_address for row in rows] == [deployer.lower()] * 3

    body = (await api_client.get(f"/transfers?token={token_address}")).json()
    items = body["items"]
    assert len(items) == 3
    amounts = sorted(int(i["value"]) for i in items)
    assert amounts == [10**18, 2 * 10**18, 3 * 10**18]
