"""配置加载与默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _expand(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


@dataclass
class ClientKey:
    name: str
    key: str
    allow_models: list[str] = field(default_factory=list)


@dataclass
class Upstream:
    name: str
    type: str
    base_url: str
    api_key: str
    # 备用 key 链（按优先级）：api_key 之外还可以配多个 key。
    # 主 key 返回 402/429（额度耗尽/限流）时，同一上游自动换下一个 key 重发。
    api_keys: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    usage_source: dict[str, Any] = field(default_factory=dict)
    balance: dict[str, Any] = field(default_factory=dict)
    no_proxy: bool = False
    # 模型前缀别名，例如 "axon/kimi-k3" 中的 "axon"
    alias: str = ""
    # 计费模式，决定成本怎么算、怎么展示：
    #   payg         按量付费 —— cost_usd 是真实支出（如 Mistral API）
    #   subscription 订阅制   —— cost_usd 是"等效价值"（如果按 API 价买要花多少），不是实际支出
    #   free         免费渠道 —— cost_usd 强制记 0
    billing_mode: str = "payg"
    # 订阅月费（可选），用于算"实际支出"
    monthly_fee: float = 0.0
    # 该上游不支持流式（或流式会挂起）时，内部用非流式请求，
    # 再把完整结果包装成 SSE 返回给客户端，客户端体验不变。
    force_non_stream: bool = False
    # 请求体兼容性改写，见 app/proxy.py 的 _normalize_request_body。
    #   developer_role: system  —— 把 role: developer 改写成 system（默认）
    #                   keep    —— 原样透传
    # Mistral 官方 API 对角色是严格枚举，收到 developer 直接 422。
    compat: dict[str, Any] = field(default_factory=dict)
    # 负载均衡：同一模型在多个上游时，按 weight 加权随机
    weight: int = 1
    # 优先级：数字小的优先，同优先级内按 weight 分流
    priority: int = 0
    # 运行时发现
    discovered_models: list[str] = field(default_factory=list)
    # 探测得到的不可用模型
    unavailable_models: set[str] = field(default_factory=set)
    # 运行时健康状态（由 health.py 维护）
    healthy: bool = True
    consecutive_failures: int = 0
    cooldown_until: float = 0.0

    @property
    def all_models(self) -> list[str]:
        """可用模型列表（已过滤掉探测失败的）。"""
        raw = self.models or self.discovered_models
        if not self.unavailable_models:
            return raw
        return [m for m in raw if m not in self.unavailable_models]

    @property
    def prefix(self) -> str:
        return self.alias or self.name

    @property
    def effective_keys(self) -> list[str]:
        """按优先级排列的全部可用 key（api_key 在前，api_keys 去重后追加）。"""
        keys: list[str] = []
        if self.api_key:
            keys.append(self.api_key)
        for k in self.api_keys:
            if k and k not in keys:
                keys.append(k)
        return keys or [self.api_key]

    def matches(self, model: str) -> bool:
        return model in self.all_models


@dataclass
class QuotaAlertConfig:
    """额度告警配置。

    只看「剩余百分比」这一维：低于阈值进 warn，再低于 critical 进 critical。
    百分比类窗口（订阅额度）和金额类窗口（余额）都折算成百分比后统一判断，
    所以这里不需要区分上游。
    """

    enabled: bool = True
    # 剩余低于这个百分比就告警
    warn_pct: float = 20.0
    # 剩余低于这个百分比算严重
    critical_pct: float = 10.0
    # 忽略的窗口，支持 fnmatch 通配符，例如 "model:*"
    ignore_windows: list[str] = field(default_factory=list)
    # 同一窗口多久之内不重复提醒（秒）
    cooldown_sec: int = 6 * 3600
    # 预留字段：恢复提醒的节流。当前实现里恢复无条件提醒一次
    # （每个窗口一轮只会恢复一次，且额度重置是好消息，不该被挡）。
    recovery_cooldown_sec: int = 3600
    # 采集器多久没更新就算「数据过期」，不参与判断（秒）
    stale_sec: int = 3 * 3600
    # 通知渠道
    notify: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    host: str
    port: int
    data_dir: Path
    collect_interval_sec: int
    client_keys: list[ClientKey]
    upstreams: list[Upstream]
    local_sources: dict[str, Any]
    proxy: str = ""
    prices: Any = None          # PriceTable，启动时注入
    # 失败重试轮数：1 = 只试一次（默认）；2 = 失败后再试一轮
    retry_rounds: int = 1
    # 是否允许跨上游故障转移。
    # 默认 False：失败立即返回错误，绝不静默切到另一个（可能更贵的）上游。
    failover: bool = False
    # 用量记录保留天数（0 = 永久保留），超期自动清理
    retention_days: int = 365
    # /api/admin/* 管理端点的鉴权 key；留空不启用（仅本机部署时可接受）
    admin_key: str = ""
    # 额度告警
    alerts: QuotaAlertConfig = field(default_factory=QuotaAlertConfig)

    def find_key(self, key: str) -> ClientKey | None:
        for ck in self.client_keys:
            if ck.key == key:
                return ck
        return None

    def route(self, model: str) -> tuple[Upstream | None, str, str]:
        """解析模型名并返回 (上游, 实际模型名, 错误提示)。

        只接受带前缀的写法：
            "axon/kimi-k3"  -> (relay-axon, "kimi-k3", "")
            "kimi-k3"       -> (None, "", "需要前缀")

        前缀写错时返回明确提示。
        """
        prefix, sep, rest = model.partition("/")
        if not sep or not rest:
            known = ", ".join(u.prefix for u in self.upstreams)
            return None, "", (
                f"Model must be prefixed with an upstream alias. "
                f"Use '<alias>/<model>', e.g. 'opgo/{model}'. Available aliases: {known}"
            )

        for up in self.upstreams:
            if up.prefix == prefix or up.name == prefix:
                return up, rest, ""

        known = ", ".join(u.prefix for u in self.upstreams)
        return None, "", (
            f"Unknown upstream prefix '{prefix}'. Available: {known}"
        )

    def find_by_prefix(self, prefix: str) -> Upstream | None:
        for up in self.upstreams:
            if up.prefix == prefix or up.name == prefix:
                return up
        return None

    def proxy_for(self, upstream: Upstream) -> str | None:
        """返回该上游应使用的代理；None 表示直连。"""
        if upstream.no_proxy:
            return None
        return self.proxy or None


def load_model_health(path: Path) -> dict[str, dict[str, dict]]:
    """读取 probe_models 生成的可用性记录。"""
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def apply_model_health(upstreams: list[Upstream], health: dict[str, dict[str, dict]]) -> None:
    """把探测结果应用到上游，标记不可用模型。"""
    for up in upstreams:
        records = health.get(up.name) or {}
        bad = {m for m, r in records.items() if (r or {}).get("status") != "ok"}
        up.unavailable_models = bad


def _load_alerts(raw: dict[str, Any]) -> QuotaAlertConfig:
    """解析 alerts 段。缺省值见 QuotaAlertConfig。"""
    section = raw.get("alerts") or {}
    if not isinstance(section, dict):
        section = {}

    def _num(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    ignore = section.get("ignore_windows") or []
    if isinstance(ignore, str):
        ignore = [ignore]

    return QuotaAlertConfig(
        enabled=bool(section.get("enabled", True)),
        warn_pct=_num(section.get("warn_pct"), 20.0),
        critical_pct=_num(section.get("critical_pct"), 10.0),
        ignore_windows=[str(x) for x in ignore],
        cooldown_sec=max(0, _int(section.get("cooldown_sec"), 6 * 3600)),
        recovery_cooldown_sec=max(0, _int(section.get("recovery_cooldown_sec"), 3600)),
        stale_sec=max(60, _int(section.get("stale_sec"), 3 * 3600)),
        notify=dict(section.get("notify") or {}),
    )


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("USAGE_GATEWAY_CONFIG") or ROOT / "config.yaml")
    if not cfg_path.exists():
        example = ROOT / "config.example.yaml"
        raise FileNotFoundError(
            f"配置文件不存在: {cfg_path}\n请先复制 {example.name} 为 {cfg_path.name} 并修改。"
        )

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    server = raw.get("server") or {}
    gateway = raw.get("gateway") or {}

    client_keys = [
        ClientKey(
            name=str(item.get("name") or f"key-{i}"),
            key=str(item["key"]),
            allow_models=list(item.get("allow_models") or []),
        )
        for i, item in enumerate(gateway.get("client_keys") or [])
        if item.get("key")
    ]

    upstreams: list[Upstream] = []
    for item in raw.get("upstreams") or []:
        if not item.get("name") or not item.get("base_url"):
            continue
        upstreams.append(
            Upstream(
                name=str(item["name"]),
                type=str(item.get("type") or "openai"),
                base_url=str(item["base_url"]).rstrip("/"),
                api_key=str(item.get("api_key") or ""),
                api_keys=[str(k) for k in (item.get("api_keys") or []) if k],
                models=list(item.get("models") or []),
                usage_source=dict(item.get("usage_source") or {}),
                balance=dict(item.get("balance") or {}),
                no_proxy=bool(item.get("no_proxy") or False),
                alias=str(item.get("alias") or ""),
                billing_mode=str(item.get("billing_mode") or "payg").lower(),
                monthly_fee=float(item.get("monthly_fee") or 0.0),
                force_non_stream=bool(item.get("force_non_stream") or False),
                compat=dict(item.get("compat") or {}),
            )
        )

    data_dir = _expand(raw.get("data_dir")) or (ROOT / "data")

    proxy = str(raw.get("proxy") or "").strip()
    if not proxy:
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()

    return Config(
        host=str(server.get("host") or "127.0.0.1"),
        port=int(server.get("port") or 9000),
        data_dir=data_dir,
        collect_interval_sec=int(raw.get("collect_interval_sec") or 60),
        client_keys=client_keys,
        upstreams=upstreams,
        local_sources=dict(raw.get("local_sources") or {}),
        proxy=proxy,
        retry_rounds=int(raw.get("retry_rounds") or 1),
        failover=bool(raw.get("failover") or False),
        retention_days=(
            365 if raw.get("retention_days") is None
            else max(0, int(raw.get("retention_days") or 0))
        ),
        admin_key=str(server.get("admin_key") or "").strip(),
        alerts=_load_alerts(raw),
    )
