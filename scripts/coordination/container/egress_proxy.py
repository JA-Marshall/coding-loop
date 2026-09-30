"""HTTPS proxy for native worker containers: CONNECT to the model providers' hosts, nothing else.

Worker containers sit on an internal Docker network with no route out; this proxy is the only
container on both that network and the default bridge. It tunnels CONNECT host:443 when host is
in the allowlist given on the command line and refuses everything else, so a model with a shell
can reach its provider but not, say, the upstream repository's merged pull request.
Standard library only; it runs under the worker image's own python3.

    python3 egress_proxy.py PORT HOST [HOST ...]
"""
import asyncio
import sys
import time

PORT = int(sys.argv[1])
ALLOWED = frozenset(host.lower() for host in sys.argv[2:])


def log(verdict, target):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {verdict} {target}", flush=True)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(reader, writer):
    target = "?"
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        host, _, port = target.rpartition(":")
        if method != "CONNECT" or port != "443" or host.lower() not in ALLOWED:
            log("DENY", f"{method} {target}")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, 443), 30)
        log("ALLOW", target)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
    except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
        log("FAIL", target)
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log("LISTEN", f"{PORT} {' '.join(sorted(ALLOWED))}")
    async with server:
        await server.serve_forever()


asyncio.run(main())
