try:
    import hashlib as _hashlib
except ImportError:  # pragma: no cover - MicroPython
    import uhashlib as _hashlib

try:
    import binascii as _binascii
except ImportError:  # pragma: no cover - MicroPython
    import ubinascii as _binascii


class _Digest:
    def __init__(self, implementation, payload=b""):
        self._digest = implementation(payload)

    def update(self, payload):
        self._digest.update(payload)

    def digest(self):
        return self._digest.digest()

    def hexdigest(self):
        return _binascii.hexlify(self.digest()).decode("ascii")


def sha1(payload=b""):
    implementation = getattr(_hashlib, "sha1", None)
    if implementation is None:
        raise RuntimeError("device firmware does not provide SHA-1")
    return _Digest(implementation, payload)


def sha256(payload=b""):
    return _Digest(_hashlib.sha256, payload)
