"""Start AutoClip Editor.

    py -3 run.py                 open the app in your browser
    py -3 run.py --port 9000     use a different port
    py -3 run.py --no-browser    just serve, do not open a window
    py -3 run.py --get-ai-model  download the optional AI title model (~1.1 GB)
"""
from __future__ import annotations

import argparse
import socket
import sys


def free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 25)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main() -> int:
    ap = argparse.ArgumentParser(description="AutoClip Editor - stream recordings to clips, clean VODs and highlight reels")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--get-ai-model", action="store_true",
                    help="download the optional AI model for title ideas (~1.1 GB) and exit")
    args = ap.parse_args()

    if args.get_ai_model:
        from app.titles import download_llm
        last = [""]

        def show(msg: str) -> None:
            if msg != last[0]:
                print("  " + msg, flush=True)
                last[0] = msg
        folder = download_llm(show)
        print(f"\n  Done - saved to {folder}\n"
              f"  AI title ideas also need:  py -3 -m pip install llama-cpp-python\n")
        return 0

    try:
        from app.server import run
    except ImportError as exc:
        print(f"\n  Missing a dependency: {exc}\n"
              f"  Install everything with:\n"
              f"      py -3 -m pip install -r requirements.txt\n")
        return 1

    from app.ffmpeg_tools import have_ffmpeg
    if not have_ffmpeg():
        print("\n  WARNING: ffmpeg was not found.\n"
              "  Install it with:  winget install Gyan.FFmpeg\n"
              "  (then reopen your terminal so it lands on PATH)\n")

    port = free_port(args.port)
    if port != args.port:
        print(f"  Port {args.port} was busy, using {port} instead.")
    try:
        run(host=args.host, port=port, open_browser=not args.no_browser, debug=args.debug)
    except KeyboardInterrupt:
        print("\n  Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
