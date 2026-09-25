"""Suite-wide guards (SPEC §8.9, F-CORE).

* An autouse **socket guard** refuses every connection to a non-loopback address:
  ``socket.connect`` / ``connect_ex`` / ``sendto``, ``socket.create_connection`` and
  ``socket.getaddrinfo`` of a non-local host raise :class:`NetworkBlocked`. Loopback
  (127.0.0.0/8, ::1, ``localhost``), ``AF_UNIX`` and ``socket.socketpair()`` stay allowed —
  asyncio's Windows event loop uses a loopback socketpair. A blocked attempt also fails the test
  at teardown, even when the code under test swallowed the error. Tests that provoke a block on
  purpose call ``socket_guard.clear()`` afterwards.
* Markers (registered in pyproject.toml): ``perf`` (excluded by default), ``slow``, ``gate``,
  ``needs_ssh_keygen`` (skipped without ``ssh-keygen`` on PATH), ``local_corpus`` (skipped unless
  ``TOKENBILL_LOCAL_CORPUS=1``).
* Hypothesis: with ``CI`` set, the ``ci`` profile derandomizes example generation so CI runs are
  reproducible (``HYPOTHESIS_PROFILE`` selects another registered profile).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
from collections.abc import Iterator
from typing import Any

import pytest

try:  # hypothesis is a dev dependency; the guards below must not require it
    from hypothesis import settings as _hypothesis_settings
except ImportError:  # pragma: no cover
    _hypothesis_settings = None

if _hypothesis_settings is not None:
    _hypothesis_settings.register_profile("ci", derandomize=True, deadline=None, print_blob=True)
    _hypothesis_settings.register_profile("dev", deadline=None)
    _profile = os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else None)
    if _profile:
        _hypothesis_settings.load_profile(_profile)


class NetworkBlocked(RuntimeError):
    """Raised by the socket guard for a non-loopback network operation."""


_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def _host_of(address: Any) -> Any:
    if isinstance(address, tuple) and address:
        return address[0]
    return address


def is_local_host(host: Any) -> bool:
    """True for loopback addresses, ``localhost`` names and ``None``/empty (unspecified local)."""
    if host is None:
        return True
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    name = host.strip().lower().rstrip(".")
    if name in ("",) or name in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _is_allowed(sock: socket.socket, address: Any) -> bool:
    family = getattr(socket, "AF_UNIX", None)
    if family is not None and sock.family == family:
        return True
    return is_local_host(_host_of(address))


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Refuse non-loopback network access for the duration of every test (see module docstring)."""
    blocked: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what: str) -> NetworkBlocked:
        blocked.append(what)
        return NetworkBlocked(f"network access blocked by the test socket guard: {what}")

    def connect(self: socket.socket, address: Any) -> None:
        if not _is_allowed(self, address):
            raise refuse("connect")
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if not _is_allowed(self, address):
            raise refuse("connect_ex")
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: bytes, *args: Any) -> int:
        address = args[-1] if args else None
        if not _is_allowed(self, address):
            raise refuse("sendto")
        return real_sendto(self, data, *args)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        if not is_local_host(_host_of(address)):
            raise refuse("create_connection")
        return real_create_connection(address, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not is_local_host(host):
            raise refuse("getaddrinfo")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield blocked
    if blocked:
        pytest.fail(
            f"test attempted non-loopback network access ({', '.join(blocked)})", pytrace=False
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``local_corpus`` tests unless opted in, and ``needs_ssh_keygen`` tests without
    ssh-keygen."""
    corpus = os.environ.get("TOKENBILL_LOCAL_CORPUS") == "1"
    ssh_keygen = shutil.which("ssh-keygen") is not None
    skip_corpus = pytest.mark.skip(reason="set TOKENBILL_LOCAL_CORPUS=1 to read the local corpus")
    skip_ssh = pytest.mark.skip(reason="ssh-keygen not on PATH")
    for item in items:
        if "local_corpus" in item.keywords and not corpus:
            item.add_marker(skip_corpus)
        if "needs_ssh_keygen" in item.keywords and not ssh_keygen:
            item.add_marker(skip_ssh)
