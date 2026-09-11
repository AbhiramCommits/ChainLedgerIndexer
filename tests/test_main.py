from types import SimpleNamespace

from chainledger.indexer import __main__ as main_mod


def test_main_wiring(monkeypatch):
    settings = SimpleNamespace(rpc_url="http://fake")
    sentinel = object()
    events: list[object] = []

    class FakeService:
        def __init__(self, settings, w3):
            events.append(("init", settings, w3))

        def run_forever(self):
            events.append("run_forever")

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "make_client", lambda url: sentinel)
    monkeypatch.setattr(main_mod, "IndexerService", FakeService)
    main_mod.main()
    assert events == [("init", settings, sentinel), "run_forever"]
