"""Launch the web UI: python -m webapp"""
import argparse
import sys

import uvicorn

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    p = argparse.ArgumentParser(description="ClipMint web UI")
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address. 127.0.0.1 (default) is local-only.")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = p.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print(
            f"\n  WARNING: binding to {args.host} exposes this to your network.\n"
            "  There is no authentication, and every job spends YOUR LLM quota.\n",
            flush=True,
        )

    print(f"\n  ClipMint  ->  http://{args.host}:{args.port}\n", flush=True)
    uvicorn.run("webapp.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
