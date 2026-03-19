#!/usr/bin/env python3
"""
Transparent domain-filtering proxy for the Nepher sandbox.

Intercepts outbound TCP connections redirected by iptables NAT REDIRECT and
filters them by hostname.  Only connections to whitelisted domains are relayed;
everything else is silently dropped.

Protocol support:
  - HTTPS (port 443): hostname extracted from TLS ClientHello SNI extension.
    No TLS termination, no certificates — raw TCP relay after SNI check.
  - HTTP  (port 80):  hostname extracted from the HTTP Host header.

Design:
  iptables redirects outbound traffic to local ports where this proxy listens.
  The proxy recovers the original destination via SO_ORIGINAL_DST, inspects the
  first packet for a hostname, and either relays or drops the connection.
  The proxy runs as a dedicated non-root user (sniproxy) so that iptables can
  exempt its own outbound connections from being redirected (--uid-owner).

Usage:
    python3 sni-proxy.py --https-port 3129 --http-port 3128 \\
        --whitelist domain1.com,domain2.com

    iptables -t nat -A OUTPUT -m owner --uid-owner sniproxy -j RETURN
    iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-port 3129
    iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-port 3128
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import struct
import sys

# Linux kernel constant — used with getsockopt() to recover the original
# destination address after an iptables NAT REDIRECT.
SO_ORIGINAL_DST = 80

logger = logging.getLogger("sni-proxy")


# ---------------------------------------------------------------------------
# Hostname extraction
# ---------------------------------------------------------------------------

def get_original_dst(sock: socket.socket) -> tuple[str, int]:
    """Recover the original (pre-NAT) destination IP and port."""
    dst = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack("!H", dst[2:4])[0]
    ip = socket.inet_ntoa(dst[4:8])
    return ip, port


def parse_tls_sni(data: bytes) -> str | None:
    """Extract the SNI hostname from a TLS ClientHello message.

    Walks the TLS record → Handshake → ClientHello → Extensions to find the
    Server Name Indication (type 0x00) extension.  Returns ``None`` if the
    data is not a valid ClientHello or does not contain an SNI extension.
    """
    if len(data) < 5 or data[0] != 0x16:  # TLS Handshake
        return None

    record_len = struct.unpack("!H", data[3:5])[0]
    if len(data) < 5 + record_len:
        return None

    pos = 5
    if pos >= len(data) or data[pos] != 0x01:  # ClientHello
        return None
    pos += 4  # type(1) + length(3)

    # Skip version(2) + random(32) + session_id(variable)
    pos += 34
    if pos >= len(data):
        return None
    pos += 1 + data[pos]  # session_id

    # Skip cipher_suites(variable)
    if pos + 2 > len(data):
        return None
    pos += 2 + struct.unpack("!H", data[pos:pos + 2])[0]

    # Skip compression_methods(variable)
    if pos >= len(data):
        return None
    pos += 1 + data[pos]

    # Extensions
    if pos + 2 > len(data):
        return None
    ext_end = pos + 2 + struct.unpack("!H", data[pos:pos + 2])[0]
    pos += 2

    while pos + 4 <= ext_end:
        ext_type = struct.unpack("!H", data[pos:pos + 2])[0]
        ext_len = struct.unpack("!H", data[pos + 2:pos + 4])[0]
        pos += 4

        if ext_type == 0x00:  # SNI
            if pos + 2 > len(data):
                return None
            sni_pos = pos + 2  # skip SNI list length
            sni_end = sni_pos + struct.unpack("!H", data[pos:pos + 2])[0]
            while sni_pos + 3 <= sni_end:
                name_type = data[sni_pos]
                name_len = struct.unpack("!H", data[sni_pos + 1:sni_pos + 3])[0]
                sni_pos += 3
                if name_type == 0x00 and sni_pos + name_len <= len(data):
                    return data[sni_pos:sni_pos + name_len].decode("ascii", errors="ignore")
                sni_pos += name_len
            return None

        pos += ext_len

    return None


def parse_http_host(data: bytes) -> str | None:
    """Extract the Host header value from an HTTP request.

    Only inspects the header portion (up to the first ``\\r\\n\\r\\n``).
    Returns the hostname without a port suffix, or ``None``.
    """
    try:
        header_end = data.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = len(data)
        for line in data[:header_end].decode("ascii", errors="ignore").split("\r\n"):
            if line.lower().startswith("host:"):
                host = line[5:].strip()
                if ":" in host:
                    host = host.split(":")[0]
                return host
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Whitelist matching
# ---------------------------------------------------------------------------

def matches_whitelist(hostname: str, whitelist: set[str]) -> bool:
    """Return True if *hostname* matches any entry in *whitelist*.

    Supports both exact matches and subdomain matches
    """
    hostname = hostname.lower()
    for domain in whitelist:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


# ---------------------------------------------------------------------------
# Async TCP relay
# ---------------------------------------------------------------------------

async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes from *reader* to *writer* until EOF or error."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    whitelist: set[str],
    *,
    is_tls: bool,
) -> None:
    """Handle a single intercepted TCP connection.

    1. Recover the original destination via SO_ORIGINAL_DST.
    2. Read the first packet and extract the hostname (SNI or Host header).
    3. If the hostname is whitelisted, open a connection to the original
       destination, forward the already-read data, and relay both directions.
    4. Otherwise, close the connection immediately.
    """
    client_addr = client_writer.get_extra_info("peername")
    sock = client_writer.get_extra_info("socket")
    proto = "HTTPS" if is_tls else "HTTP"

    try:
        orig_ip, orig_port = get_original_dst(sock)
    except Exception as exc:
        logger.warning(f"[{client_addr}] Failed to get original dst: {exc}")
        client_writer.close()
        return

    try:
        data = await asyncio.wait_for(client_reader.read(16384), timeout=5.0)
        if not data:
            client_writer.close()
            return

        hostname = parse_tls_sni(data) if is_tls else parse_http_host(data)

        if not hostname:
            logger.info(f"[{proto}] [{client_addr}] -> {orig_ip}:{orig_port} — no hostname, BLOCKED")
            client_writer.close()
            return

        if not matches_whitelist(hostname, whitelist):
            logger.info(f"[{proto}] [{client_addr}] -> {hostname} ({orig_ip}:{orig_port}) — BLOCKED")
            client_writer.close()
            return

        logger.info(f"[{proto}] [{client_addr}] -> {hostname} ({orig_ip}:{orig_port}) — ALLOWED")

        # Connect to the real server and forward the initial data
        server_reader, server_writer = await asyncio.wait_for(
            asyncio.open_connection(orig_ip, orig_port),
            timeout=10.0,
        )
        server_writer.write(data)
        await server_writer.drain()

        # Bidirectional relay
        await asyncio.gather(
            _relay(client_reader, server_writer),
            _relay(server_reader, client_writer),
        )

    except asyncio.TimeoutError:
        logger.warning(f"[{proto}] [{client_addr}] -> {orig_ip}:{orig_port} — timeout")
    except (ConnectionError, OSError) as exc:
        logger.debug(f"[{proto}] [{client_addr}] -> {orig_ip}:{orig_port} — {exc}")
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transparent domain-filtering proxy (SNI + Host header)",
    )
    parser.add_argument("--port", "--https-port", type=int, default=3129,
                        help="HTTPS intercept port (default: 3129)")
    parser.add_argument("--http-port", type=int, default=0,
                        help="HTTP intercept port (0 = disabled)")
    parser.add_argument("--whitelist", required=True,
                        help="Comma-separated list of allowed domains")
    args = parser.parse_args()

    whitelist = {d.strip().lower() for d in args.whitelist.split(",") if d.strip()}
    logger.info(f"Whitelisted domains: {whitelist}")

    servers: list[asyncio.Server] = []

    https_server = await asyncio.start_server(
        lambda r, w: handle_connection(r, w, whitelist, is_tls=True),
        "0.0.0.0", args.port,
    )
    logger.info(f"HTTPS proxy listening on :{args.port}")
    servers.append(https_server)

    if args.http_port > 0:
        http_server = await asyncio.start_server(
            lambda r, w: handle_connection(r, w, whitelist, is_tls=False),
            "0.0.0.0", args.http_port,
        )
        logger.info(f"HTTP proxy listening on :{args.http_port}")
        servers.append(http_server)

    async with asyncio.TaskGroup() as tg:
        for srv in servers:
            tg.create_task(srv.serve_forever())


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(main())
