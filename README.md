# NVML Per-User GPU Exporter

A Prometheus exporter built on NVIDIA's NVML library. Reports device-level GPU
metrics and **aggregates GPU memory + utilization per Linux user** by mapping
each NVML-reported PID back to its owner via `/proc/<pid>/status`.

主要用途:在多人共用 GPU 的伺服器上,看「誰在吃 GPU、吃多少」。

## How it works

NVML 本身**沒有** user 欄位 — `nvmlDeviceGetComputeRunningProcesses` /
`nvmlDeviceGetProcessUtilization` 只回 PID + memory / util。Exporter 每次被
scrape 時:

1. 對每張 GPU 呼叫 `nvmlDeviceGetMemoryInfo` / `nvmlDeviceGetUtilizationRates`
   / `nvmlDeviceGetPowerUsage` / `nvmlDeviceGetTemperature` → device-level metrics
2. 呼叫 `nvmlDeviceGetComputeRunningProcesses` + `nvmlDeviceGetGraphicsRunningProcesses`
   拿 PID + GPU memory
3. 呼叫 `nvmlDeviceGetProcessUtilization` 拿 per-PID SM / mem-IO / encoder / decoder util
4. 對每個 PID:讀 `/proc/<pid>/status` 的 `Uid:` → `pwd.getpwuid()` 換成 username
5. 以 `(gpu, user, uid)` 為 key 加總後 yield 出 `nvml_user_gpu_*` metric family

PID→user lookup 有 LRU cache (4096 entries),避免每 scrape 都做 syscall。

## Metrics

### Device-level — labels: `gpu`, `uuid`, `name`
| Metric | Description |
| --- | --- |
| `nvml_gpu_memory_total_bytes` | Total GPU memory |
| `nvml_gpu_memory_used_bytes` | Used GPU memory |
| `nvml_gpu_utilization_ratio` | SM utilization (0–1) |
| `nvml_gpu_memory_utilization_ratio` | Memory-bandwidth utilization (0–1) |
| `nvml_gpu_power_watts` | Current power draw |
| `nvml_gpu_temperature_celsius` | Current temperature |

### Per-user — labels: `gpu`, `user`, `uid`
| Metric | Description |
| --- | --- |
| `nvml_user_gpu_memory_bytes` | Sum of GPU memory across that user's processes |
| `nvml_user_gpu_processes` | Number of GPU processes owned by the user |
| `nvml_user_gpu_sm_utilization_ratio` | SM util summed per user (clipped to 1.0) |
| `nvml_user_gpu_mem_io_utilization_ratio` | Memory IO util per user |
| `nvml_user_gpu_enc_utilization_ratio` | NVENC util per user |
| `nvml_user_gpu_dec_utilization_ratio` | NVDEC util per user |

Special user labels:
- `user="<gone>"` — Process 在 NVML 列完之後讀 `/proc` 前就死了
- `user="uid:1234"` — UID 在 `/etc/passwd` 沒對應 (常見於容器 UID 沒 mapping 到 host)

### Health
- `nvml_up` — 1/0
- `nvml_scrape_duration_seconds`
- `nvml_scrape_errors` (labels: `kind`)

## Install

```bash
git clone https://github.com/as6325400/nvml_exporter.git
cd nvml_exporter
pip3 install --user -r requirements.txt
```

需要:
- Python 3.10+
- NVIDIA driver with NVML
- `/proc` 可讀 (default Linux 大多數 distro 都讓任意 user 讀 `/proc/<pid>/status`,
  跑 nobody 就行;若一律解到 `<gone>`/`uid:<n>`,改成 root 跑)

## Run

```bash
python3 nvml_user_exporter.py --port 9835 --addr 0.0.0.0
curl -s localhost:9835/metrics | grep nvml_
```

CLI flags:
- `--port` (default 9835)
- `--addr` (default 0.0.0.0)
- `--log-level` (default INFO; or env `LOG_LEVEL`)

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: nvml-user
    static_configs:
      - targets: ['gpu-host-1:9835']
```

實用 PromQL:
```promql
# Top users by GPU memory across the cluster
topk(10, sum by (user) (nvml_user_gpu_memory_bytes))

# Per-user share of a specific card
sum by (user) (nvml_user_gpu_memory_bytes{gpu="0"})
  / on(gpu) group_left() nvml_gpu_memory_total_bytes{gpu="0"}

# Who's currently doing GPU compute?
sum by (user) (nvml_user_gpu_sm_utilization_ratio) > 0
```

## WSL2 caveat ⚠️

WSL2 上的 NVIDIA driver **不支援** `nvmlDeviceGetComputeRunningProcesses`,
等同 `nvidia-smi` 顯示 "No running processes found"。所以在 WSL2:

- Device-level metrics (memory, util, power, temp) 都正常
- `nvml_user_gpu_memory_bytes` / `nvml_user_gpu_processes` 都會是空的
- `nvmlDeviceGetProcessUtilization` 有時會回到 Windows host PID — 這些 PID 在
  WSL 的 `/proc` 找不到,會標成 `user="<gone>"`,可忽略

要拿到實際 per-user 數字必須跑在原生 Linux server 上。

## Systemd

`systemd/nvml-user-exporter.service` 是 Linux server 部署用的範本。改一下
`ExecStart` 路徑:

```bash
sudo cp systemd/nvml-user-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nvml-user-exporter
```

## Scope / 不做什麼

- **Per-process metrics** — Prometheus 對高 cardinality 敏感 (PID 一直變),
  per-user 已能回答「誰在吃 GPU」。要 per-process 請接 DCGM exporter。
- **MIG / vGPU 切分** — 暫無支援。
- **容器→user 對應** — NVML 看到的是 host namespace PID,exporter 在 host 上跑
  就會自動解到真實 user;跑在容器內 (且沒掛 host `/proc`) 會全部變 `<gone>`。
- **TLS / auth** — 內網 scrape 不需要,要的話前面擺 reverse proxy。
