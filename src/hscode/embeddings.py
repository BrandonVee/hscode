"""建库与查询共用的句向量模型和编码格式。"""
from __future__ import annotations

import math
import os
from pathlib import Path
from collections.abc import Iterable

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIMENSION = 512


def load_model(*, threads: int | None = None, download: bool = True):
    from fastembed import TextEmbedding
    model_path = os.environ.get("HS_MODEL_PATH")
    if model_path:
        directory = Path(model_path)
        for name in ("model_optimized.onnx", "tokenizer.json"):
            if not (directory / name).is_file():
                raise ValueError(f"本地模型目录缺少 {name}：{directory}")
    return TextEmbedding(model_name=MODEL_NAME, threads=threads,
                         cache_dir=os.environ.get("HS_MODEL_CACHE"),
                         local_files_only=not download,
                         specific_model_path=model_path or None)


def vector_literal(values: Iterable[float]) -> str:
    vector = [float(value) for value in values]
    if len(vector) != DIMENSION or not all(math.isfinite(value) for value in vector):
        raise ValueError(f"模型必须输出 {DIMENSION} 维有限数值")
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
