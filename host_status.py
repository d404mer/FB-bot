"""Снимок загрузки VPS для отчёта админу в Telegram (HTML-текст)."""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import time
from typing import Any

logger = logging.getLogger(__name__)


def _bytes_human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _linux_meminfo() -> tuple[int | None, int | None]:
    """Возвращает (available_bytes, total_bytes) из /proc/meminfo или (None, None)."""
    try:
        avail: int | None = None
        total: int | None = None
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                elif line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
        return avail, total
    except OSError:
        return None, None


def collect_host_metrics() -> dict[str, Any]:
    """Собрать метрики; при наличии psutil — точнее CPU/RAM/RSS процесса."""
    out: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_pid": os.getpid(),
        "cpu_percent": None,
        "loadavg": None,
        "mem_used_bytes": None,
        "mem_total_bytes": None,
        "disk_path": "/",
        "disk_used_bytes": None,
        "disk_total_bytes": None,
        "uptime_sec": None,
    }
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            out["uptime_sec"] = float(f.read().split()[0])
    except OSError:
        pass
    if hasattr(os, "getloadavg"):
        try:
            out["loadavg"] = os.getloadavg()
        except OSError:
            pass
    try:
        du = shutil.disk_usage(out["disk_path"])
        out["disk_used_bytes"] = du.used
        out["disk_total_bytes"] = du.total
    except OSError:
        try:
            du = shutil.disk_usage(".")
            out["disk_path"] = "."
            out["disk_used_bytes"] = du.used
            out["disk_total_bytes"] = du.total
        except OSError:
            pass

    try:
        import psutil  # type: ignore[import-untyped]

        out["cpu_percent"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        out["mem_used_bytes"] = vm.used
        out["mem_total_bytes"] = vm.total
        try:
            proc = psutil.Process(os.getpid())
            out["process_rss_bytes"] = proc.memory_info().rss
        except psutil.Error:
            pass
    except ImportError:
        avail, total = _linux_meminfo()
        out["mem_total_bytes"] = total
        if avail is not None and total is not None:
            out["mem_used_bytes"] = total - avail

    return out


def format_status_html(metrics: dict[str, Any], *, ao3_user: str | None = None, targets_count: int | None = None) -> str:
    """Краткий HTML-отчёт для send_message(parse_mode='HTML')."""
    lines = [
        "<b>Статус VPS</b>",
        f"Хост: <code>{metrics.get('hostname', '?')}</code>",
        f"ОС: <code>{metrics.get('platform', '?')}</code>",
    ]
    if ao3_user:
        lines.append(f"AO3: <code>{ao3_user}</code>")
    if targets_count is not None:
        lines.append(f"Подписок на топики: <b>{targets_count}</b>")

    cpu = metrics.get("cpu_percent")
    if cpu is not None:
        lines.append(f"CPU (снимок): <b>{cpu}%</b>")
    la = metrics.get("loadavg")
    if la is not None:
        lines.append(f"Load avg: <code>{la[0]:.2f}</code> <code>{la[1]:.2f}</code> <code>{la[2]:.2f}</code>")

    mu, mt = metrics.get("mem_used_bytes"), metrics.get("mem_total_bytes")
    if mu is not None and mt is not None:
        lines.append(f"RAM: <b>{_bytes_human(mu)}</b> / {_bytes_human(mt)} ({100 * mu / mt:.0f}%)")
    elif mt is not None:
        lines.append(f"RAM всего: {_bytes_human(mt)}")

    prss = metrics.get("process_rss_bytes")
    if prss is not None:
        lines.append(f"RSS процесса бота (PID {metrics.get('python_pid')}): <b>{_bytes_human(prss)}</b>")

    du, dt = metrics.get("disk_used_bytes"), metrics.get("disk_total_bytes")
    dp = metrics.get("disk_path", "/")
    if du is not None and dt is not None:
        lines.append(f"Диск ({dp}): <b>{_bytes_human(du)}</b> / {_bytes_human(dt)} ({100 * du / dt:.0f}%)")

    up = metrics.get("uptime_sec")
    if up is not None:
        sec = int(up)
        d, rem = divmod(sec, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"Uptime: <code>{d}д {h:02d}:{m:02d}:{s:02d}</code>")

    try:
        import psutil  # noqa: F401
    except ImportError:
        lines.append("(Установите <code>psutil</code> для точнее CPU и RSS процесса.)")

    return "\n".join(lines)
