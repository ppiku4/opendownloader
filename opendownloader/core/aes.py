"""AES-128-CBC decryption for standard (clear-key) HLS encryption.

Uses the OpenSSL libcrypto that ships with Python itself (via ctypes), so no
extra package is needed. Falls back to the `cryptography` package if present.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import sys
import threading

_lock = threading.Lock()
_lib = None
_tried = False


def _candidates() -> list[str]:
    out: list[str] = []
    if sys.platform == "win32":
        dirs = [getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable),
                os.path.join(sys.base_prefix, "DLLs"), os.path.join(sys.prefix, "DLLs")]
        for d in dirs:
            if d:
                out += sorted(glob.glob(os.path.join(d, "libcrypto-*.dll")), reverse=True)
    else:
        if getattr(sys, "_MEIPASS", ""):
            out += sorted(glob.glob(os.path.join(sys._MEIPASS, "libcrypto.*")))
        found = ctypes.util.find_library("crypto")
        if found:
            out.append(found)
        out += ["libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.dylib"]
    return out


def _load():
    global _lib, _tried
    with _lock:
        if _tried:
            return _lib
        _tried = True
        try:
            import _ssl  # noqa: F401  (loads Python's own libcrypto on Windows)
        except ImportError:
            pass
        for path in _candidates():
            try:
                lib = ctypes.CDLL(path)
                lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
                lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
                lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
                lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                   ctypes.c_char_p, ctypes.c_char_p]
                lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                                                  ctypes.c_char_p, ctypes.c_int]
                lib.EVP_DecryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
                _lib = lib
                break
            except (OSError, AttributeError):
                continue
        return _lib


def _openssl_decrypt(lib, data: bytes, key: bytes, iv: bytes) -> bytes:
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if lib.EVP_DecryptInit_ex(ctx, lib.EVP_aes_128_cbc(), None, key, iv) != 1:
            raise RuntimeError("EVP_DecryptInit_ex failed")
        lib.EVP_CIPHER_CTX_set_padding(ctx, 0)
        out = ctypes.create_string_buffer(len(data) + 32)
        n = ctypes.c_int(0)
        if lib.EVP_DecryptUpdate(ctx, out, ctypes.byref(n), data, len(data)) != 1:
            raise RuntimeError("EVP_DecryptUpdate failed")
        total = n.value
        tail = ctypes.create_string_buffer(32)
        n2 = ctypes.c_int(0)
        if lib.EVP_DecryptFinal_ex(ctx, tail, ctypes.byref(n2)) != 1:
            raise RuntimeError("EVP_DecryptFinal_ex failed")
        return out.raw[:total] + tail.raw[:n2.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def decrypt_aes128_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt and strip PKCS#7 padding (tolerates unpadded segments)."""
    data = data[: len(data) - len(data) % 16]
    lib = _load()
    if lib is not None:
        out = _openssl_decrypt(lib, data, key, iv)
    else:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as e:
            raise RuntimeError("No AES implementation available (install 'cryptography')") from e
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        out = dec.update(data) + dec.finalize()
    pad = out[-1] if out else 0
    if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        out = out[:-pad]
    return out
