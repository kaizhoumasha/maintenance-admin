"""离线发布准备、完整性校验、镜像加载及运行制品核对;不提交或发布。"""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

SERVICES = {"api", "worker", "beat", "migrate"}
PAYLOAD = (
    "docker-compose.yml",
    "docker-compose.release.yml",
    "docker-compose.site.yml",
    ".env.example",
    "deploy/nginx/default.conf",
    "docs/deploy.md",
    "README.md",
    "CHANGELOG.md",
    "scripts/backup_pg.sh",
    "scripts/restore_pg.sh",
    "scripts/clear_redis_after_restore.sh",
    "scripts/release_bundle.py",
)


def run(*args: str, **kwargs) -> str:
    return subprocess.check_output(args, text=True, **kwargs).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files() -> tuple[str, ...]:
    names = run("git", "ls-files", "-co", "--exclude-standard", "-z").split("\0")
    return tuple(sorted(name for name in set(names) if name and Path(name).is_file()))


def source_hash(names: tuple[str, ...] | None = None) -> str:
    digest = hashlib.sha256()
    for name in source_files() if names is None else names:
        digest.update(name.encode() + b"\0" + sha256(Path(name)).encode() + b"\0")
    return digest.hexdigest()


def payload_files(root: Path, source_paths: set[str]) -> list[str]:
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark")
    pending, included = list(PAYLOAD), set()
    while pending:
        name = pending.pop()
        if name in included:
            continue
        if name not in source_paths:
            raise ValueError(f"交付文件未纳入源码指纹: {name}")
        included.add(name)
        if Path(name).suffix.lower() != ".md":
            continue
        for token in parser.parse(Path(name).read_text()):
            for child in token.children or ():
                if child.type != "link_open":
                    continue
                url = urlsplit(child.attrGet("href") or "")
                dependency = Path(os.path.normpath(Path(name).parent / unquote(url.path)))
                if url.scheme or url.netloc or dependency.suffix.lower() != ".md":
                    continue
                if (root / dependency).resolve().is_relative_to(root):
                    pending.append(dependency.as_posix())
    return sorted(included)


def image_info(image: str) -> dict:
    return json.loads(run("docker", "image", "inspect", image))[0]


def prepare(image: str, output: Path, allow_dirty: bool) -> None:
    if output.exists():
        raise ValueError("输出目录已存在,请保留旧制品并使用新目录")
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*", image):
        raise ValueError("镜像必须使用固定标签")
    if image.rsplit(":", 1)[1] in {"latest", "dev"}:
        raise ValueError("镜像必须使用固定标签,禁止 latest/dev")
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        raise ValueError("镜像标签已存在,禁止覆盖;请使用新标签")
    root = Path.cwd().resolve()
    if Path(run("git", "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("请在仓库根目录执行")
    dirty = bool(run("git", "status", "--porcelain", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise ValueError("工作区未提交;仅隔离验收可显式使用 --allow-dirty")
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    main = ast.parse(Path("app/main.py").read_text())
    versions = [
        ast.literal_eval(kw.value)
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
        for kw in node.keywords
        if kw.arg == "version"
    ]
    if versions != [version]:
        raise ValueError("pyproject.toml 与 FastAPI 版本不一致")
    revision = run("git", "rev-parse", "HEAD")
    names = source_files()
    fingerprint = source_hash(names)
    labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
        "release.source-sha256": fingerprint,
        "release.dirty": str(dirty).lower(),
    }
    command = ["docker", "build", "-t", image]
    for key, value in labels.items():
        command.extend(["--label", f"{key}={value}"])
    subprocess.run([*command, "."], check=True)
    if source_files() != names or source_hash(names) != fingerprint:
        raise ValueError("构建期间源码变化,不能生成发布清单;请使用新标签重试")
    info = image_info(image)
    heads = run("uv", "run", "--frozen", "alembic", "heads").splitlines()
    if len(heads) != 1:
        raise ValueError("数据库必须只有一个 Alembic head")
    config = json.loads(
        run(
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.release.yml",
            "--profile",
            "app",
            "config",
            "--format",
            "json",
            env={**os.environ, "APP_IMAGE": image},
        )
    )
    infrastructure = {}
    for service in ("postgres", "redis", "nginx"):
        ref = config["services"][service]["image"]
        if subprocess.run(["docker", "image", "inspect", ref], capture_output=True).returncode:
            subprocess.run(["docker", "pull", ref], check=True)
        details = image_info(ref)
        digests = details.get("RepoDigests") or []
        if not digests:
            raise ValueError(f"{service} 缺少仓库摘要,无法固定基础设施镜像")
        infrastructure[service] = {"reference": digests[0], "image_id": details["Id"]}
    payload = payload_files(root, set(names))
    output.mkdir(parents=True)
    for name in payload:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(name, target)
    with (output / "docker-compose.release.yml").open("a") as stream:
        for service, details in infrastructure.items():
            stream.write(f"  {service}:\n    image: {details['reference']}\n")
    (output / "image.env").write_text(f"APP_IMAGE={image}\n")
    subprocess.run(["docker", "image", "save", "-o", str(output / "image.tar"), image], check=True)
    files = {
        str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()
    }
    if source_files() != names or source_hash(names) != fingerprint:
        raise ValueError("打包期间源码变化,不能生成发布清单;请使用新标签重试")
    manifest = {
        "version": version,
        "source_revision": revision,
        "source_dirty": dirty,
        "source_sha256": fingerprint,
        "image": image,
        "image_id": info["Id"],
        "platform": f"{info['Os']}/{info['Architecture']}",
        "alembic_head": heads[0].split()[0],
        "created_at": datetime.now(UTC).isoformat(),
        "infrastructure_images": infrastructure,
        "files": files,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{digest}  {name}\n"
            for name, digest in {**files, "manifest.json": sha256(output / "manifest.json")}.items()
        )
    )
    print(f"已准备 {output}; source_dirty={dirty}; 未提交、未推送、未发布")


def verify(directory: Path) -> dict:
    sums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        path = directory / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
            raise ValueError(f"非法制品路径: {name}")
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"非法制品路径: {name}")
        if name in sums or sha256(path) != digest:
            raise ValueError(f"制品校验失败: {name}")
        sums[name] = digest
    manifest = json.loads((directory / "manifest.json").read_text())
    if sums != {**manifest["files"], "manifest.json": sha256(directory / "manifest.json")}:
        raise ValueError("发布清单与校验文件不一致")
    print("制品完整性校验通过")
    return manifest


def load(directory: Path) -> None:
    manifest = verify(directory)
    existing = subprocess.run(
        ["docker", "image", "inspect", manifest["image"]], capture_output=True, text=True
    )
    if existing.returncode == 0 and json.loads(existing.stdout)[0]["Id"] != manifest["image_id"]:
        raise ValueError("本地同名固定标签指向另一镜像,拒绝覆盖")
    subprocess.run(["docker", "image", "load", "-i", str(directory / "image.tar")], check=True)
    if image_info(manifest["image"])["Id"] != manifest["image_id"]:
        raise ValueError("加载后的镜像与清单不一致")
    print("镜像已加载并与清单一致")


def check(directory: Path, project: str) -> None:
    manifest = verify(directory)
    ids = run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}")
    if not ids:
        raise ValueError("没有找到该 Compose 项目")
    counts = dict.fromkeys(SERVICES, 0)
    for container in json.loads(run("docker", "inspect", *ids.splitlines())):
        labels = container["Config"]["Labels"]
        service = labels.get("com.docker.compose.service")
        if service not in SERVICES or labels.get("com.docker.compose.oneoff", "").lower() == "true":
            continue
        counts[service] += 1
        state = container["State"]
        if container["Image"] != manifest["image_id"]:
            raise ValueError(f"{service} 使用的不是清单中的镜像")
        if service == "migrate":
            if state["Status"] != "exited" or state["ExitCode"] != 0:
                raise ValueError("迁移服务未成功完成")
        elif not state["Running"]:
            raise ValueError(f"{service} 未运行")
    if not all(counts.values()) or counts["beat"] != 1 or counts["migrate"] != 1:
        raise ValueError(f"应用服务缺失或 beat/migrate 非单实例: {counts}")
    print("四个应用服务镜像一致,迁移成功,beat 单实例;健康与业务需另行验收")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    create = subs.add_parser("prepare")
    create.add_argument("--image", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--allow-dirty", action="store_true")
    for name in ("verify", "load", "check"):
        sub = subs.add_parser(name)
        sub.add_argument("directory", type=Path)
        if name == "check":
            sub.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.image, args.output, args.allow_dirty)
        elif args.command == "verify":
            verify(args.directory)
        elif args.command == "load":
            load(args.directory)
        else:
            check(args.directory, args.project)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
