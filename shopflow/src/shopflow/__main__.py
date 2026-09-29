"""Entry point: `python -m shopflow <service|loadgen>`."""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import UTC, datetime

MODULES = {
    "storefront": "shopflow.services.storefront",
    "api-gateway": "shopflow.services.gateway",
    "auth-service": "shopflow.services.auth",
    "order-service": "shopflow.services.order",
    "payment-service": "shopflow.services.payment",
    "notification-service": "shopflow.services.notification",
    "inventory-service": "shopflow.services.inventory",
    "loadgen": "shopflow.loadgen",
}


def fatal(service: str, message: str, error_type: str) -> None:
    line = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "level": "critical", "service": service,
            "version": os.environ.get("RELEASE_VERSION", "1.0.0"), "pod": os.environ.get("POD_NAME", ""),
            "msg": f"startup failed: {message}", "error_type": error_type}
    print(json.dumps(line), flush=True)
    sys.exit(1)


def main() -> None:
    service = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SERVICE_NAME", "")
    if service == "netem":
        from shopflow.netem import create_app_entry

        create_app_entry()
        return
    if service not in MODULES:
        print(f"usage: python -m shopflow <{'|'.join(MODULES)}|netem>", file=sys.stderr)
        sys.exit(2)
    import uvicorn

    try:
        app = importlib.import_module(MODULES[service]).create_app()
    except Exception as exc:  # configuration errors must be visible, then crash
        fatal(service, f"{type(exc).__name__}: {exc}", type(exc).__name__)
        return
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning",
                access_log=False, loop="uvloop", http="httptools", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
