from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.main import app  # noqa: E402


def main() -> None:
    # IMPORTANT: the desktop client selects an available port at runtime and
    # passes it via RECURDO_PORT. Hard-coding 8000 causes the UI to wait forever
    # on the wrong port (it polls /ready on the chosen port).
    host = os.environ.get("RECURDO_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = (os.environ.get("RECURDO_PORT") or "8000").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 8000

    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
