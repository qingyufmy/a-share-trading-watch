"""Readiness checks shared by scheduled reports and the execution engine."""
import os
import shutil
import tempfile
from pathlib import Path


def storage_readiness(base_dir, min_free_bytes=512 * 1024 * 1024):
    base = Path(base_dir)
    try:
        free = shutil.disk_usage(base).free
        # Probe only our own temporary file; never clean existing user data.
        with tempfile.TemporaryFile(dir=base) as handle:
            handle.write(b"a-share-write-probe")
            handle.flush()
            os.fsync(handle.fileno())
        ready = free >= min_free_bytes
        return {"ready": ready, "free_bytes": free, "minimum_free_bytes": min_free_bytes,
                "reason": "存储可写且余量充足" if ready else "磁盘余量不足，禁止新增仓及生成大型报告"}
    except OSError as exc:
        return {"ready": False, "free_bytes": None, "minimum_free_bytes": min_free_bytes,
                "reason": f"存储写入检查失败：{exc}"}
