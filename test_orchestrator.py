"""End-to-end test suite for AOSP Block Device Build Container Orchestrator.

Tests the 4 required assertions from section 5.2 of the PRD:
1. Initialization verification (link)
2. Snapshot isolation verification
3. Deactivation idempotency verification
4. Force sync (clear-all) verification

Also verifies snapshot space-saving mechanism and git sync mode.
"""

import os
import subprocess
import sys
import tempfile
import pytest
from click.testing import CliRunner

# ── Helpers ───────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from aosp_orch.storage import (
    vg_exists,
    lv_exists,
    is_mounted,
    is_lv_mounted,
    docker_container_exists,
    docker_container_running,
    get_lv_data_percent,
    get_lv_size_info,
    remove_lv,
    umount,
    docker_rm,
)
from aosp_orch.main import (
    cli,
    get_base_project,
    VG_NAME,
    THIN_POOL_NAME,
    _pool_image_path,
    _base_lv_name,
    _base_mount_path,
    _snapshot_lv_name,
    _workspace_mount_path,
    load_config,
)


def run_cli(config_path: str, *args) -> subprocess.CompletedProcess:
    """Run the orchestrator CLI with given config path."""
    cmd = [sys.executable, "-m", "aosp_orch", "--config", config_path] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a system command."""
    return subprocess.run(cmd, capture_output=True, text=True)


def read_config(config_path: str) -> dict:
    """Read config from given path."""
    import yaml
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def write_config(config_path: str, config: dict) -> None:
    """Write config atomically."""
    import yaml
    tmp_path = config_path + ".test.tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.rename(tmp_path, config_path)


# ── Config profiles ──────────────────────────────────────────

MOCK_WORKDIR = "/tmp/aosp_test_mock"
PROD_WORKDIR = "/tmp/aosp_test_prod"


def _mock_config() -> dict:
    """Mock mode config template."""
    return {
        "global": {
            "mode": "mock",
            "workdir": MOCK_WORKDIR,
            "pool_image_size_gb": 2,
        },
        "base_projects": [
            {
                "name": "xxx",
                "repo_url": "https://github.com/mock/manifest.git",
                "repo_branch": "main",
                "docker_image": "aosp-builder:mock",
                "base_lv_size_gb": 1,
                "build_config": {
                    "setup_commands": [
                        "source build/envsetup.sh",
                        "lunch mock_target-eng",
                    ],
                    "compile_command": "m -j$(nproc)",
                    "env_vars": {"USE_CCACHE": "1"},
                },
                "workspaces": [],
            },
        ],
    }


def _prod_config() -> dict:
    """Prod mode + git sync config template."""
    return {
        "global": {
            "mode": "prod",
            "workdir": PROD_WORKDIR,
            "pool_image_size_gb": 2,
        },
        "base_projects": [
            {
                "name": "aosp",
                "repo_url": "https://github.com/xiaohuirong/txt2sub",
                "repo_branch": "main",
                "sync_type": "git",
                "docker_image": "alpine/git",
                "base_lv_size_gb": 1,
                "build_config": {
                    "setup_commands": ["touch prepare"],
                    "compile_command": "echo $(uname -a) > build",
                    "env_vars": {"USE_CCACHE": "1"},
                },
                "workspaces": [
                    {"name": "feature-a"},
                ],
            },
        ],
    }


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def setup_mock_docker_image():
    """Ensure mock Docker image exists before tests."""
    from aosp_orch.storage import docker_build_mock, docker_image_exists
    if not docker_image_exists("aosp-builder:mock"):
        docker_build_mock("aosp-builder:mock")


@pytest.fixture()
def mock_config_path(tmp_path):
    """Create a temp config file for mock tests."""
    config_path = str(tmp_path / "config.yaml")
    write_config(config_path, _mock_config())
    yield config_path


@pytest.fixture()
def prod_config_path(tmp_path):
    """Create a temp config file for prod/git tests."""
    config_path = str(tmp_path / "config.yaml")
    write_config(config_path, _prod_config())
    yield config_path


@pytest.fixture(autouse=True)
def cleanup_environment():
    """Cleanup LVM, mounts, and containers before and after each test."""
    _force_cleanup_all()
    yield
    _force_cleanup_all()


def _force_cleanup_all():
    """Force cleanup ALL LVM, mounts, containers, and pool images across both workdirs."""
    workdirs = [MOCK_WORKDIR, PROD_WORKDIR]

    # Remove ALL containers matching aosp_ pattern
    result = run_cmd(["docker", "ps", "-a", "--filter", "name=aosp_", "--format", "{{.Names}}"])
    if result.returncode == 0 and result.stdout.strip():
        for c_name in result.stdout.strip().split("\n"):
            c_name = c_name.strip()
            if c_name:
                docker_rm(c_name)

    # Unmount everything under any test workdir AND all VG mounts
    mount_result = run_cmd(["mount"])
    if mount_result.returncode == 0:
        for line in mount_result.stdout.strip().split("\n"):
            parts = line.split()
            mount_point = parts[2] if len(parts) > 2 else ""
            should_umount = False
            for wd in workdirs:
                if wd in line:
                    should_umount = True
                    break
            if (should_umount or f"/dev/mapper/{VG_NAME}-" in line or f"/dev/{VG_NAME}/" in line) and mount_point:
                umount(mount_point)

    # Remove ALL LVs in the VG
    if vg_exists(VG_NAME):
        # First pass: unmount any mounted LVs
        mount_result = run_cmd(["mount"])
        if mount_result.returncode == 0:
            for line in mount_result.stdout.strip().split("\n"):
                parts = line.split()
                mount_point = parts[2] if len(parts) > 2 else ""
                should_umount = False
                for wd in workdirs:
                    if wd in line:
                        should_umount = True
                        break
                if (should_umount or f"/dev/mapper/{VG_NAME}-" in line or f"/dev/{VG_NAME}/" in line) and mount_point:
                    umount(mount_point)

        # Lazy unmount any remaining VG mounts
        run_cmd(["sudo", "umount", "-l", f"/dev/{VG_NAME}"])
        dm_result = run_cmd(["sudo", "dmsetup", "ls", "--target", "thin"])
        if dm_result.returncode == 0 and dm_result.stdout.strip():
            for line in dm_result.stdout.strip().split("\n"):
                name = line.split()[0] if line.strip() else ""
                if name.startswith(f"{VG_NAME}-"):
                    run_cmd(["sudo", "dmsetup", "remove", "--force", name])

        # Remove all LVs: deactivate then force remove
        lvs_result = run_cmd(["sudo", "lvs", "--noheadings", "-o", "lv_name", VG_NAME])
        if lvs_result.returncode == 0 and lvs_result.stdout.strip():
            lv_names = [name.strip() for name in lvs_result.stdout.strip().split("\n") if name.strip()]
            # Deactivate all LVs first
            for lv_name in lv_names:
                run_cmd(["sudo", "lvchange", "-an", f"/dev/{VG_NAME}/{lv_name}"])
            # Remove snapshots first, then base LVs, then pool
            for lv_name in sorted(lv_names, key=lambda n: (n == THIN_POOL_NAME, not n.startswith("s-"))):
                run_cmd(["sudo", "lvremove", "-ff", "-y", f"/dev/{VG_NAME}/{lv_name}"])

        # Force remove VG and PV
        run_cmd(["sudo", "vgremove", "-ff", "-y", VG_NAME])

    # Detach all loop devices associated with any pool image
    for wd in workdirs:
        pool_image_path = os.path.join(wd, "pool.img")
        result = run_cmd(["sudo", "losetup", "-j", pool_image_path])
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                dev = line.split(":")[0].strip()
                if dev:
                    run_cmd(["sudo", "losetup", "-d", dev])

    # Remove pool images and workdirs
    for wd in workdirs:
        pool_image_path = os.path.join(wd, "pool.img")
        if os.path.exists(pool_image_path):
            run_cmd(["sudo", "rm", "-f", pool_image_path])
        if os.path.exists(wd):
            run_cmd(["sudo", "rm", "-rf", wd])


# ── Test Cases ────────────────────────────────────────────────

