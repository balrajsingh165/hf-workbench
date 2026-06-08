from src.sec_filings import primary_document_url_fetchable


def test_periodic_reports_not_fetchable() -> None:
    assert primary_document_url_fetchable("10-K") is False
    assert primary_document_url_fetchable("10-K/A") is False
    assert primary_document_url_fetchable("10-Q") is False
    assert primary_document_url_fetchable("10-Q/A") is False


def test_event_filings_fetchable() -> None:
    assert primary_document_url_fetchable("8-K") is True
    assert primary_document_url_fetchable("8-K/A") is True
    assert primary_document_url_fetchable("6-K") is True
    assert primary_document_url_fetchable("S-1") is True
