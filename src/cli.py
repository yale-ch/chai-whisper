"""CHAI-Whisper local app CLI."""

import argparse

from .app import app


def main() -> None:
    p = argparse.ArgumentParser(prog="chai-whisper", description="CHAI-Whisper local front-end.")
    sub = p.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the local web app.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    serve.add_argument("-p", "--port", type=int, default=5058, help="Port (default 5058).")
    serve.add_argument("--debug", action="store_true", help="Flask debug mode.")

    args = p.parse_args()
    if args.command == "serve" or args.command is None:
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 5058)
        debug = getattr(args, "debug", False)
        # threaded=True so background transcription jobs don't block status polls.
        app.run(host=host, port=port, debug=debug, threaded=True)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
