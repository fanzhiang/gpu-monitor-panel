# -*- coding: utf-8 -*-
"""模型顾问引擎 — 纯本地计算,不依赖网络。

输入: GPU VRAM (MB)、系统 RAM、CPU 核心数
输出: Qwen3/Gemma/Ornith/DeepSeek 全系列每个量化级别的可行性矩阵 + 最优推荐 + 显存拆解
"""

import json
import os
from typing import Optional


# ── 模型库 (已验证的公开数据) ──────────────────────────────
# 参数量单位: B (十亿), active_params_b = MoE 激活参数 / Dense = 总参数
# layers: Transformer 层数
# hidden_size: 隐藏维度
# num_attention_heads: 注意力头数 (全局, MoE 为总头数)
# num_kv_heads: KV 头数

MODELS = {
    # ── Qwen3.8 系列 ───────────────────────────────────────
    "qwen3.8-27b": {
        "params_b": 26.9,
        "layers": 40,
        "hidden_size": 3584,
        "num_attention_heads": 28,
        "num_kv_heads": 8,
        "is_moe": False,
        "kv_hybrid": True,   # Qwen3.8 混合注意力 (GDN 线性层无逐 token KV)
        # 实测权重锚点 (GB, Ollama/本地部署工具): 用于校准所有量化级别的权重估算
        "measured_weight_gb": {"Q4_K_M": 17.5},
    },
    "qwen3.8-9b": {
        "params_b": 8.75,
        "layers": 28,
        "hidden_size": 4096,
        "num_attention_heads": 28,
        "num_kv_heads": 4,
        "is_moe": False,
        "kv_hybrid": True,
        "measured_weight_gb": {"Q8_0": 7.0},
    },
    "qwen3.8-flash": {
        # 官方规格: 主干 125B MoE + 51B N-gram 嵌入(可卸载到内存), 每 token 激活 ~6B
        # 48 层 = 12 x (3xGDN-MoE + 1xQSA-MoE); GDN 线性层无逐 token KV,
        # 仅 12 个 QSA 层有 KV (2 kv_heads x 256 dim) -> KV 极小
        "params_b": 6.0,
        "params_b_total": 176.0,   # 125B 主干 + 51B N-gram
        "layers": 48,
        "hidden_size": 2560,
        "num_attention_heads": 24,
        "num_kv_heads": 2,
        "is_moe": True,
        "kv_per_token_mb": 0.024,  # QSA 层实测推算, 绕过通用公式
        "ctx_native": 262144,      # 原生 262K (YaRN 可扩 1M)
    },

    # ── Qwen3.6 / Qwen3 旧系列 ─────────────────────────────
    "qwen3-0.6b": {
        "params_b": 0.6,
        "layers": 28,
        "hidden_size": 1024,
        "num_attention_heads": 16,
        "num_kv_heads": 8,
        "is_moe": False,
    },
    "qwen3-1.7b": {
        "params_b": 1.7,
        "layers": 28,
        "hidden_size": 2048,
        "num_attention_heads": 16,
        "num_kv_heads": 8,
        "is_moe": False,
    },
    "qwen3-4b": {
        "params_b": 4.75,       # MoE active (~4.75B)
        "params_b_total": 6.0,   # total params for KV-cache scaling
        "layers": 36,
        "hidden_size": 2816,
        "num_attention_heads": 20,
        "num_kv_heads": 4,
        "is_moe": True,
    },
    "qwen3-8b": {
        "params_b": 8.75,
        "layers": 28,
        "hidden_size": 4096,
        "num_attention_heads": 28,
        "num_kv_heads": 4,
        "is_moe": False,
    },
    "qwen3-14b-a3b": {
        "params_b": 3.57,       # MoE active (~3.6B), total ~14B
        "params_b_total": 14.0,
        "layers": 28,
        "hidden_size": 3072,
        "num_attention_heads": 24,
        "num_kv_heads": 8,
        "is_moe": True,
    },
    "qwen3-35b-a3b": {
        "params_b": 3.57,       # MoE active (~3.6B), total ~35B
        "params_b_total": 35.0,
        "layers": 40,
        "hidden_size": 4096,
        "num_attention_heads": 28,
        "num_kv_heads": 8,
        "is_moe": True,
    },
    "qwen3-32b": {
        "params_b": 31.75,      # Dense (~31.75B)
        "layers": 40,
        "hidden_size": 5120,
        "num_attention_heads": 32,
        "num_kv_heads": 8,
        "is_moe": False,
    },
    "qwen3-235b-a22b": {
        "params_b": 6.91,       # MoE active (~6.9B), total ~235B
        "params_b_total": 235.0,
        "layers": 64,
        "hidden_size": 4096,
        "num_attention_heads": 32,
        "num_kv_heads": 8,
        "is_moe": True,
    },

    # ── Gemma 系列 ─────────────────────────────────────────
    "gemma-12b": {
        "params_b": 12.7,       # Gemma 2 12B (Dense)
        "layers": 48,
        "hidden_size": 4608,
        "num_attention_heads": 36,
        "num_kv_heads": 18,
        "is_moe": False,
    },
    "gemma-26b": {
        "params_b": 25.7,       # Gemma 3 26B (Dense)
        "layers": 48,
        "hidden_size": 4608,
        "num_attention_heads": 36,
        "num_kv_heads": 18,
        "is_moe": False,
        "kv_hybrid": True,      # Gemma 3: 5:1 local:global 滑窗注意力
    },
    "gemma-31b": {
        "params_b": 30.7,       # Gemma 3 31B (Dense)
        "layers": 48,
        "hidden_size": 5120,
        "num_attention_heads": 32,
        "num_kv_heads": 8,
        "is_moe": False,
        "kv_hybrid": True,
    },

    # ── Ornith (中文优化) 系列 ───────────────────────────────
    "ornith-9b": {
        "params_b": 8.75,       # Ornith 1.5 9B (Llama-like Dense)
        "layers": 32,
        "hidden_size": 4096,
        "num_attention_heads": 32,
        "num_kv_heads": 8,
        "is_moe": False,
    },
    "ornith-35b": {
        "params_b": 3.6,        # Ornith 1.5 35B MoE active (~3.6B), total ~35B
        "params_b_total": 35.0,
        "layers": 46,
        "hidden_size": 6656,
        "num_attention_heads": 48,
        "num_kv_heads": 8,
        "is_moe": True,
        "measured_weight_gb": {"Q4_K_M": 21.7},
    },

    # ── DeepSeek V4 系列 ───────────────────────────────────
    "deepseek-v4-flash": {
        # 官方规格: 284B 总参 / 13B 激活, 43 层, hidden 4096, 1M 上下文
        # MLA 单潜在 KV (512 维) + CSA/HCA 压缩注意力 (KV 仅 V3.2 的 7%)
        "params_b": 13.0,
        "params_b_total": 284.0,
        "layers": 43,
        "hidden_size": 4096,
        "num_attention_heads": 64,
        "num_kv_heads": 1,
        "is_moe": True,
        "kv_per_token_mb": 0.01,   # MLA 潜在 KV + CSA/HCA 压缩, 下限值
        "ctx_native": 1048576,     # 原生 1M
    },
}


# ── 量化级别与每参数字节数 (bytes/param) ────────────────────────

QUANT_PRESETS = {
    "FP16": {"bytes_per_param": 2.0, "quality": "无损", "speed": "最快"},
    "Q8_0": {"bytes_per_param": 1.0, "quality": "接近 FP16", "speed": "快"},
    "Q6_K": {"bytes_per_param": 0.78, "quality": "高精度", "speed": "中等"},
    "Q5_K_M": {"bytes_per_param": 0.65, "quality": "良好", "speed": "中等偏慢"},
    "Q4_K_M": {"bytes_per_param": 0.55, "quality": "可用", "speed": "较慢"},
    "Q3_K_L": {"bytes_per_param": 0.47, "quality": "一般 (大模型)", "speed": "慢"},
    "Q3_K_S": {"bytes_per_param": 0.43, "quality": "一般 (小模型)", "speed": "慢"},
    "Q2_K": {"bytes_per_param": 0.34, "quality": "较差", "speed": "最慢"},
}

# ── 用户偏好: 显存估算采用的默认上下文长度 (tokens) ─────────────────
DEFAULT_CTX = 128 * 1024  # 128K tokens


# ── KV Cache 每 token 内存公式 ────────────────────────────────────

def calc_kv_per_token_mb(hidden_size: int, num_kv_heads: int, layers: int) -> float:
    """计算每 token 的 KV Cache 显存 (MB)，FP16 K+V。"""
    head_dim = hidden_size // num_kv_heads if num_kv_heads > 0 else hidden_size
    # 每 token: layers * num_kv_heads * head_dim * 2 (K+V) * 2 bytes (FP16 / 1024^2
    return layers * num_kv_heads * head_dim * 2 * 2 / (1024 ** 2)


# ── 模型推荐引擎核心逻辑 ───────────────────────────────────────────

class ModelAdvisor:
    """模型顾问引擎。"""

    def __init__(self, vram_mb: int, ram_mb: int, cpu_cores: int):
        self.vram_mb = vram_mb  # GPU VRAM (MB)
        self.ram_mb = ram_mb     # 系统内存 (MB)
        self.cpu_cores = cpu_cores

    def recommend(self, model_name: Optional[str] = None) -> dict:
        """生成推荐。如果不指定模型名,返回全部模型的摘要。"""
        results = {}

        for name, arch in MODELS.items():
            rec = self._calc_recommendation(name, arch)
            results[name] = rec

        if model_name and model_name in results:
            return {model_name: results[model_name]}

        # 返回全部摘要 (用于面板展示)
        summary = {}
        for name, rec in results.items():
            summary[name] = {
                "best_quant": rec["best_quant"],
                "max_context_tokens": rec["max_context_tokens"],
                "recommended_vram_mb": round(rec["recommended_vram_mb"], 1),
                "is_moe": rec["is_moe"],
                "active_params_b": rec["active_params_b"],
            }
        return summary

    def _calc_recommendation(self, name: str, arch: dict) -> dict:
        """计算单个模型的推荐。"""
        params_b = arch["params_b"]
        layers = arch["layers"]
        hidden_size = arch["hidden_size"]
        num_kv_heads = arch["num_kv_heads"]
        is_moe = arch.get("is_moe", False)

        # MoE 权重必须用总参数量 (params_b 只是激活参数, 用于速度/KV估算)
        total_b = arch.get("params_b_total", params_b) if is_moe else params_b

        # 实测锚点校准: 该模型有实测权重时, 按比例校准所有量化级别的权重估算
        calib = 1.0
        meas = arch.get("measured_weight_gb", {})
        if meas:
            q_anchor, meas_gb = next(iter(meas.items()))
            base_anchor_mb = total_b * 1e9 * QUANT_PRESETS[q_anchor]["bytes_per_param"] / (1024 ** 2)
            if base_anchor_mb > 0:
                calib = meas_gb * 1024 / base_anchor_mb

        # KV cache 每 token 计算链:
        # 1) 结构基线 (layers * kv_heads * head_dim * 2 * 2bytes)
        # 2) MoE 按 (active/total)² 缩减（经验公式，与实测 256K 对齐）
        # 3) 运行时 KV Q4 量化 (llama.cpp -ctk/-ctv q4_0, 主流本地部署默认可用) × 0.25
        # 4) 仅混合注意力架构 (Gemma3 5:1 local:global / Qwen3.8 等, 条目标 kv_hybrid=True)
        #    有效 KV 层占比 × 0.25 —— 普通 dense/GQA 模型不吃这一项
        # 特殊架构 (MLA/QSA/CSA 等非标准 KV) 用模型条目里的显式 kv_per_token_mb 覆盖
        kv_override = arch.get("kv_per_token_mb")
        if kv_override:
            kv_per_token_mb = float(kv_override)
        else:
            kv_per_token_mb = calc_kv_per_token_mb(hidden_size, num_kv_heads, layers)
            if is_moe and "params_b_total" in arch and arch["params_b_total"] > params_b:
                ratio = params_b / arch["params_b_total"]
                kv_per_token_mb *= (ratio ** 2)
            if arch.get("kv_hybrid"):
                kv_per_token_mb *= 0.25
            kv_per_token_mb *= 0.25
            # 下限：每 token 不低于 0.01 MB（防止超大 MoE 过度乐观）
            kv_per_token_mb = max(kv_per_token_mb, 0.01)

        # 每个量化级别的显存计算
        quant_results = {}
        for quant_name, preset in QUANT_PRESETS.items():
            bpp = preset["bytes_per_param"]

            # 模型权重显存 (MB) = total_params_b * 10^9 * bpp * 校准系数 / 1024^2
            # (实测锚点权重已含运行时开销, 故不再叠加额外 headroom)
            weight_mb = total_b * 1e9 * bpp * calib / (1024 ** 2)

            # KV Cache 预算: VRAM - 权重
            available_for_kv_mb = self.vram_mb - weight_mb

            if available_for_kv_mb <= 0:
                quant_results[quant_name] = {
                    "bytes_per_param": bpp,
                    "weight_mb": round(weight_mb, 1),
                    "kv_per_token_mb": round(kv_per_token_mb, 4),
                    "max_context_tokens": 0,
                    "total_vram_mb": round(weight_mb + kv_per_token_mb * DEFAULT_CTX, 1),
                    "feasible": False,
                }
                continue

            max_ctx = int(available_for_kv_mb / kv_per_token_mb) if kv_per_token_mb > 0 else 0
            # 上限: 不超过模型原生训练上下文 (默认 256K)
            max_ctx = min(max_ctx, arch.get("ctx_native", 256 * 1024))

            # 总显存 (权重 + KV for 默认上下文长度 DEFAULT_CTX tokens)
            default_ctx = min(DEFAULT_CTX, max_ctx) if max_ctx > 0 else 0
            total_vram = weight_mb + kv_per_token_mb * default_ctx

            quant_results[quant_name] = {
                "bytes_per_param": bpp,
                "weight_mb": round(weight_mb, 1),
                "kv_per_token_mb": round(kv_per_token_mb, 4),
                "max_context_tokens": max_ctx,
                "total_vram_mb": round(total_vram, 1),
                # 可行 = 权重装得下 且 上下文 >= 32K (最低可用长度)
                "feasible": weight_mb < self.vram_mb * 0.98 and max_ctx >= 32 * 1024,
            }

        # 选择最优量化：Q4_K_M 是默认首选（用户偏好），不可行时降级
        feasible = [(q, r) for q, r in quant_results.items() if r["feasible"]]

        best = None
        if feasible:
            # 1. 优先找 Q4_K_M（只要存在就选它，即使用不到 32K）
            q4_found = False
            for q, r in feasible:
                if q == "Q4_K_M":
                    best = (q, r)
                    q4_found = True
                    break

            # 2. 如果 Q4_K_M ctx >= 32K，直接用；否则尝试 Q5_K_M/Q6_K 看能否到 32K+
            if q4_found and best[1]["max_context_tokens"] < 32 * 1024:
                for q, r in feasible:
                    if q in ("Q5_K_M", "Q6_K") and r["max_context_tokens"] >= 32 * 1024:
                        best = (q, r)
                        break

            # 3. 如果 Q4_K_M 不存在，选 ctx 最长的可行量化（通常是高质量量化）
            if not q4_found:
                feasible.sort(key=lambda x: x[1]["max_context_tokens"], reverse=True)
                best = feasible[0]

        if not best:
            # 都不行，选总显存最小的
            all_sorted = sorted(quant_results.items(), key=lambda x: x[1]["total_vram_mb"])
            best = all_sorted[0]

        best_quant_name, best_result = best

        # VRAM 拆解
        weight_mb = best_result["weight_mb"]
        kv_mb_for_default = best_result["kv_per_token_mb"] * min(DEFAULT_CTX, best_result["max_context_tokens"]) if best_result["max_context_tokens"] > 0 else 0
        overhead_mb = self.vram_mb - weight_mb - kv_mb_for_default

        # 估算速度 (基于参数量和量化级别)
        speed = self._estimate_speed(params_b, best_quant_name, is_moe)

        return {
            "model_name": name,
            "is_moe": is_moe,
            "active_params_b": params_b,
            "total_params_b": arch.get("params_b_total", None),
            "layers": layers,
            "hidden_size": hidden_size,
            "num_kv_heads": num_kv_heads,
            "kv_per_token_mb": round(kv_per_token_mb, 4),
            "weight_calibrated": calib != 1.0,
            "kv_quantized_q8": True,
            "quant_results": quant_results,
            "best_quant": best_quant_name,
            "max_context_tokens": best_result["max_context_tokens"],
            "recommended_vram_mb": round(weight_mb + kv_mb_for_default, 1),
            "vram_breakdown": {
                "weights_mb": round(weight_mb, 1),
                "kv_cache_mb": round(kv_mb_for_default, 1),
                "overhead_mb": round(overhead_mb, 1),
            },
            "estimated_speed": speed,
            "quality_note": QUANT_PRESETS[best_quant_name]["quality"],
        }

    def _estimate_speed(self, params_b: float, quant: str, is_moe: bool) -> str:
        """估算推理速度 (tokens/s)。"""
        # 基准: FP16 8B ~ 50 tok/s (RTX 4090), 每翻倍参数量 /2.5
        base_speed = 50 * ((params_b / 8) ** (-0.7))

        # 量化加速比 (Q4 比 FP16 快约 1.8x, Q8 快 1.3x)
        speedup = {
            "FP16": 1.0,
            "Q8_0": 1.3,
            "Q6_K": 1.5,
            "Q5_K_M": 1.7,
            "Q4_K_M": 1.9,
            "Q3_K_L": 2.1,
            "Q3_K_S": 2.2,
            "Q2_K": 2.3,
        }.get(quant, 1.0)

        # MoE 加速 (激活参数少, 实际推理快)
        moe_bonus = 1.5 if is_moe else 1.0

        est_tps = base_speed * speedup * moe_bonus

        if est_tps >= 30:
            return f"很快 ({est_tps:.0f} tok/s)"
        elif est_tps >= 15:
            return f"较快 ({est_tps:.0f} tok/s)"
        elif est_tps >= 8:
            return f"中等 ({est_tps:.0f} tok/s)"
        else:
            return f"较慢 ({est_tps:.0f} tok/s)"


# ── 本机配置加载器 ────────────────────────────────────────────────

def load_local_config() -> dict:
    """从 probe_sys.py 输出或默认值加载本机配置（结果缓存，进程探测只跑一次）。"""
    global _local_cfg_cache
    if _local_cfg_cache is not None:
        return _local_cfg_cache

    import subprocess, json, sys

    try:
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_sys.py")],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=8,
        )
        if result.returncode == 0:
            cfg = json.loads(result.stdout)
            # 探测不完整（无 GPU/显存字段）时回退默认值，避免顾问界面渲染崩溃
            if isinstance(cfg, dict) and cfg.get("gpu") and cfg["gpu"].get("vram_mb"):
                _local_cfg_cache = cfg
                return _local_cfg_cache
            raise ValueError("probe 输出不完整")
    except Exception:
        pass

    # 默认值 (RTX 5090 Laptop GPU / 32GB RAM / i9-14900HX)
    _local_cfg_cache = {
        "gpu": {"name": "RTX 5090 Laptop GPU", "vram_mb": 24463, "driver": "32.0.16.1656"},
        "ram_mb": 32768,
        "cpu": {"name": "Intel Core i9-14900HX", "cores": 32},
    }
    return _local_cfg_cache


_local_cfg_cache = None


def warm_local_config(callback=None):
    """后台预热本机配置缓存（避免 GUI 点击时阻塞）。"""
    import threading

    def _run():
        load_local_config()
        if callback:
            try:
                callback()
            except Exception:
                pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ── 多设备配置 (推荐目标机) ────────────────────────────────────────
# local = 本机自动探测; 其他为手动登记的部署目标机
DEVICES = {
    "amd395-128g": {
        "label": "AMD 395 (128G 统一内存)",
        "gpu": {"name": "AMD Ryzen AI Max+ 395", "vram_mb": 118784},
        "ram_mb": 118784,   # 统一内存: CPU/GPU 共享, 预留 ~10G 给系统
        "cpu": {"name": "AMD Ryzen AI Max+ 395", "cores": 16},
        "note": "统一内存大容量低带宽, MoE 大模型(激活参数少)为最佳场景",
    },
}

DEVICE_LABELS = {"local": "本机 (自动检测)", **{k: v["label"] for k, v in DEVICES.items()}}


def get_device_config(device_key: str = "local") -> dict:
    """按设备 key 返回配置; local 走自动探测, 未知 key 回退本机。"""
    if device_key != "local" and device_key in DEVICES:
        return dict(DEVICES[device_key])
    return load_local_config()


# ── 主入口 ────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = load_local_config()

    vram_mb = config["gpu"]["vram_mb"]
    ram_mb = config.get("ram_mb", 32768)
    cpu_cores = config.get("cpu", {}).get("cores", 16)

    advisor = ModelAdvisor(vram_mb, ram_mb, cpu_cores)

    # 输出全部模型推荐摘要
    summary = advisor.recommend()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
