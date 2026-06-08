"""SEC form helpers shared by the filings API and citation index."""


def primary_document_url_fetchable(form: str | None) -> bool:
    """Whether the filing primary doc is suitable for ``web_fetch``.

    10-K / 10-Q primaries are inline iXBRL; generic scrapers return XBRL
    context soup, not MD&A or risk-factor prose. Those rows still appear in
    ``recent_filings`` for calendar/triage; they omit ``primary_document_url``.
    """
    f = (form or "").strip().upper()
    if not f:
        return False
    return not (f.startswith("10-K") or f.startswith("10-Q"))
