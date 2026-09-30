"""建库与查询共用的句向量模型和编码格式。"""
from __future__ import annotations

import math
import os
from collections.abc import Iterable

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIMENSION = 512


def load_model(*, threads: int | None = None):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME, threads=threads,
                         cache_dir=os.environ.get("HS_MODEL_CACHE"))


def vector_literal(values: Iterable[float]) -> str:
    vector = [float(value) for value in values]
    if len(vector) != DIMENSION or not all(math.isfinite(value) for value in vector):
        raise ValueError(f"模型必须输出 {DIMENSION} 维有限数值")
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
