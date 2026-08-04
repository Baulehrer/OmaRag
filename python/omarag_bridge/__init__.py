"""Oracle of Daedalus backend bridge."""

from .runtime import configure_process_environment

# Console entry points import this package before Uvicorn/FastAPI or any model
# runtime. Allocator and thread limits therefore take effect at the earliest
# Python-controlled boundary (launchers also export them before exec).
configure_process_environment()

__version__ = "0.7.0"
