from __future__ import annotations

import os

os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")

import paddle


class GPUProbeError(RuntimeError):
    pass


def probe_gpu(required_capability: tuple[int, int] = (12, 0)) -> dict:
    """强制 GPU 探针。任一不满足即抛 GPUProbeError，绝不静默回退 CPU（需求 12）。"""
    info: dict = {}
    info["compiled_with_cuda"] = paddle.is_compiled_with_cuda()
    if not info["compiled_with_cuda"]:
        raise GPUProbeError(
            "paddlepaddle 未编译 CUDA 支持。请确认安装的是 paddlepaddle-gpu（cu129），而非 CPU 版。"
        )
    info["device_count"] = int(paddle.device.cuda.device_count())
    if info["device_count"] <= 0:
        raise GPUProbeError(
            "未检测到可用 CUDA 设备。请检查 WSL2 的 NVIDIA 驱动透传（/usr/lib/wsl/lib/libcuda.so.1）。"
        )
    info["device_name"] = paddle.device.cuda.get_device_name(0)
    cap = paddle.device.cuda.get_device_capability(0)
    info["capability"] = tuple(cap)
    info["capability_str"] = f"sm_{cap[0] * 10 + cap[1]}"
    if (cap[0], cap[1]) < required_capability:
        raise GPUProbeError(
            f"GPU 算力 {cap} 低于要求 {required_capability}（Blackwell sm_120+）。"
            f" 当前设备：{info['device_name']}"
        )
    # 实际算子执行验证
    try:
        x = paddle.randn([512, 512])
        y = x.cuda()
        z = paddle.matmul(y, y)
        paddle.device.synchronize()
        _ = float(z.sum())
        info["op_exec_ok"] = True
    except Exception as e:
        info["op_exec_ok"] = False
        raise GPUProbeError(f"GPU 算子执行失败：{type(e).__name__}: {e}") from e
    info["place"] = str(paddle.device.get_device())
    return info


def format_probe(info: dict) -> str:
    return (
        f"GPU: {info['device_name']} | "
        f"capability={info['capability_str']} | "
        f"count={info['device_count']} | "
        f"place={info['place']} | op_ok={info['op_exec_ok']}"
    )
