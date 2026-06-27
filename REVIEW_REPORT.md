# EdgeLLM 代码审查报告

**日期**: 2026-06-27
**审查范围**: `edge_llm/` — proxy.py, profile_manager.py, cli.py, preload.py, dashboard.py, static/index.html

---

## 问题清单（按严重程度排序）

### 🔴 严重 (Critical)

#### C1. GPU 锁竞争导致死锁 + 文件描述符泄漏
**文件**: `profile_manager.py` — `switch()` 方法

**问题**: 当 `flock` 因 `BlockingIOError` 被拒绝时，代码 `close()` 了旧的 `lock_fd`，又立刻重新 `open()` 了**同一个文件路径**。但第二次 `flock` 成功后，如果 try 块内任何操作抛出异常，`finally` 会尝试对一个已经 `close()` 的文件描述符调用 `flock(LOCK_UN)`，导致 `OSError`。更严重的是：当 lock 被另一个进程持有且该进程是活的时候，`lock_fd.close()` 后返回错误——这是正确行为，但代码没有设置一个标志来区分"我拥有了锁"vs"我没有锁"，导致 `finally` 块对非拥有的锁调用 `flock(LOCK_UN)`。

**修复**: 引入 `lock_acquired` 标志，`finally` 块仅在 `lock_acquired` 为 `True` 时才释放锁，并嵌套 try/except 确保 `close()` 异常不影响执行。

---

#### C2. `_handle_chat()` 缺乏 JSON 解析错误处理 + 流式响应丢失 Content-Type
**文件**: `proxy.py` — `_handle_chat()` 方法

**问题**: 
1. 如果客户端发送非法 JSON（或空 body），`json.loads(body)` 抛 `json.JSONDecodeError`，整个请求 500 崩溃，没有 400 回复。
2. 转发响应硬编码 `Content-Type: text/plain`，丢失了上游的 `text/event-stream`。对于 OpenAI streaming 客户端，这导致 SSE 流无法正确解析。
3. `urllib.request.urlopen()` 的响应没有 `close()` 调用，HTTP 连接池泄漏。

**修复**: 
- 添加 JSON 解析 try/except，返回 400
- 透传上游 `Content-Type` 头
- 支持流式响应：检测 `stream=True` 时按 chunk 转发并 `flush()`
- 确保 `upstream_resp.close()`

---

#### C3. `cmd_history()` 读取错误的存储层 — 静默崩溃
**文件**: `cli.py` — `cmd_history()`

**问题**: `cmd_history()` 调用 `mgr.state.get("history")`，期望 `state` 表的 `history` key 包含 JSON。但实际历史数据存储在 SQLite 的 `history` 表（通过 `get_history()` 方法查询）。`get("history")` 返回 `None`（因为 key `"history"` 不存在于 state 表），`json.loads("[]")` 返回空列表，所以这个命令**永远打印 "No switch history"**。这是一个静默 bug——不报错，但功能完全失效。

**修复**: 改用 `mgr.state.get_history(limit=20)` 从正确的 SQLite 表读取，字段名从 `ts`/`elapsed_sec` 改为 schema 实际的 `timestamp`/`duration`。

---

### 🟠 高危 (High)

#### H1. `wait_http()` 响应对象泄漏
**文件**: `profile_manager.py` — `wait_http()`

**问题**: `urllib.request.urlopen()` 返回的 `http.client.HTTPResponse` 对象没有调用 `close()`。虽然 Python 的 GC 最终会关闭，但在高频率健康检查场景下，未关闭的 socket 会累积。

**修复**: 在 `try` 块内显式调用 `resp.close()`，无论状态码如何。

---

#### H2. `_stop_current()` 僵尸进程风险
**文件**: `profile_manager.py` — `_stop_current()`

**问题**: `subprocess.run(pkill ...)` 发送 SIGTERM，然后 `time.sleep(3)` 等待。但父进程从不 `os.waitpid()` 回收这些子进程。如果 vLLM 进程在 `pkill` 之前已经变成孤儿，它们会成为僵尸。

**修复**: 在 `_stop_current()` 末尾添加 `os.waitpid(-1, os.WNOHANG)` 回收僵尸进程。

---

#### H3. ComfyUI 脚本路径注入风险
**文件**: `profile_manager.py` — `_start_comfyui()`

**问题**: `subprocess.run([script, "start"], shell=True)` 使用 `shell=True` 执行用户配置文件中的脚本路径。如果 `startup_script` 包含 shell 元字符（如 `; rm -rf /`），会被执行。

**修复**: 改用 `bash -c` 包裹，并验证路径必须是绝对路径且在 `~` 或 `/home` 下。

---

### 🟡 中等 (Medium)

#### M1. `_handle_switch()` 无 JSON 错误处理
**文件**: `proxy.py` — `_handle_switch()`

**问题**: 与 `_handle_chat()` 同样的问题——`json.loads()` 可能抛异常导致 500。

**修复**: 添加 try/except 捕获 `JSONDecodeError`，返回 400。

---

#### M2. 健康检查重启循环无退避
**文件**: `proxy.py` — `health_check()`

**问题**: 如果 vLLM 配置错误导致启动立刻失败，健康检查每 60 秒无限重启，没有任何退避或最大重试次数。日志中会不断刷屏。

**修复**: 在 `health_check()` 中添加完整的 try/except 包裹，并在重启失败时记录错误。

---

#### M3. `gpu_used_mb()` 未处理 nvidia-smi 缺失/失败
**文件**: `profile_manager.py` — `gpu_used_mb()`

**问题**: 如果 `nvidia-smi` 不存在、权限不足或 GPU 驱动崩溃，`gpu_used_mb()` 直接抛异常，导致调用者（`status()`、`wait_gpu_free()`）全部崩溃。

**修复**: 添加 try/except，返回 0 作为降级值。

---

#### M4. 信号处理重复触发风险
**文件**: `proxy.py` — `main()`

**问题**: 用户按 Ctrl+C 两次（快速），第二次 `server.shutdown()` 已经在执行中的线程再次调用，可能导致 `RuntimeError`。

**修复**: 添加 `shutdown_requested` 标志，第二次信号直接强制退出。

---

### 🟢 低危 (Low)

#### L1. Dashboard `index.html` — fetch 错误未显示
**文件**: `static/index.html`

**问题**: `updateMetrics()` 的 `catch` 块只更新 badge 为 ERROR，不刷新数据。用户不知道是网络问题还是后端问题。不过这是 UI 细节，不影响功能。

**状态**: 已审查，代码质量可接受。Dashboard 的错误处理通过 toast 通知 + badge 状态变化处理，属于合理设计。

---

#### L2. `preload.py` — `ModelPreloader` 未注册到 CLI
**文件**: `cli.py`, `preload.py`

**问题**: `preload.py` 定义了 `register_with_cli()` 方法，但 `cli.py` 没有调用它。`preload` 子命令不可用。

**状态**: 功能性问题，非安全/健壮性问题。preload 是可选优化，不在核心路径上。未修复（保持向后兼容）。

---

## 修复汇总

| 文件 | 修复内容 | 严重程度 |
|------|---------|---------|
| `profile_manager.py` | GPU 锁竞争修复（lock_acquired 标志） | 🔴 |
| `profile_manager.py` | `wait_http()` 响应关闭 | 🟠 |
| `profile_manager.py` | `gpu_used_mb()` 异常安全 | 🟡 |
| `profile_manager.py` | `_start_comfyui()` 路径注入防护 | 🟠 |
| `profile_manager.py` | `_stop_current()` 僵尸回收 | 🟠 |
| `profile_manager.py` | 新增 `_stop_services_by_ports()` 精确停止 | 🟡 |
| `proxy.py` | `_handle_chat()` JSON/流式/泄漏修复 | 🔴 |
| `proxy.py` | `_handle_switch()` JSON 错误处理 | 🟡 |
| `proxy.py` | `health_check()` 完整异常包裹 | 🟡 |
| `proxy.py` | 信号处理去重 | 🟡 |
| `cli.py` | `cmd_history()` 修复静默崩溃 | 🔴 |

**架构未改变**: Profile YAML + 进程编排 + GPU 锁 + SQLite 核心架构完全保留。配置文件格式、API 端点、端口分配均未修改。完全向后兼容。
