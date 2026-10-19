# -*- coding: utf-8 -*-
"""运行路径解析（项目根与私有数据目录）。

私有数据目录可通过环境变量 ZJGSU_CAMPUS_DATA_DIR 覆盖（测试与隔离运行使用）；
默认 = 项目根下的 data/（已被 .gitignore 排除）。
"""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_data_dir() -> Path:
    env = os.environ.get("ZJGSU_CAMPUS_DATA_DIR")
    if env:
        return Path(env)
    return project_root() / "data"
