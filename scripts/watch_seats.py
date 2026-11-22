"""A tiny WebSocket client for the live seat map.

python scripts/watch_seats.py <showtimeId>                watch until Ctrl+C
python scripts/watch_seats.py <showtimeId> --seconds 5    stop after 5 seconds
"""

import argparse
import asyncio
from contextlib import suppress
from urllib.parse import quote

from websockets.asyncio.client import connect  # installed with fastapi[standard]
from websockets.exceptions import ConnectionClosed


async def watch(url: str, seconds: float | None) -> None:
    async with connect(url, proxy=None) as socket:
        print(f"connected to {url}", flush=True)
        with suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                try:
                    while True:
                        print(await socket.recv(), flush=True)
                except ConnectionClosed as closed:
                    code = closed.rcvd.code if closed.rcvd else "without a close frame"
                    print(f"closed by the server: {code}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the live seat map messages of a showtime.")
    parser.add_argument("showtime_id")
    parser.add_argument("--base", default="ws://127.0.0.1:8000", help="default: %(default)s")
    parser.add_argument("--seconds", type=float, help="stop after this long")
    args = parser.parse_args()
    try:
        asyncio.run(watch(f"{args.base}/ws/showtimes/{quote(args.showtime_id)}", args.seconds))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise SystemExit(f"Can't connect to {args.base}: {exc}. Is the server running?") from None


if __name__ == "__main__":
    main()
