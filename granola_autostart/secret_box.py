"""Encrypt calendar addresses for this Windows user with DPAPI.

A secret iCal address works like a password, so config.json keeps it encrypted. Only the same
Windows user on the same PC can decrypt it.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

_ENTROPY = b"granola-autostart calendar address"
_UI_FORBIDDEN = 0x01


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def protect(text: str) -> str:
    return base64.b64encode(_crypt("CryptProtectData", text.encode("utf-8"))).decode("ascii")


def unprotect(token: str) -> str:
    """Raises ValueError when the token is not ours to read (another user, another PC, or edited)."""
    try:
        data = base64.b64decode(token.encode("ascii"), validate=True)
        return _crypt("CryptUnprotectData", data).decode("utf-8")
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError("could not decrypt") from error


def _crypt(name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = getattr(crypt32, name)
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Blob),
    ]
    function.restype = wintypes.BOOL
    source_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.c_void_p))
    entropy = _Blob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.c_void_p))
    result = _Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), f"{name} failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(result.pbData)
