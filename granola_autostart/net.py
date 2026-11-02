"""How this app opens a web address: https only, and a redirect never leaves it."""

from __future__ import annotations

import urllib.error
import urllib.request


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect, but never to plain http. The calendar downloads and the update check and download all use it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.casefold().startswith("https://"):
            raise urllib.error.HTTPError(req.full_url, code, "redirect away from https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_opener() -> urllib.request.OpenerDirector:
    """An opener for https addresses and nothing else. It is built from the handlers it needs, not from
    ``build_opener``'s defaults, so file:, ftp:, data: and plain http addresses have no handler and fail."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        HttpsOnlyRedirects(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener
