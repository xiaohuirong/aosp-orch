"""AOSP Block Device Build Container Orchestrator - CLI Entry Point."""

import sys
import os
import logging

import click
import tomlkit

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
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_CONFIG_DIR, "config.toml")


def _resolve_config_path() -> str:
    """Resolve config path: env var > default ~/.config/aosp-orch/config.toml."""
    env_path = os.environ.get("AOSP_ORCH_CONFIG")
    if env_path:
        return env_path
    return DEFAULT_CONFIG_PATH


def load_config(config_path: str) -> dict:
    """Load config.toml from the given path."""
    with open(config_path, "r") as f:
        return tomlkit.load(f)


def save_config(config: dict, config_path: str) -> None:
    """Atomically save config.toml (write to .tmp then rename)."""
    dir_path = os.path.dirname(config_path)
    os.makedirs(dir_path, exist_ok=True)
    tmp_path = os.path.join(dir_path, ".config.toml.tmp")
    with open(tmp_path, "w") as f:
        tomlkit.dump(config, f)
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


def container_name(base_project_name: str, ws_name: str) -> str:
    """Generate Docker container name."""
    return f"aosp_{base_project_name}_{ws_name}"


def ensure_pool_and_vg(config: dict) -> str | None:
    """Ensure the LVM pool and VG exist. Returns loop device path or None if already set up."""
    g = config["global"]
    vg_name = g["lvm_vg_name"]
    pool_image_path = g["pool_image_path"]
    thin_pool_name = g["thin_pool_name"]
    pool_size_gb = g["pool_image_size_gb"]

    if vg_exists(vg_name):
        logger.info("VG '%s' already exists", vg_name)
        return None

    # Create pool image
    if not os.path.exists(pool_image_path):
        create_pool_image(pool_image_path, pool_size_gb)

    # Setup loop device
    loop_dev = get_loop_device_for_image(pool_image_path)
    if not loop_dev:
        loop_dev = setup_loop_device(pool_image_path)

    # Initialize LVM thin pool
    init_lvm_thin_pool(loop_dev, vg_name, thin_pool_name)
    return loop_dev


def _ensure_base_lv(config: dict, bp: dict) -> None:
    """Lazy-init: ensure pool, VG, base LV exist, are formatted, mounted, and populated.

    This is the core lazy-load logic called by activate/sync/compile when they
    discover the base infrastructure is missing.
    """
    g = config["global"]
    vg_name = g["lvm_vg_name"]
    thin_pool_name = g["thin_pool_name"]
    mode = g["mode"]
    base_lv_name = bp["base_lv_name"]
    base_mount_path = bp["base_mount_path"]
    docker_image = bp["docker_image"]

    # 1. Ensure pool and VG
    ensure_pool_and_vg(config)

    # 2. Create base LV if not exists
    if not lv_exists(vg_name, base_lv_name):
        create_thin_lv(vg_name, thin_pool_name, base_lv_name, bp["base_lv_size_gb"])
        format_ext4(f"/dev/{vg_name}/{base_lv_name}")

    # Ensure base LV is activated
    activate_lv(vg_name, base_lv_name)

    # 3. Mount base LV if not mounted
    os.makedirs(base_mount_path, exist_ok=True)
    if not is_lv_mounted(vg_name, base_lv_name):
        mount(f"/dev/{vg_name}/{base_lv_name}", base_mount_path)

    # Fix ownership
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", base_mount_path])

    # 4. Populate if empty (first-time setup)
    # Check if base LV has content by looking for a marker
    marker = os.path.join(base_mount_path, ".aosp_base_initialized")
    if not os.path.exists(marker):
        if mode == "mock":
            if not docker_image_exists(docker_image):
                docker_build_mock(docker_image)
            mock_populate_base(base_mount_path)
        else:
            build_config = bp.get("build_config", {})
            c_name = container_name(bp["name"], "default")
            if docker_container_exists(c_name):
                docker_rm(c_name)
            docker_run(c_name, base_mount_path, f"/{bp['name']}", docker_image)
            docker_exec(c_name, f"cd /{bp['name']} && repo init -u {bp['repo_url']} -b {bp['repo_branch']}")
            docker_exec(c_name, f"cd /{bp['name']} && repo sync")
            for cmd in build_config.get("setup_commands", []):
                docker_exec(c_name, f"cd /{bp['name']} && {cmd}")
            compile_cmd = build_config.get("compile_command", "")
            env_vars = build_config.get("env_vars", {})
            env_str = " ".join(f"{k}={v}" for k, v in env_vars.items())
            if compile_cmd:
                docker_exec(c_name, f"cd /{bp['name']} && {env_str} {compile_cmd}")
            docker_rm(c_name)
        # Write marker
        with open(marker, "w") as f:
            f.write("initialized")


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
@click.option("--pool-image-path", default=None, help="LVM-on-File 镜像物理路径")
@click.option("--pool-image-size-gb", type=int, default=None, help="存储池大小 (GB)")
@click.option("--lvm-vg-name", default=None, help="虚拟卷组名称")
@click.option("--thin-pool-name", default=None, help="LVM精简配置池名称")
@click.pass_context
def init(ctx, mode, pool_image_path, pool_image_size_gb, lvm_vg_name, thin_pool_name):
    """交互式或参数化初始化 config.toml 中的 [global] 配置。"""
    config_path = ctx.obj["config_path"]

    # Load existing config or create new one
    if os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = tomlkit.document()

    g = config.setdefault("global", tomlkit.table())

    # Current defaults
    defaults = {
        "version": g.get("version", "3.2.0"),
        "mode": g.get("mode", "mock"),
        "pool_image_path": g.get("pool_image_path", "/aosp_pool.img"),
        "pool_image_size_gb": g.get("pool_image_size_gb", 10 if mode == "prod" else 2),
        "lvm_vg_name": g.get("lvm_vg_name", "vgaosp_pool"),
        "thin_pool_name": g.get("thin_pool_name", "aosp_thin_pool"),
    }

    # If all options provided via CLI args, use them directly (non-interactive)
    all_provided = all(v is not None for v in [mode, pool_image_path, pool_image_size_gb, lvm_vg_name, thin_pool_name])

    if all_provided:
        g["version"] = defaults["version"]
        g["mode"] = mode
        g["pool_image_path"] = pool_image_path
        g["pool_image_size_gb"] = pool_image_size_gb
        g["lvm_vg_name"] = lvm_vg_name
        g["thin_pool_name"] = thin_pool_name
    else:
        # Interactive mode
        click.echo("=== 初始化 AOSP 编排器全局配置 ===")
        click.echo(f"配置文件: {config_path}")
        click.echo("（括号内为当前值/默认值，直接回车保留）\n")

        g["version"] = defaults["version"]

        g["mode"] = click.prompt("运行模式 (mock/prod)", default=defaults["mode"])

        effective_mode = g["mode"]
        size_default = 400 if effective_mode == "prod" else 2

        g["pool_image_path"] = click.prompt("存储池镜像路径", default=defaults["pool_image_path"])

        pool_size_default = pool_image_size_gb if pool_image_size_gb is not None else size_default
        g["pool_image_size_gb"] = click.prompt("存储池大小 (GB)", type=int, default=pool_size_default)

        g["lvm_vg_name"] = click.prompt("LVM 卷组名称", default=lvm_vg_name or defaults["lvm_vg_name"])
        g["thin_pool_name"] = click.prompt("精简池名称", default=thin_pool_name or defaults["thin_pool_name"])

    # Ensure base_projects exists
    config.setdefault("base_projects", [])

    save_config(config, config_path)
    click.echo(f"\n配置已保存到 {config_path}")
    click.echo("  mode            = %s" % g["mode"])
    click.echo("  pool_image_path = %s" % g["pool_image_path"])
    click.echo("  pool_image_size = %d GB" % g["pool_image_size_gb"])
    click.echo("  lvm_vg_name     = %s" % g["lvm_vg_name"])
    click.echo("  thin_pool_name  = %s" % g["thin_pool_name"])


@cli.command()
@click.option("--name", default=None, help="Base project 名称")
@click.option("--repo-url", default=None, help="远程清单仓库地址")
@click.option("--repo-branch", default=None, help="清单仓库分支")
@click.option("--docker-image", default=None, help="Docker 镜像名称")
@click.option("--base-lv-size-gb", type=int, default=None, help="基底卷大小 (GB)")
@click.option("--base-mount-path", default=None, help="基底卷挂载路径")
@click.pass_context
def link(ctx, name, repo_url, repo_branch, docker_image, base_lv_size_gb, base_mount_path):
    """交互式或参数化配置 base project（仅写配置，不触发 LVM 操作）。"""
    config_path = ctx.obj["config_path"]

    # Load or create config
    if os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = tomlkit.document()
        config.setdefault("global", tomlkit.table())
        g = config["global"]
        g["version"] = "3.2.0"
        g["mode"] = "mock"
        g["pool_image_path"] = "/aosp_pool.img"
        g["pool_image_size_gb"] = 2
        g["lvm_vg_name"] = "vgaosp_pool"
        g["thin_pool_name"] = "aosp_thin_pool"

    g = config["global"]
    mode = g["mode"]

    # Check if all link-specific options are provided
    all_provided = all(v is not None for v in [name, repo_url, repo_branch, docker_image, base_lv_size_gb, base_mount_path])

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
        bp = tomlkit.table()
        bp["name"] = bp_name
        bp["repo_url"] = repo_url or "https://android.googlesource.com/platform/manifest"
        bp["repo_branch"] = repo_branch or "main"
        bp["docker_image"] = docker_image or ("aosp-builder:mock" if mode == "mock" else "aosp-builder:latest")
        bp["base_lv_name"] = f"{bp_name}_base_lv"
        bp["base_lv_size_gb"] = base_lv_size_gb or (1 if mode == "mock" else 100)
        bp["base_mount_path"] = base_mount_path or f"/tmp/aosp_workspaces/{bp_name}/base_mount"

        build_config = tomlkit.table()
        build_config["setup_commands"] = ["source build/envsetup.sh", "lunch aosp_x86_64-eng"]
        build_config["compile_command"] = "m -j$(nproc)"
        env_vars = tomlkit.table()
        env_vars["USE_CCACHE"] = "1"
        build_config["env_vars"] = env_vars
        bp["build_config"] = build_config

        bp["workspaces"] = tomlkit.aot()

        config.setdefault("base_projects", []).append(bp)
    else:
        # Update existing base project with any provided values
        if repo_url is not None:
            bp["repo_url"] = repo_url
        if repo_branch is not None:
            bp["repo_branch"] = repo_branch
        if docker_image is not None:
            bp["docker_image"] = docker_image
        if base_lv_size_gb is not None:
            bp["base_lv_size_gb"] = base_lv_size_gb
        if base_mount_path is not None:
            bp["base_mount_path"] = base_mount_path

    if not all_provided:
        # Interactive: let user review/edit key fields
        bp["repo_url"] = click.prompt("清单仓库地址", default=bp["repo_url"])
        bp["repo_branch"] = click.prompt("清单分支", default=bp["repo_branch"])
        bp["docker_image"] = click.prompt("Docker 镜像", default=bp["docker_image"])
        bp["base_lv_size_gb"] = click.prompt("基底卷大小 (GB)", type=int, default=bp["base_lv_size_gb"])
        bp["base_mount_path"] = click.prompt("基底卷挂载路径", default=bp["base_mount_path"])

    # Save config only — no LVM operations
    save_config(config, config_path)
    click.echo(f"Base project '{bp_name}' configured. Run 'activate' to materialize.")


@cli.command()
@click.argument("workspace_name")
@click.option("--base", required=True, help="Base project name")
@click.pass_context
def create(ctx, workspace_name, base):
    """Create a lightweight workspace (metadata only)."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)
    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    if get_workspace(bp, workspace_name) is not None:
        click.echo(f"Workspace '{workspace_name}' already exists in '{base}'", err=True)
        sys.exit(1)

    mount_path = os.path.join(
        os.path.dirname(bp["base_mount_path"]),
        workspace_name,
    )

    new_ws = {
        "name": workspace_name,
        "status": "inactive",
        "snapshot_lv_name": f"{workspace_name}_snapshot_lv",
        "mount_path": mount_path,
    }
    bp.setdefault("workspaces", []).append(new_ws)
    save_config(config, config_path)
    click.echo(f"Workspace '{workspace_name}' created (inactive).")


@cli.command()
@click.argument("workspace_name")
@click.pass_context
def activate(ctx, workspace_name):
    """Activate a workspace (lazy snapshot + mount + container)."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)
    g = config["global"]
    vg_name = g["lvm_vg_name"]

    # Find the workspace across all base projects
    bp = None
    ws = None
    for b in config.get("base_projects", []):
        w = get_workspace(b, workspace_name)
        if w is not None:
            bp = b
            ws = w
            break

    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found", err=True)
        sys.exit(1)

    if ws["status"] == "active":
        click.echo(f"Workspace '{workspace_name}' is already active.")
        return

    base_lv_name = bp["base_lv_name"]
    snapshot_lv_name = ws["snapshot_lv_name"]
    mount_path = ws["mount_path"]
    docker_image = bp["docker_image"]

    # Lazy: ensure base LV exists and is populated before creating snapshot
    _ensure_base_lv(config, bp)

    # Unmount base LV after ensuring it's populated (snapshots are taken from unmounted base)
    if is_lv_mounted(vg_name, base_lv_name):
        umount(bp["base_mount_path"])

    # 1. Lazy snapshot: create if not exists
    if not lv_exists(vg_name, snapshot_lv_name):
        create_snapshot(vg_name, base_lv_name, snapshot_lv_name)

    # Ensure snapshot LV is activated (thin snapshots have activation skip flag)
    activate_lv(vg_name, snapshot_lv_name)

    # 2. Mount snapshot
    os.makedirs(mount_path, exist_ok=True)
    if not is_lv_mounted(vg_name, snapshot_lv_name):
        mount(f"/dev/{vg_name}/{snapshot_lv_name}", mount_path)

    # Fix ownership: mount via sudo makes root own the mount point
    _sudo_run(["chown", "-R", f"{os.getuid()}:{os.getgid()}", mount_path])

    # 3. Start container
    c_name = container_name(bp["name"], workspace_name)
    if docker_container_exists(c_name):
        docker_rm(c_name)
    uid = os.getuid()
    gid = os.getgid()
    docker_run(c_name, mount_path, f"/{bp['name']}", docker_image, uid=uid, gid=gid)

    # 4. Update status
    ws["status"] = "active"
    save_config(config, config_path)
    click.echo(f"Workspace '{workspace_name}' activated.")


@cli.command()
@click.argument("workspace_name")
@click.pass_context
def enter(ctx, workspace_name):
    """Enter a workspace container interactively."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)

    bp = None
    ws = None
    for b in config.get("base_projects", []):
        w = get_workspace(b, workspace_name)
        if w is not None:
            bp = b
            ws = w
            break

    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found", err=True)
        sys.exit(1)

    if ws["status"] != "active":
        click.echo(f"Workspace '{workspace_name}' is not active. Activate it first.", err=True)
        sys.exit(1)

    c_name = container_name(bp["name"], workspace_name)
    os.execvp("docker", ["docker", "exec", "-it", c_name, "/bin/bash"])


@cli.command()
@click.argument("workspace_name")
@click.pass_context
def deactivate(ctx, workspace_name):
    """Deactivate a workspace (stop container + unmount)."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)
    g = config["global"]
    vg_name = g["lvm_vg_name"]

    bp = None
    ws = None
    for b in config.get("base_projects", []):
        w = get_workspace(b, workspace_name)
        if w is not None:
            bp = b
            ws = w
            break

    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found", err=True)
        sys.exit(1)

    if ws["status"] == "inactive":
        click.echo(f"Workspace '{workspace_name}' is already inactive.")
        return

    c_name = container_name(bp["name"], workspace_name)
    mount_path = ws["mount_path"]

    # 1. Stop and remove container
    if docker_container_exists(c_name):
        docker_rm(c_name)

    # 2. Unmount
    if is_mounted(mount_path):
        umount(mount_path)

    # 3. Update status
    ws["status"] = "inactive"
    save_config(config, config_path)
    click.echo(f"Workspace '{workspace_name}' deactivated.")


@cli.command()
@click.argument("workspace_name")
@click.pass_context
def remove(ctx, workspace_name):
    """Remove a workspace entirely (deactivate + destroy snapshot)."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)
    g = config["global"]
    vg_name = g["lvm_vg_name"]

    bp = None
    ws = None
    bp_idx = None
    ws_idx = None
    for bi, b in enumerate(config.get("base_projects", [])):
        for wi, w in enumerate(b.get("workspaces", [])):
            if w["name"] == workspace_name:
                bp = b
                ws = w
                bp_idx = bi
                ws_idx = wi
                break
        if bp is not None:
            break

    if ws is None:
        click.echo(f"Workspace '{workspace_name}' not found", err=True)
        sys.exit(1)

    # 1. Deactivate if active
    if ws["status"] == "active":
        c_name = container_name(bp["name"], workspace_name)
        if docker_container_exists(c_name):
            docker_rm(c_name)
        if is_mounted(ws["mount_path"]):
            umount(ws["mount_path"])

    # 2. Destroy snapshot LV
    snapshot_lv_name = ws["snapshot_lv_name"]
    if lv_exists(vg_name, snapshot_lv_name):
        remove_lv(vg_name, snapshot_lv_name)

    # 3. Remove from config
    config["base_projects"][bp_idx]["workspaces"].pop(ws_idx)
    save_config(config, config_path)
    click.echo(f"Workspace '{workspace_name}' removed.")


@cli.command()
@click.option("--base", required=True, help="Base project name")
@click.pass_context
def sync(ctx, base):
    """Force re-sync and re-compile the base (destroys all workspaces)."""
    config_path = ctx.obj["config_path"]
    config = load_config(config_path)
    g = config["global"]
    vg_name = g["lvm_vg_name"]
    mode = g["mode"]

    bp = get_base_project(config, base)
    if bp is None:
        click.echo(f"Base project '{base}' not found in config", err=True)
        sys.exit(1)

    base_lv_name = bp["base_lv_name"]
    base_mount_path = bp["base_mount_path"]
    docker_image = bp["docker_image"]

    # 1. Destroy all workspaces
    workspaces = list(bp.get("workspaces", []))
    for ws in workspaces:
        ws_name = ws["name"]
        c_name = container_name(base, ws_name)
        if docker_container_exists(c_name):
            docker_rm(c_name)
        if is_mounted(ws["mount_path"]):
            umount(ws["mount_path"])
        if lv_exists(vg_name, ws["snapshot_lv_name"]):
            remove_lv(vg_name, ws["snapshot_lv_name"])

    # Clear workspaces from config
    bp["workspaces"].clear()
    save_config(config, config_path)

    # 2. Ensure base LV exists (lazy)
    _ensure_base_lv(config, bp)

    # 3. Remove initialization marker to force re-populate
    marker = os.path.join(base_mount_path, ".aosp_base_initialized")
    if os.path.exists(marker):
        os.remove(marker)

    # Re-populate
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
        docker_exec(c_name, f"cd /{base} && repo init -u {bp['repo_url']} -b {bp['repo_branch']}")
        docker_exec(c_name, f"cd /{base} && repo sync")
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

    # 4. Unmount base LV
    umount(base_mount_path)
    click.echo(f"Base project '{base}' synced successfully.")


@cli.command()
@click.option("--base", required=True, help="Base project name")
@click.pass_context
def compile(ctx, base):
    """Alias for sync - re-compile the base."""
    ctx.invoke(sync, base=base)


if __name__ == "__main__":
    cli()
