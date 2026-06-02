#!/usr/bin/env python3
"""NVML per-user GPU usage Prometheus exporter.

Exposes device-level GPU metrics and aggregates per-process GPU memory /
utilization by the Linux user that owns each PID.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import pwd
import re
import signal
import socket
import sys
import time
from collections import defaultdict
from functools import lru_cache
from typing import Iterable, Optional

import pynvml
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

log = logging.getLogger("nvml_user_exporter")

UNKNOWN_USER = "<gone>"
DOCKER_SOCK = "/var/run/docker.sock"

# Matches docker / containerd / podman container IDs inside a cgroup path.
# Examples that match (group 1 is the container ID):
#   /system.slice/docker-7af3c2....scope
#   /docker/7af3c2....
#   /kubepods/.../cri-containerd-7af3c2....scope
#   /machine.slice/libpod-7af3c2....scope
_CGROUP_CID_RE = re.compile(
    r'(?:docker[-/]|libpod-|cri-containerd-|containerd-)([0-9a-f]{12,64})(?:\.scope)?'
)


@lru_cache(maxsize=4096)
def _username_for_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


@lru_cache(maxsize=4096)
def container_id_for_pid(pid: int) -> Optional[str]:
    """Look up the docker / containerd / podman container ID owning a PID, or None."""
    try:
        with open(f"/proc/{pid}/cgroup", "r") as fh:
            for line in fh:
                m = _CGROUP_CID_RE.search(line)
                if m:
                    return m.group(1)
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return None


class DockerResolver:
    """Looks up a container's effective 'user' via the Docker socket."""

    def __init__(self, sock_path: str = DOCKER_SOCK) -> None:
        self.sock_path = sock_path
        self.enabled = os.path.exists(sock_path) and os.access(sock_path, os.R_OK | os.W_OK)
        self._cache: dict[str, Optional[str]] = {}
        self._warned = False
        if not self.enabled:
            log.warning(
                "Docker socket %s not available / not readable - container detection disabled",
                sock_path,
            )

    def _inspect(self, cid: str) -> Optional[dict]:
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.sock_path)
            conn = http.client.HTTPConnection("localhost", timeout=1.0)
            conn.sock = sock
            conn.request("GET", f"/containers/{cid}/json")
            resp = conn.getresponse()
            if resp.status != 200:
                resp.read()
                return None
            return json.loads(resp.read())
        except (OSError, json.JSONDecodeError, http.client.HTTPException) as e:
            if not self._warned:
                log.warning("Docker inspect failed (will keep trying): %s", e)
                self._warned = True
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def user_for_container(self, cid: str) -> Optional[str]:
        """Return a best-effort user label for a container, or None."""
        if not self.enabled:
            return None
        if cid in self._cache:
            return self._cache[cid]
        info = self._inspect(cid)
        result: Optional[str] = None
        if info:
            cfg = info.get("Config") or {}
            labels = cfg.get("Labels") or {}
            # Preferred: explicit label set by gpu-docker wrapper.
            for k in ("gpu.user", "user", "owner", "maintainer"):
                v = labels.get(k)
                if v:
                    result = v
                    break
            # Fallback: --user value if explicitly set.
            if result is None and cfg.get("User"):
                result = f"user:{cfg['User']}"
            # Last resort: image name, so at least we know it's a container.
            if result is None:
                img = (cfg.get("Image") or "").split("/")[-1].split(":")[0]
                if img:
                    result = f"container:{img}"
        self._cache[cid] = result
        return result


# Set in main() when --detect-containers is enabled.
_DOCKER: Optional[DockerResolver] = None


def resolve_user(pid: int) -> tuple[str, str]:
    """Return (username, uid_str) for a PID. Reads /proc/<pid>/status.

    When container detection is enabled and the host-side resolution lands on
    root (uid 0), try to map the container to a real user via Docker labels.
    The returned uid is preserved as "0" so the metric uid label stays truthful.
    """
    user = UNKNOWN_USER
    uid_str = "-1"
    try:
        with open(f"/proc/{pid}/status", "r") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    user = _username_for_uid(uid)
                    uid_str = str(uid)
                    break
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return user, uid_str

    if _DOCKER is not None and uid_str == "0":
        cid = container_id_for_pid(pid)
        if cid is not None:
            cu = _DOCKER.user_for_container(cid)
            if cu:
                return cu, "0"
    return user, uid_str


class NvmlCollector:
    """Build metrics from NVML on each Prometheus scrape."""

    def __init__(self) -> None:
        self._last_seen_ts: dict[int, int] = {}
        self._warned_not_supported: set[str] = set()

    # --- helpers ---------------------------------------------------------

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned_not_supported:
            self._warned_not_supported.add(key)
            log.warning(msg)

    def _safe(self, fn, *args, default=None, kind: str | None = None):
        """Call an NVML function, swallow expected errors."""
        try:
            return fn(*args)
        except pynvml.NVMLError as e:
            err_code = getattr(e, "value", None)
            if err_code == pynvml.NVML_ERROR_NOT_SUPPORTED:
                if kind:
                    self._warn_once(
                        kind,
                        f"NVML {fn.__name__} not supported on this driver/platform "
                        f"(likely WSL2). This metric will be empty.",
                    )
                return default
            log.debug("NVML call %s failed: %s", fn.__name__, e)
            return default

    def _list_processes(self, handle) -> list:
        """Combined compute+graphics processes, deduped by PID."""
        seen: dict[int, object] = {}
        for fn, kind in (
            (pynvml.nvmlDeviceGetComputeRunningProcesses, "compute_procs"),
            (pynvml.nvmlDeviceGetGraphicsRunningProcesses, "graphics_procs"),
        ):
            procs = self._safe(fn, handle, default=[], kind=kind) or []
            for p in procs:
                seen.setdefault(p.pid, p)
        return list(seen.values())

    def _process_util(self, handle, gpu_index: int) -> dict[int, object]:
        """Per-PID utilization samples since last scrape, keyed by PID."""
        last_ts = self._last_seen_ts.get(gpu_index, 0)
        samples = self._safe(
            pynvml.nvmlDeviceGetProcessUtilization,
            handle,
            last_ts,
            default=[],
            kind="proc_util",
        ) or []
        out: dict[int, object] = {}
        max_ts = last_ts
        for s in samples:
            out[s.pid] = s
            if s.timeStamp > max_ts:
                max_ts = s.timeStamp
        if max_ts > last_ts:
            self._last_seen_ts[gpu_index] = max_ts
        return out

    # --- main entry ------------------------------------------------------

    def collect(self) -> Iterable:
        start = time.monotonic()
        errors: dict[str, int] = defaultdict(int)

        up = GaugeMetricFamily("nvml_up", "1 if NVML is reachable, else 0")
        scrape_dur = GaugeMetricFamily(
            "nvml_scrape_duration_seconds", "Time spent collecting NVML metrics"
        )

        dev_mem_total = GaugeMetricFamily(
            "nvml_gpu_memory_total_bytes", "Total GPU memory (bytes)",
            labels=["gpu", "uuid", "name"],
        )
        dev_mem_used = GaugeMetricFamily(
            "nvml_gpu_memory_used_bytes", "Used GPU memory (bytes)",
            labels=["gpu", "uuid", "name"],
        )
        dev_util = GaugeMetricFamily(
            "nvml_gpu_utilization_ratio", "GPU SM utilization (0-1)",
            labels=["gpu", "uuid", "name"],
        )
        dev_mem_util = GaugeMetricFamily(
            "nvml_gpu_memory_utilization_ratio", "GPU memory bandwidth utilization (0-1)",
            labels=["gpu", "uuid", "name"],
        )
        dev_power = GaugeMetricFamily(
            "nvml_gpu_power_watts", "Current GPU power draw (watts)",
            labels=["gpu", "uuid", "name"],
        )
        dev_temp = GaugeMetricFamily(
            "nvml_gpu_temperature_celsius", "Current GPU temperature (C)",
            labels=["gpu", "uuid", "name"],
        )

        user_mem = GaugeMetricFamily(
            "nvml_user_gpu_memory_bytes",
            "GPU memory used summed by Linux user (bytes)",
            labels=["gpu", "user", "uid"],
        )
        user_procs = GaugeMetricFamily(
            "nvml_user_gpu_processes",
            "Number of GPU processes per user",
            labels=["gpu", "user", "uid"],
        )
        user_sm = GaugeMetricFamily(
            "nvml_user_gpu_sm_utilization_ratio",
            "Sum of SM utilization samples per user (0-1, clipped)",
            labels=["gpu", "user", "uid"],
        )
        user_mio = GaugeMetricFamily(
            "nvml_user_gpu_mem_io_utilization_ratio",
            "Sum of memory IO utilization samples per user (0-1, clipped)",
            labels=["gpu", "user", "uid"],
        )
        user_enc = GaugeMetricFamily(
            "nvml_user_gpu_enc_utilization_ratio",
            "Sum of encoder utilization samples per user (0-1, clipped)",
            labels=["gpu", "user", "uid"],
        )
        user_dec = GaugeMetricFamily(
            "nvml_user_gpu_dec_utilization_ratio",
            "Sum of decoder utilization samples per user (0-1, clipped)",
            labels=["gpu", "user", "uid"],
        )

        try:
            count = pynvml.nvmlDeviceGetCount()
            up.add_metric([], 1)
        except pynvml.NVMLError as e:
            log.error("nvmlDeviceGetCount failed: %s", e)
            up.add_metric([], 0)
            errors["device_count"] += 1
            yield up
            scrape_dur.add_metric([], time.monotonic() - start)
            yield scrape_dur
            yield self._errors_metric(errors)
            return

        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            except pynvml.NVMLError as e:
                log.warning("get handle gpu=%d failed: %s", i, e)
                errors["device_handle"] += 1
                continue

            uuid = self._safe(pynvml.nvmlDeviceGetUUID, handle, default="") or ""
            name = self._safe(pynvml.nvmlDeviceGetName, handle, default="") or ""
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if isinstance(uuid, bytes):
                uuid = uuid.decode("utf-8", "replace")
            gpu_label = str(i)
            base = [gpu_label, uuid, name]

            mem = self._safe(pynvml.nvmlDeviceGetMemoryInfo, handle, kind="mem")
            if mem is not None:
                dev_mem_total.add_metric(base, float(mem.total))
                dev_mem_used.add_metric(base, float(mem.used))

            util = self._safe(pynvml.nvmlDeviceGetUtilizationRates, handle, kind="util")
            if util is not None:
                dev_util.add_metric(base, util.gpu / 100.0)
                dev_mem_util.add_metric(base, util.memory / 100.0)

            power_mw = self._safe(pynvml.nvmlDeviceGetPowerUsage, handle, kind="power")
            if power_mw is not None:
                dev_power.add_metric(base, power_mw / 1000.0)

            temp = self._safe(
                pynvml.nvmlDeviceGetTemperature,
                handle,
                pynvml.NVML_TEMPERATURE_GPU,
                kind="temp",
            )
            if temp is not None:
                dev_temp.add_metric(base, float(temp))

            # --- per-user aggregation -----------------------------------
            procs = self._list_processes(handle)
            util_samples = self._process_util(handle, i)

            per_user_mem: dict[tuple[str, str], int] = defaultdict(int)
            per_user_count: dict[tuple[str, str], int] = defaultdict(int)
            per_user_sm: dict[tuple[str, str], float] = defaultdict(float)
            per_user_mio: dict[tuple[str, str], float] = defaultdict(float)
            per_user_enc: dict[tuple[str, str], float] = defaultdict(float)
            per_user_dec: dict[tuple[str, str], float] = defaultdict(float)

            pid_to_user: dict[int, tuple[str, str]] = {}

            for p in procs:
                key = pid_to_user.get(p.pid)
                if key is None:
                    key = resolve_user(p.pid)
                    pid_to_user[p.pid] = key
                per_user_count[key] += 1
                used = getattr(p, "usedGpuMemory", None)
                # NVML returns a sentinel for "not available"; treat as 0.
                if used is None or used == pynvml.NVML_VALUE_NOT_AVAILABLE_ulonglong:
                    used = 0
                per_user_mem[key] += int(used)

            for pid, s in util_samples.items():
                key = pid_to_user.get(pid)
                if key is None:
                    key = resolve_user(pid)
                    pid_to_user[pid] = key
                per_user_sm[key] += s.smUtil / 100.0
                per_user_mio[key] += s.memUtil / 100.0
                per_user_enc[key] += s.encUtil / 100.0
                per_user_dec[key] += s.decUtil / 100.0

            for key, val in per_user_mem.items():
                user_mem.add_metric([gpu_label, key[0], key[1]], float(val))
            for key, val in per_user_count.items():
                user_procs.add_metric([gpu_label, key[0], key[1]], float(val))
            for store, fam in (
                (per_user_sm, user_sm),
                (per_user_mio, user_mio),
                (per_user_enc, user_enc),
                (per_user_dec, user_dec),
            ):
                for key, val in store.items():
                    fam.add_metric([gpu_label, key[0], key[1]], min(val, 1.0))

        yield up
        yield dev_mem_total
        yield dev_mem_used
        yield dev_util
        yield dev_mem_util
        yield dev_power
        yield dev_temp
        yield user_mem
        yield user_procs
        yield user_sm
        yield user_mio
        yield user_enc
        yield user_dec
        scrape_dur.add_metric([], time.monotonic() - start)
        yield scrape_dur
        yield self._errors_metric(errors)

    @staticmethod
    def _errors_metric(errors: dict[str, int]) -> CounterMetricFamily:
        c = CounterMetricFamily(
            "nvml_scrape_errors",
            "Count of NVML errors encountered during the most recent scrape",
            labels=["kind"],
        )
        for k, v in errors.items():
            c.add_metric([k], float(v))
        return c


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=9835, help="HTTP port (default: 9835)")
    p.add_argument("--addr", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO)",
    )
    p.add_argument(
        "--detect-containers",
        action="store_true",
        help=(
            "When a PID resolves to root, look up its Docker container and "
            "use the container's 'gpu.user' label (or fallback to image name). "
            "Requires read+write access to /var/run/docker.sock."
        ),
    )
    p.add_argument(
        "--docker-sock",
        default=DOCKER_SOCK,
        help=f"Path to Docker socket (default: {DOCKER_SOCK})",
    )
    return p.parse_args()


def _install_signal_handlers() -> None:
    def _shutdown(signum, _frame):
        log.info("Received signal %d, shutting down", signum)
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as e:
        log.error("nvmlInit failed: %s", e)
        return 1

    driver = "?"
    try:
        d = pynvml.nvmlSystemGetDriverVersion()
        driver = d.decode() if isinstance(d, bytes) else d
    except pynvml.NVMLError:
        pass
    log.info("NVML initialized, driver=%s", driver)

    if args.detect_containers:
        global _DOCKER
        _DOCKER = DockerResolver(args.docker_sock)

    REGISTRY.register(NvmlCollector())
    _install_signal_handlers()

    log.info("Listening on http://%s:%d/metrics", args.addr, args.port)
    start_http_server(args.port, addr=args.addr)
    signal.pause()
    return 0


if __name__ == "__main__":
    sys.exit(main())
