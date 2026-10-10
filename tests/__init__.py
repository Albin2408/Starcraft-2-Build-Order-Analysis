"""Test package. Points the app at a throwaway database (and a separate Redis
database number) *before* anything imports config, so tests never touch real data."""

import atexit
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="sc2tests-")
os.environ["SC2_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SC2_UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["SC2_PRO_DIR"] = os.path.join(_tmp, "pro")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")


@atexit.register
def _cleanup():
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)
