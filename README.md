# EdgeLLM — Local LLM Profile Switcher

> 本地 GPU 资源调度系统，管理 vLLM + ComfyUI 的互斥与共存。

## 架构

```
OpenClaw / User
    ↓
EdgeLLM Proxy (:8999) — auto-routing + auto-switch
    ↓
ProfileManager — profile definition + GPU lifecycle
    ↓
vLLM (various ports) + ComfyUI (:8188)
```

## 快速开始

```bash
# 查看状态
python3 ~/edge_llm/edge-llm status

# 列出 profile
python3 ~/edge_llm/edge-llm list

# 切换
python3 ~/edge_llm/edge-llm switch qw36_full      # 27B 独占
python3 ~/edge_llm/edge-llm switch qw35_comfyui    # 9B + ComfyUI 共存
python3 ~/edge_llm/edge-llm switch idle            # 释放 GPU

# 启动自动路由代理
python3 ~/edge_llm/edge_llm/proxy.py &
```

## 核心概念

### Profile

Profile 是预定义的 GPU 资源分配方案，包含 vLLM 和 ComfyUI 的启动参数。

| Profile | 模型 | GPU 分配 | 场景 |
|---------|------|---------|------|
| `qw36_full` | Qwen3.6-27B NVFP4 | gpu_util=0.90 (~29GB) | 纯 LLM 推理 |
| `qw35_comfyui` | Qwen3.5-9B GPTQ + ComfyUI | gpu_util=0.4 (~13GB) | LLM + 生图共存 |
| `gemma_full` | Gemma4-26B A4B | gpu_util=0.90 (~29GB) | 纯 LLM 推理 |
| `comfyui_only` | ComfyUI 独占 | 全显存 | 纯生图 |
| `idle` | 无 | 释放 | GPU 空闲 |

### 自动路由代理

在 `:8999` 监听 OpenAI 兼容请求，自动判断目标模型并切换 profile：

```bash
# 设置 OpenClaw 使用代理端口
curl -X POST http://localhost:8999/switch \
  -d '{"profile": "qw36_full"}'

# 发送请求（自动路由）
curl http://localhost:8999/v1/chat/completions \
  -d '{"model": "vllm_qwen27b", "messages": [{"role": "user", "content": "hi"}]}'
```

### 模型预加载

通过 mmap 将模型权重文件保留在 OS page cache，减少切换时的磁盘 I/O：

```bash
# 预加载模型
python3 ~/edge_llm/edge-llm preload Qwen3.6-27B-Text-NVFP4-MTP
python3 ~/edge_llm/edge-llm preload_status
python3 ~/edge_llm/edge-llm preload_clear
```

## 配置

### Profile 定义

编辑 `profiles.yaml` 添加/修改 profile：

```yaml
profiles:
  my_new_model:
    description: "自定义模型"
    gpu_owner: vllm
    vllm:
      model_dir: my-model
      served_name: my_model
      port: 8003
      conda_env: my-model-env
      max_model_len: 128000
      gpu_memory_utilization: 0.80
      max_num_seqs: 4
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EDGE_PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `EDGE_PROXY_PORT` | `8999` | 代理端口 |
| `EDGE_AUTO_SWITCH` | `1` | 是否自动切换 |
| `EDGE_HEALTH_CHECK` | `60` | 健康检查间隔（秒） |
| `EDGE_PRELOAD_MAX_GB` | `16` | 预加载最大 RAM |

## 文件结构

```
edge_llm/
├── edge-llm              # CLI 入口
├── profiles.yaml          # Profile 定义
├── edge_llm/
│   ├── __init__.py
│   ├── profile_manager.py # 核心调度引擎
│   ├── cli.py             # CLI 命令
│   ├── proxy.py           # 自动路由代理
│   └── preload.py         # 模型预加载
└── tests/
    └── test_local.py      # 本地测试
```

## 状态管理

所有状态持久化在 `~/.edge_llm/state.db`（SQLite），包含：
- `current_profile`：当前激活的 profile
- `history`：切换历史（最近 50 条）

崩溃后自动恢复：切换前先 kill 所有已知服务，不依赖记录的状态。
