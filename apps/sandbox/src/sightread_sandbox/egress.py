"""The egress proxy: the sandbox VMs' one way out (docs/sandbox.md § Network).

An HTTP `CONNECT` to port 443 of a host on the list the worker writes, whose addresses are
all global, carrying a TLS hello that names the same host, and nothing else. The proxy
holds no secret and no setting of the database, and past the hello it sees nothing of
what the tunnel carries. `python -m sightread_sandbox.egress`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import EgressSettings

# Named outright: run with `python -m`, `__name__` is `__main__`.
logger = logging.getLogger("sightread_sandbox.egress")

# One label of a host name: the rule the API checks SANDBOX_EGRESS_HOSTS by
# (sightread.config.parse_egress_hosts). The file is the worker's, but nothing in it
# passes unchecked.
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# A CONNECT is a line and a few headers: a longer head is refused.
HEAD_MAX = 8192
# A TLS record carries at most 2^14 bytes (RFC 8446 § 5.1). A ClientHello past 64 KiB,
# over however many records, is refused: a real one fits in one record.
RECORD_MAX = 1 << 14
HELLO_MAX = 1 << 16
CHUNK = 1 << 16

Resolve = Callable[[str], Awaitable[list[str]]]
Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class Refused(Exception):
    """A request answered with an error status; the reason is the status line's text and
    the log's."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class NoHello(Exception):
    """What followed the tunnel's opening is not a TLS ClientHello the proxy can read."""


def plain_name(name: str) -> bool:
    """A host name as the list holds one: two labels or more, the last not all digits,
    so no address passes for a name."""
    labels = name.split(".")
    return (
        len(name) <= 253
        and len(labels) >= 2
        and all(LABEL.fullmatch(label) for label in labels)
        and not labels[-1].isdigit()
    )


def listed(host: str, entries: frozenset[str]) -> bool:
    """`host` on the list: named there, or under a `*.example.com` entry, which takes every
    name under `example.com` and not `example.com` itself."""
    if not plain_name(host):
        return False
    labels = host.split(".")
    return host in entries or any(
        "*." + ".".join(labels[index:]) in entries for index in range(1, len(labels))
    )


def is_global(address: str) -> bool:
    """A public address: not loopback, private, link-local, multicast, reserved or
    unspecified — the guard the worker's web fetches keep (sightread.jobs.chat_web)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_addresses(host: str) -> list[str]:
    """Every address the name resolves to, once each, in the system resolver's order:
    glibc puts an address this host has no route to after the others."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class HostList:
    """The list in the file the worker writes (sightread.sandbox.egress), read again
    whenever the file changes: the worker replaces it whole, so a change is a new inode.
    No file, or one that cannot be read, lets nothing through."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: frozenset[str] = frozenset()
        # The file's identity when last read; `()` once reading it failed, so the failure
        # is logged once and not again until the file has been read.
        self._stamp: tuple[int, ...] | None = None

    def current(self) -> frozenset[str]:
        try:
            status = self.path.stat()
            stamp = (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
            if stamp != self._stamp:
                text = self.path.read_text(encoding="utf-8")
                entries = set()
                for line in text.splitlines():
                    entry = line.strip().lower()
                    if not entry or entry.startswith("#"):
                        continue
                    if plain_name(entry.removeprefix("*.")):
                        entries.add(entry)
                    else:
                        logger.warning("skipped %r in %s: not a host name", entry[:100], self.path)
                self.entries, self._stamp = frozenset(entries), stamp
                logger.info("the sandbox's sites: %s", ", ".join(sorted(entries)) or "none")
        except (OSError, UnicodeDecodeError) as exc:
            if self._stamp != ():
                logger.warning(
                    "cannot read the sandbox's sites at %s (%s): nothing goes through",
                    self.path,
                    exc.__class__.__name__,
                )
            self.entries, self._stamp = frozenset(), ()
        return self.entries


def response(status: int, reason: str) -> bytes:
    """An error answer, the reason in its status line — which Python's clients repeat in
    their error — and as its body; the connection closes after it."""
    body = f"{reason}\n".encode()
    allow = "Allow: CONNECT\r\n" if status == 405 else ""
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{allow}Connection: close\r\n\r\n"
    )
    return head.encode() + body


async def read_head(reader: asyncio.StreamReader) -> bytes | None:
    """The request head through its blank line; None for a connection closed before its
    first byte — the healthcheck's probe."""
    head = bytearray()
    while True:
        try:
            line = await reader.readline()
        except ValueError as exc:
            # A line past the stream's buffer limit.
            raise Refused(400, "The request head is too long") from exc
        if not line:
            if not head:
                return None
            raise Refused(400, "The request ended before its blank line")
        head += line
        if len(head) > HEAD_MAX:
            raise Refused(400, "The request head is too long")
        if line in (b"\r\n", b"\n"):
            return bytes(head)


class Fields:
    """A TLS structure read field by field, every length checked against what is there."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def take(self, size: int) -> bytes:
        if self.at + size > len(self.data):
            raise NoHello("a TLS hello cut short")
        self.at += size
        return self.data[self.at - size : self.at]

    def number(self, size: int) -> int:
        return int.from_bytes(self.take(size))

    def vector(self, length_size: int) -> bytes:
        return self.take(self.number(length_size))

    def done(self) -> bool:
        return self.at == len(self.data)


def server_name(hello: bytes) -> str | None:
    """The host name of a ClientHello's body (RFC 8446 § 4.1.2): its one `server_name`
    entry (RFC 6066 § 3), lower-cased; None when it has none. A hello that does not parse,
    or names more than one, raises `NoHello`: a server could read either name."""
    fields = Fields(hello)
    fields.take(2 + 32)  # legacy_version, random
    fields.vector(1)  # legacy_session_id
    fields.vector(2)  # cipher_suites
    fields.vector(1)  # legacy_compression_methods
    if fields.done():
        return None
    extensions = Fields(fields.vector(2))
    if not fields.done():
        raise NoHello("bytes after a TLS hello's extensions")
    names: list[bytes] = []
    seen: set[int] = set()
    while not extensions.done():
        kind = extensions.number(2)
        data = extensions.vector(2)
        if kind in seen:
            # RFC 8446 § 4.2: an extension appears once; servers differ on a second.
            raise NoHello("an extension twice in a TLS hello")
        seen.add(kind)
        if kind == 0:  # server_name
            listing = Fields(data)
            entries = Fields(listing.vector(2))
            if not listing.done():
                raise NoHello("bytes after a TLS hello's server names")
            while not entries.done():
                name_type = entries.number(1)
                name = entries.vector(2)
                if name_type == 0:  # host_name
                    names.append(name)
    if not names:
        return None
    if len(names) > 1:
        raise NoHello("a TLS hello naming two hosts")
    try:
        return names[0].decode("ascii").lower().removesuffix(".")
    except UnicodeDecodeError as exc:
        raise NoHello("a TLS hello naming a host that is not ASCII") from exc


async def read_hello(reader: asyncio.StreamReader) -> tuple[bytes, str | None]:
    """The client's first TLS records as read, through the end of its ClientHello, which
    one record or several may carry (RFC 8446 § 5.1), and the host name it names. Anything
    else — another record, another message, the connection closed — raises `NoHello`."""
    raw = bytearray()
    message = bytearray()
    try:
        while True:
            header = await reader.readexactly(5)
            length = int.from_bytes(header[3:5])
            # A handshake record (22) of TLS's version family (3), not empty, not past
            # the size a record may carry.
            if header[0] != 22 or header[1] != 3 or not 0 < length <= RECORD_MAX:
                raise NoHello("not a TLS handshake record")
            fragment = await reader.readexactly(length)
            raw += header + fragment
            message += fragment
            if len(message) < 4:
                continue
            if message[0] != 1:  # client_hello
                raise NoHello("a TLS handshake that is not a ClientHello")
            size = 4 + int.from_bytes(message[1:4])
            if size > HELLO_MAX:
                raise NoHello("a TLS hello past the size the proxy reads")
            if len(message) >= size:
                return bytes(raw), server_name(bytes(message[4:size]))
    except asyncio.IncompleteReadError as exc:
        raise NoHello("the connection closed before a TLS hello") from exc


class Proxy:
    """The CONNECT handler. `resolve` and `connect` are the system's resolver and
    `asyncio.open_connection` unless a test passes its own."""

    def __init__(
        self,
        settings: EgressSettings,
        *,
        resolve: Resolve | None = None,
        connect: Connect | None = None,
    ) -> None:
        self.settings = settings
        self.hosts = HostList(Path(settings.sandbox_egress_hosts_file))
        self.resolve = resolve or resolve_addresses
        self.connect = connect or asyncio.open_connection
        # Connections open per client address: one VM each on the network.
        self.open: dict[str, int] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client = str(peer[0]) if peer else "unknown"
        self.open[client] = self.open.get(client, 0) + 1
        try:
            await self.serve(client, reader, writer)
        except OSError as exc:
            # A peer gone mid-way: a reset, a broken pipe.
            logger.info("connection of %s ended: %s", client, exc.__class__.__name__)
        except Exception:
            logger.exception("connection of %s failed", client)
        finally:
            self.open[client] -= 1
            if not self.open[client]:
                del self.open[client]
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def serve(
        self, client: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        timeout = self.settings.sandbox_egress_connect_timeout_seconds
        try:
            try:
                async with asyncio.timeout(timeout):
                    head = await read_head(reader)
            except TimeoutError:
                raise Refused(408, "No request in time") from None
            if head is None:
                return
            host = self.check(head, self.open[client])
            upstream = await self.open_tunnel(host)
        except Refused as refusal:
            logger.info("refused %s: %s", client, refusal.reason)
            writer.write(response(refusal.status, refusal.reason))
            with contextlib.suppress(OSError):
                await writer.drain()
            return
        upstream_reader, upstream_writer = upstream
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            try:
                async with asyncio.timeout(timeout):
                    hello, name = await read_hello(reader)
            except (TimeoutError, NoHello) as exc:
                reason = str(exc) or "no TLS hello in time"
                logger.warning("closed %s to %s: %s", client, host, reason)
                return
            if name != host:
                # A CDN serves many sites at one address and answers for whichever the
                # hello names: only the listed one may be named.
                logger.warning("closed %s to %s: its TLS hello names %r", client, host, name)
                return
            upstream_writer.write(hello)
            await upstream_writer.drain()
            logger.info("tunnel %s to %s", client, host)
            sent, received = await self.pipe(reader, writer, upstream_reader, upstream_writer)
            logger.info("tunnel %s to %s closed: %d bytes out, %d in", client, host, sent, received)
        finally:
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()

    def check(self, head: bytes, open_now: int) -> str:
        """The host a request may tunnel to, or why it may not — decided before any name
        is looked up, so a refused name never reaches a resolver."""
        parts = head.splitlines()[0].decode("latin-1").split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise Refused(400, "The proxy takes CONNECT host:443 HTTP/1.1")
        method, target, _version = parts
        if method != "CONNECT":
            raise Refused(405, "Only HTTPS leaves the sandbox, through CONNECT to port 443")
        name, colon, port = target.rpartition(":")
        if not colon or not (port.isascii() and port.isdigit()):
            raise Refused(400, "CONNECT names host:port")
        if int(port) != 443:
            raise Refused(403, "Only port 443 is open")
        if open_now > self.settings.sandbox_egress_max_tunnels:
            raise Refused(429, "Too many tunnels from this sandbox at once")
        host = name.lower().removesuffix(".")
        if not listed(host, self.hosts.current()):
            shown = host if plain_name(host) else "That host"
            raise Refused(403, f"{shown} is not on the sandbox's list of sites")
        return host

    async def open_tunnel(self, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection to the host's checked addresses, each in the resolver's order, the
        next only when one cannot be reached — all within the connect timeout."""
        try:
            async with asyncio.timeout(self.settings.sandbox_egress_connect_timeout_seconds):
                try:
                    addresses = await self.resolve(host)
                except (OSError, UnicodeError) as exc:
                    raise Refused(502, f"{host} could not be resolved") from exc
                if not addresses:
                    raise Refused(502, f"{host} could not be resolved")
                if not all(is_global(address) for address in addresses):
                    raise Refused(403, f"{host} resolves to an address that is not public")
                for address in addresses[:-1]:
                    # An address the proxy cannot connect to — an IPv6 one without an
                    # IPv6 route, a server that refuses — gives way to the next one.
                    with contextlib.suppress(OSError):
                        return await self.connect(address, 443)
                try:
                    return await self.connect(addresses[-1], 443)
                except OSError:
                    raise Refused(502, f"{host} could not be reached") from None
        except TimeoutError:
            raise Refused(504, f"{host} did not answer in time") from None

    async def pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        """Both ways until both have ended, one fails, or neither has moved a byte for
        `SANDBOX_EGRESS_IDLE_SECONDS`; the bytes moved each way. An end one way is passed
        on as one (a half close), and the other way carries on."""
        loop = asyncio.get_running_loop()
        idle = self.settings.sandbox_egress_idle_seconds
        last = loop.time()
        moved = [0, 0]

        async def copy(
            source: asyncio.StreamReader, target: asyncio.StreamWriter, way: int
        ) -> None:
            nonlocal last
            while data := await source.read(CHUNK):
                last = loop.time()
                moved[way] += len(data)
                target.write(data)
                await target.drain()
            if target.can_write_eof():
                target.write_eof()

        copies = {
            asyncio.create_task(copy(reader, upstream_writer, 0)),
            asyncio.create_task(copy(upstream_reader, writer, 1)),
        }
        try:
            while copies:
                done, copies = await asyncio.wait(
                    copies, timeout=max(0.0, last + idle - loop.time())
                )
                # Every finished copy's outcome is taken, so none is left unretrieved.
                failed = [task for task in done if task.exception() is not None]
                if failed or (not done and loop.time() - last >= idle):
                    break
        finally:
            for task in copies:
                task.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
        return moved[0], moved[1]


async def run(settings: EgressSettings) -> None:
    proxy = Proxy(settings)
    # Every interface of the container: it is on the sandbox's network and its own way
    # out alone (docs/sandbox.md § Network).
    server = await asyncio.start_server(
        proxy.handle,
        host="0.0.0.0",  # noqa: S104
        port=settings.sandbox_egress_port,
    )
    logger.info(
        "egress proxy on port %d, the list at %s",
        settings.sandbox_egress_port,
        settings.sandbox_egress_hosts_file,
    )
    proxy.hosts.current()
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(EgressSettings()))


if __name__ == "__main__":
    main()
