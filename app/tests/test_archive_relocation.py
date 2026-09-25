from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src import config, data_migration
from src.core.models import ArchiveIntent, Artist, Asset, Work
from src.core.runtime import Runtime
from src.data_migration import MigrationError
from src.web import create_app


@pytest.fixture
def relocation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    runtime = Runtime(source / "data/pixiv_tracker.db", source / "images", create=True)
    with runtime.database.write() as session:
        work = Work(id=1, pixiv_illust_id="10", title="preserve", artist=Artist(id=1, pixiv_user_id="1", name="artist"))
        work.assets = [Asset(role="image", relative_path="10_p0.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
    Image.new("RGB", (4, 4), "red").save(source / "images/10_p0.png")
    env = tmp_path / "fixture.env"
    env.write_text(f"DATA_ROOT={source}\nPORT=1919\n", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_ROOT", source)
    monkeypatch.setattr(config, "DATA_DIR", source / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", source / "images")
    monkeypatch.setattr(config, "env_file", env)
    app = create_app(lambda: runtime, authenticate=False, schedule=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000)) as client:
        yield client, runtime, source, tmp_path / "target", env


def test_verified_copy_switches_runtime_and_keeps_original_files(relocation):
    client, runtime, source, target, env = relocation
    before = (source / "images/10_p0.png").read_bytes()
    response = client.post("/settings/change-path", data={"new_path": str(target)}, headers={"Origin": "http://localhost"})
    assert response.status_code == 200, response.text
    assert client.get("/api/library").json()["items"][0]["title"] == "preserve"
    assert client.get("/images/10_p0.png").content == before
    assert (source / "images/10_p0.png").read_bytes() == before
    assert (target / "images/10_p0.png").read_bytes() == before
    assert str(target) in env.read_text(encoding="utf-8")
    second = target.parent / "second"
    again = client.post("/settings/change-path", data={"new_path": str(second)}, headers={"Origin": "http://localhost"})
    assert again.status_code == 200, again.text
    assert len(list((second / "data").glob("relocation-*.json"))) == 2
    assert client.get("/images/10_p0.png").content == before
    with pytest.raises(RuntimeError, match="关闭"):
        runtime.library.query()


def test_configuration_commit_failure_keeps_old_runtime_usable(relocation, monkeypatch):
    client, runtime, source, target, env = relocation
    before = env.read_bytes()

    def fail_commit(*_):
        raise OSError("simulated disk full")

    monkeypatch.setattr(data_migration, "persist_data_root", fail_commit)
    response = client.post("/settings/change-path", data={"new_path": str(target)}, headers={"Origin": "http://localhost"})
    assert response.status_code == 500
    assert env.read_bytes() == before
    assert config.DATA_ROOT == source
    assert client.get("/api/library").status_code == 200
    assert client.get("/images/10_p0.png").status_code == 200
    assert client.post("/api/tasks", json={"kind": "verify"}, headers={"Origin": "http://localhost"}).status_code == 200


def test_same_length_corruption_is_rejected_before_activation(relocation, monkeypatch):
    client, runtime, source, target, env = relocation
    before = env.read_bytes()
    copy = data_migration.shutil.copy2

    def corrupt(source_file, target_file, *args, **kwargs):
        result = copy(source_file, target_file, *args, **kwargs)
        if str(target_file).endswith("10_p0.png"):
            content = Path(target_file).read_bytes()
            Path(target_file).write_bytes(b"x" * len(content))
        return result

    monkeypatch.setattr(data_migration.shutil, "copy2", corrupt)
    response = client.post("/settings/change-path", data={"new_path": str(target)}, headers={"Origin": "http://localhost"})
    assert response.status_code == 500
    assert env.read_bytes() == before
    assert client.get("/images/10_p0.png").status_code == 200
