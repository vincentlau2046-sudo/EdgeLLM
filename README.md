# EdgeLLM — 本地 LLM 模型切换系统 v4.0

> **版本**: v4.0.0  
> **更新**: 2026-06-28  
> **硬件**: RTX 5090D (32GB VRAM)  
> **核心理念**: 模型即插件 — 一个 YAML 文件 = 一个可部署的模型

---

## 目录

- [概述](#概述)
- [快速开始](#快速开始)
- [核心概念](#核心概念)
  - [模型即插件](#模型即插件)
  - [三态 GPU 状态机](#三态-gpu-状态机)
- [目录结构](#目录结构)
- [CLI 参考](#cli-参考)
- [模型配置格式](#模型配置格式)
- [增删模型](#增删模型)
- [Proxy 服务](#proxy-服务)
- [状态持久化](#状态持久化)
- [故障恢复](#故障恢复)
- [版本历史](#版本历史)

---

## 概述

EdgeLLM 管理单卡 GPU 上多个互斥/共存 LLM 推理服务和图像生成服务的生命周期。

**v4.0 核心变化**：

| v3.x (Profile) | v4.0 (Model Plugin) |
|-----------------|---------------------|
| `profiles.yaml` 单文件 | `models.d/` 目录，一文件一模型 |
| 独占/共享是 Profile 的组合策略 | 模型的 `mode` 属性 |
| GPU 锁二值（持有/不持有） | 三态（idle/exclusive/shared） |
| N 模型 × M 组合 = Profile 爆炸 | N 文件，无组合 |
| `edge-llm switch <profile>` | `edge-llm switch <model_name>` |
| 停止只能 switch idle | `edge-llm stop <model_name>` 停单个 |

---

## 快速开始

```bash
# 查看当前状态
edge-llm status

# 列出可用模型
edge-llm models

# 切换到 Qwen3.6-27B（独占模式）
edge-llm switch qwen36-27b

# 释放 GPU
edge-llm switch idle

# 切换到 Qwen3.5-9B（共享模式）
edge-llm switch qwen35-9b

# 在共享模式下加入 ComfyUI
edge-llm switch comfyui

# 停止单个共享服务
edge-llm stop qwen35-9b

# 强制重置
edge-llm reset
```

---

## 核心概念

### 模型即插件

每个模型/服务由 `models.d/` 下的一个 YAML 文件定义。文件自带一切：

- 模型参数（路径、端口、conda 环境、vLLM 参数）
- 部署模式（`mode: exclusive` 或 `mode: shared`）
- 服务类型（`type: vllm` 或 `type: comfyui`）

**增删模型 = 增删 YAML 文件**，零改动代码。

### 三态 GPU 状态机

```
                 switch(exclusive)
   idle ─────────────────────────────→ exclusive
    ↑                                    │
    │              switch(shared)         │   switch(idle)
    │         idle ──────→ shared ←──────│←───────┘
    │                          │         
    │          switch(idle)    │         
    └──────────────────────────┘         

   ❌ exclusive → shared  : 必须先 idle
   ❌ shared → exclusive   : 必须先 idle
```

| 当前状态 | 允许操作 | 效果 |
|----------|----------|------|
| `idle` | `switch <exclusive_model>` | GPU 全锁 |
| `idle` | `switch <shared_model>` | GPU 共享锁 |
| `exclusive` | `switch idle` | 释放 GPU |
| `shared` | `switch <shared_model/service>` | 加入共享服务 |
| `shared` | `stop <model>` | 移除单个服务 |
| `shared` | `switch idle` | 停所有，释放 GPU |
| `exclusive` | `switch <shared_*>` | **❌ 拒绝** |
| `shared` | `switch <exclusive_*>` | **❌ 拒绝** |

---

## 目录结构

```
~/edge_llm/
├── models.d/                        # 模型配置目录（插件式）
│   ├── qwen36-27b.yaml              # mode: exclusive, type: vllm
│   ├── qwen35-9b.yaml               # mode: shared, type: vllm
│   ├── gemma4-26b.yaml              # mode: exclusive, type: vllm
│   └── comfyui.yaml                 # mode: shared, type: comfyui
├── edge_llm/
│   ├── config.py                    # ModelConfig + load_models() + 常量
│   ├── state.py                     # GPUMode + validate_transition + StateDB
│   ├── gpu_lock.py                  # GPULock (flock)
│   ├── health.py                    # HTTP/GPU 健康检查
│   ├── process_manager.py           # vLLM + ComfyUI 进程管理
│   ├── manager.py                   # ModelManager (编排层)
│   ├── cli.py                       # CLI
│   ├── proxy.py                     # HTTP 代理
│   ├── dashboard.py                 # Dashboard HTML
│   └── preload.py                   # 模型预加载 (实验性)
├── scripts/
│   ├── switch_vllm.sh               # 向后兼容 wrapper
│   ├── switch_comfyui.sh            # 向后兼容 wrapper
│   └── edge-llm-recovery.sh         # 紧急恢复
└── tests/
    ├── test_v4.py                   # v4.0 测试
    └── test_local.py                # v3.x 旧测试 (待清理)
```

---

## CLI 参考

### `edge-llm status`

```
GPU Mode : 🔒 exclusive
Services : ['qwen36-27b']
  qwen36-27b: ✅
PIDs     : vLLM PID=12345
GPU      : 29140/32607 MiB used
```

### `edge-llm models`

```
Available Models (4):
name                 mode         type       description
----------------------------------------------------------------------
comfyui              shared       comfyui    ComfyUI 图像生成
gemma4-26b           exclusive    vllm       Gemma4-26B A4B NVFP4
qwen35-9b            shared       vllm       Qwen3.5-9B GPTQ-4bit
qwen36-27b           exclusive    vllm       Qwen3.6-27B NVFP4 + MTP
```

过滤：`edge-llm models --mode exclusive`

### `edge-llm switch <model_name|idle>`

遵守三态规则。独占模型全锁 GPU，共享模型允许共存。

### `edge-llm stop <model_name>`

停止单个共享服务。其他共享服务保留。最后一个停止后自动转 idle。

### `edge-llm reset`

强制重置到 idle。

### `edge-llm reconcile`

状态对账。

### `edge-llm history`

切换历史。

---

## 模型配置格式

### vLLM 模型（独占）

```yaml
# models.d/qwen36-27b.yaml
name: qwen36-27b           # 必须匹配文件名（去掉 .yaml）
description: "Qwen3.6-27B NVFP4 + MTP"
mode: exclusive             # exclusive = GPU 全锁
type: vllm                  # 可省略，默认 vllm

vllm:
  model_dir: Qwen3.6-27B-Text-NVFP4-MTP
  served_name: vllm_qwen27b
  port: 8000
  conda_env: qw36-27b-vllm
  max_model_len: 128000
  gpu_memory_utilization: 0.90
  max_num_seqs: 4
  kv_cache_dtype: fp8
  speculative_config: '{"method": "mtp", "num_speculative_tokens": 3}'
  extra_flags: >-
    --max-num-batched-tokens 8192
    --enable-prefix-caching
    --trust-remote-code
```

### vLLM 模型（共享）

```yaml
# models.d/qwen35-9b.yaml
name: qwen35-9b
description: "Qwen3.5-9B GPTQ-4bit"
mode: shared                # shared = 允许与其他共享服务共存
type: vllm

vllm:
  model_dir: Qwen3.5-9B-GPTQ-4bit/Qwen3.5-9B-GPTQ-4bit
  served_name: vllm_qw35_gptq
  port: 8002
  conda_env: qw35-9b-vllm
  max_model_len: 128000
  gpu_memory_utilization: 0.4
  max_num_seqs: 4
  kv_cache_dtype: fp8
  extra_flags: >-
    --quantization gptq_marlin
    --trust-remote-code
```

### ComfyUI

```yaml
# models.d/comfyui.yaml
name: comfyui
description: "ComfyUI 图像生成"
mode: shared
type: comfyui

conda_env: comfyui
port: 8188
working_dir: ~/ComfyUI
health_url: http://localhost:8188/system_stats
extra_flags: --cache-none --enable-manager
```

---

## 增删模型

### 添加新模型

1. 下载模型到 `~/models/`
2. 创建 conda 环境
3. 写 `~/edge_llm/models.d/new-model.yaml`
4. `edge-llm models` 可见 → `edge-llm switch new-model` 可用

### 删除模型

1. 删 `~/edge_llm/models.d/old-model.yaml`
2. 已运行的模型不受影响（直到下次 switch）

### name 字段规则

YAML 内 `name` 字段必须与文件名（去掉 `.yaml` 后缀）一致。连字符命名：`qwen36-27b.yaml` → `name: qwen36-27b`。

---

## Proxy 服务

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Dashboard |
| GET | `/health` | Proxy 健康检查 |
| GET | `/status` | 完整状态 JSON |
| GET | `/models` | 可用模型列表 |
| GET | `/system` | 系统信息 |
| GET | `/history` | 切换历史 |
| POST | `/v1/chat/completions` | 转发到 vLLM |
| POST | `/switch` | 切换模型 `{"model": "qwen36-27b"}` |
| POST | `/stop` | 停止单个服务 `{"model": "comfyui"}` |
| POST | `/reset` | 强制重置 |
| POST | `/reconcile` | 状态对账 |

### 自动路由

Proxy 根据 `model` 字段动态查找 `models.d/` 中的配置，自动切换到对应模型。

---

## 状态持久化

### StateDB (`~/.edge_llm/state.db`)

| key | 示例 | 说明 |
|-----|------|------|
| `gpu_mode` | `exclusive` / `shared` / `idle` | GPU 状态机 |
| `active_services` | `["qwen36-27b"]` | JSON 数组 |
| `vllm_pid` | `12345` | vLLM 进程组 PGID |
| `comfyui_pid` | `67890` | ComfyUI PGID |
| `profile_state` | `healthy` | 服务健康状态 |

---

## 故障恢复

```bash
# 锁冲突
rm -f /tmp/edge_llm_gpu.lock && edge-llm reconcile

# 进程卡死
edge-llm reset

# GPU 显存不释放
~/edge_llm/scripts/edge-llm-recovery.sh --full

# state.db 损坏
rm -f ~/.edge_llm/state.db && edge-llm reconcile
```

---

## 版本历史

| 版本 | 日期 | 关键变更 |
|------|------|----------|
| v1.0 | 2026-06-25 | bash-only switch_vllm.sh |
| v2.0 | 2026-06-27 | Python 重写，8 个 bug 修复 |
| v3.0 | 2026-06-28 | 进程组管理、状态机、三态健康检查 |
| v3.1 | 2026-06-28 | 模块化拆分、ComfyUI 原生管理 |
| v3.2 | 2026-06-28 | Proxy 稳健重写、systemd watchdog |
| **v4.0** | **2026-06-28** | **模型即插件、三态 GPU 状态机、消除 Profile、models.d/ 目录** |
