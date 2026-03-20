"""配置管理: 从 TOML 文件加载，运行时可热更新。

加载优先级:
  1. config.toml  (用户自定义，git-ignored)
  2. config.default.toml  (默认值，随仓库分发)
  3. 硬编码 fallback  (万一两个文件都丢了)

用法:
  cfg = load_config()                       # 自动搜索
  cfg = load_config("/path/to/config.toml") # 指定文件
  save_config(cfg, "/path/to/config.toml")  # 保存修改
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ── 配置文件搜索路径 ─────────────────────────────────────────────────

_THIS_DIR = Path(__file__).parent
_DEFAULT_TOML = _THIS_DIR / "config.default.toml"
_USER_TOML = _THIS_DIR / "config.toml"


# ── Pydantic schemas (只做类型校验，默认值全部从 TOML 来) ────────────

class ReputationConfig(BaseModel):
    max_gain: float = 0.1
    max_penalty: float = -0.05
    default_score: float = 0.5
    cold_start_threshold: int = 10
    cold_start_floor: float = 0.1
    cold_start_ramp: float = 0.9
    event_weights: dict[str, float] = Field(default_factory=lambda: {
        "result_selected": 0.10,
        "result_rejected": -0.05,
        "adjudication_adopted": 0.05,
        "adjudication_failed": -0.03,
        "task_completed_on_time": 0.05,
        "task_timed_out": -0.05,
    })
    selection_boost_multiplier: float = 0.05
    selector_judgment_boost: float = 0.01
    burst_window: int = 10
    burst_threshold: int = 8
    negotiation_gain_multiplier: float = 0.01
    negotiation_gain_min: float = -0.1
    negotiation_gain_max: float = 0.2


class MatcherConfig(BaseModel):
    weight_reputation: float = 0.6
    weight_domain: float = 0.25
    weight_keyword: float = 0.15
    default_reputation: float = 0.5
    ability_threshold: float = 0.5
    price_tolerance: float = 0.1
    target_min_reputation: float = 0.3


class EconomyConfig(BaseModel):
    platform_fee_rate: float = 0.05


class PushConfig(BaseModel):
    max_retries: int = 2


class TaskConfig(BaseModel):
    default_max_concurrent_bidders: int = 5
    default_max_depth: int = 10


class APIConfig(BaseModel):
    list_tasks_default_limit: int = 50
    list_tasks_max_limit: int = 200
    logs_default_limit: int = 50
    logs_max_limit: int = 500


class ClusterConfig(BaseModel):
    seed_nodes: list[str] = Field(default_factory=list)
    heartbeat_interval: int = 10
    heartbeat_fan_out: int = 3
    suspect_rounds: int = 3
    offline_rounds: int = 6
    node_id: str = ""
    endpoint: str = ""
    protocol_version: str = "0.1.0"


class NetworkConfig(BaseModel):
    reputation: ReputationConfig = Field(default_factory=ReputationConfig)
    matcher: MatcherConfig = Field(default_factory=MatcherConfig)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
    push: PushConfig = Field(default_factory=PushConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)


# ── 加载/保存 ────────────────────────────────────────────────────────

def load_config(path: str | Path | None = None) -> NetworkConfig:
    """从 TOML 文件加载配置。

    path=None 时自动搜索:
      1. eacn/network/config.toml  (用户覆盖)
      2. eacn/network/config.default.toml  (默认)
    """
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        return _parse_toml(p)

    # 先加载 default，再用 user 覆盖
    data: dict[str, Any] = {}
    if _DEFAULT_TOML.exists():
        data = _read_toml(_DEFAULT_TOML)
    if _USER_TOML.exists():
        user_data = _read_toml(_USER_TOML)
        _deep_merge(data, user_data)

    return NetworkConfig(**data)


def save_config(config: NetworkConfig, path: str | Path | None = None) -> Path:
    """将配置写入 TOML 文件。默认写到 config.toml。"""
    p = Path(path) if path else _USER_TOML
    lines = _to_toml(config.model_dump())
    p.write_text(lines, encoding="utf-8")
    return p


# ── 内部工具 ──────────────────────────────────────────────────────────

def _read_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_toml(path: Path) -> NetworkConfig:
    return NetworkConfig(**_read_toml(path))


def _deep_merge(base: dict, override: dict) -> None:
    """递归合并 override 到 base (in-place)。"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _to_toml(data: dict[str, Any]) -> str:
    """简单 TOML 序列化 (不依赖第三方库)。"""
    lines: list[str] = []

    # 先输出标量字段
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_toml_value(v)}")

    # 再输出子表
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append("")
            lines.append(f"[{k}]")
            for sk, sv in v.items():
                if not isinstance(sv, dict):
                    lines.append(f"{sk} = {_toml_value(sv)}")
            # 嵌套子表 (e.g. reputation.event_weights)
            for sk, sv in v.items():
                if isinstance(sv, dict):
                    lines.append("")
                    lines.append(f"[{k}.{sk}]")
                    for ssk, ssv in sv.items():
                        lines.append(f"{ssk} = {_toml_value(ssv)}")

    return "\n".join(lines) + "\n"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    return str(v)
