import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_bundle

SCRIPT = Path(__file__).parents[2] / "scripts/release_bundle.py"


def bundle(tmp_path):
    payload = b"image archive"
    (tmp_path / "image.tar").write_bytes(payload)
    manifest = {"files": {"image.tar": hashlib.sha256(payload).hexdigest()}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    sums = {
        **manifest["files"],
        "manifest.json": hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest(),
    }
    (tmp_path / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sums.items())
    )
    return tmp_path


def cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True
    )


def test_verify_accepts_intact_bundle(tmp_path):
    result = cli("verify", bundle(tmp_path))
    assert result.returncode == 0, result.stderr


def test_external_site_configuration_does_not_change_bundle_integrity(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    bundle(release)
    site = tmp_path / "site-nginx.conf"
    site.write_text("set_real_ip_from 192.0.2.10;\n")
    assert cli("verify", release).returncode == 0
    site.write_text("set_real_ip_from 192.0.2.11;\n")
    result = cli("verify", release)
    assert result.returncode == 0, result.stderr


def test_site_override_mounts_external_configuration_read_only(tmp_path):
    root = SCRIPT.parents[1]
    site = tmp_path / "site-nginx.conf"
    site.touch()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(root / "docker-compose.yml"),
            "-f",
            str(root / "docker-compose.release.yml"),
            "-f",
            str(root / "docker-compose.site.yml"),
            "--profile",
            "app",
            "config",
            "--format",
            "json",
        ],
        env={
            **os.environ,
            "APP_IMAGE": "maintenance-admin:0.2.0-test",
            "SITE_NGINX_CONFIG": str(site),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    volumes = json.loads(result.stdout)["services"]["nginx"]["volumes"]
    mounts = [v for v in volumes if v["target"] == "/etc/nginx/conf.d/default.conf"]
    assert len(mounts) == 1
    assert mounts[0]["source"] == str(site)
    assert mounts[0]["read_only"] is True
    assert mounts[0]["bind"]["create_host_path"] is False


def test_restore_completion_requires_selecting_release_instead_of_development_start(tmp_path):
    backup = tmp_path / "backup.dump"
    backup.touch()
    result = subprocess.run(
        ["bash", str(SCRIPT.with_name("restore_pg.sh")), str(backup)],
        env={
            **os.environ,
            "PG_EXEC": "true",
            "REDIS_EXEC": "true",
            "CONFIRM": "yes",
            "JWT_ROTATED": "yes",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "目标发布目录" in result.stdout
    assert "docker compose" not in result.stdout


def test_verify_rejects_changed_archive(tmp_path):
    bundle(tmp_path)
    (tmp_path / "image.tar").write_bytes(b"changed")
    result = cli("verify", tmp_path)
    assert result.returncode != 0
    assert "image.tar" in result.stderr


def test_verify_rejects_paths_outside_bundle(tmp_path):
    bundle(tmp_path)
    (tmp_path / "SHA256SUMS").write_text("0" * 64 + "  ../outside\n")
    result = cli("verify", tmp_path)
    assert result.returncode != 0
    assert "路径" in result.stderr


def test_prepare_rejects_reused_output_before_build(tmp_path):
    result = cli("prepare", "--image", "maintenance-admin:0.2.0-test", "--output", tmp_path)
    assert result.returncode != 0
    assert "已存在" in result.stderr


def test_prepare_rejects_mutable_development_tag(tmp_path):
    result = cli("prepare", "--image", "maintenance-admin:latest", "--output", tmp_path / "new")
    assert result.returncode != 0
    assert "固定标签" in result.stderr


def write_manifest(directory, **updates):
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest.update(updates)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    sums = {
        **manifest["files"],
        "manifest.json": release_bundle.sha256(directory / "manifest.json"),
    }
    (directory / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sums.items())
    )
    return manifest


@pytest.mark.parametrize("kind", ["absolute", "symlink", "parent-symlink"])
def test_verify_rejects_unsafe_payload_paths(tmp_path, kind):
    release = tmp_path / "release"
    release.mkdir()
    bundle(release)
    outside = tmp_path / "outside"
    outside.write_bytes(b"image archive")
    if kind == "absolute":
        name = str(outside)
    elif kind == "symlink":
        name = "image.tar"
        (release / name).unlink()
        (release / name).symlink_to(outside)
    else:
        (release / "linked").symlink_to(tmp_path, target_is_directory=True)
        name = "linked/outside"
    (release / "SHA256SUMS").write_text(f"{release_bundle.sha256(outside)}  {name}\n")
    with pytest.raises(ValueError, match="非法制品路径"):
        release_bundle.verify(release)


@pytest.mark.parametrize("kind", ["duplicate", "manifest-mismatch"])
def test_verify_rejects_inconsistent_checksums(tmp_path, kind):
    bundle(tmp_path)
    sums = tmp_path / "SHA256SUMS"
    if kind == "duplicate":
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + "\n")
    else:
        write_manifest(tmp_path, files={})
        sums.write_text(
            f"{release_bundle.sha256(tmp_path / 'image.tar')}  image.tar\n" + sums.read_text()
        )
    with pytest.raises(ValueError, match="校验失败|不一致"):
        release_bundle.verify(tmp_path)


@pytest.mark.parametrize(
    "existing, loaded, error",
    [
        ("other", "expected", "拒绝覆盖"),
        (None, "other", "加载后的镜像"),
        (None, "expected", None),
        ("expected", "expected", None),
    ],
)
def test_load_preserves_fixed_tag_and_verifies_loaded_image(
    tmp_path, monkeypatch, existing, loaded, error
):
    bundle(tmp_path)
    write_manifest(tmp_path, image="app:0.2.0", image_id="expected")
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0 if existing else 1, json.dumps([{"Id": existing}])
        )

    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: json.dumps([{"Id": loaded}]))
    if error:
        with pytest.raises(ValueError, match=error):
            release_bundle.load(tmp_path)
    else:
        release_bundle.load(tmp_path)
    assert any("load" in call for call in calls) is (existing != "other")


@pytest.mark.parametrize(
    "failure",
    [
        "project-missing",
        "service-missing",
        "wrong-image",
        "stopped",
        "migration-running",
        "migration-failed",
        "duplicate-beat",
        "duplicate-migrate",
        "ignored-oneoff",
        "healthy",
    ],
)
def test_check_requires_matching_running_services_and_successful_single_migration(
    tmp_path, monkeypatch, failure
):
    bundle(tmp_path)
    write_manifest(tmp_path, image_id="expected")
    containers = [
        {
            "Config": {"Labels": {"com.docker.compose.service": service}},
            "Image": "expected",
            "State": {
                "Status": "exited" if service == "migrate" else "running",
                "Running": service != "migrate",
                "ExitCode": 0,
            },
        }
        for service in ("api", "worker", "beat", "migrate")
    ]
    if failure == "service-missing":
        containers.pop(1)
    elif failure == "wrong-image":
        containers[0]["Image"] = "other"
    elif failure == "stopped":
        containers[1]["State"]["Running"] = False
    elif failure == "migration-running":
        containers[-1]["State"]["Status"] = "running"
    elif failure == "migration-failed":
        containers[-1]["State"]["ExitCode"] = 1
    elif failure.startswith("duplicate-"):
        containers.append(containers[2 if failure == "duplicate-beat" else 3])
    elif failure == "ignored-oneoff":
        containers.append(
            {
                "Config": {
                    "Labels": {
                        "com.docker.compose.service": "beat",
                        "com.docker.compose.oneoff": "True",
                    }
                },
                "Image": "other",
                "State": {},
            }
        )
        containers.append({"Config": {"Labels": {"com.docker.compose.service": "redis"}}})

    def output(args, **kwargs):
        assert args[:2] in (("docker", "ps"), ("docker", "inspect"))
        if args[1] == "ps":
            assert args[-1] == "label=com.docker.compose.project=isolated"
            return "" if failure == "project-missing" else "container-id"
        return json.dumps(containers)

    monkeypatch.setattr(subprocess, "check_output", output)
    if failure in {"healthy", "ignored-oneoff"}:
        release_bundle.check(tmp_path, "isolated")
    else:
        with pytest.raises(
            ValueError,
            match={
                "project-missing": "没有找到",
                "service-missing": "服务缺失",
                "wrong-image": "不是清单",
                "stopped": "未运行",
                "migration-running": "迁移服务",
                "migration-failed": "迁移服务",
                "duplicate-beat": "非单实例",
                "duplicate-migrate": "非单实例",
            }[failure],
        ):
            release_bundle.check(tmp_path, "isolated")


@pytest.mark.parametrize(
    "failure, error",
    [
        ("root", "仓库根目录"),
        ("dirty", "工作区未提交"),
        ("version", "版本不一致"),
        ("tag", "标签已存在"),
        ("source-drift", "源码变化"),
        ("heads", "一个 Alembic head"),
        ("digest", "缺少仓库摘要"),
    ],
)
def test_prepare_rejects_unsafe_source_or_unpinned_infrastructure(
    tmp_path, monkeypatch, failure, error
):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app").mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    (root / "app/main.py").write_text(
        'app = FastAPI(version="' + ("0.1.0" if failure == "version" else "0.2.0") + '")\n'
    )
    monkeypatch.chdir(root)
    builds = []
    hashes = iter(["before", "after"] if failure == "source-drift" else ["before", "before"])
    monkeypatch.setattr(release_bundle, "source_hash", lambda: next(hashes))

    def execute(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            exists = failure == "tag" or args[-1] != "app:0.2.0-test"
            return subprocess.CompletedProcess(args, 0 if exists else 1)
        assert args[:2] == ["docker", "build"]
        builds.append(args)
        return subprocess.CompletedProcess(args, 0)

    def output(args, **kwargs):
        if args == ("git", "rev-parse", "--show-toplevel"):
            return str(tmp_path if failure == "root" else root)
        if args[:2] == ("git", "status"):
            return " M app/main.py" if failure == "dirty" else ""
        if args == ("git", "rev-parse", "HEAD"):
            return "revision"
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "image-id",
                        "Os": "linux",
                        "Architecture": "arm64",
                        "RepoDigests": [] if failure == "digest" else ["image@sha256:abc"],
                    }
                ]
            )
        if args[:2] == ("uv", "run"):
            return "first (head)\nsecond (head)" if failure == "heads" else "first (head)"
        assert args[:2] == ("docker", "compose")
        return json.dumps(
            {"services": {name: {"image": name} for name in ("postgres", "redis", "nginx")}}
        )

    monkeypatch.setattr(subprocess, "run", execute)
    monkeypatch.setattr(subprocess, "check_output", output)
    destination = tmp_path / "release"
    with pytest.raises(ValueError, match=error):
        release_bundle.prepare("app:0.2.0-test", destination, allow_dirty=False)
    assert not destination.exists()
    assert bool(builds) is (failure in {"source-drift", "heads", "digest"})
