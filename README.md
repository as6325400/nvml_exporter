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
- `user="container:<image>"` — Docker container 但沒 `gpu.user` label,fallback 到 image 名稱 (需 `--detect-containers`)
- `user="<from container label>"` — Docker container 帶 `gpu.user` label,顯示實際使用者 (需 `--detect-containers` + 用 `gpu-docker` wrapper)

### Health
- `nvml_up` — 1/0
- `nvml_scrape_duration_seconds`
- `nvml_scrape_errors` (labels: `kind`)

## Install

需要: Python 3.10+、NVIDIA driver with NVML。

直接 clone 到 `/opt/nvml_exporter`,在裡面建一個 venv,然後啟用 systemd。
一次 copy-paste 就好:

```bash
sudo git clone https://github.com/as6325400/nvml_exporter.git /opt/nvml_exporter
cd /opt/nvml_exporter
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp systemd/nvml-user-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nvml-user-exporter
```

驗證:

```bash
sudo systemctl status nvml-user-exporter --no-pager
curl -s localhost:9835/metrics | grep nvml_user_gpu_
```

### 想先在前景測一下 (不裝 systemd)

```bash
sudo .venv/bin/python /opt/nvml_exporter/nvml_user_exporter.py --port 9835
# 另一個 shell
curl -s localhost:9835/metrics | grep nvml_
```

CLI flags: `--port` (預設 9835)、`--addr` (預設 0.0.0.0)、`--log-level` (預設 INFO)、
`--detect-containers` (查 Docker container 的 `gpu.user` label,見下節)、
`--docker-sock` (預設 `/var/run/docker.sock`)。

### Debug

service 啟動後 curl 沒東西 → 看
`sudo journalctl -u nvml-user-exporter -n 30 --no-pager`,常見原因:
- `status=203/EXEC` — `/opt/nvml_exporter/.venv/bin/python` 不存在 (venv 沒建好)
- `ModuleNotFoundError: No module named 'pynvml'` — venv 裡沒裝依賴
- Metrics 都出來但 `user` 全是 `<gone>` — `/proc` 有 `hidepid=2`,維持
  `User=root` 即可 (default unit 已是 root)

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

## Docker container 解析 (`--detect-containers`)

Container 預設 root 跑,所以 host 上的 `/proc/<pid>/status` 看到的 `Uid:` 永遠
是 0,exporter 會把所有 container 流量都算給 `root`。打開 `--detect-containers`
後,當 PID 解到 root,exporter 會額外:

1. 讀 `/proc/<pid>/cgroup` 抓出 container ID (docker / containerd / podman 都認)
2. 透過 `/var/run/docker.sock` 呼叫 `GET /containers/<id>/json`
3. 從 container 的 Labels 取 `gpu.user` (或 `user` / `owner` / `maintainer`)
4. 都沒有就 fallback 到 `Config.User` → `container:<image-name>`

要拿到「真正使用者」必須有第 3 步的 label。配套的 `tools/gpu-docker` wrapper
自動加 label,user 用它跑 `docker run` 不用改習慣:

```bash
sudo install -m 0755 tools/gpu-docker /usr/local/bin/gpu-docker
# 然後讓 user 用 gpu-docker run ...;或在 /etc/profile.d/ 加 alias docker=gpu-docker
```

執行 exporter 時加 flag:

```bash
/opt/nvml_exporter/.venv/bin/python /opt/nvml_exporter/nvml_user_exporter.py \
  --port 9835 --detect-containers
```

systemd 跑的話:`sudo systemctl edit --full nvml-user-exporter` 在 `ExecStart` 後
加 `--detect-containers`。Service 已經 `User=root` 所以一定能讀 docker socket;
如果改成非 root user,要把該 user 加進 `docker` group (`SupplementaryGroups=docker`)。

需求/限制:
- 需要 docker daemon + socket 在本機
- Container ID 從 cgroup 抓,所以 rootless docker 也 work
- k8s pod 路徑 (`/kubepods/.../cri-containerd-<id>`) 也支援,但 label 要從 k8s
  那邊塞,docker socket 看不到 — 那種環境建議改接 DCGM exporter

## Grafana

兩份 dashboard,搭配 drill-down 一起用:

- **`grafana/cluster.json`** — cluster overview。一張表每台 host 一列
  (GPUs / Mem Used / Mem % / Power / Max Temp / Active Users),點 Host 那欄
  就會跳到該 node 的詳細 dashboard;上方還有 cluster 級的 stat 與時序圖
- **`grafana/host.json`** — 單一 host 的 per-user 詳細頁。Drill-down 的目標,
  也可以直接從上方 `Host` 下拉切換。包含 device-level 時序、per-user stacked
  memory / SM util、與一個即時 per-user × GPU 的 table

匯入順序:**先匯 `host.json`,再匯 `cluster.json`** (cluster 裡的連結指到
host dashboard 的 uid `nvml-host-detail`,先存在連結才有效)。
Grafana UI → Dashboards → New → Import → 選檔案 → 指到你的 Prometheus datasource。

## Scope / 不做什麼

- **Per-process metrics** — Prometheus 對高 cardinality 敏感 (PID 一直變),
  per-user 已能回答「誰在吃 GPU」。要 per-process 請接 DCGM exporter。
- **MIG / vGPU 切分** — 暫無支援。
- **容器→user 對應** — NVML 看到的是 host namespace PID,exporter 在 host 上跑
  就會自動解到真實 user;跑在容器內 (且沒掛 host `/proc`) 會全部變 `<gone>`。
- **TLS / auth** — 內網 scrape 不需要,要的話前面擺 reverse proxy。
