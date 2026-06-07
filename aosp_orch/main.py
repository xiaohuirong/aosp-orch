"""AOSP Block Device Build Container Orchestrator - CLI Entry Point."""

import sys
import os
import logging

import click
import yaml

from .storage import (
    _sudo_run,
    setup_loop_device,
    detach_loop_device,
    get_loop_device_for_image,
    vg_exists,
    create_pool_image,
    init_lvm_thin_pool,
    create_thin_lv,
    create_snapshot,
    lv_exists,
    remove_lv,
    activate_lv,
    format_ext4,
    mount,
    umount,
    is_mounted,
    is_lv_mounted,
    docker_build_mock,
    docker_image_exists,
    docker_run,
    docker_exec,
    docker_rm,
    docker_container_exists,
    mock_populate_base,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "aosp-orch")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")

# Hardcoded constants - not configurable to prevent accidental changes
VG_NAME = "vg0"
THIN_POOL_NAME = "pool0"
POOL_IMAGE_FILENAME = "pool.img"


# ── Path derivation helpers ──────────────────────────────────

def _pool_image_path(config: dict) -> str:
    """Derive pool image path from workdir."""
    return os.path.join(config["global"]["workdir"], POOL_IMAGE_FILENAME)


def _base_lv_name(project_name: str) -> str:
    """Generate base LV name from project name."""
    return project_name


def _base_mount_path(config: dict, project_name: str) -> str:
    """Generate base mount path from workdir + project name."""
    return os.path.join(config["global"]["workdir"], project_name, "base_mount")


def _snapshot_lv_name(workspace_name: str) -> str:
    """Generate snapshot LV name from workspace name."""
    return f"s-{workspace_name}"


def _workspace_mount_path(config: dict, project_name: str, workspace_name: str) -> str:
    """Generate workspace mount path from workdir + project + workspace."""
    return os.path.join(config["global"]["workdir"], project_name, workspace_name)


# ── Config helpers ───────────────────────────────────────────

def _resolve_config_path() -> str:
    """Resolve config path: env var > default ~/.config/aosp-orch/config.yaml."""
    env_path = os.environ.get("AOSP_ORCH_CONFIG")
    if env_path:
        return env_path
    return DEFAULT_CONFIG_PATH


def load_config(config_path: str) -> dict:
    """Load config.yaml from the given path."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def validate_config(config: dict) -> list[str]:
    """Validate config structure. Returns list of error messages (empty = valid)."""
    errors = []

    # [global] section
    g = config.get("global")
    if g is None:
        errors.append("Missing 'global' section")
    else:
        for key in ("mode", "workdir", "pool_image_size_gb"):
            if key not in g:
                errors.append(f"Missing global.{key}")
        if "mode" in g and g["mode"] not in ("mock", "prod"):
            errors.append(f"global.mode must be 'mock' or 'prod', got '{g['mode']}'")
        if "pool_image_size_gb" in g and not isinstance(g["pool_image_size_gb"], (int, float)) or g.get("pool_image_size_gb", 0) <= 0:
            errors.append("global.pool_image_size_gb must be a positive number")

    # base_projects
    bps = config.get("base_projects")
    if bps is None:
        errors.append("Missing 'base_projects'")
    elif not isinstance(bps, list):
        errors.append("base_projects must be a list")
    else:
        for i, bp in enumerate(bps):
            prefix = f"base_projects[{i}]" + (f"({bp.get('name', '?')})" if "name" in bp else "")
            for key in ("name", "repo_url", "repo_branch", "docker_image", "base_lv_size_gb"):
                if key not in bp:
                    errors.append(f"Missing {prefix}.{key}")
            if "base_lv_size_gb" in bp and (not isinstance(bp["base_lv_size_gb"], (int, float)) or bp["base_lv_size_gb"] <= 0):
                errors.append(f"{prefix}.base_lv_size_gb must be a positive number")
            # Check workspaces structure
            for j, ws in enumerate(bp.get("workspaces", [])):
                ws_prefix = f"{prefix}.workspaces[{j}]" + (f"({ws.get('name', '?')})" if "name" in ws else "")
                if "name" not in ws:
                    errors.append(f"Missing {ws_prefix}.name")

    return errors


def _resolve_base(config: dict, base: str | None) -> str:
    """Resolve base project name: --base param > global.default_base.

    Exits with error if neither is set.
    """
    if base is not None:
        return base
    default_base = config.get("global", {}).get("default_base")
    if default_base:
        return default_base
    click.echo("未指定 --base 参数，且未设置 global.default_base。请使用 --base 或先 add 一个项目并设为默认。", err=True)
    sys.exit(1)


def load_and_validate_config(config_path: str) -> dict:
    """Load config and validate. Exits with error if invalid."""
    if not os.path.exists(config_path):
        click.echo(f"配置文件不存在: {config_path}\n请先运行 'init' 或 'add' 命令创建配置。", err=True)
        sys.exit(1)
    config = load_config(config_path)
    errors = validate_config(config)
    if errors:
        click.echo(f"配置文件 {config_path} 存在错误:", err=True)
        for e in errors:
            click.echo(f"  - {e}", err=True)
        sys.exit(1)
    return config


def save_config(config: dict, config_path: str) -> None:
    """Atomically save config.yaml (write to .tmp then rename)."""
    dir_path = os.path.dirname(config_path)
    os.makedirs(dir_path, exist_ok=True)
    tmp_path = os.path.join(dir_path, ".config.yaml.tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.rename(tmp_path, config_path)


def get_base_project(config: dict, name: str) -> dict | None:
    """Find a base project by name."""
    for bp in config.get("base_projects", []):
        if bp["name"] == name:
            return bp
    return None


def get_workspace(base_project: dict, ws_name: str) -> dict | None:
    """Find a workspace by name within a base project."""
    for ws in base_project.get("workspaces", []):
        if ws["name"] == ws_name:
            return ws
    return None


def _resolve_bp(ctx, base: str | None) -> tuple[dict, dict, str]:
    """Load config, resolve base, and get base project. Returns (config, bp, base).

    Exits with error if config invalid or base project not found.
    """
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)
    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)
    return config, bp, base


def _mount_lv(lv_name: str, mount_path: str) -> None:
    """Activate LV, mount it, and fix ownership."""
    activate_lv(VG_NAME, lv_name)
    os.makedirs(mount_path, exist_ok=True)
    if not is_lv_mounted(VG_NAME, lv_name):
        mount(f"/dev/{VG_NAME}/{lv_name}", mount_path)
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", mount_path])


def _start_container(c_name: str, mount_path: str, project_name: str, docker_image: str) -> None:
    """Remove existing container if any, then start a new one."""
    if docker_container_exists(c_name):
        docker_rm(c_name)
    docker_run(c_name, mount_path, f"/{project_name}", docker_image, uid=os.getuid(), gid=os.getgid())


def _is_workspace_active(base_project_name: str, ws_name: str) -> bool:
    """Determine workspace active status from Docker (source of truth)."""
    return docker_container_exists(container_name(base_project_name, ws_name))


def container_name(base_project_name: str, ws_name: str) -> str:
    """Generate Docker container name."""
    return f"aosp_{base_project_name}_{ws_name}"


# ── LVM / Pool helpers ───────────────────────────────────────

def _ensure_pool_and_vg(config: dict) -> None:
    """Create pool image, loop device, VG, and thin pool if they don't exist."""
    pool_image_path = _pool_image_path(config)
    pool_size_gb = config["global"]["pool_image_size_gb"]

    if vg_exists(VG_NAME):
        return

    # Create pool image
    if not os.path.exists(pool_image_path):
        os.makedirs(os.path.dirname(pool_image_path), exist_ok=True)
        create_pool_image(pool_image_path, pool_size_gb)

    # Setup loop device
    loop_dev = get_loop_device_for_image(pool_image_path)
    if not loop_dev:
        loop_dev = setup_loop_device(pool_image_path)

    # Initialize LVM thin pool
    init_lvm_thin_pool(loop_dev, VG_NAME, THIN_POOL_NAME)


def _run_sync(c_name: str, bp: dict) -> None:
    """Run sync commands inside a container based on project's sync_type."""
    sync_type = bp.get("sync_type", "repo")
    project_dir = bp["name"]
    repo_url = bp["repo_url"]
    repo_branch = bp["repo_branch"]

    if sync_type == "git":
        git_repo_dir = f"/{project_dir}/git-repo"
        check = docker_exec(c_name, f"test -d {git_repo_dir}/.git && echo EXISTS || echo MISSING")
        if "EXISTS" in check.stdout:
            result = docker_exec(c_name, f"cd {git_repo_dir} && git pull", check=False)
            if result.returncode != 0:
                click.echo(f"git pull 失败: {result.stderr}")
                if click.confirm("是否清除目录并重新 git clone?", default=True):
                    docker_exec(c_name, f"rm -rf {git_repo_dir}")
                    docker_exec(c_name, f"git clone -b {repo_branch} {repo_url} {git_repo_dir}")
                else:
                    click.echo("跳过同步，继续后续操作。")
        else:
            docker_exec(c_name, f"git clone -b {repo_branch} {repo_url} {git_repo_dir}")
    else:
        docker_exec(c_name, f"cd /{project_dir} && repo init -u {repo_url} -b {repo_branch}")
        docker_exec(c_name, f"cd /{project_dir} && repo sync")


def _populate_base(config: dict, bp: dict, force: bool = False) -> None:
    """Populate base LV with content (mount, fill, unmount).

    Base LV must already exist and be formatted. This function mounts it,
    populates it based on mode, writes the marker, and unmounts it.

    If force=True, removes the marker first to force re-population (used by sync).
    """
    mode = config["global"]["mode"]
    project_name = bp["name"]
    base_lv_name = _base_lv_name(project_name)
    base_mount_path = _base_mount_path(config, project_name)
    docker_image = bp["docker_image"]

    # Mount base LV
    activate_lv(VG_NAME, base_lv_name)
    os.makedirs(base_mount_path, exist_ok=True)
    if not is_lv_mounted(VG_NAME, base_lv_name):
        mount(f"/dev/{VG_NAME}/{base_lv_name}", base_mount_path)
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", base_mount_path])

    # Remove marker if forcing re-population
    marker = os.path.join(base_mount_path, ".aosp_base_initialized")
    if force and os.path.exists(marker):
        os.remove(marker)

    # Populate
    if mode == "mock":
        if not docker_image_exists(docker_image):
            docker_build_mock(docker_image)
        mock_populate_base(base_mount_path)
    else:
        build_config = bp.get("build_config", {})
        c_name = container_name(project_name, "default")
        if docker_container_exists(c_name):
            docker_rm(c_name)
        docker_run(c_name, base_mount_path, f"/{project_name}", docker_image)
        _run_sync(c_name, bp)
        for cmd in build_config.get("setup_commands", []):
            docker_exec(c_name, f"cd /{project_name} && {cmd}")
        compile_cmd = build_config.get("compile_command", "")
        env_vars = build_config.get("env_vars", {})
        env_str = " ".join(f"{k}={v}" for k, v in env_vars.items())
        if compile_cmd:
            docker_exec(c_name, f"cd /{project_name} && {env_str} {compile_cmd}")
        docker_rm(c_name)

    # Write marker
    with open(marker, "w") as f:
        f.write("initialized")

    # Unmount base LV (snapshots must be taken from unmounted base for consistency)
    if is_lv_mounted(VG_NAME, base_lv_name):
        umount(base_mount_path)


# ── Internal helpers for workspace cleanup ──────────────────

def _destroy_workspace(config: dict, base: str, ws_name: str) -> None:
    """Destroy a single workspace: stop container, unmount, remove snapshot LV.

    Does NOT remove config entry.
    """
    c_name = container_name(base, ws_name)
    if docker_container_exists(c_name):
        docker_rm(c_name)
    ws_mount_path = _workspace_mount_path(config, base, ws_name)
    if is_mounted(ws_mount_path):
        umount(ws_mount_path)
    snapshot_lv_name = _snapshot_lv_name(ws_name)
    if lv_exists(VG_NAME, snapshot_lv_name):
        remove_lv(VG_NAME, snapshot_lv_name)


def _destroy_all_workspaces(config: dict, bp: dict) -> None:
    """Destroy all workspace containers, mounts, and snapshot LVs.

    Used by sync and remove. Does NOT remove config entries.
    """
    base = bp["name"]
    for ws in list(bp.get("workspaces", [])):
        _destroy_workspace(config, base, ws["name"])


def _destroy_lvm_infrastructure(config: dict) -> None:
    """Destroy all LVM infrastructure: containers, mounts, LVs, VG, loop device.

    Used by init when user chooses to overwrite existing pool image.
    """
    # Destroy all workspaces for all base projects
    for bp in config.get("base_projects", []):
        _destroy_all_workspaces(config, bp)
        # Also destroy base LV and default container
        base = bp["name"]
        base_lv_name = _base_lv_name(base)
        c_name = container_name(base, "default")
        if docker_container_exists(c_name):
            docker_rm(c_name)
        base_mount_path = _base_mount_path(config, base)
        if is_mounted(base_mount_path):
            umount(base_mount_path)
        if lv_exists(VG_NAME, base_lv_name):
            remove_lv(VG_NAME, base_lv_name)

    # Destroy VG and thin pool
    if vg_exists(VG_NAME):
        # Deactivate all LVs first
        _sudo_run(["lvchange", "-an", f"/dev/{VG_NAME}"], check=False)
        _sudo_run(["vgremove", "-ff", "-y", VG_NAME], check=False)

    # Detach loop device
    pool_image_path = _pool_image_path(config)
    loop_dev = get_loop_device_for_image(pool_image_path)
    if loop_dev:
        detach_loop_device(loop_dev)


# ── CLI Commands ──────────────────────────────────────────────

@click.group()
@click.option("--config", "config_path", default=None,
              help=f"配置文件路径 (默认: {DEFAULT_CONFIG_PATH})")
@click.pass_context
def cli(ctx, config_path):
    """AOSP Block Device Build Container Orchestrator"""
    ctx.ensure_object(dict)
    if config_path:
        resolved = os.path.abspath(config_path)
    else:
        resolved = _resolve_config_path()
    ctx.obj["config_path"] = resolved


@cli.command()
@click.option("--mode", type=click.Choice(["mock", "prod"]), default=None,
              help="运行模式: mock (测试) | prod (生产)")
@click.option("--workdir", default=None, help="工作目录 (所有数据存放根)")
@click.option("--pool-image-size-gb", type=int, default=None, help="存储池大小 (GB)")
@click.pass_context
def init(ctx, mode, workdir, pool_image_size_gb):
    """交互式或参数化初始化配置文件中的 global 配置。"""
    config_path = ctx.obj["config_path"]

    # Load existing config or create new one
    if os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = {}

    g = config.setdefault("global", {})

    # Current defaults
    defaults = {
        "mode": g.get("mode", "mock"),
        "workdir": g.get("workdir", os.path.join(os.path.expanduser("~"), "aosp-workspace")),
        "pool_image_size_gb": g.get("pool_image_size_gb", 10 if mode == "prod" else 2),
    }

    # If all options provided via CLI args, use them directly (non-interactive)
    all_provided = all(v is not None for v in [mode, workdir, pool_image_size_gb])

    if all_provided:
        g["mode"] = mode
        g["workdir"] = os.path.abspath(workdir)
        g["pool_image_size_gb"] = pool_image_size_gb
    else:
        # Interactive mode
        click.echo("=== 初始化 AOSP 编排器全局配置 ===")
        click.echo(f"配置文件: {config_path}")
        click.echo("（括号内为当前值/默认值，直接回车保留）\n")

        g["mode"] = click.prompt("运行模式 (mock/prod)", default=defaults["mode"])

        effective_mode = g["mode"]
        size_default = 400 if effective_mode == "prod" else 2

        g["workdir"] = os.path.abspath(click.prompt("工作目录", default=defaults["workdir"]))

        pool_size_default = pool_image_size_gb if pool_image_size_gb is not None else size_default
        g["pool_image_size_gb"] = click.prompt("存储池大小 (GB)", type=int, default=pool_size_default)

    # Ensure base_projects exists
    config.setdefault("base_projects", [])

    save_config(config, config_path)

    # Check if pool image already exists before creating LVM disk infrastructure
    pool_image_path = _pool_image_path(config)
    if os.path.exists(pool_image_path):
        if not click.confirm(f"\nPool image 已存在: {pool_image_path}\n是否覆盖?（覆盖将销毁所有现有数据）", default=False):
            click.echo("保留现有 pool image，跳过磁盘创建。")
        else:
            # Destroy existing VG/LV/mounts before removing pool image
            _destroy_lvm_infrastructure(config)
            _sudo_run(["rm", "-f", pool_image_path])
            _ensure_pool_and_vg(config)
            click.echo("Pool image 已覆盖并重新创建。")
    else:
        # Create LVM disk infrastructure: pool image, loop device, VG, thin pool
        _ensure_pool_and_vg(config)

    click.echo(f"\n配置已保存到 {config_path}")
    click.echo("  mode            = %s" % g["mode"])
    click.echo("  workdir         = %s" % g["workdir"])
    click.echo("  pool_image      = %s" % _pool_image_path(config))
    click.echo("  pool_image_size = %d GB" % g["pool_image_size_gb"])
    click.echo(f"  lvm_vg_name     = {VG_NAME} (硬编码)")
    click.echo(f"  thin_pool_name  = {THIN_POOL_NAME} (硬编码)")
    default_base = g.get("default_base")
    if default_base:
        click.echo(f"  default_base    = {default_base}")


@cli.command("add")
@click.option("--name", default=None, help="Base project 名称")
@click.option("--repo-url", default=None, help="远程清单仓库地址")
@click.option("--repo-branch", default=None, help="清单仓库分支")
@click.option("--docker-image", default=None, help="Docker 镜像名称")
@click.option("--base-lv-size-gb", type=int, default=None, help="基底卷大小 (GB)")
@click.option("--sync-type", type=click.Choice(["repo", "git"]), default=None,
              help="代码同步方式: repo (默认) | git")
@click.pass_context
def add_cmd(ctx, name, repo_url, repo_branch, docker_image, base_lv_size_gb, sync_type):
    """配置 base project 并创建 base LV（格式化 + 填充内容）。"""
    config_path = ctx.obj["config_path"]

    # Load or create config
    if os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = {
            "global": {
                "mode": "mock",
                "workdir": os.path.join(os.path.expanduser("~"), "aosp-workspace"),
                "pool_image_size_gb": 2,
            },
            "base_projects": [],
        }

    g = config["global"]
    mode = g["mode"]

    # Check if all add-specific options are provided
    all_provided = all(v is not None for v in [name, repo_url, repo_branch, docker_image, base_lv_size_gb, sync_type])

    if all_provided:
        bp_name = name
    else:
        click.echo("=== 配置 Base Project ===")
        click.echo("（括号内为当前值/默认值，直接回车保留）\n")

        existing_names = [bp["name"] for bp in config.get("base_projects", [])]
        if existing_names:
            click.echo(f"已有项目: {', '.join(existing_names)}")

        bp_name = click.prompt("项目名称", default=name or "aosp")

    # Find or create the base project
    bp = get_base_project(config, bp_name)
    if bp is None:
        bp = {
            "name": bp_name,
            "repo_url": repo_url or "https://android.googlesource.com/platform/manifest",
            "repo_branch": repo_branch or "main",
            "sync_type": sync_type or "repo",
            "docker_image": docker_image or ("aosp-builder:mock" if mode == "mock" else "aosp-builder:latest"),
            "base_lv_size_gb": base_lv_size_gb or (1 if mode == "mock" else 100),
            "build_config": {
                "setup_commands": ["source build/envsetup.sh", "lunch aosp_x86_64-eng"],
                "compile_command": "m -j$(nproc)",
                "env_vars": {"USE_CCACHE": "1"},
            },
            "workspaces": [],
        }
        config.setdefault("base_projects", []).append(bp)
    else:
        if repo_url is not None:
            bp["repo_url"] = repo_url
        if repo_branch is not None:
            bp["repo_branch"] = repo_branch
        if sync_type is not None:
            bp["sync_type"] = sync_type
        if docker_image is not None:
            bp["docker_image"] = docker_image
        if base_lv_size_gb is not None:
            bp["base_lv_size_gb"] = base_lv_size_gb

    if not all_provided:
        bp["repo_url"] = click.prompt("清单仓库地址", default=bp["repo_url"])
        bp["repo_branch"] = click.prompt("清单分支", default=bp["repo_branch"])
        bp.setdefault("sync_type", "repo")
        bp["sync_type"] = click.prompt("同步方式 (repo/git)", default=bp["sync_type"])
        bp["docker_image"] = click.prompt("Docker 镜像", default=bp["docker_image"])
        bp["base_lv_size_gb"] = click.prompt("基底卷大小 (GB)", type=int, default=bp["base_lv_size_gb"])

    # Ask if this should be the default base project
    current_default = g.get("default_base")
    if current_default == bp_name:
        is_default = True
    elif all_provided:
        is_default = False
    else:
        is_default = click.confirm(f"是否将 '{bp_name}' 设为默认项目?", default=True)

    if is_default:
        g["default_base"] = bp_name

    # Save config
    save_config(config, config_path)

    # Create LVM infrastructure: pool, VG, base LV, format, populate
    base_lv_name = _base_lv_name(bp_name)
    _ensure_pool_and_vg(config)

    if not lv_exists(VG_NAME, base_lv_name):
        create_thin_lv(VG_NAME, THIN_POOL_NAME, base_lv_name, bp["base_lv_size_gb"])
        format_ext4(f"/dev/{VG_NAME}/{base_lv_name}")
        _populate_base(config, bp)
    else:
        # Base LV already exists — check if populated
        base_mount_path = _base_mount_path(config, bp_name)
        activate_lv(VG_NAME, base_lv_name)
        os.makedirs(base_mount_path, exist_ok=True)
        mount(f"/dev/{VG_NAME}/{base_lv_name}", base_mount_path)
        marker = os.path.join(base_mount_path, ".aosp_base_initialized")
        needs_populate = not os.path.exists(marker)
        umount(base_mount_path)
        if needs_populate:
            _populate_base(config, bp)

    click.echo(f"Base project '{bp_name}' linked and initialized.")
    click.echo(f"  base_lv_name    = {_base_lv_name(bp_name)} (自动生成)")
    click.echo(f"  base_mount_path = {_base_mount_path(config, bp_name)} (自动生成)")
    if is_default:
        click.echo(f"  default_base    = {bp_name}")


@cli.command("new")
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def new_cmd(ctx, workspace_name, base):
    """创建工作区：写配置 + 创建快照 LV。幂等：workspace 已存在但快照不存在时仅创建快照。"""
    config, bp, base = _resolve_bp(ctx, base)

    ws_exists = get_workspace(bp, workspace_name) is not None

    # Check base LV exists
    base_lv_name = _base_lv_name(base)
    if not lv_exists(VG_NAME, base_lv_name):
        click.echo(f"Base LV '{base}' 不存在，正在创建...")
        _ensure_pool_and_vg(config)
        create_thin_lv(VG_NAME, THIN_POOL_NAME, base_lv_name, bp["base_lv_size_gb"])
        format_ext4(f"/dev/{VG_NAME}/{base_lv_name}")
        _populate_base(config, bp)

    # Create snapshot LV if not exists
    snapshot_lv_name = _snapshot_lv_name(workspace_name)
    if not lv_exists(VG_NAME, snapshot_lv_name):
        create_snapshot(VG_NAME, base_lv_name, snapshot_lv_name)

    # Write config if workspace doesn't exist yet
    if not ws_exists:
        new_ws = {"name": workspace_name}
        bp.setdefault("workspaces", []).append(new_ws)
        save_config(config, ctx.obj["config_path"])
    mount_path = _workspace_mount_path(config, base, workspace_name)
    click.echo(f"Workspace '{workspace_name}' created.")
    click.echo(f"  snapshot_lv_name = {_snapshot_lv_name(workspace_name)} (自动生成)")
    click.echo(f"  mount_path        = {mount_path} (自动生成)")


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def mount_cmd(ctx, workspace_name, base):
    """挂载工作区快照。不指定 workspace 时挂载 base LV。前提：LV 已存在。"""
    config, bp, base = _resolve_bp(ctx, base)

    project_name = bp["name"]

    if workspace_name is None:
        # Mount base LV directly
        lv_name = _base_lv_name(project_name)
        if not lv_exists(VG_NAME, lv_name):
            click.echo(f"Base LV '{base}' 不存在，请先运行 'add --name {base}'。", err=True)
            sys.exit(1)
        mount_path = _base_mount_path(config, project_name)
        _mount_lv(lv_name, mount_path)
        click.echo(f"Base LV '{base}' mounted at {mount_path}.")
        return

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'，请先运行 'new {workspace_name} --base {base}'。", err=True)
        sys.exit(1)

    lv_name = _snapshot_lv_name(workspace_name)
    if not lv_exists(VG_NAME, lv_name):
        click.echo(f"Snapshot LV '{lv_name}' 不存在，请先运行 'new {workspace_name} --base {base}'。", err=True)
        sys.exit(1)

    mount_path = _workspace_mount_path(config, project_name, workspace_name)
    _mount_lv(lv_name, mount_path)
    click.echo(f"Workspace '{workspace_name}' mounted at {mount_path}.")


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def unmount_cmd(ctx, workspace_name, base):
    """卸载工作区快照。不指定 workspace 时卸载 base LV。"""
    config, bp, base = _resolve_bp(ctx, base)

    project_name = bp["name"]

    if workspace_name is None:
        base_lv_name = _base_lv_name(project_name)
        base_mount_path = _base_mount_path(config, project_name)
        if not is_mounted(base_mount_path) and not is_lv_mounted(VG_NAME, base_lv_name):
            click.echo(f"Base LV '{base}' is already unmounted.")
            return
        if is_mounted(base_mount_path):
            umount(base_mount_path)
        click.echo(f"Base LV '{base}' unmounted.")
        return

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'", err=True)
        sys.exit(1)

    mount_path = _workspace_mount_path(config, bp["name"], workspace_name)
    snapshot_lv_name = _snapshot_lv_name(workspace_name)

    if not is_mounted(mount_path) and not is_lv_mounted(VG_NAME, snapshot_lv_name):
        click.echo(f"Workspace '{workspace_name}' is already unmounted.")
        return

    if is_mounted(mount_path):
        umount(mount_path)

    click.echo(f"Workspace '{workspace_name}' unmounted.")


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def activate(ctx, workspace_name, base):
    """激活工作区（mount + 启动容器）。不指定 workspace 时激活 base LV。

    前提：workspace 已 new，base LV 已 add。
    """
    config, bp, base = _resolve_bp(ctx, base)

    project_name = bp["name"]
    docker_image = bp["docker_image"]

    if workspace_name is None:
        # Activate base LV: mount + start default container
        ctx.invoke(mount_cmd, workspace_name=None, base=base)
        mount_path = _base_mount_path(config, project_name)
        _start_container(container_name(project_name, "default"), mount_path, project_name, docker_image)
        click.echo(f"Base LV '{base}' activated.")
        return

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'，请先运行 'new {workspace_name} --base {base}'。", err=True)
        sys.exit(1)

    if _is_workspace_active(base, workspace_name):
        click.echo(f"Workspace '{workspace_name}' is already active.")
        return

    # Mount
    ctx.invoke(mount_cmd, workspace_name=workspace_name, base=base)

    # Start container
    mount_path = _workspace_mount_path(config, project_name, workspace_name)
    _start_container(container_name(project_name, workspace_name), mount_path, project_name, docker_image)

    click.echo(f"Workspace '{workspace_name}' activated.")


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def enter(ctx, workspace_name, base):
    """进入工作区容器。不指定 workspace 时进入 base LV 的 default 容器。

    前提：workspace 已 activate。
    """
    config, bp, base = _resolve_bp(ctx, base)

    project_name = bp["name"]

    if workspace_name is None:
        c_name = container_name(project_name, "default")
        if not docker_container_exists(c_name):
            if click.confirm(f"Base LV '{base}' 未激活，是否立即激活?", default=True):
                ctx.invoke(activate, workspace_name=None, base=base)
            else:
                sys.exit(1)
        os.execvp("docker", ["docker", "exec", "-it", container_name(project_name, "default"), "/bin/sh"])
        return

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        if click.confirm(f"Workspace '{workspace_name}' 不存在，是否立即创建?", default=True):
            ctx.invoke(new_cmd, workspace_name=workspace_name, base=base)
        else:
            sys.exit(1)

    if not _is_workspace_active(base, workspace_name):
        if click.confirm(f"Workspace '{workspace_name}' 未激活，是否立即激活?", default=True):
            ctx.invoke(activate, workspace_name=workspace_name, base=base)
        else:
            sys.exit(1)

    c_name = container_name(bp["name"], workspace_name)
    os.execvp("docker", ["docker", "exec", "-it", c_name, "/bin/sh"])


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def deactivate(ctx, workspace_name, base):
    """去激活工作区（停容器 + 卸载）。不指定 workspace 时去激活 base LV。"""
    config, bp, base = _resolve_bp(ctx, base)

    project_name = bp["name"]

    if workspace_name is None:
        c_name = container_name(project_name, "default")
        if docker_container_exists(c_name):
            docker_rm(c_name)
        ctx.invoke(unmount_cmd, workspace_name=None, base=base)
        click.echo(f"Base LV '{base}' deactivated.")
        return

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'", err=True)
        sys.exit(1)

    if not _is_workspace_active(base, workspace_name):
        click.echo(f"Workspace '{workspace_name}' is already inactive.")
        return

    # 1. Stop and remove container
    c_name = container_name(bp["name"], workspace_name)
    if docker_container_exists(c_name):
        docker_rm(c_name)

    # 2. Unmount
    ctx.invoke(unmount_cmd, workspace_name=workspace_name, base=base)

    click.echo(f"Workspace '{workspace_name}' deactivated.")


@cli.command("del")
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def del_cmd(ctx, workspace_name, base):
    """彻底销毁工作区（deactivate + 销毁快照 + 删配置）。"""
    config, bp, base = _resolve_bp(ctx, base)

    ws = None
    ws_idx = None
    for wi, w in enumerate(bp.get("workspaces", [])):
        if w["name"] == workspace_name:
            ws = w
            ws_idx = wi
            break

    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'", err=True)
        sys.exit(1)

    # 1. Deactivate if active
    if _is_workspace_active(base, workspace_name):
        ctx.invoke(deactivate, workspace_name=workspace_name, base=base)
    elif is_mounted(_workspace_mount_path(config, bp["name"], workspace_name)):
        ctx.invoke(unmount_cmd, workspace_name=workspace_name, base=base)

    # 2. Destroy snapshot LV
    snapshot_lv_name = _snapshot_lv_name(workspace_name)
    if lv_exists(VG_NAME, snapshot_lv_name):
        remove_lv(VG_NAME, snapshot_lv_name)

    # 3. Remove from config
    bp["workspaces"].pop(ws_idx)
    save_config(config, ctx.obj["config_path"])
    click.echo(f"Workspace '{workspace_name}' removed.")


@cli.command()
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def sync(ctx, base):
    """基底强制更新（清盘流）：销毁所有工作区快照 + 重新填充 base LV。"""
    config, bp, base = _resolve_bp(ctx, base)

    base_lv_name = _base_lv_name(bp["name"])

    # 1. Destroy all workspace snapshots and containers (keep config entries)
    _destroy_all_workspaces(config, bp)

    # 2. Check base LV exists
    if not lv_exists(VG_NAME, base_lv_name):
        click.echo(f"Base LV '{bp['name']}' 不存在，请先运行 'add --name {bp['name']}'。", err=True)
        sys.exit(1)

    # 3. Force re-populate base LV
    _populate_base(config, bp, force=True)

    click.echo(f"Base project '{bp['name']}' synced successfully.")


@cli.command("remove")
@click.argument("base_name", required=False)
@click.pass_context
def remove_cmd(ctx, base_name):
    """删除 base project（销毁所有工作区 + 销毁 base LV + 删除配置条目）。需二次确认。"""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = base_name or _resolve_base(config, None)
    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    if not click.confirm(f"将删除 base project '{base}' 及其所有 workspace，确认?", default=False):
        click.echo("已取消。")
        return

    # 1. Destroy all workspace snapshots and containers
    _destroy_all_workspaces(config, bp)

    # 2. Destroy base LV
    base_lv_name = _base_lv_name(base)
    base_mount_path = _base_mount_path(config, base)
    if is_mounted(base_mount_path):
        umount(base_mount_path)
    if lv_exists(VG_NAME, base_lv_name):
        remove_lv(VG_NAME, base_lv_name)

    # 3. Remove from config
    config["base_projects"] = [bp for bp in config["base_projects"] if bp["name"] != base]

    # 4. Clear default_base if it was this project
    if config["global"].get("default_base") == base:
        del config["global"]["default_base"]

    save_config(config, config_path)
    click.echo(f"Base project '{base}' removed.")


@cli.command("default")
@click.argument("base_name", required=False)
@click.pass_context
def default_cmd(ctx, base_name):
    """查看/设置/清空 default base project。

    无参数：清空 default_base。
    指定项目名：设为 default_base。
    """
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)

    if base_name is None:
        # Clear default_base
        current = config.get("global", {}).get("default_base")
        if not current:
            click.echo("当前没有设置 default base。")
            return
        del config["global"]["default_base"]
        save_config(config, config_path)
        click.echo(f"已清空 default base（原为 '{current}'）。")
        return

    # Set default_base
    bp = get_base_project(config, base_name)
    if bp is None:
        click.echo(f"Base project '{base_name}' not found in config，请先运行 'add --name {base_name}'。", err=True)
        sys.exit(1)

    config["global"]["default_base"] = base_name
    save_config(config, config_path)
    click.echo(f"已将 '{base_name}' 设为 default base。")


@cli.command()
@click.argument("workspace_name", required=False)
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def rebase(ctx, workspace_name, base):
    """删除 workspace 快照（停容器 + 卸载 + 删快照），保留 base LV 和配置条目。

    不指定 workspace 时删除该 base 下所有 workspace。需二次确认。
    """
    config, bp, base = _resolve_bp(ctx, base)

    if workspace_name is None:
        # All workspaces
        ws_list = list(bp.get("workspaces", []))
        if not ws_list:
            click.echo("没有 workspace 需要删除。")
            return
        ws_names = [ws["name"] for ws in ws_list]
        if not click.confirm(f"将删除所有 workspace: {', '.join(ws_names)}，确认?", default=False):
            click.echo("已取消。")
            return
        _destroy_all_workspaces(config, bp)
        click.echo(f"已删除 workspace: {', '.join(ws_names)}")
    else:
        ws = get_workspace(bp, workspace_name)
        if ws is None:
            click.echo(f"Workspace '{workspace_name}' not found in '{bp['name']}'", err=True)
            sys.exit(1)
        if not click.confirm(f"将删除 workspace '{workspace_name}'，确认?", default=False):
            click.echo("已取消。")
            return
        _destroy_workspace(config, bp["name"], workspace_name)
        click.echo(f"Workspace '{workspace_name}' 已删除。")

    click.echo(f"Base project '{bp['name']}' rebase 完成。下次 new 可重建快照。")


@cli.command()
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def compile_cmd(ctx, base):
    """Alias for sync - re-compile the base."""
    ctx.invoke(sync, base=base)


if __name__ == "__main__":
    cli()
