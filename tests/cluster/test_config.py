"""Tests for ClusterConfig."""

from eacn.network.config import ClusterConfig


class TestClusterConfig:
    def test_default_values(self):
        cfg = ClusterConfig()
        assert cfg.seed_nodes == []
        assert cfg.heartbeat_interval == 10
        assert cfg.heartbeat_fan_out == 3
        assert cfg.suspect_rounds == 3
        assert cfg.offline_rounds == 6
        assert cfg.node_id == ""
        assert cfg.endpoint == ""
        assert cfg.protocol_version == "0.1.0"

    def test_custom_values(self):
        cfg = ClusterConfig(
            seed_nodes=["http://seed:8000"],
            heartbeat_interval=5,
            node_id="my-node",
            endpoint="http://me:8000",
        )
        assert cfg.seed_nodes == ["http://seed:8000"]
        assert cfg.heartbeat_interval == 5
        assert cfg.node_id == "my-node"

    def test_standalone_mode_no_seeds(self):
        cfg = ClusterConfig()
        assert not bool(cfg.seed_nodes)

    def test_cluster_mode_with_seeds(self):
        cfg = ClusterConfig(seed_nodes=["http://s1:8000", "http://s2:8000"])
        assert bool(cfg.seed_nodes)
        assert len(cfg.seed_nodes) == 2
