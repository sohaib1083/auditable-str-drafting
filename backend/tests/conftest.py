import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="sar-test-")
os.environ["SAR_DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["SAR_STAGE_DELAY"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
