from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("filename", ["compose.local.yml", "compose.deploy.yml"])
def test_workers_are_isolated_and_share_required_configuration(filename):
    services = yaml.safe_load((ROOT / "infra" / filename).read_text(encoding="utf-8"))["services"]
    general, index = services["ai_worker"], services["ai_index_worker"]
    for worker, queues, name in (
        (general, "--queues=ai_general", "--hostname=general@%h"),
        (index, "--queues=worklog_index", "--hostname=index@%h"),
    ):
        assert queues in worker["command"]
        assert "--concurrency=1" in worker["command"]
        assert name in worker["command"]
    assert general["image"] == index["image"]
    assert general["environment"] == index["environment"]
    assert general["volumes"] == index["volumes"]


def test_deployment_lifecycle_includes_both_workers():
    script = (ROOT / "infra/scripts/remote-deploy.sh").read_text(encoding="utf-8")
    for command in ("pull", "rm -sf", "up -d --remove-orphans"):
        line = next(line for line in script.splitlines() if line.strip().startswith(command + " "))
        assert "ai_worker" in line.split()
        assert "ai_index_worker" in line.split()
