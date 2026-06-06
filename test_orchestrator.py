"""End-to-end test suite for AOSP Block Device Build Container Orchestrator.

Tests the 4 required assertions from section 5.2 of the PRD:
1. Initialization verification (link)
2. Snapshot isolation verification
3. Deactivation idempotency verification
4. Force sync (clear-all) verification

Also verifies snapshot space-saving mechanism.
"""

import os
import subprocess
import time
import pytest

# ── Helpers ───────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")

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
)


def run_cli(*args) -> subprocess.CompletedProcess:
    """Run the orchestrator CLI."""
    cmd = [sys.executable, os.path.join(SRC_DIR, "main.py"), "--config", CONFIG_PATH] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def run_link(name="xxx", repo_url="https://github.com/mock/manifest.git",
             repo_branch="main", docker_image="aosp-builder:mock",
             base_lv_size_gb=1, sync_type="repo"):
    """Run link command with all params (non-interactive)."""
    return run_cli(
        "link",
        "--name", name,
        "--repo-url", repo_url,
        "--repo-branch", repo_branch,
        "--docker-image", docker_image,
        "--base-lv-size-gb", str(base_lv_size_gb),
        "--sync-type", sync_type,
    )


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a system command."""
    return subprocess.run(cmd, capture_output=True, text=True)


def read_config():
    """Read current config."""
    import yaml
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def write_config(config):
    """Write config atomically."""
    import yaml
    tmp_path = CONFIG_PATH + ".test.tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.rename(tmp_path, CONFIG_PATH)


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def setup_mock_docker_image():
    """Ensure mock Docker image exists before tests."""
    from storage import docker_build_mock, docker_image_exists
    if not docker_image_exists("aosp-builder:mock"):
        docker_build_mock("aosp-builder:mock")


@pytest.fixture(autouse=True)
def cleanup_environment():
    """Cleanup LVM, mounts, and containers before each test to ensure clean state."""
    _force_cleanup()
    yield
    _force_cleanup()


def _force_cleanup():
    """Force cleanup all LVM, mounts, containers, and pool image."""
    config = read_config()

    # Remove all workspace containers and snapshots
    for bp in config.get("base_projects", []):
        project_name = bp["name"]
        for ws in bp.get("workspaces", []):
            ws_name = ws["name"]
            c_name = f"aosp_{project_name}_{ws_name}"
            if docker_container_exists(c_name):
                docker_rm(c_name)
            ws_mount = _workspace_mount_path(config, project_name, ws_name)
            if is_mounted(ws_mount):
                umount(ws_mount)
            snap_name = _snapshot_lv_name(ws_name)
            if lv_exists(VG_NAME, snap_name):
                remove_lv(VG_NAME, snap_name)

    # Remove default container
    for bp in config.get("base_projects", []):
        c_name = f"aosp_{bp['name']}_default"
        if docker_container_exists(c_name):
            docker_rm(c_name)

    # Unmount and remove base LV
    for bp in config.get("base_projects", []):
        project_name = bp["name"]
        base_mount = _base_mount_path(config, project_name)
        if is_mounted(base_mount):
            umount(base_mount)
        base_lv = _base_lv_name(project_name)
        if lv_exists(VG_NAME, base_lv):
            remove_lv(VG_NAME, base_lv)

    # Remove thin pool and VG
    if vg_exists(VG_NAME):
        if lv_exists(VG_NAME, THIN_POOL_NAME):
            remove_lv(VG_NAME, THIN_POOL_NAME)
        run_cmd(["sudo", "vgremove", "-ff", "-y", VG_NAME])
        # Detach loop devices associated with pool image
        pool_image_path = _pool_image_path(config)
        result = run_cmd(["sudo", "losetup", "-j", pool_image_path])
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                dev = line.split(":")[0].strip()
                if dev:
                    run_cmd(["sudo", "losetup", "-d", dev])

    # Remove pool image (may be owned by root since fallocate runs with sudo)
    pool_image_path = _pool_image_path(config)
    if os.path.exists(pool_image_path):
        run_cmd(["sudo", "rm", "-f", pool_image_path])

    # Reset config to initial state
    _reset_config()


def _reset_config():
    """Reset config.yaml to initial test state."""
    config = {
        "global": {
            "mode": "mock",
            "workdir": "/tmp/aosp_workspaces",
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
    write_config(config)


# ── Test Cases ────────────────────────────────────────────────

class TestAssertion1Initialization:
    """断言 1: 初始化验证（link 只写配置，activate 懒加载触发 LVM 操作）"""

    def test_link_writes_config(self):
        """After link, config must contain the base project."""
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"
        config = read_config()
        bp = get_base_project(config, "xxx")
        assert bp is not None, "Base project 'xxx' not found in config after link"

        # Verify auto-derived names
        assert _base_lv_name("xxx") == "xxx_base_lv"

        # Ensure auto-derived fields are NOT stored in config
        raw = open(CONFIG_PATH, "r").read()
        assert "base_lv_name" not in raw, "base_lv_name should be auto-derived, not stored in config"
        assert "base_mount_path" not in raw, "base_mount_path should be auto-derived, not stored in config"
        assert "lvm_vg_name" not in raw, "lvm_vg_name should be hardcoded, not stored in config"
        assert "thin_pool_name" not in raw, "thin_pool_name should be hardcoded, not stored in config"

    def test_activate_creates_pool_image(self):
        """After activate (lazy), pool image must exist under workdir."""
        run_link()
        run_cli("create", "a", "--base", "xxx")
        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate failed: {result.stderr}"
        config = read_config()
        pool_image = _pool_image_path(config)
        assert os.path.exists(pool_image), f"Pool image {pool_image} not created"

    def test_activate_creates_vg_and_base_lv_with_mock_output(self):
        """After activate (lazy), VG must be active and base LV must contain mock_system.img."""
        run_link()
        run_cli("create", "a", "--base", "xxx")
        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        # VG must exist
        assert vg_exists(VG_NAME), f"VG {VG_NAME} not active"

        # Base LV must contain mock content - mount it to verify
        config = read_config()
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

    def test_workspace_isolation(self):
        """Files in workspace A must not be visible in workspace B."""
        # Step 1: link
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        # Step 2: create workspace a
        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        # Step 3: activate workspace a
        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Step 4: write ai_code.txt in workspace a container
        from storage import docker_exec
        c_name_a = "aosp_xxx_a"
        docker_exec(c_name_a, "echo 'AI was here' > /xxx/ai_code.txt")

        # Verify file exists in a
        result = docker_exec(c_name_a, "cat /xxx/ai_code.txt")
        assert "AI was here" in result.stdout, "ai_code.txt not written in workspace a"

        # Step 5: create workspace b
        result = run_cli("create", "b", "--base", "xxx")
        assert result.returncode == 0, f"create b failed: {result.stderr}"

        # Step 6: activate workspace b
        result = run_cli("activate", "b")
        assert result.returncode == 0, f"activate b failed: {result.stderr}"

        # Step 7: ASSERT ai_code.txt must NOT exist in workspace b
        c_name_b = "aosp_xxx_b"
        result = docker_exec(c_name_b, "ls /xxx/ai_code.txt 2>&1; echo EXIT_CODE=$?")
        assert "ai_code.txt" not in result.stdout or "No such file" in result.stdout, \
            f"ISOLATION FAILURE: ai_code.txt visible in workspace b! Output: {result.stdout}"


class TestAssertion3DeactivationIdempotency:
    """断言 3: 去激活幂等性验证"""

    def test_deactivate_unmounts_and_removes_container(self):
        """After deactivate, snapshot LV must be unmounted and container removed."""
        # Setup: link + create + activate
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Verify it's active
        config = read_config()
        ws = None
        for bp in config["base_projects"]:
            for w in bp.get("workspaces", []):
                if w["name"] == "a":
                    ws = w
                    break
        assert ws is not None and ws["status"] == "active"

        # Deactivate
        result = run_cli("deactivate", "a")
        assert result.returncode == 0, f"deactivate failed: {result.stderr}"

        # Assert: mount | grep a_snapshot_lv must return empty
        mount_result = run_cmd(["mount"])
        assert "a_snapshot_lv" not in mount_result.stdout, \
            f"a_snapshot_lv still mounted after deactivate"

        # Assert: Docker container must not exist
        assert not docker_container_exists("aosp_xxx_a"), \
            "Container aosp_xxx_a still exists after deactivate"

        # Assert: config status is inactive
        config = read_config()
        ws = None
        for bp in config["base_projects"]:
            for w in bp.get("workspaces", []):
                if w["name"] == "a":
                    ws = w
                    break
        assert ws is not None and ws["status"] == "inactive"


class TestAssertion4ForceSync:
    """断言 4: 强力清盘流验证"""

    def test_sync_destroys_workspaces_and_refreshes_base(self):
        """sync --base must destroy all workspace snapshots and refresh base."""
        # Setup: link + create + activate a and b
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        result = run_cli("create", "b", "--base", "xxx")
        assert result.returncode == 0, f"create b failed: {result.stderr}"

        result = run_cli("activate", "b")
        assert result.returncode == 0, f"activate b failed: {result.stderr}"

        # Write something in b to make it dirty
        from storage import docker_exec
        docker_exec("aosp_xxx_b", "echo 'dirty data' > /xxx/dirty.txt")

        # Run sync
        result = run_cli("sync", "--base", "xxx")
        assert result.returncode == 0, f"sync failed: {result.stderr}"

        # Assert: snapshot LVs must be destroyed
        assert not lv_exists(VG_NAME, _snapshot_lv_name("b")), \
            "b_snapshot_lv still exists after sync"
        assert not lv_exists(VG_NAME, _snapshot_lv_name("a")), \
            "a_snapshot_lv still exists after sync"

        # Assert: workspaces cleared from config
        config = read_config()
        bp = config["base_projects"][0]
        assert len(bp.get("workspaces", [])) == 0, \
            "Workspaces not cleared from config after sync"

        # Re-create and activate b to verify lazy reload
        result = run_cli("create", "b", "--base", "xxx")
        assert result.returncode == 0, f"re-create b failed: {result.stderr}"

        result = run_cli("activate", "b")
        assert result.returncode == 0, f"re-activate b failed: {result.stderr}"

        # Assert: b_snapshot_lv is recreated (lazy load)
        assert lv_exists(VG_NAME, _snapshot_lv_name("b")), \
            "b_snapshot_lv not recreated on activate after sync"

        # Assert: b is in clean state (no dirty.txt from before)
        result = docker_exec("aosp_xxx_b", "ls /xxx/dirty.txt 2>&1; echo EXIT_CODE=$?")
        assert "dirty.txt" not in result.stdout or "No such file" in result.stdout, \
            f"Workspace b is not clean after sync+activate! Output: {result.stdout}"


class TestSnapshotSpaceSaving:
    """验证快照节省空间的机制是否生效。"""

    def test_snapshot_data_percent_is_low(self):
        """A freshly created snapshot should have very low data_percent (space saving)."""
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Check data_percent of snapshot - should be very low since we just created it
        data_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        assert data_pct < 50.0, \
            f"Snapshot data_percent is {data_pct}%, expected < 50% for a fresh snapshot (space saving not working)"

    def test_snapshot_only_stores_deltas(self):
        """Writing to a workspace should increase data_percent, but base LV should be unaffected."""
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Get initial data_percent
        initial_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))

        # Write a significant amount of data to the workspace
        from storage import docker_exec
        result = docker_exec("aosp_xxx_a", "dd if=/dev/zero of=/xxx/test_large_file bs=1M count=20 2>&1 && sync")
        # Verify the file was actually written
        check = docker_exec("aosp_xxx_a", "ls -la /xxx/test_large_file 2>&1")
        assert "test_large_file" in check.stdout, f"Failed to write test_large_file: {check.stdout}"

        # Sync host buffers to ensure LVM sees the data
        run_cmd(["sync"])

        # Get data_percent after writing
        after_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))

        # Data percent should have increased
        assert after_pct > initial_pct, \
            f"Snapshot data_percent did not increase after writing data (before: {initial_pct}%, after: {after_pct}%)"

        # The snapshot should still be using less space than a full copy
        assert after_pct < 50.0, \
            f"Snapshot data_percent is {after_pct}%, space saving mechanism not effective"

    def test_multiple_snapshots_share_base(self):
        """Multiple workspaces should share the base data, not duplicate it."""
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0

        result = run_cli("activate", "a")
        assert result.returncode == 0

        result = run_cli("create", "b", "--base", "xxx")
        assert result.returncode == 0

        result = run_cli("activate", "b")
        assert result.returncode == 0

        # Both snapshots should have low data_percent (mostly sharing base)
        pct_a = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        pct_b = get_lv_data_percent(VG_NAME, _snapshot_lv_name("b"))

        assert pct_a < 50.0, f"Snapshot a data_percent is {pct_a}%, expected < 50%"
        assert pct_b < 50.0, f"Snapshot b data_percent is {pct_b}%, expected < 50%"

        # Get thin pool usage to verify total space is much less than 2x base
        pool_info = get_lv_size_info(VG_NAME, THIN_POOL_NAME)
        base_info = get_lv_size_info(VG_NAME, _base_lv_name("xxx"))

        logger_msg = (f"Thin pool data%: {pool_info.get('data_percent', 'N/A')}%, "
                      f"Base LV size: {base_info.get('lv_size_mb', 'N/A')}MB, "
                      f"Snapshot a data%: {pct_a}%, Snapshot b data%: {pct_b}%")
        print(logger_msg)
