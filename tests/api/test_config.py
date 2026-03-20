"""Tests: Config API + TOML-based config management.

Covers: GET /api/admin/config, PUT /api/admin/config
        TOML 文件加载/保存, config 注入到各模块.
"""

import pytest
from pathlib import Path

from tests.api.conftest import create_task, bid, submit_result

from eacn.network.config import NetworkConfig, load_config, save_config

class TestGetConfig:
    @pytest.mark.asyncio
    async def test_get_config_returns_all_sections(self, client):
        resp = await client.get("/api/admin/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "reputation" in data
        assert "matcher" in data
        assert "economy" in data
        assert "push" in data
        assert "task" in data
        assert "api" in data

    @pytest.mark.asyncio
    async def test_get_config_reputation_defaults(self, client):
        data = (await client.get("/api/admin/config")).json()
        rep = data["reputation"]
        assert rep["max_gain"] == 0.1
        assert rep["max_penalty"] == -0.05
        assert rep["default_score"] == 0.5
        assert rep["cold_start_threshold"] == 10
        assert "result_selected" in rep["event_weights"]

    @pytest.mark.asyncio
    async def test_get_config_matcher_defaults(self, client):
        data = (await client.get("/api/admin/config")).json()
        m = data["matcher"]
        assert m["weight_reputation"] == 0.6
        assert m["weight_domain"] == 0.25
        assert m["weight_keyword"] == 0.15
        assert m["ability_threshold"] == 0.5
        assert m["price_tolerance"] == 0.1
        assert m["target_min_reputation"] == 0.3

    @pytest.mark.asyncio
    async def test_get_config_economy_defaults(self, client):
        data = (await client.get("/api/admin/config")).json()
        assert data["economy"]["platform_fee_rate"] == 0.05

    @pytest.mark.asyncio
    async def test_get_config_push_defaults(self, client):
        data = (await client.get("/api/admin/config")).json()
        assert data["push"]["max_retries"] == 2

    @pytest.mark.asyncio
    async def test_get_config_task_defaults(self, client):
        data = (await client.get("/api/admin/config")).json()
        assert data["task"]["default_max_concurrent_bidders"] == 5
        assert data["task"]["default_max_depth"] == 10

class TestUpdateConfig:
    @pytest.mark.asyncio
    async def test_update_reputation_config(self, client):
        resp = await client.put("/api/admin/config", json={
            "reputation": {"max_gain": 0.2},
        })
        assert resp.status_code == 200
        assert resp.json()["reputation"]["max_gain"] == 0.2

        # Verify it persisted
        data = (await client.get("/api/admin/config")).json()
        assert data["reputation"]["max_gain"] == 0.2

    @pytest.mark.asyncio
    async def test_update_matcher_config(self, client):
        resp = await client.put("/api/admin/config", json={
            "matcher": {"ability_threshold": 0.3},
        })
        assert resp.status_code == 200
        assert resp.json()["matcher"]["ability_threshold"] == 0.3

    @pytest.mark.asyncio
    async def test_update_economy_config(self, client):
        resp = await client.put("/api/admin/config", json={
            "economy": {"platform_fee_rate": 0.03},
        })
        assert resp.status_code == 200
        assert resp.json()["economy"]["platform_fee_rate"] == 0.03

    @pytest.mark.asyncio
    async def test_update_unknown_key_400(self, client):
        resp = await client.put("/api/admin/config", json={
            "nonexistent": {"foo": "bar"},
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_multiple_sections(self, client):
        resp = await client.put("/api/admin/config", json={
            "reputation": {"max_gain": 0.15},
            "matcher": {"price_tolerance": 0.2},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["reputation"]["max_gain"] == 0.15
        assert data["matcher"]["price_tolerance"] == 0.2

class TestConfigAffectsBehavior:
    @pytest.mark.asyncio
    async def test_lowered_ability_threshold_accepts_weaker_bids(self, client, funded_network):
        """Lowering ability_threshold should accept bids that were rejected before."""
        funded_network.reputation._scores["weak"] = 0.3
        await funded_network.dht.announce("coding", "weak")

        # With default threshold=0.5, weak agent (0.3 rep × 0.5 conf = 0.15) should be rejected
        await create_task(client, task_id="t1")
        b = await bid(client, task_id="t1", agent_id="weak", confidence=0.5, price=50.0)
        assert b["status"] == "rejected"

        # Lower threshold to 0.1
        await client.put("/api/admin/config", json={
            "matcher": {"ability_threshold": 0.1},
        })

        # Now same bid should pass
        await create_task(client, task_id="t2")
        b = await bid(client, task_id="t2", agent_id="weak", confidence=0.5, price=50.0)
        assert b["status"] == "executing"

    @pytest.mark.asyncio
    async def test_custom_config_on_init(self):
        """Network can be initialized with custom config."""
        from eacn.network.app import Network
        config = NetworkConfig(
            reputation={"max_gain": 0.3, "default_score": 0.7},
            matcher={"ability_threshold": 0.2},
            economy={"platform_fee_rate": 0.10},
        )
        net = Network(config=config)
        assert net.reputation.MAX_GAIN == 0.3
        assert net.reputation.DEFAULT_SCORE == 0.7
        assert net.matcher._ability_threshold == 0.2
        assert net.settlement.platform_fee_rate == 0.10

class TestTOMLConfig:
    def test_load_default_toml(self):
        """config.default.toml 应能正常加载。"""
        from eacn.network.config import _DEFAULT_TOML
        cfg = load_config(_DEFAULT_TOML)
        assert cfg.reputation.max_gain == 0.1
        assert cfg.matcher.ability_threshold == 0.5
        assert cfg.economy.platform_fee_rate == 0.05
        assert "result_selected" in cfg.reputation.event_weights

    def test_load_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.toml")

    def test_save_and_reload(self, tmp_path):
        """保存再加载应完全一致。"""
        cfg = NetworkConfig(
            reputation={"max_gain": 0.3},
            matcher={"ability_threshold": 0.2},
        )
        path = tmp_path / "test_config.toml"
        save_config(cfg, path)
        assert path.exists()

        reloaded = load_config(path)
        assert reloaded.reputation.max_gain == 0.3
        assert reloaded.matcher.ability_threshold == 0.2
        # 其余应保持默认值
        assert reloaded.economy.platform_fee_rate == 0.05

    def test_user_override_merges(self, tmp_path):
        """用户文件只覆盖指定的字段，其余保留默认值。"""
        user_toml = tmp_path / "config.toml"
        user_toml.write_text('[matcher]\nability_threshold = 0.1\n')
        cfg = load_config(user_toml)
        assert cfg.matcher.ability_threshold == 0.1
        # 未指定的保持默认
        assert cfg.matcher.weight_reputation == 0.6

    def test_toml_readable_by_humans(self, tmp_path):
        """生成的 TOML 文件应是可读的纯文本。"""
        cfg = NetworkConfig()
        path = tmp_path / "readable.toml"
        save_config(cfg, path)
        content = path.read_text()
        assert "[reputation]" in content
        assert "[matcher]" in content
        assert "platform_fee_rate" in content
        assert "[reputation.event_weights]" in content
