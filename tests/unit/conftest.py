import pytest


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own settings out of every test."""
    monkeypatch.delenv("FINANCIAL_DATA_API_KEY", raising=False)
    monkeypatch.delenv("FINANCIAL_DATA_BASE_URL", raising=False)
