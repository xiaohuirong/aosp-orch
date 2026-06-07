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
import tempfile
import pytest

# ── Helpers ───────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Add src to path for direct imports
import sys
sys.path.insert(0, SRC_DIR)

from storage import (
    vg_exists,
    lv_exists,
    is_mounted,
    is_lv_mounted,
    docker_container_exists,
    get_lv_data_percent,
    get_lv_size_info,
    remove_lv,
    umount,
    docker_rm,
)
from main import (
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
    cmd = [sys.executable, os.path.join(SRC_DIR, "main.py"), "--config", config_path] + list(args)
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
    from storage import docker_build_mock, docker_image_exists
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

class TestAssertion1Initialization:
    """断言 1: 初始化验证（link 只写配置，activate 懒加载触发 LVM 操作）"""

    def test_link_writes_config(self, mock_config_path):
        """After link, config must contain the base project."""
        result = run_cli(mock_config_path,
            "link", "--name", "xxx",
            "--repo-url", "https://github.com/mock/manifest.git",
            "--repo-branch", "main",
            "--docker-image", "aosp-builder:mock",
            "--base-lv-size-gb", "1",
            "--sync-type", "repo",
        )
        assert result.returncode == 0, f"link failed: {result.stderr}"
        config = read_config(mock_config_path)
        bp = get_base_project(config, "xxx")
        assert bp is not None, "Base project 'xxx' not found in config after link"

        # Verify auto-derived names
        assert _base_lv_name("xxx") == "xxx"

        # Ensure auto-derived fields are NOT stored in config
        raw = open(mock_config_path, "r").read()
        assert "base_lv_name" not in raw, "base_lv_name should be auto-derived, not stored in config"
        assert "base_mount_path" not in raw, "base_mount_path should be auto-derived, not stored in config"
        assert "lvm_vg_name" not in raw, "lvm_vg_name should be hardcoded, not stored in config"
        assert "thin_pool_name" not in raw, "thin_pool_name should be hardcoded, not stored in config"

    def test_activate_creates_pool_image(self, mock_config_path):
        """After activate (lazy), pool image must exist under workdir."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"
        config = read_config(mock_config_path)
        pool_image = _pool_image_path(config)
        assert os.path.exists(pool_image), f"Pool image {pool_image} not created"

    def test_activate_creates_vg_and_base_lv_with_mock_output(self, mock_config_path):
        """After activate (lazy), VG must be active and base LV must contain mock_system.img."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        # VG must exist
        assert vg_exists(VG_NAME), f"VG {VG_NAME} not active"

        # Base LV must contain mock content - mount it to verify
        config = read_config(mock_config_path)
        base_lv_name = _base_lv_name("xxx")
        base_mount_path = _base_mount_path(config, "xxx")

        # The base LV may be unmounted after activate (snapshot is mounted instead)
        if not is_lv_mounted(VG_NAME, base_lv_name):
            from storage import activate_lv
            activate_lv(VG_NAME, base_lv_name)
            from storage import mount as do_mount
            os.makedirs(base_mount_path, exist_ok=True)
            do_mount(f"/dev/{VG_NAME}/{base_lv_name}", base_mount_path)

        mock_img = os.path.join(base_mount_path, "out", "mock_system.img")
        assert os.path.exists(mock_img), f"mock_system.img not found at {mock_img}"

        envsetup = os.path.join(base_mount_path, "build", "envsetup.sh")
        assert os.path.exists(envsetup), f"envsetup.sh not found at {envsetup}"

        # Cleanup: unmount
        umount(base_mount_path)


class TestAssertion2SnapshotIsolation:
    """断言 2: 快照独立性验证"""

    def test_workspace_isolation(self, mock_config_path):
        """Files in workspace A must not be visible in workspace B."""
        # Create and activate workspace a
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Write ai_code.txt in workspace a container
        from storage import docker_exec
        c_name_a = "aosp_xxx_a"
        docker_exec(c_name_a, "echo 'AI was here' > /xxx/ai_code.txt")

        # Verify file exists in a
        result = docker_exec(c_name_a, "cat /xxx/ai_code.txt")
        assert "AI was here" in result.stdout, "ai_code.txt not written in workspace a"

        # Create and activate workspace b
        run_cli(mock_config_path, "create", "b", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "b", "--base", "xxx")
        assert result.returncode == 0, f"activate b failed: {result.stderr}"

        # ASSERT ai_code.txt must NOT exist in workspace b
        c_name_b = "aosp_xxx_b"
        result = docker_exec(c_name_b, "ls /xxx/ai_code.txt 2>&1; echo EXIT_CODE=$?")
        assert "ai_code.txt" not in result.stdout or "No such file" in result.stdout, \
            f"ISOLATION FAILURE: ai_code.txt visible in workspace b! Output: {result.stdout}"


class TestAssertion3DeactivationIdempotency:
    """断言 3: 去激活幂等性验证"""

    def test_deactivate_unmounts_and_removes_container(self, mock_config_path):
        """After deactivate, snapshot LV must be unmounted and container removed."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Verify it's active
        assert docker_container_exists("aosp_xxx_a"), \
            "Container aosp_xxx_a should exist after activate"

        # Deactivate
        result = run_cli(mock_config_path, "deactivate", "a", "--base", "xxx")
        assert result.returncode == 0, f"deactivate failed: {result.stderr}"

        # Assert: mount | grep s-a must return empty
        mount_result = run_cmd(["mount"])
        assert "s-a" not in mount_result.stdout, \
            f"s-a still mounted after deactivate"

        # Assert: Docker container must not exist
        assert not docker_container_exists("aosp_xxx_a"), \
            "Container aosp_xxx_a still exists after deactivate"


class TestAssertion4ForceSync:
    """断言 4: 强力清盘流验证"""

    def test_sync_destroys_workspaces_and_refreshes_base(self, mock_config_path):
        """sync --base must destroy all workspace snapshots and refresh base."""
        # Setup: create + activate a and b
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        run_cli(mock_config_path, "create", "b", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "b", "--base", "xxx")
        assert result.returncode == 0, f"activate b failed: {result.stderr}"

        # Write something in b to make it dirty
        from storage import docker_exec
        docker_exec("aosp_xxx_b", "echo 'dirty data' > /xxx/dirty.txt")

        # Run sync
        result = run_cli(mock_config_path, "sync", "--base", "xxx")
        assert result.returncode == 0, f"sync failed: {result.stderr}"

        # Assert: snapshot LVs must be destroyed
        assert not lv_exists(VG_NAME, _snapshot_lv_name("b")), \
            "s-b still exists after sync"
        assert not lv_exists(VG_NAME, _snapshot_lv_name("a")), \
            "s-a still exists after sync"

        # Assert: workspaces kept in config
        config = read_config(mock_config_path)
        bp = config["base_projects"][0]
        ws_names = [w["name"] for w in bp.get("workspaces", [])]
        assert "a" in ws_names and "b" in ws_names, \
            "Workspaces should still exist in config after sync"

        # Re-activate b to verify lazy reload
        result = run_cli(mock_config_path, "activate", "b", "--base", "xxx")
        assert result.returncode == 0, f"re-activate b failed: {result.stderr}"

        # Assert: s-b is recreated (lazy load)
        assert lv_exists(VG_NAME, _snapshot_lv_name("b")), \
            "s-b not recreated on activate after sync"

        # Assert: b is in clean state (no dirty.txt from before)
        result = docker_exec("aosp_xxx_b", "ls /xxx/dirty.txt 2>&1; echo EXIT_CODE=$?")
        assert "dirty.txt" not in result.stdout or "No such file" in result.stdout, \
            f"Workspace b is not clean after sync+activate! Output: {result.stdout}"


class TestSnapshotSpaceSaving:
    """验证快照节省空间的机制是否生效。"""

    def test_snapshot_data_percent_is_low(self, mock_config_path):
        """A freshly created snapshot should have very low data_percent (space saving)."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        data_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        assert data_pct < 50.0, \
            f"Snapshot data_percent is {data_pct}%, expected < 50% for a fresh snapshot (space saving not working)"

    def test_snapshot_only_stores_deltas(self, mock_config_path):
        """Writing to a workspace should increase data_percent."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "activate", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        initial_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))

        # Write data to the workspace
        from storage import docker_exec
        result = docker_exec("aosp_xxx_a", "dd if=/dev/zero of=/xxx/test_large_file bs=1M count=20 2>&1 && sync")
        check = docker_exec("aosp_xxx_a", "ls -la /xxx/test_large_file 2>&1")
        assert "test_large_file" in check.stdout, f"Failed to write test_large_file: {check.stdout}"

        run_cmd(["sync"])

        after_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        assert after_pct > initial_pct, \
            f"Snapshot data_percent did not increase after writing data (before: {initial_pct}%, after: {after_pct}%)"
        assert after_pct < 50.0, \
            f"Snapshot data_percent is {after_pct}%, space saving mechanism not effective"

    def test_multiple_snapshots_share_base(self, mock_config_path):
        """Multiple workspaces should share the base data, not duplicate it."""
        run_cli(mock_config_path, "create", "a", "--base", "xxx")
        run_cli(mock_config_path, "activate", "a", "--base", "xxx")

        run_cli(mock_config_path, "create", "b", "--base", "xxx")
        run_cli(mock_config_path, "activate", "b", "--base", "xxx")

        pct_a = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        pct_b = get_lv_data_percent(VG_NAME, _snapshot_lv_name("b"))

        assert pct_a < 50.0, f"Snapshot a data_percent is {pct_a}%, expected < 50%"
        assert pct_b < 50.0, f"Snapshot b data_percent is {pct_b}%, expected < 50%"


class TestGitSync:
    """验证 git sync 模式：git clone 到 git-repo 子目录，git pull 增量更新。"""

    def test_git_clone_to_git_repo_dir(self, prod_config_path):
        """activate should git clone into /{project}/git-repo directory."""
        result = run_cli(prod_config_path, "activate", "feature-a", "--base", "aosp")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        # Verify git-repo directory exists and has .git
        from storage import docker_exec
        c_name = "aosp_aosp_feature-a"
        check = docker_exec(c_name, "test -d /aosp/git-repo/.git && echo EXISTS || echo MISSING")
        assert "EXISTS" in check.stdout, f"git-repo/.git not found in container. Output: {check.stdout}"

        # Verify repo content exists
        check = docker_exec(c_name, "ls /aosp/git-repo/")
        assert check.returncode == 0, f"Failed to list git-repo contents: {check.stderr}"

    def test_git_pull_on_reactivate(self, prod_config_path):
        """Re-activating after sync should git pull instead of git clone."""
        # First activate: git clone
        result = run_cli(prod_config_path, "activate", "feature-a", "--base", "aosp")
        assert result.returncode == 0, f"first activate failed: {result.stderr}"

        # Deactivate
        result = run_cli(prod_config_path, "deactivate", "feature-a", "--base", "aosp")
        assert result.returncode == 0, f"deactivate failed: {result.stderr}"

        # Re-activate: should git pull (not fail)
        result = run_cli(prod_config_path, "activate", "feature-a", "--base", "aosp")
        assert result.returncode == 0, f"re-activate failed: {result.stderr}"

        # Verify git-repo still intact
        from storage import docker_exec
        c_name = "aosp_aosp_feature-a"
        check = docker_exec(c_name, "test -d /aosp/git-repo/.git && echo EXISTS || echo MISSING")
        assert "EXISTS" in check.stdout, f"git-repo/.git not found after re-activate. Output: {check.stdout}"
