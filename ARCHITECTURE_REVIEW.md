# EdgeLLM 架构审视报告

> 审视日期：2026-06-28  
> 审视范围：~/edge_llm/ 全部源码 + scripts/ + profiles.yaml  
> 审视者：系统软件架构审视

---

## 一、项目现状

EdgeLLM 管理单卡 RTX 5090D (32GB VRAM) 上 3 个互斥 vLLM 模型 + ComfyUI 的生命周期。核心逻辑 679 行 Python + 208 行 Bash，总体约 2200 行。

**当前状态**：功能基本可用，但存在多个架构级缺陷导致生产环境不稳定。刚修复了 5 个关键 bug（见附录 A），但深层问题仍需结构性解决。

---

## 二、架构审视

### C1. 🔴 Critical — 双系统状态冲突

**Python 系统**（`profile_manager.py` + `cli.py`）和 **Bash 系统**（`switch_vllm.sh` + `switch_comfyui.sh`）各自独立管理进程，互不知晓：

| 维度 | Python 系统 | Bash 系统 |
|------|------------|-----------|
| 进程启动 | `subprocess.Popen` 直接调 vllm 二进制 | `nohup env vllm serve &` + `source activate` |
| 进程停止 | `pkill -f "vllm.*{port}"` | `pkill -f "vllm.*{port}"` |
| 状态追踪 | SQLite `state.db` | 无（靠 PID 文件） |
| GPU 锁 | `flock(/tmp/edge_llm_gpu.lock)` | 无 |
| 健康检查 | `wait_http(/health, 300s)` | `curl /v1/models, 300s` |
| 日志路径 | `~/.edge_llm/` + `~/models/vllm_logs/{conda_env}.log` | `~/models/vllm_logs/{model}.log` |

**冲突场景**：
1. Bash 启动 vLLM → Python `reconcile()` 检测到 "orphan" → 杀掉
2. Python 启动 vLLM → 用户直接跑 `switch_vllm.sh stop` → 杀掉但 state.db 不更新
3. Bash 不检查 GPU lock → 绕过 Python 的互斥控制
4. 日志文件名不一致（`qw36-27b-vllm.log` vs `qw36.log`）导致 Python 读不到 Bash 的日志

**建议**：Bash 脚本应退化为"底层执行器"，由 Python 统一调度。或至少让 Bash 脚本在操作前检查 state.db + GPU lock。

---

### C2. 🔴 Critical — 三源状态不一致

系统存在三个独立的状态源，无原子性保证：

```
1. SQLite state.db    → current_profile = "qw36_full"
2. GPU lock file      → PID 12345 (或不存在)
3. 实际进程           → vllm serve on :8000 (或不存在)
```

**不一致场景**：

| state.db | GPU lock | 实际进程 | 后果 |
|----------|----------|----------|------|
| qw36_full | 空 | vllm :8000 运行中 | reconcile 误判为 orphan 并杀掉 |
| idle | PID 999 | vllm :8000 运行中 | switch 被拒绝 "lock held" |
| qw36_full | PID 123 | 无进程（被外部 kill） | status 显示 ✅ 但实际 ❌ |
| idle | 空 | EngineCore 孤儿占 GPU | 新 vLLM 启动 OOM |

**根因**：状态写入不是原子的。`switch()` 在 finally 释放 lock，但进程可能在 lock 释放后崩溃。`_stop_current()` 杀进程后不验证 GPU 实际释放。

**建议**：引入 "状态机 + 单一权威" 模式 — state.db 为唯一权威，每次操作后 `reconcile()` 验证。

---

### C3. 🔴 Critical — 进程生命周期管理缺陷

#### 3a. 孤儿进程问题

vLLM 使用多进程架构（APIServer + EngineCore）。`pkill -f "vllm.*{port}"` 只杀 APIServer，EngineCore 变成孤儿（ppid → systemd），继续占 GPU：

```
PID 14241 (EngineCore) ppid=1 (systemd) → 占 31GB VRAM，无法被 pkill vllm.*8000 匹配
```

**当前代码**：`_stop_current()` 和 `_force_kill_all()` 都用 `pkill -f "vllm.*{port}"` 和 `pkill -f "vllm serve"`，**无法匹配 EngineCore 进程**（它的 cmd 不含 `vllm serve`）。

**建议**：用 PID 文件追踪 vLLM 主进程，向主进程发 SIGTERM → vLLM 内部会正确关闭子进程。超时后再 SIGKILL 整个进程组（`os.killpg`）。

#### 3b. Popen vs nohup 的差异

Python 的 `Popen` 不用 `nohup`，意味着：
- 如果 CLI 进程被 Ctrl+C，Popen 子进程收到 SIGHUP 可能退出
- 但 EngineCore 不一定退出（见 3a）

Bash 的 `nohup` 隔离了信号，但 Popen 没有。

**建议**：`Popen` 启动时用 `start_new_session=True`（等同于 `setsid`），隔离信号。

#### 3c. SIGTERM → SIGKILL 的时间窗口

`_stop_current()` 的流程：
```python
pkill -f vllm.*{port}   # SIGTERM
sleep(3)
if wait_http(/health, 2):  # 还活着？
    pkill -9 -f vllm.*{port}  # SIGKILL
pkill -9 -f "vllm serve"     # 终极 SIGKILL
```

问题：vLLM 收到 SIGTERM 后需要时间清理 CUDA context（可能 5-10 秒），3 秒太短。而 `wait_http(/health, 2)` 在进程关闭端口后立即返回 False，可能跳过 SIGKILL。

**建议**：用 `os.waitpid` 或轮询 `/proc/{pid}` 而非 `wait_http` 判断进程存活。

---

### H1. 🟠 High — proxy.py 架构风险

#### 4a. 单线程 HTTPServer 阻塞

`http.server.HTTPServer` 是同步单线程。`_handle_chat()` 中的 `urllib.request.urlopen(upstream, timeout=300)` 会阻塞整个服务器 5 分钟。在此期间所有请求（包括 `/status`、`/switch`）都会排队等待。

**影响**：Dashboard 自动刷新（5s 间隔）在长推理期间完全卡死。

**建议**：使用 `ThreadingHTTPServer` 或异步框架（aiohttp/fastapi）。

#### 4b. 流式代理的内存风险

`_handle_chat()` 的 streaming 路径逐块转发，这是正确的。但非 streaming 路径有 bug：

```python
if not stream:
    resp_body = upstream_resp.read()      # 读取一次
    self.send_header("Content-Length", str(len(resp_body)))
self.end_headers()
if stream:
    # ...
else:
    resp_body = upstream_resp.read()      # 又读取一次！已经 EOF
    self.wfile.write(resp_body)           # 写空数据
```

非 streaming 请求会返回空 body（第二次 read 返回空），客户端收到截断响应。

#### 4c. health_check 已禁用但线程仍在跑

`health_loop` 每 60 秒调用 `health_check()`，但 `health_check()` 现在只做日志。这个线程是无害的浪费，但应该有配置项完全禁用。

#### 4d. auto_switch 的竞态

`ensure_profile()` 有 cooldown（10 秒），但两个并发请求可能同时触发 switch。`_acquire_gpu_lock()` 只在 `ProfileManager` 层面互斥，`ProxyManager` 没有加锁。

---

### H2. 🟠 High — GPU Lock 设计缺陷

#### 5a. Lock 文件不是 PID 感知的

当前实现：先 open → flock → write PID。但如果持有锁的进程崩溃（SIGKILL），lock 被 OS 自动释放，但文件内容仍是旧 PID。下一个进程尝试 stale detection 时 `os.kill(old_pid, 0)` 可能命中一个**不相关的重用 PID**。

**建议**：用 `fcntl.LOCK_EX + fcntl.LOCK_NB` 即可，flock 在进程死亡后自动释放。不需要文件内容，PID 追踪应该通过 state.db。

#### 5b. Lock 作用域过大

`switch()` 在 acquire lock 后执行整个 stop → start → wait_http（可能 5 分钟），期间 lock 一直持有。如果 CLI 被 Ctrl+C 但 lock_fd 未正确释放（Python 的 try/finally 在 SIGINT 时可能不执行），lock 变成"幽灵持有"。

**建议**：Lock 粒度应该只覆盖 "决策" 阶段（检查状态 → 决定是否切换），而非整个执行阶段。执行阶段用 state.db 的 "switching" 状态互斥。

#### 5c. Lock 释放不清理文件

`_release_gpu_lock()` 用 `os.ftruncate(0) + os.close()`，但文件还在 `/tmp/edge_llm_gpu.lock`。下次 acquire 时读到的 PID 是空的，stale detection 逻辑不会触发。

---

### H3. 🟠 High — 错误处理不完善

#### 6a. wait_http 吞掉所有异常

```python
except urllib.error.HTTPError as e:
    pass  # keep waiting
except Exception:
    pass  # connection refused etc — keep waiting
```

503 被正确处理，但其他 HTTP 错误（401、404、500）也被静默忽略。如果 vLLM 启动参数错误导致 /health 返回 404，`wait_http` 会空等 300 秒。

**建议**：对非 503 的 HTTP 错误记录日志，连续 N 次同类型错误则提前返回。

#### 6b. Popen stdout 文件句柄泄漏

```python
proc = subprocess.Popen(cmd, stdout=open(str(log_file), "a"), ...)
```

`open()` 返回的文件对象没有被 close。每次 `_start_vllm` 泄漏一个 fd。

**建议**：用 `with open(...)` 或在 Popen 后显式 close。

#### 6c. kill_port 的竞态

```python
pid = int(pidfile.read_text().strip())
os.kill(pid, signal.SIGTERM)
```

PID 可能在 read 和 kill 之间被回收重用。虽然概率低，但在高频切换场景下可能发生。

---

### H4. 🟠 High — bash 脚本的 set -euo pipefail 隐患

`switch_vllm.sh` 使用 `set -euo pipefail`：

1. **`set -e`**：`pkill` 返回非 0（没有匹配进程）时脚本退出。虽然有 `|| true`，但如果有人添加新代码时忘记加，脚本会意外退出。

2. **`set -u`**：如果 `$PORT_QW35` 等变量在脚本开头未定义，任何引用都会报错退出。当前定义在脚本顶部，但如果用户 source 了脚本到自己的 shell，可能出问题。

3. **`set -o pipefail`**：`nvidia-smi --query-gpu=... | head -1` 如果 nvidia-smi 失败，整个 pipeline 返回非 0，可能触发 `set -e` 退出。

**建议**：在 `case` 分支内部局部禁用 `set -e`，或改用显式错误检查。

---

### M1. 🟡 Medium — 配置硬编码

| 硬编码项 | 位置 | 应改为 |
|----------|------|--------|
| `~/miniconda3/envs` | profile_manager.py L640 | 配置或 conda info 查询 |
| `~/models` | profile_manager.py L27 | profiles.yaml 或环境变量 |
| `~/models/vllm_logs` | profile_manager.py L632 | profiles.yaml |
| `/tmp/edge_llm_gpu.lock` | profile_manager.py L26 | XDG_RUNTIME_DIR |
| `~/.edge_llm/state.db` | profile_manager.py L25 | 配置 |
| `32768` (GPU MB) | profile_manager.py L139 | nvidia-smi 动态查询 |
| `8999` (proxy port) | proxy.py L31 | 已环境变量化 ✅ |
| Dashboard HTML | proxy.py 内联 | 已有 dashboard.py 但未使用 |

**建议**：统一到 profiles.yaml 或 `config.py`。

---

### M2. 🟡 Medium — dashboard.py 未被使用

`dashboard.py` 定义了精美的 `DASHBOARD_HTML`（281 行），但 `proxy.py` 内联了一个简化版 fallback dashboard，且 `static/index.html` 也存在。三份 Dashboard 代码，没有一份被正式引用。

**建议**：只保留一份，优先用 `dashboard.py` 的完整版。

---

### M3. 🟡 Medium — preload.py 未集成

`preload.py`（169 行）实现了模型权重预加载（mmap 到 page cache），但：
1. 未在 CLI 中注册（`register_with_cli` 定义的 dispatch 没有被调用）
2. 未在 `switch()` 流程中触发
3. 未在 proxy 中暴露 API

**建议**：要么集成到 switch 流程（switch 前预加载目标模型），要么移除减少维护负担。

---

### M4. 🟡 Medium — reconcile 逻辑不够健壮

```python
for port in self._all_vllm_ports():
    if wait_http(f"http://localhost:{port}/health", timeout=2):
        actual_vllm_ports.add(port)
```

2 秒超时在模型加载期间不够。如果 vLLM 正在加载（/health 返回 503），`wait_http` 返回 False，reconcile 认为端口没有进程，可能执行错误操作。

**建议**：reconcile 应该用 `_check_vllm_status()` 而非 `wait_http()`，区分 ✅ / ⏳ / ❌ 三种状态。

---

### M5. 🟡 Medium — 日志不统一

- Python 代码用 `logging.getLogger("edge_llm")`
- CLI 用 `logging.getLogger("edge_llm.cli")`
- Proxy 用 `logging.getLogger("edge_llm.proxy")`
- Bash 脚本用 `echo` 到 stdout
- 没有统一的日志文件

**建议**：所有日志写入 `~/.edge_llm/logs/`，按日期轮转。

---

### M6. 🟡 Medium — pkill 模式匹配风险

```python
subprocess.run(["pkill", "-f", f"vllm.*{port}"], ...)
```

如果端口是 8000，`vllm.*8000` 会匹配：
- `vllm serve ... --port 8000` ✅
- `vim vllm_config_8000.yaml` ❌ 误杀
- `grep vllm.*8000 logfile` ❌ 误杀（极少见）

更安全的做法是用 PID 文件或 `--exact` 参数。

---

### L1. 🟢 Low — proxy.py 的路径遍历风险

`_serve_dashboard()` 只提供硬编码的 `static/index.html`，不接受用户输入的路径，不存在路径遍历风险。✅

### L2. 🟢 Low — GPU lock 文件权限

`os.open(lock_path, O_RDWR | O_CREAT, 0o644)` — 644 权限允许其他用户读取 PID，但不允许写入。在单用户桌面环境可接受。✅

### L3. 🟢 Low — 缺少单元测试

`tests/test_local.py` 存在但内容未知。核心逻辑（GPU lock、state 转换、进程管理）应该有 mock 测试。

---

## 三、重构方案

按优先级排序。每项标注影响范围和回归风险。

### Phase 1：关键可靠性修复（1-2 天）

| # | 改动 | 影响 | 回归风险 |
|---|------|------|----------|
| 1.1 | **修复孤儿进程**：`_start_vllm` 用 `start_new_session=True` 启动；`_stop_current` 用进程组 kill（`os.killpg`）而非 pkill | profile_manager.py | 中 — 改变进程管理方式 |
| 1.2 | **修复状态一致性**：`_stop_current()` 成功后验证 GPU 释放 + 进程不存在；`switch()` 每步后 reconcile | profile_manager.py | 低 — 增加验证 |
| 1.3 | **Bash 脚本加锁**：`switch_vllm.sh` / `switch_comfyui.sh` 开始前检查 GPU lock 和 state.db | switch_vllm.sh, switch_comfyui.sh | 低 — 只增加前置检查 |
| 1.4 | **统一日志路径**：Python 和 Bash 都写入 `~/.edge_llm/logs/` | 全部文件 | 低 |
| 1.5 | **修复 proxy 非流式代理 bug**：消除重复 `read()` | proxy.py | 低 |

### Phase 2：架构改善（3-5 天）

| # | 改动 | 影响 | 回归风险 |
|---|------|------|----------|
| 2.1 | **ThreadingHTTPServer**：proxy 改用 `ThreadingHTTPServer` 替代单线程 `HTTPServer` | proxy.py | 低 |
| 2.2 | **reconcile 用三态检查**：区分 ✅ / ⏳ / ❌，加载中不执行 kill | profile_manager.py | 中 |
| 2.3 | **GPU lock 简化**：移除 PID 文件内容，纯靠 flock 自动释放 | profile_manager.py | 低 |
| 2.4 | **配置集中化**：硬编码路径移到 profiles.yaml 或 config.py | profile_manager.py, profiles.yaml | 中 |
| 2.5 | **Dashboard 统一**：proxy 使用 dashboard.py 的完整版，移除内联 fallback | proxy.py, dashboard.py | 低 |
| 2.6 | **preload 集成或移除**：决策后执行 | preload.py | 低 |

### Phase 3：工程化提升（1 周）

| # | 改动 | 影响 | 回归风险 |
|---|------|------|----------|
| 3.1 | **进程管理改用 PID 文件 + killpg**：替代 pkill 模式匹配 | profile_manager.py | 高 |
| 3.2 | **state.db 增加 switching 状态**：switching / healthy / idle 三态 | profile_manager.py, cli.py, proxy.py | 中 |
| 3.3 | **proxy.py 用 FastAPI 重写**：解决同步阻塞、流式代理、类型安全 | proxy.py | 高 |
| 3.4 | **单元测试**：mock GPU/进程，测试状态转换、锁、reconcile | tests/ | 低 |
| 3.5 | **systemd 集成**：proxy 服务 + vLLM watchdog | systemd unit | 中 |

---

## 四、测试计划

### 4.1 核心场景验证清单

| # | 场景 | 验证方法 | 预期结果 |
|---|------|----------|----------|
| T1 | 冷启动 → switch qw36_full | `edge-llm switch qw36_full` | 60-90s 内完成，status ✅ |
| T2 | qw36 → switch idle → switch qw36 | 连续两条命令 | idle 后 GPU < 1GB，再 switch 成功 |
| T3 | qw36 → switch gemma | `edge-llm switch gemma_full` | qw36 停止、gemma 启动成功 |
| T4 | switch 期间 Ctrl+C | switch 后 10 秒按 Ctrl+C | vLLM 进程存活或被正确清理 |
| T5 | 两个终端同时 switch | 终端 1: switch qw36, 终端 2: switch gemma | 第二个被 lock 拒绝 |
| T6 | vLLM 加载中 reconcile | 启动 vLLM 后立即 reconcile | 不杀掉正在加载的进程 |
| T7 | 孤儿 EngineCore | 手动 kill vLLM 主进程 | EngineCore 也被清理，GPU 释放 |
| T8 | state.db 丢失 | `rm state.db` 后 `edge-llm status` | 自动重建，状态正确 |
| T9 | GPU lock 残留 | `echo 999 > /tmp/edge_llm_gpu.lock` 后 switch | 检测 stale，自动清理 |
| T10 | proxy 流式请求 | 通过 proxy 发 stream=true 请求 | SSE 流正常返回 |
| T11 | proxy 非流式请求 | 通过 proxy 发 stream=false 请求 | 完整 JSON 返回 |
| T12 | Dashboard 刷新 | 启动 vLLM 期间访问 Dashboard | 显示 ⏳ 状态，不触发杀进程 |
| T13 | Bash + Python 混合 | switch_vllm.sh qw36 → edge-llm status | 两者状态一致 |
| T14 | recovery.sh 紧急恢复 | vLLM 卡死后运行 recovery.sh | GPU 释放，state 重置 |
| T15 | 长时间运行稳定性 | qw36 运行 2 小时 | 不被 health_check 杀掉 |

### 4.2 回归测试命令

```bash
# 快速冒烟测试（5 分钟）
edge-llm switch idle && sleep 3 && \
edge-llm switch qw36_full && sleep 5 && \
edge-llm status && \
curl -s http://localhost:8000/v1/models | head -1 && \
edge-llm switch idle

# 稳定性测试（30 分钟）
edge-llm switch qw36_full
# 等待 30 分钟，每 5 分钟检查一次
for i in $(seq 1 6); do
  sleep 300
  echo "=== Check $i ==="
  edge-llm status
  curl -s http://localhost:8000/health
done
edge-llm switch idle
```

---

## 附录 A：已修复 Bug 清单

| # | Bug | 根因 | 修复 | 日期 |
|---|-----|------|------|------|
| B1 | switch_vllm.sh PORT 计算错误 | bash `&& ||` 优先级陷阱，PORT 变成两行 | 改用 `if/elif/else` | 06-28 |
| B2 | _start_vllm() ninja 找不到 | Popen 没有 conda env PATH | 添加 conda bin/ 到 PATH | 06-28 |
| B3 | proxy health_check 杀加载中进程 | 不区分 503(loading) 和真的挂了 | 禁用 auto-restart，只做日志 | 06-28 |
| B4 | GPU lock 残留 | Ctrl+C 后 lock_fd 未释放 | `rm -f /tmp/edge_llm_gpu.lock` workaround | 06-28 |
| B5 | EngineCore 孤儿进程 | pkill 不匹配子进程 | `kill -9` 指定 PID（临时方案） | 06-28 |
| B6 | wait_http 不处理 503 | 只认 `resp.status == 200` | 添加 HTTPError 503 处理 | 06-28 |
| B7 | state.db 被恢复脚本删除 | `rm -rf state.db` 后未重建表 | `CREATE TABLE IF NOT EXISTS` | 06-27 |
| B8 | Gemma 模型路径大小写 | profiles.yaml 与磁盘不一致 | 统一为小写 | 06-27 |

---

## 附录 B：文件依赖关系图

```
profiles.yaml
    └── profile_manager.py (读取配置)
         ├── cli.py (CLI 入口)
         └── proxy.py (HTTP 代理)
              └── dashboard.py (未使用，有独立 HTML)

scripts/switch_vllm.sh ←── 独立运行，不调用 Python
scripts/switch_comfyui.sh ←── 被 profile_manager.py 调用(stop)
scripts/edge-llm-recovery.sh ←── 独立运行

~/.edge_llm/state.db ←── profile_manager.py (读写)
/tmp/edge_llm_gpu.lock ←── profile_manager.py (flock)
~/models/vllm_logs/*.log ←── 两个系统都写
~/models/vllm_logs/*.pid ←── switch_vllm.sh 写，profile_manager.py 也写
```

**关键问题**：两个系统通过文件系统松耦合，但缺乏协议保证一致性。

---

## 附录 C：推荐的项目结构

```
~/edge_llm/
├── config.py              # 集中配置（替代硬编码）
├── profiles.yaml          # Profile 定义
├── edge-llm               # CLI 入口脚本
├── edge_llm/
│   ├── __init__.py
│   ├── profile_manager.py # 核心状态机
│   ├── process_mgr.py     # 进程管理（从 profile_manager 拆出）
│   ├── gpu_lock.py        # GPU 锁（从 profile_manager 拆出）
│   ├── state_db.py        # 状态 DB（从 profile_manager 拆出）
│   ├── cli.py             # CLI
│   ├── proxy.py           # HTTP 代理
│   ├── dashboard.py       # Dashboard HTML
│   ├── preload.py         # 预加载（或移除）
│   └── static/            # 静态文件
├── scripts/
│   ├── switch_vllm.sh     # 保留但加锁检查
│   ├── switch_comfyui.sh
│   └── edge-llm-recovery.sh
├── tests/
│   ├── test_state.py      # StateDB 单元测试
│   ├── test_lock.py       # GPU Lock 单元测试
│   └── test_switch.py     # 集成测试（mock）
└── ARCHITECTURE_REVIEW.md # 本文档
```

核心拆分原则：`profile_manager.py` 当前 679 行承担了太多职责（配置解析 + 状态管理 + GPU 锁 + 进程启停 + 健康检查），应拆分为 4 个模块。
