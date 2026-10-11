"""Open a dashboard running on another computer as if it were local.

NOTE - THIS SHOULD ONLY BE USED FOR TESTING, NOT SECURE AT ALL

Browsers only allow the microphone on localhost (or HTTPS) pages, so a
dashboard opened at http://<other-pc>:8080/ can't record. Run this on the
computer you're using: it listens on localhost and forwards every connection
to the dashboard's computer, so http://localhost:8080/ works, microphone
included.

Usage (copy just this file over; no other files needed):
    python local_proxy.py 192.168.0.90            # forward localhost:8080 to 192.168.0.90:8080
    python local_proxy.py 192.168.0.90:8080 --port 9000

The dashboard's computer must listen on its network address: in its
config_local.toml, [server] host = "0.0.0.0".

Standard library only (Python 3.8+).
"""

import argparse
import asyncio

BUFFER = 65536


async def pipe(reader, writer):
    """Copy bytes one way until that side closes."""
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


def handler(host, port):
    async def handle(client_reader, client_writer):
        try:
            server_reader, server_writer = await asyncio.open_connection(host, port)
        except OSError as err:
            print(f"cannot reach {host}:{port}: {err}")
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))
    return handle


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="the dashboard's computer: HOST or HOST:PORT (default port 8080)")
    parser.add_argument("--port", type=int, default=8080, help="local port to listen on (default: 8080)")
    args = parser.parse_args()
    host, _, port = args.target.partition(":")
    port = int(port or 8080)

    server = await asyncio.start_server(handler(host, port), "127.0.0.1", args.port)
    print(f"Forwarding http://localhost:{args.port}/ to {host}:{port}; Ctrl+C to stop")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
