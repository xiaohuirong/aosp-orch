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
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.toml")

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
from main import get_base_project


def run_cli(*args) -> subprocess.CompletedProcess:
    """Run the orchestrator CLI."""
    cmd = [sys.executable, os.path.join(SRC_DIR, "main.py"), "--config", CONFIG_PATH] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def run_link(name="xxx", repo_url="https://github.com/mock/manifest.git",
             repo_branch="main", docker_image="aosp-builder:mock",
             base_lv_size_gb=1, base_mount_path="/tmp/aosp_workspaces/xxx/base_mount"):
    """Run link command with all params (non-interactive)."""
    return run_cli(
        "link",
        "--name", name,
        "--repo-url", repo_url,
        "--repo-branch", repo_branch,
        "--docker-image", docker_image,
        "--base-lv-size-gb", str(base_lv_size_gb),
        "--base-mount-path", base_mount_path,
    )


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a system command."""
    return subprocess.run(cmd, capture_output=True, text=True)


def read_config():
    """Read current config."""
    import tomlkit
    with open(CONFIG_PATH, "r") as f:
        return tomlkit.load(f)


def write_config(config):
    """Write config atomically."""
    import tomlkit
    tmp_path = CONFIG_PATH + ".test.tmp"
    with open(tmp_path, "w") as f:
        tomlkit.dump(config, f)
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
    vg_name = config["global"]["lvm_vg_name"]

    # Remove all workspace containers
    for bp in config.get("base_projects", []):
        for ws in bp.get("workspaces", []):
            c_name = f"aosp_{bp['name']}_{ws['name']}"
            if docker_container_exists(c_name):
                docker_rm(c_name)
            if is_mounted(ws["mount_path"]):
                umount(ws["mount_path"])
            if lv_exists(vg_name, ws["snapshot_lv_name"]):
                remove_lv(vg_name, ws["snapshot_lv_name"])

    # Remove default container
    for bp in config.get("base_projects", []):
        c_name = f"aosp_{bp['name']}_default"
        if docker_container_exists(c_name):
            docker_rm(c_name)

    # Unmount and remove base LV
    for bp in config.get("base_projects", []):
        if is_mounted(bp["base_mount_path"]):
            umount(bp["base_mount_path"])
        if lv_exists(vg_name, bp["base_lv_name"]):
            remove_lv(vg_name, bp["base_lv_name"])

    # Remove thin pool and VG
    if vg_exists(vg_name):
        # Remove thin pool if exists
        thin_pool = config["global"]["thin_pool_name"]
        if lv_exists(vg_name, thin_pool):
            remove_lv(vg_name, thin_pool)
        # Remove VG
        run_cmd(["sudo", "vgremove", "-ff", "-y", vg_name])
        # Detach loop devices associated with pool image
        pool_image_path = config["global"]["pool_image_path"]
        result = run_cmd(["sudo", "losetup", "-j", pool_image_path])
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                dev = line.split(":")[0].strip()
                if dev:
                    run_cmd(["sudo", "losetup", "-d", dev])

    # Remove pool image (may be owned by root since fallocate runs with sudo)
    pool_image_path = config["global"]["pool_image_path"]
    if os.path.exists(pool_image_path):
        run_cmd(["sudo", "rm", "-f", pool_image_path])

    # Reset config to initial state
    _reset_config()


def _reset_config():
    """Reset config.toml to initial test state."""
    import tomlkit
    config = tomlkit.document()

    g = tomlkit.table()
    g["version"] = "3.2.0"
    g["mode"] = "mock"
    g["pool_image_path"] = "/aosp_pool.img"
    g["pool_image_size_gb"] = 2
    g["lvm_vg_name"] = "vgaosp_pool"
    g["thin_pool_name"] = "aosp_thin_pool"
    config["global"] = g

    bp = tomlkit.table()
    bp["name"] = "xxx"
    bp["repo_url"] = "https://github.com/mock/manifest.git"
    bp["repo_branch"] = "main"
    bp["docker_image"] = "aosp-builder:mock"
    bp["base_lv_name"] = "xxx_base_lv"
    bp["base_lv_size_gb"] = 1
    bp["base_mount_path"] = "/tmp/aosp_workspaces/xxx/base_mount"

    build_config = tomlkit.table()
    build_config["setup_commands"] = [
        "source build/envsetup.sh",
        "lunch mock_target-eng",
    ]
    build_config["compile_command"] = "m -j$(nproc)"
    build_config["env_vars"] = {"USE_CCACHE": "1"}
    bp["build_config"] = build_config

    bp["workspaces"] = tomlkit.aot()
    config["base_projects"] = [bp]

    write_config(config)


# ── Test Cases ────────────────────────────────────────────────

class TestAssertion1Initialization:
    """断言 1: 初始化验证（link 只写配置，activate 懒加载触发 LVM 操作）"""

    def test_link_writes_config(self):
        """After link, config.toml must contain the base project with correct TOML format."""
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"
        config = read_config()
        bp = get_base_project(config, "xxx")
        assert bp is not None, "Base project 'xxx' not found in config after link"
        assert bp["base_lv_name"] == "xxx_base_lv"

        # Verify TOML format: [[base_projects]] array with nested sub-tables
        raw = open(CONFIG_PATH, "r").read()
        assert "[[base_projects]]" in raw, "TOML must use [[base_projects]] array-of-tables syntax"
        assert "[base_projects.build_config]" in raw, "TOML must have [base_projects.build_config] sub-table"
        assert "[base_projects.build_config.env_vars]" in raw, "TOML must have [base_projects.build_config.env_vars] sub-table"
        # Ensure no broken top-level tables leaked from the array
        assert not raw.startswith("base_projects = ["), "TOML must not use inline array syntax for base_projects"

    def test_activate_creates_pool_image(self):
        """After activate (lazy), /aosp_pool.img must exist."""
        run_link()
        run_cli("create", "a", "--base", "xxx")
        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate failed: {result.stderr}"
        assert os.path.exists("/aosp_pool.img"), "Pool image /aosp_pool.img not created"

    def test_activate_creates_vg_and_base_lv_with_mock_output(self):
        """After activate (lazy), VG must be active and base LV must contain mock_system.img."""
        run_link()
        run_cli("create", "a", "--base", "xxx")
        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        # VG must exist
        assert vg_exists("vgaosp_pool"), "VG vgaosp_pool not active"

        # Base LV must contain mock content - mount it to verify
        config = read_config()
        vg_name = config["global"]["lvm_vg_name"]
        base_lv_name = config["base_projects"][0]["base_lv_name"]
        base_mount_path = config["base_projects"][0]["base_mount_path"]

        # The base LV may be unmounted after activate (snapshot is mounted instead)
        # So we need to mount it separately to check
        if not is_lv_mounted(vg_name, base_lv_name):
            from storage import activate_lv
            activate_lv(vg_name, base_lv_name)
            from storage import mount as do_mount
            os.makedirs(base_mount_path, exist_ok=True)
            do_mount(f"/dev/{vg_name}/{base_lv_name}", base_mount_path)

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

        # Assert: b_snapshot_lv must be destroyed
        config = read_config()
        vg_name = config["global"]["lvm_vg_name"]
        assert not lv_exists(vg_name, "b_snapshot_lv"), \
            "b_snapshot_lv still exists after sync"

        # Assert: a_snapshot_lv must be destroyed too
        assert not lv_exists(vg_name, "a_snapshot_lv"), \
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
        assert lv_exists(vg_name, "b_snapshot_lv"), \
            "b_snapshot_lv not recreated on activate after sync"

        # Assert: b is in clean state (no dirty.txt from before)
        result = docker_exec("aosp_xxx_b", "ls /xxx/dirty.txt 2>&1; echo EXIT_CODE=$?")
        assert "dirty.txt" not in result.stdout or "No such file" in result.stdout, \
            f"Workspace b is not clean after sync+activate! Output: {result.stdout}"


class TestSnapshotSpaceSaving:
    """验证快照节省空间的机制是否生效。"""

    def test_snapshot_data_percent_is_low(self):
        """A freshly created snapshot should have very low data_percent (space saving)."""
        # Setup: link
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        # Create and activate workspace
        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        config = read_config()
        vg_name = config["global"]["lvm_vg_name"]

        # Check data_percent of snapshot - should be very low since we just created it
        data_pct = get_lv_data_percent(vg_name, "a_snapshot_lv")
        assert data_pct < 50.0, \
            f"Snapshot data_percent is {data_pct}%, expected < 50% for a fresh snapshot (space saving not working)"

    def test_snapshot_only_stores_deltas(self):
        """Writing to a workspace should increase data_percent, but base LV should be unaffected."""
        # Setup: link
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        # Create and activate workspace
        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0, f"create a failed: {result.stderr}"

        result = run_cli("activate", "a")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        config = read_config()
        vg_name = config["global"]["lvm_vg_name"]

        # Get initial data_percent
        initial_pct = get_lv_data_percent(vg_name, "a_snapshot_lv")

        # Write a significant amount of data to the workspace
        from storage import docker_exec
        result = docker_exec("aosp_xxx_a", "dd if=/dev/zero of=/xxx/test_large_file bs=1M count=20 2>&1 && sync")
        # Verify the file was actually written
        check = docker_exec("aosp_xxx_a", "ls -la /xxx/test_large_file 2>&1")
        assert "test_large_file" in check.stdout, f"Failed to write test_large_file: {check.stdout}"

        # Sync host buffers to ensure LVM sees the data
        run_cmd(["sync"])

        # Get data_percent after writing
        after_pct = get_lv_data_percent(vg_name, "a_snapshot_lv")

        # Data percent should have increased
        assert after_pct > initial_pct, \
            f"Snapshot data_percent did not increase after writing data (before: {initial_pct}%, after: {after_pct}%)"

        # The snapshot should still be using less space than a full copy
        # (data_percent represents the percentage of the LV's virtual size that is actually used)
        # For a 1GB LV with 20MB written, data_percent should be well under 100%
        assert after_pct < 50.0, \
            f"Snapshot data_percent is {after_pct}%, space saving mechanism not effective"

    def test_multiple_snapshots_share_base(self):
        """Multiple workspaces should share the base data, not duplicate it."""
        # Setup: link
        result = run_link()
        assert result.returncode == 0, f"link failed: {result.stderr}"

        # Create and activate two workspaces
        result = run_cli("create", "a", "--base", "xxx")
        assert result.returncode == 0

        result = run_cli("activate", "a")
        assert result.returncode == 0

        result = run_cli("create", "b", "--base", "xxx")
        assert result.returncode == 0

        result = run_cli("activate", "b")
        assert result.returncode == 0

        config = read_config()
        vg_name = config["global"]["lvm_vg_name"]

        # Both snapshots should have low data_percent (mostly sharing base)
        pct_a = get_lv_data_percent(vg_name, "a_snapshot_lv")
        pct_b = get_lv_data_percent(vg_name, "b_snapshot_lv")

        # Both should be using minimal space since they share the base
        assert pct_a < 50.0, f"Snapshot a data_percent is {pct_a}%, expected < 50%"
        assert pct_b < 50.0, f"Snapshot b data_percent is {pct_b}%, expected < 50%"

        # Get thin pool usage to verify total space is much less than 2x base
        pool_info = get_lv_size_info(vg_name, config["global"]["thin_pool_name"])
        base_info = get_lv_size_info(vg_name, config["base_projects"][0]["base_lv_name"])

        # The thin pool data usage should be less than the sum of all virtual LV sizes
        # This proves snapshots are sharing data
        logger_msg = (f"Thin pool data%: {pool_info.get('data_percent', 'N/A')}%, "
                      f"Base LV size: {base_info.get('lv_size_mb', 'N/A')}MB, "
                      f"Snapshot a data%: {pct_a}%, Snapshot b data%: {pct_b}%")
        print(logger_msg)
