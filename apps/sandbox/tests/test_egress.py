"""The egress proxy over a stubbed resolver and a local site: nothing leaves this host."""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest

from sightread_sandbox.config import EgressSettings
from sightread_sandbox.egress import HostList, NoHello, Proxy, listed, server_name

# A public address the stub resolver hands out; nothing ever connects to it.
PUBLIC = "151.101.0.223"


def write_list(path: Path, *hosts: str) -> None:
    """The file as the worker writes it: a header, one entry a line, replaced whole."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text("# the sandbox's sites\n" + "".join(f"{host}\n" for host in hosts))
    os.replace(partial, path)


def openssl_hello(server_hostname: str | None) -> bytes:
    """The records a real TLS client opens with: OpenSSL's ClientHello, SNI and all."""
    context = ssl.create_default_context()
    if server_hostname is None:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    tls = context.wrap_bio(
        ssl.MemoryBIO(), outgoing := ssl.MemoryBIO(), server_hostname=server_hostname
    )
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def hello_body(*extensions: tuple[int, bytes], with_extensions: bool = True) -> bytes:
    """A ClientHello's body, built by hand: one cipher suite, the given extensions."""
    body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
    if not with_extensions:
        return body
    block = b"".join(kind.to_bytes(2) + len(data).to_bytes(2) + data for kind, data in extensions)
    return body + len(block).to_bytes(2) + block


def sni(*names: bytes) -> bytes:
    entries = b"".join(b"\x00" + len(name).to_bytes(2) + name for name in names)
    return len(entries).to_bytes(2) + entries


def record(body: bytes) -> bytes:
    """A ClientHello body as one handshake record."""
    message = b"\x01" + len(body).to_bytes(3) + body
    return b"\x16\x03\x01" + len(message).to_bytes(2) + message


async def read_to_end(reader: asyncio.StreamReader) -> bytes:
    """Everything until the proxy closes; a reset counts as closed."""
    try:
        return await asyncio.wait_for(reader.read(), 5)
    except ConnectionResetError:
        return b""


@pytest.fixture
async def site():
    """A site that echoes what it receives and keeps all of it for the test to read."""
    received = bytearray()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while data := await reader.read(65536):
                received.extend(data)
                writer.write(data)
                await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield SimpleNamespace(port=server.sockets[0].getsockname()[1], received=received)
    server.close()


@pytest.fixture
async def start(tmp_path, site):
    """Starts a proxy on the list given — no file at all for None — over a resolver that
    answers from `addresses` (PUBLIC otherwise) and a connect that reaches the site, or
    refuses for an address in `refuse`. Returns its port, the lookups and the attempts."""
    servers = []
    clients: list[asyncio.StreamWriter] = []

    async def start_proxy(
        hosts: tuple[str, ...] | None = ("pypi.org", "files.pythonhosted.org"),
        addresses: dict[str, list[str]] | None = None,
        refuse: tuple[str, ...] = (),
        resolve_delay: float = 0.0,
        **limits: float,
    ) -> SimpleNamespace:
        hosts_file = tmp_path / "hosts"
        if hosts is not None:
            write_list(hosts_file, *hosts)
        settings = EgressSettings(sandbox_egress_hosts_file=str(hosts_file), **limits)
        lookups: list[str] = []
        attempts: list[tuple[str, int]] = []

        async def resolve(host: str) -> list[str]:
            lookups.append(host)
            await asyncio.sleep(resolve_delay)
            return list((addresses or {}).get(host, [PUBLIC]))

        async def connect(address: str, port: int):
            attempts.append((address, port))
            if address in refuse:
                raise ConnectionRefusedError(111, "Connection refused")
            return await asyncio.open_connection("127.0.0.1", site.port)

        proxy = Proxy(settings, resolve=resolve, connect=connect)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]

        async def request(line: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append(writer)
            writer.write(f"{line}\r\nUser-Agent: test\r\n\r\n".encode())
            return reader, writer, await asyncio.wait_for(reader.readline(), 5)

        async def tunnel(host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            reader, writer, status = await request(f"CONNECT {host}:443 HTTP/1.1")
            assert status.startswith(b"HTTP/1.1 200 "), status
            assert await reader.readline() == b"\r\n"
            return reader, writer

        return SimpleNamespace(
            port=port,
            proxy=proxy,
            lookups=lookups,
            attempts=attempts,
            request=request,
            tunnel=tunnel,
            hosts_file=hosts_file,
            clients=clients,
        )

    yield start_proxy
    for writer in clients:
        writer.close()
    for server in servers:
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 5)


async def test_a_listed_host_is_tunnelled_with_its_hello_first(start, site) -> None:
    proxy = await start()
    reader, writer = await proxy.tunnel("pypi.org")
    hello = openssl_hello("pypi.org")
    writer.write(hello + b"the rest")
    await writer.drain()

    # What the site receives is the hello, untouched, and what follows it; the echo comes
    # back through the tunnel.
    echoed = await asyncio.wait_for(reader.readexactly(len(hello) + 8), 5)
    assert echoed == hello + b"the rest"
    assert bytes(site.received) == hello + b"the rest"
    assert proxy.lookups == ["pypi.org"]
    assert proxy.attempts == [(PUBLIC, 443)]


async def test_a_host_off_the_list_is_refused_before_any_lookup(start) -> None:
    proxy = await start(hosts=("pypi.org", "*.example.com"))
    for target in ("github.com", "example.com", "pypi.org.evil.example", "evilpypi.org"):
        reader, _, status = await proxy.request(f"CONNECT {target}:443 HTTP/1.1")
        assert status.startswith(b"HTTP/1.1 403 "), (target, status)
        assert f"{target} is not on the sandbox's list of sites".encode() in status
        assert (await read_to_end(reader)).endswith(b"list of sites\n")
    assert proxy.lookups == []

    # `*.example.com` takes every name under it, at any depth, in any case.
    await proxy.tunnel("files.example.com")
    await proxy.tunnel("A.B.Example.COM")
    # A trailing dot is the same name.
    await proxy.tunnel("pypi.org.")
    assert proxy.lookups == ["files.example.com", "a.b.example.com", "pypi.org"]


async def test_only_connect_to_port_443_leaves(start) -> None:
    proxy = await start()
    reader, _, status = await proxy.request("GET http://pypi.org/simple/ HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 405 ")
    assert b"Allow: CONNECT\r\n" in await read_to_end(reader)

    _, _, status = await proxy.request("CONNECT pypi.org:80 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 403 Only port 443 is open")
    for line in ("CONNECT pypi.org HTTP/1.1", "CONNECT pypi.org:443", "hello"):
        _, _, status = await proxy.request(line)
        assert status.startswith(b"HTTP/1.1 400 "), (line, status)
    assert proxy.lookups == []


async def test_an_address_never_passes_for_a_name(start) -> None:
    proxy = await start(hosts=("pypi.org",))
    for target in (PUBLIC, "[2a04:4e42::223]", "127.0.0.1", "localhost"):
        _, _, status = await proxy.request(f"CONNECT {target}:443 HTTP/1.1")
        assert status.startswith(b"HTTP/1.1 403 "), (target, status)
    assert proxy.lookups == []


async def test_a_listed_name_pointing_inside_is_refused(start) -> None:
    for inside in ("10.250.250.3", "127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1"):
        proxy = await start(addresses={"pypi.org": [PUBLIC, inside]})
        _, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
        assert status.startswith(b"HTTP/1.1 403 pypi.org resolves to an address that is not public")
        assert proxy.attempts == [], inside


async def test_the_next_address_when_one_cannot_be_reached(start, site) -> None:
    proxy = await start(
        addresses={"pypi.org": ["2a04:4e42::223", PUBLIC]}, refuse=("2a04:4e42::223",)
    )
    await proxy.tunnel("pypi.org")
    assert proxy.attempts == [("2a04:4e42::223", 443), (PUBLIC, 443)]

    proxy = await start(addresses={"pypi.org": [PUBLIC]}, refuse=(PUBLIC,))
    _, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 502 pypi.org could not be reached")

    proxy = await start(resolve_delay=1.0, sandbox_egress_connect_timeout_seconds=0.2)
    _, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 504 pypi.org did not answer in time")


@pytest.mark.parametrize(
    "first_bytes",
    [
        pytest.param(openssl_hello("evil.example"), id="another-name"),
        pytest.param(openssl_hello(None), id="no-name"),
        pytest.param(record(hello_body((0, sni(b"pypi.org", b"evil.example")))), id="two-names"),
        pytest.param(
            record(hello_body((0, sni(b"pypi.org")), (0, sni(b"evil.example")))),
            id="two-server-name-extensions",
        ),
        pytest.param(b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n", id="not-tls"),
    ],
)
async def test_a_tunnel_without_the_listed_name_in_its_hello_closes_before_the_site(
    start, site, first_bytes
) -> None:
    proxy = await start()
    reader, writer = await proxy.tunnel("pypi.org")
    writer.write(first_bytes)
    await writer.drain()
    assert await read_to_end(reader) == b""
    # The proxy had connected, but not a byte reached the site.
    assert proxy.attempts == [(PUBLIC, 443)]
    assert bytes(site.received) == b""


async def test_a_hello_over_two_records_is_read_whole(start, site) -> None:
    proxy = await start()
    hello = openssl_hello("pypi.org")
    message = hello[5:]
    first, second = message[:40], message[40:]
    split = (
        hello[:3] + len(first).to_bytes(2) + first + hello[:3] + len(second).to_bytes(2) + second
    )
    reader, writer = await proxy.tunnel("pypi.org")
    writer.write(split)
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(split)), 5) == split
    assert bytes(site.received) == split


async def test_the_list_follows_the_file(start) -> None:
    # No file yet — a worker that has not written it — lets nothing through.
    proxy = await start(hosts=None)
    _, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 403 ")

    write_list(proxy.hosts_file, "pypi.org")
    await proxy.tunnel("pypi.org")

    write_list(proxy.hosts_file, "github.com")
    _, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 403 ")
    await proxy.tunnel("github.com")

    # Emptied, nothing goes through.
    write_list(proxy.hosts_file)
    _, _, status = await proxy.request("CONNECT github.com:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 403 ")


def test_entries_that_are_not_host_names_are_skipped(tmp_path) -> None:
    path = tmp_path / "hosts"
    write_list(
        path,
        "https://github.com/astral-sh",
        "10.0.0.1",
        "*.com",
        "*",
        "localhost",
        "github.com:443",
        "PyPI.org",
        "*.githubusercontent.com",
    )
    assert HostList(path).current() == frozenset({"pypi.org", "*.githubusercontent.com"})


def test_a_wildcard_takes_the_names_under_it_alone() -> None:
    entries = frozenset({"*.example.com", "pypi.org"})
    assert listed("a.example.com", entries)
    assert listed("a.b.example.com", entries)
    assert not listed("example.com", entries)
    assert not listed("badexample.com", entries)
    assert not listed("*.example.com", entries)
    assert not listed("www.pypi.org", entries)


async def test_too_many_tunnels_from_one_sandbox(start) -> None:
    proxy = await start(sandbox_egress_max_tunnels=1)
    await proxy.tunnel("pypi.org")
    reader, _, status = await proxy.request("CONNECT pypi.org:443 HTTP/1.1")
    assert status.startswith(b"HTTP/1.1 429 ")
    # Counted by the sandbox's address: the refused one is gone, the open one stays.
    await read_to_end(reader)
    assert proxy.proxy.open == {"127.0.0.1": 1}


async def test_a_quiet_tunnel_is_closed(start) -> None:
    proxy = await start(sandbox_egress_idle_seconds=0.3)
    reader, writer = await proxy.tunnel("pypi.org")
    hello = openssl_hello("pypi.org")
    writer.write(hello)
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(len(hello)), 5) == hello
    # Neither way moves a byte: the proxy closes the tunnel.
    assert await read_to_end(reader) == b""


async def test_a_request_must_come_in_time(start) -> None:
    proxy = await start(sandbox_egress_connect_timeout_seconds=0.2)
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    proxy.clients.append(writer)
    assert (await read_to_end(reader)).startswith(b"HTTP/1.1 408 ")

    # And the hello, once the tunnel is open.
    reader, _ = await proxy.tunnel("pypi.org")
    assert await read_to_end(reader) == b""


def test_a_hello_is_read_strictly() -> None:
    assert server_name(hello_body((0, sni(b"PyPI.org")))) == "pypi.org"
    assert server_name(hello_body((0x2B, b"\x02\x03\x04"))) is None
    assert server_name(hello_body(with_extensions=False)) is None
    with pytest.raises(NoHello):
        server_name(hello_body((0, sni(b"pypi.org")))[:-3])
    with pytest.raises(NoHello):
        server_name(hello_body((0x2B, b"\x02\x03\x04"), (0x2B, b"\x02\x03\x04")))
    with pytest.raises(NoHello):
        server_name(hello_body((0, sni("pypí.org".encode()))))
