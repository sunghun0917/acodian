"""Start the isolated AI HTTP service with the same provider stubs as the worker."""

from worker_hooks import install

install()

import uvicorn

uvicorn.run("app.main:app", host="0.0.0.0", port=8200, access_log=False)
