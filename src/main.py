"""AOSP Block Device Build Container Orchestrator - CLI Entry Point."""

import sys
import os
import logging

import click
import yaml

# Ensure src is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from storage import (
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
    deactivate_lv,
    get_lv_data_percent,
    get_lv_size_info,
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
    mock_compile,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "aosp-orch")
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.yaml")

# Hardcoded constants - not configurable to prevent accidental changes
VG_NAME = "vgaosp_pool"
THIN_POOL_NAME = "aosp_thin_pool"
POOL_IMAGE_FILENAME = "aosp_pool.img"


# ── Path derivation helpers ──────────────────────────────────

def _pool_image_path(config: dict) -> str:
    """Derive pool image path from workdir."""
    return os.path.join(config["global"]["workdir"], POOL_IMAGE_FILENAME)


def _base_lv_name(project_name: str) -> str:
    """Generate base LV name from project name."""
    return f"{project_name}_base_lv"


def _base_mount_path(config: dict, project_name: str) -> str:
    """Generate base mount path from workdir + project name."""
    return os.path.join(config["global"]["workdir"], project_name, "base_mount")


def _snapshot_lv_name(workspace_name: str) -> str:
    """Generate snapshot LV name from workspace name."""
    return f"{workspace_name}_snapshot_lv"


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
    click.echo("未指定 --base 参数，且未设置 global.default_base。请使用 --base 或先 link 一个项目并设为默认。", err=True)
    sys.exit(1)


def load_and_validate_config(config_path: str) -> dict:
    """Load config and validate. Exits with error if invalid."""
    if not os.path.exists(config_path):
        click.echo(f"配置文件不存在: {config_path}\n请先运行 'init' 或 'link' 命令创建配置。", err=True)
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


def _is_workspace_active(base_project_name: str, ws_name: str) -> bool:
    """Determine workspace active status from Docker (source of truth)."""
    return docker_container_exists(container_name(base_project_name, ws_name))


def container_name(base_project_name: str, ws_name: str) -> str:
    """Generate Docker container name."""
    return f"aosp_{base_project_name}_{ws_name}"


# ── LVM / Pool helpers ───────────────────────────────────────

def ensure_pool_and_vg(config: dict) -> str | None:
    """Ensure the LVM pool and VG exist. Returns loop device path or None if already set up."""
    pool_image_path = _pool_image_path(config)
    pool_size_gb = config["global"]["pool_image_size_gb"]

    if vg_exists(VG_NAME):
        logger.info("VG '%s' already exists", VG_NAME)
        return None

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
    return loop_dev


def _run_sync(c_name: str, bp: dict) -> None:
    """Run sync commands inside a container based on project's sync_type.

    sync_type: "repo" (default) or "git"
    - repo: repo init -u <url> -b <branch> && repo sync
    - git:
      - if .git exists: git pull
      - if git pull fails: ask user to clear and re-clone or skip
      - if .git not exists: git clone -b <branch> <url> <project_name>
    """
    sync_type = bp.get("sync_type", "repo")
    project_dir = bp["name"]
    repo_url = bp["repo_url"]
    repo_branch = bp["repo_branch"]

    if sync_type == "git":
        # Git repo lives in /{project_dir}/git-repo, mount point is /{project_dir}
        git_repo_dir = f"/{project_dir}/git-repo"
        check = docker_exec(c_name, f"test -d {git_repo_dir}/.git && echo EXISTS || echo MISSING")
        if "EXISTS" in check.stdout:
            # Try git pull first
            result = docker_exec(c_name, f"cd {git_repo_dir} && git pull", check=False)
            if result.returncode != 0:
                click.echo(f"git pull 失败: {result.stderr}")
                if click.confirm("是否清除目录并重新 git clone?", default=True):
                    docker_exec(c_name, f"rm -rf {git_repo_dir}")
                    docker_exec(c_name, f"git clone -b {repo_branch} {repo_url} {git_repo_dir}")
                else:
                    click.echo("跳过同步，继续后续操作。")
        else:
            # Fresh clone
            docker_exec(c_name, f"git clone -b {repo_branch} {repo_url} {git_repo_dir}")
    else:
        # Default: repo
        docker_exec(c_name, f"cd /{project_dir} && repo init -u {repo_url} -b {repo_branch}")
        docker_exec(c_name, f"cd /{project_dir} && repo sync")


def _ensure_base_lv(config: dict, bp: dict) -> None:
    """Lazy-init: ensure pool, VG, base LV exist, are formatted, and populated.

    This is the core lazy-load logic called by activate/sync/compile when they
    discover the base infrastructure is missing.

    Optimization: only mounts the base LV when first-time population is needed.
    If the base LV already exists and is initialized, we skip the mount/unmount
    cycle entirely — snapshots are taken from the unmounted base for consistency.
    """
    mode = config["global"]["mode"]
    project_name = bp["name"]
    base_lv_name = _base_lv_name(project_name)
    base_mount_path = _base_mount_path(config, project_name)
    docker_image = bp["docker_image"]

    # 1. Ensure pool and VG
    ensure_pool_and_vg(config)

    # 2. Create base LV if not exists
    if not lv_exists(VG_NAME, base_lv_name):
        create_thin_lv(VG_NAME, THIN_POOL_NAME, base_lv_name, bp["base_lv_size_gb"])
        format_ext4(f"/dev/{VG_NAME}/{base_lv_name}")

    # 3. Check if population is needed (without mounting if possible)
    #    If base LV exists and is not mounted, we need to mount briefly to check marker.
    #    If base LV is already mounted (e.g. from a previous interrupted run), check directly.
    needs_populate = False
    was_already_mounted = is_lv_mounted(VG_NAME, base_lv_name)

    if was_already_mounted:
        # Already mounted — check marker directly
        marker = os.path.join(base_mount_path, ".aosp_base_initialized")
        needs_populate = not os.path.exists(marker)
    else:
        # Not mounted — we must mount to check if population is needed
        activate_lv(VG_NAME, base_lv_name)
        os.makedirs(base_mount_path, exist_ok=True)
        mount(f"/dev/{VG_NAME}/{base_lv_name}", base_mount_path)
        _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", base_mount_path])
        marker = os.path.join(base_mount_path, ".aosp_base_initialized")
        needs_populate = not os.path.exists(marker)

    # 4. Populate if empty (first-time setup)
    if needs_populate:
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

    # 5. Unmount base LV if we mounted it (snapshots must be taken from unmounted base
    #    for filesystem consistency — crash-consistent snapshots are not guaranteed)
    if not was_already_mounted and is_lv_mounted(VG_NAME, base_lv_name):
        umount(base_mount_path)


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


@cli.command()
@click.option("--name", default=None, help="Base project 名称")
@click.option("--repo-url", default=None, help="远程清单仓库地址")
@click.option("--repo-branch", default=None, help="清单仓库分支")
@click.option("--docker-image", default=None, help="Docker 镜像名称")
@click.option("--base-lv-size-gb", type=int, default=None, help="基底卷大小 (GB)")
@click.option("--sync-type", type=click.Choice(["repo", "git"]), default=None,
              help="代码同步方式: repo (默认) | git")
@click.pass_context
def link(ctx, name, repo_url, repo_branch, docker_image, base_lv_size_gb, sync_type):
    """交互式或参数化配置 base project（仅写配置，不触发 LVM 操作）。"""
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

    # Check if all link-specific options are provided
    all_provided = all(v is not None for v in [name, repo_url, repo_branch, docker_image, base_lv_size_gb, sync_type])

    if all_provided:
        # Non-interactive: use provided values
        bp_name = name
    else:
        # Interactive mode
        click.echo("=== 配置 Base Project ===")
        click.echo("（括号内为当前值/默认值，直接回车保留）\n")

        # List existing projects for reference
        existing_names = [bp["name"] for bp in config.get("base_projects", [])]
        if existing_names:
            click.echo(f"已有项目: {', '.join(existing_names)}")

        bp_name = click.prompt("项目名称", default=name or "aosp")

    # Find or create the base project
    bp = get_base_project(config, bp_name)
    if bp is None:
        # Create new base project entry
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
        # Update existing base project with any provided values
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
        # Interactive: let user review/edit key fields
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

    # Save config only — no LVM operations
    save_config(config, config_path)
    # Show auto-derived paths
    click.echo(f"Base project '{bp_name}' configured.")
    click.echo(f"  base_lv_name    = {_base_lv_name(bp_name)} (自动生成)")
    click.echo(f"  base_mount_path = {_base_mount_path(config, bp_name)} (自动生成)")
    if is_default:
        click.echo(f"  default_base    = {bp_name}")
    click.echo("Run 'activate' to materialize.")


@cli.command()
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def create(ctx, workspace_name, base):
    """Create a lightweight workspace (metadata only)."""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)
    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    if get_workspace(bp, workspace_name) is not None:
        click.echo(f"Workspace '{workspace_name}' already exists in '{base}'", err=True)
        sys.exit(1)

    new_ws = {
        "name": workspace_name,
    }
    bp.setdefault("workspaces", []).append(new_ws)
    save_config(config, config_path)
    mount_path = _workspace_mount_path(config, base, workspace_name)
    click.echo(f"Workspace '{workspace_name}' created.")
    click.echo(f"  snapshot_lv_name = {_snapshot_lv_name(workspace_name)} (自动生成)")
    click.echo(f"  mount_path        = {mount_path} (自动生成)")


@cli.command()
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def activate(ctx, workspace_name, base):
    """Activate a workspace (lazy snapshot + mount + container).

    Auto-creates the workspace if it doesn't exist yet.
    """
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        # Auto-create workspace if not exists
        if not click.confirm(f"Workspace '{workspace_name}' 不存在，是否创建?", default=True):
            sys.exit(0)
        ws = {"name": workspace_name}
        bp.setdefault("workspaces", []).append(ws)
        save_config(config, config_path)
        click.echo(f"Workspace '{workspace_name}' created.")

    if _is_workspace_active(base, workspace_name):
        click.echo(f"Workspace '{workspace_name}' is already active.")
        return

    project_name = bp["name"]
    base_lv_name = _base_lv_name(project_name)
    snapshot_lv_name = _snapshot_lv_name(workspace_name)
    mount_path = _workspace_mount_path(config, project_name, workspace_name)
    docker_image = bp["docker_image"]

    # Lazy: ensure base LV exists and is populated before creating snapshot
    # _ensure_base_lv handles mount/unmount internally — base LV will be
    # unmounted after this call (snapshots are taken from unmounted base)
    _ensure_base_lv(config, bp)

    # 1. Lazy snapshot: create if not exists
    if not lv_exists(VG_NAME, snapshot_lv_name):
        create_snapshot(VG_NAME, base_lv_name, snapshot_lv_name)

    # Ensure snapshot LV is activated (thin snapshots have activation skip flag)
    activate_lv(VG_NAME, snapshot_lv_name)

    # 2. Mount snapshot
    os.makedirs(mount_path, exist_ok=True)
    if not is_lv_mounted(VG_NAME, snapshot_lv_name):
        mount(f"/dev/{VG_NAME}/{snapshot_lv_name}", mount_path)

    # Fix ownership: mount via sudo makes root own the mount point
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", mount_path])

    # 3. Start container
    c_name = container_name(project_name, workspace_name)
    if docker_container_exists(c_name):
        docker_rm(c_name)
    uid = os.getuid()
    gid = os.getgid()
    docker_run(c_name, mount_path, f"/{project_name}", docker_image, uid=uid, gid=gid)

    click.echo(f"Workspace '{workspace_name}' activated.")


@cli.command()
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def enter(ctx, workspace_name, base):
    """Enter a workspace container interactively.

    Auto-activates the workspace if not yet active.
    """
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        if not click.confirm(f"Workspace '{workspace_name}' 不存在，是否创建?", default=True):
            sys.exit(0)
        ws = {"name": workspace_name}
        bp.setdefault("workspaces", []).append(ws)
        save_config(config, config_path)
        click.echo(f"Workspace '{workspace_name}' created.")

    if not _is_workspace_active(base, workspace_name):
        if not click.confirm(f"Workspace '{workspace_name}' 未激活，是否激活?", default=True):
            sys.exit(0)
        ctx.invoke(activate, workspace_name=workspace_name, base=base)

    c_name = container_name(bp["name"], workspace_name)
    os.execvp("docker", ["docker", "exec", "-it", c_name, "/bin/sh"])


@cli.command()
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def deactivate(ctx, workspace_name, base):
    """Deactivate a workspace (stop container + unmount)."""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    ws = get_workspace(bp, workspace_name)
    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found in '{base}'", err=True)
        sys.exit(1)

    if not _is_workspace_active(base, workspace_name):
        click.echo(f"Workspace '{workspace_name}' is already inactive.")
        return

    c_name = container_name(bp["name"], workspace_name)
    mount_path = _workspace_mount_path(config, bp["name"], workspace_name)

    # 1. Stop and remove container
    if docker_container_exists(c_name):
        docker_rm(c_name)

    # 2. Unmount
    if is_mounted(mount_path):
        umount(mount_path)

    click.echo(f"Workspace '{workspace_name}' deactivated.")


@cli.command()
@click.argument("workspace_name")
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def remove(ctx, workspace_name, base):
    """Remove a workspace entirely (deactivate + destroy snapshot)."""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

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
        c_name = container_name(bp["name"], workspace_name)
        if docker_container_exists(c_name):
            docker_rm(c_name)
        mount_path = _workspace_mount_path(config, bp["name"], workspace_name)
        if is_mounted(mount_path):
            umount(mount_path)

    # 2. Destroy snapshot LV
    snapshot_lv_name = _snapshot_lv_name(workspace_name)
    if lv_exists(VG_NAME, snapshot_lv_name):
        remove_lv(VG_NAME, snapshot_lv_name)

    # 3. Remove from config
    bp["workspaces"].pop(ws_idx)
    save_config(config, config_path)
    click.echo(f"Workspace '{workspace_name}' removed.")


@cli.command()
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def sync(ctx, base):
    """Force re-sync and re-compile the base (destroys all workspaces)."""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)
    mode = config["global"]["mode"]

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    project_name = bp["name"]
    base_mount_path = _base_mount_path(config, project_name)
    docker_image = bp["docker_image"]

    # 1. Destroy all workspace snapshots and containers (keep config entries)
    workspaces = list(bp.get("workspaces", []))
    for ws in workspaces:
        ws_name = ws["name"]
        c_name = container_name(base, ws_name)
        if docker_container_exists(c_name):
            docker_rm(c_name)
        ws_mount_path = _workspace_mount_path(config, base, ws_name)
        if is_mounted(ws_mount_path):
            umount(ws_mount_path)
        snapshot_lv_name = _snapshot_lv_name(ws_name)
        if lv_exists(VG_NAME, snapshot_lv_name):
            remove_lv(VG_NAME, snapshot_lv_name)

    # 2. Ensure base LV exists (lazy) — this leaves base LV unmounted
    _ensure_base_lv(config, bp)

    # 3. Mount base LV for re-population
    base_lv_name = _base_lv_name(project_name)
    activate_lv(VG_NAME, base_lv_name)
    os.makedirs(base_mount_path, exist_ok=True)
    if not is_lv_mounted(VG_NAME, base_lv_name):
        mount(f"/dev/{VG_NAME}/{base_lv_name}", base_mount_path)
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", base_mount_path])

    # 4. Remove initialization marker to force re-populate
    marker = os.path.join(base_mount_path, ".aosp_base_initialized")
    if os.path.exists(marker):
        os.remove(marker)

    # 5. Re-populate
    if mode == "mock":
        if not docker_image_exists(docker_image):
            docker_build_mock(docker_image)
        mock_populate_base(base_mount_path)
    else:
        build_config = bp.get("build_config", {})
        c_name = container_name(base, "default")
        if docker_container_exists(c_name):
            docker_rm(c_name)
        docker_run(c_name, base_mount_path, f"/{base}", docker_image)
        _run_sync(c_name, bp)
        for cmd in build_config.get("setup_commands", []):
            docker_exec(c_name, f"cd /{base} && {cmd}")
        compile_cmd = build_config.get("compile_command", "")
        env_vars = build_config.get("env_vars", {})
        env_str = " ".join(f"{k}={v}" for k, v in env_vars.items())
        if compile_cmd:
            docker_exec(c_name, f"cd /{base} && {env_str} {compile_cmd}")
        docker_rm(c_name)

    # Write marker back
    with open(marker, "w") as f:
        f.write("initialized")

    # 6. Unmount base LV (snapshots should be taken from unmounted base)
    if is_lv_mounted(VG_NAME, base_lv_name):
        umount(base_mount_path)
    click.echo(f"Base project '{base}' synced successfully.")


@cli.command()
@click.option("--base", default=None, help="Base project 名称 (默认使用 global.default_base)")
@click.pass_context
def unlink(ctx, base):
    """删除 base project 配置（销毁所有工作区 + 删除配置条目）。"""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    # 1. Destroy all workspace snapshots and containers
    for ws in list(bp.get("workspaces", [])):
        ws_name = ws["name"]
        c_name = container_name(base, ws_name)
        if docker_container_exists(c_name):
            docker_rm(c_name)
        ws_mount_path = _workspace_mount_path(config, base, ws_name)
        if is_mounted(ws_mount_path):
            umount(ws_mount_path)
        snapshot_lv_name = _snapshot_lv_name(ws_name)
        if lv_exists(VG_NAME, snapshot_lv_name):
            remove_lv(VG_NAME, snapshot_lv_name)

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
    click.echo(f"Base project '{base}' unlinked.")


@cli.command()
@click.option("--base", default=None, help="Base project name (默认使用 global.default_base)")
@click.pass_context
def compile(ctx, base):
    """Alias for sync - re-compile the base."""
    config_path = ctx.obj["config_path"]
    config = load_and_validate_config(config_path)
    base = _resolve_base(config, base)
    ctx.invoke(sync, base=base)


if __name__ == "__main__":
    cli()
