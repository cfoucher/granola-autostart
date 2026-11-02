import urllib.error
import urllib.request

import pytest

from granola_autostart import net


def test_redirects_may_not_leave_https():
    handler = net.HttpsOnlyRedirects()
    with pytest.raises(urllib.error.HTTPError) as error:
        handler.redirect_request(urllib.request.Request("https://a/x"), None, 302, "Found", {}, "http://a/x")
    assert error.value.code == 302
    moved = handler.redirect_request(urllib.request.Request("https://a/x"), None, 302, "Found", {}, "https://b/y")
    assert moved.full_url == "https://b/y"


def test_a_redirect_to_plain_http_is_refused_whatever_its_case():
    handler = net.HttpsOnlyRedirects()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(urllib.request.Request("https://a/x"), None, 302, "Found", {}, "HTTP://a/x")
    moved = handler.redirect_request(urllib.request.Request("https://a/x"), None, 302, "Found", {}, "HTTPS://b/y")
    assert moved.full_url == "HTTPS://b/y"


def test_the_opener_follows_redirects_through_that_handler():
    opener = net.https_opener()
    assert any(isinstance(handler, net.HttpsOnlyRedirects) for handler in opener.handlers)
    assert not any(type(handler) is urllib.request.HTTPRedirectHandler for handler in opener.handlers)


def test_the_opener_has_no_handler_for_files_ftp_data_or_plain_http():
    handlers = {type(handler) for handler in net.https_opener().handlers}
    assert urllib.request.HTTPSHandler in handlers
    refused = {urllib.request.FileHandler, urllib.request.FTPHandler, urllib.request.DataHandler, urllib.request.HTTPHandler}
    assert not handlers & refused
    # What is left has to be enough to open an address and report an error status.
    assert {urllib.request.HTTPErrorProcessor, urllib.request.HTTPDefaultErrorHandler, urllib.request.UnknownHandler} <= handlers


@pytest.mark.parametrize("address", ["file:///C:/Windows/win.ini", "data:text/plain,hi", "ftp://example.com/x", "http://example.com/x"])
def test_an_address_that_is_not_https_is_not_opened(address, monkeypatch):
    # No proxy is set, so nothing here could reach a network.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "http_proxy", "https_proxy", "ftp_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        net.https_opener().open(address, timeout=1)
