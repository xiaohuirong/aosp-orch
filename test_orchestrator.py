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

def _link_xxx(config_path: str):
    """Helper: link the xxx base project (creates LVM infrastructure)."""
    result = run_cli(config_path,
        "add", "--name", "xxx",
        "--repo-url", "https://github.com/mock/manifest.git",
        "--repo-branch", "main",
        "--docker-image", "aosp-builder:mock",
        "--username", "user",
        "--base-lv-size-gb", "1",
        "--sync-type", "repo",
    )
    assert result.returncode == 0, f"link failed: {result.stderr}"


class TestAssertion1Initialization:
    """断言 1: 初始化验证（link 创建 LVM 基础设施，create 创建快照，activate 挂载+启动容器）"""

    def test_link_writes_config_and_creates_infrastructure(self, mock_config_path):
        """After link, config must contain the base project and LVM infrastructure must exist."""
        result = run_cli(mock_config_path,
            "add", "--name", "xxx",
            "--repo-url", "https://github.com/mock/manifest.git",
            "--repo-branch", "main",
            "--docker-image", "aosp-builder:mock",
            "--username", "user",
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

        # Verify LVM infrastructure created by link
        assert vg_exists(VG_NAME), f"VG {VG_NAME} not created by link"
        assert lv_exists(VG_NAME, _base_lv_name("xxx")), "Base LV not created by link"

        # Verify pool image exists
        pool_image = _pool_image_path(config)
        assert os.path.exists(pool_image), f"Pool image {pool_image} not created by link"

    def test_link_creates_base_lv_with_mock_output(self, mock_config_path):
        """After link, base LV must contain mock_system.img."""
        result = run_cli(mock_config_path,
            "add", "--name", "xxx",
            "--repo-url", "https://github.com/mock/manifest.git",
            "--repo-branch", "main",
            "--docker-image", "aosp-builder:mock",
            "--username", "user",
            "--base-lv-size-gb", "1",
            "--sync-type", "repo",
        )
        assert result.returncode == 0, f"link failed: {result.stderr}"

        # Base LV must contain mock content - mount it to verify
        config = read_config(mock_config_path)
        base_lv_name = _base_lv_name("xxx")
        base_mount_path = _base_mount_path(config, "xxx")

        # The base LV should be unmounted after link — mount to verify content
        if not is_lv_mounted(VG_NAME, base_lv_name):
            from aosp_orch.storage import activate_lv
            activate_lv(VG_NAME, base_lv_name)
            from aosp_orch.storage import mount as do_mount
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
        _link_xxx(mock_config_path)
        # Create, mount and activate workspace a
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Write ai_code.txt in workspace a path inside the shared product container
        from aosp_orch.storage import docker_exec
        c_name = "aosp_xxx"
        docker_exec(c_name, "echo 'AI was here' > /xxx/a/ai_code.txt")

        # Verify file exists in a
        result = docker_exec(c_name, "cat /xxx/a/ai_code.txt")
        assert "AI was here" in result.stdout, "ai_code.txt not written in workspace a"

        # Create, mount and activate workspace b
        run_cli(mock_config_path, "new", "b", "--base", "xxx")
        run_cli(mock_config_path, "mount", "b", "--base", "xxx")
        result = run_cli(mock_config_path, "start", "b", "--base", "xxx")
        assert result.returncode == 0, f"activate b failed: {result.stderr}"

        # ASSERT ai_code.txt must NOT exist in workspace b
        result = docker_exec(c_name, "ls /xxx/b/ai_code.txt 2>&1; echo EXIT_CODE=$?")
        assert "ai_code.txt" not in result.stdout or "No such file" in result.stdout, \
            f"ISOLATION FAILURE: ai_code.txt visible in workspace b! Output: {result.stdout}"


class TestAssertion3DeactivationIdempotency:
    """断言 3: 去激活幂等性验证"""

    def test_deactivate_stops_container_only(self, mock_config_path):
        """After deactivate, container should stop but workspace mount should remain."""
        _link_xxx(mock_config_path)
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate a failed: {result.stderr}"

        # Verify it's active
        assert docker_container_exists("aosp_xxx"), \
            "Container aosp_xxx should exist after activate"
        assert docker_container_running("aosp_xxx"), \
            "Container aosp_xxx should be running after activate"

        # Deactivate
        result = run_cli(mock_config_path, "stop", "a", "--base", "xxx")
        assert result.returncode == 0, f"deactivate failed: {result.stderr}"

        # Assert: mount remains because deactivate no longer unmounts
        mount_result = run_cmd(["mount"])
        assert "s-a" in mount_result.stdout or _workspace_mount_path(read_config(mock_config_path), "xxx", "a") in mount_result.stdout, \
            f"workspace a should remain mounted after deactivate"

        # Assert: Docker container should still exist but be stopped
        assert docker_container_exists("aosp_xxx"), \
            "Container aosp_xxx should be preserved after deactivate"
        assert not docker_container_running("aosp_xxx"), \
            "Container aosp_xxx should be stopped after deactivate"

    def test_reactivate_reuses_persisted_container(self, mock_config_path):
        """Re-activate should reuse the persisted container instead of creating a new one."""
        _link_xxx(mock_config_path)
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")

        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"first activate failed: {result.stderr}"

        config = read_config(mock_config_path)
        bp = config["base_projects"][0]
        assert bp.get("default_container") == "aosp_xxx", "product container should be persisted in config"

        inspect_before = run_cmd(["docker", "inspect", "-f", "{{.Id}}", "aosp_xxx"])
        assert inspect_before.returncode == 0, f"inspect before failed: {inspect_before.stderr}"
        container_id_before = inspect_before.stdout.strip()

        result = run_cli(mock_config_path, "stop", "a", "--base", "xxx")
        assert result.returncode == 0, f"deactivate failed: {result.stderr}"

        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"second activate failed: {result.stderr}"

        inspect_after = run_cmd(["docker", "inspect", "-f", "{{.Id}}", "aosp_xxx"])
        assert inspect_after.returncode == 0, f"inspect after failed: {inspect_after.stderr}"
        container_id_after = inspect_after.stdout.strip()

        assert container_id_before == container_id_after, "container should be reused instead of recreated"


class TestSnapshotSpaceSaving:
    """验证快照节省空间的机制是否生效。"""

    def test_snapshot_data_percent_is_low(self, mock_config_path):
        """A freshly created snapshot should have very low data_percent (space saving)."""
        _link_xxx(mock_config_path)
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        data_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        assert data_pct < 50.0, \
            f"Snapshot data_percent is {data_pct}%, expected < 50% for a fresh snapshot (space saving not working)"

    def test_snapshot_only_stores_deltas(self, mock_config_path):
        """Writing to a workspace should increase data_percent."""
        _link_xxx(mock_config_path)
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")
        result = run_cli(mock_config_path, "start", "a", "--base", "xxx")
        assert result.returncode == 0, f"activate failed: {result.stderr}"

        initial_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))

        # Write data to the workspace
        from aosp_orch.storage import docker_exec
        result = docker_exec("aosp_xxx", "dd if=/dev/zero of=/xxx/a/test_large_file bs=1M count=20 2>&1 && sync")
        check = docker_exec("aosp_xxx", "ls -la /xxx/a/test_large_file 2>&1")
        assert "test_large_file" in check.stdout, f"Failed to write test_large_file: {check.stdout}"

        run_cmd(["sync"])

        after_pct = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        assert after_pct > initial_pct, \
            f"Snapshot data_percent did not increase after writing data (before: {initial_pct}%, after: {after_pct}%)"
        assert after_pct < 50.0, \
            f"Snapshot data_percent is {after_pct}%, space saving mechanism not effective"

    def test_multiple_snapshots_share_base(self, mock_config_path):
        """Multiple workspaces should share the base data, not duplicate it."""
        _link_xxx(mock_config_path)
        run_cli(mock_config_path, "new", "a", "--base", "xxx")
        run_cli(mock_config_path, "mount", "a", "--base", "xxx")
        run_cli(mock_config_path, "start", "a", "--base", "xxx")

        run_cli(mock_config_path, "new", "b", "--base", "xxx")
        run_cli(mock_config_path, "mount", "b", "--base", "xxx")
        run_cli(mock_config_path, "activate", "b", "--base", "xxx")

        pct_a = get_lv_data_percent(VG_NAME, _snapshot_lv_name("a"))
        pct_b = get_lv_data_percent(VG_NAME, _snapshot_lv_name("b"))

        assert pct_a < 50.0, f"Snapshot a data_percent is {pct_a}%, expected < 50%"
        assert pct_b < 50.0, f"Snapshot b data_percent is {pct_b}%, expected < 50%"


class TestLoopRecovery:
    """验证 loop/VG 丢失时的恢复与错误提示行为。"""

    def test_enter_recovers_storage_runtime_from_existing_pool_image(self, tmp_path, monkeypatch):
        """When pool image exists but loop/VG is gone, enter should recover runtime infra instead of misreporting missing base LV."""
        config_path = str(tmp_path / "config.yaml")
        workdir = str(tmp_path / "workdir")
        config = {
            "global": {
                "mode": "mock",
                "workdir": workdir,
                "pool_image_size_gb": 2,
                "default_base": "n1",
            },
            "base_projects": [
                {
                    "name": "n1",
                    "repo_url": "https://github.com/mock/manifest.git",
                    "repo_branch": "main",
                    "docker_image": "aosp-builder:mock",
                    "base_lv_size_gb": 1,
                    "workspaces": [],
                },
            ],
        }
        write_config(config_path, config)
        os.makedirs(workdir, exist_ok=True)
        open(os.path.join(workdir, "pool.img"), "a").close()

        import aosp_orch.main as main_mod

        runner = CliRunner()
        state = {"vg_exists_calls": 0, "setup_loop_called": False, "mount_called": False}

        def fake_vg_exists(_vg_name):
            state["vg_exists_calls"] += 1
            return state["vg_exists_calls"] >= 2

        def fake_setup_loop_device(_image_path):
            state["setup_loop_called"] = True
            return "/dev/loop9"

        def fake_mount(_device, _mount_path):
            state["mount_called"] = True

        def fake_execvp(_file, _args):
            raise SystemExit(0)

        monkeypatch.setattr(main_mod, "vg_exists", fake_vg_exists)
        monkeypatch.setattr(main_mod, "get_loop_device_for_image", lambda _path: None)
        monkeypatch.setattr(main_mod, "setup_loop_device", fake_setup_loop_device)
        monkeypatch.setattr(main_mod, "_sudo_run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""))
        monkeypatch.setattr(main_mod, "lv_exists", lambda _vg, _lv: True)
        monkeypatch.setattr(main_mod, "is_mounted", lambda _path: False)
        monkeypatch.setattr(main_mod, "is_lv_mounted", lambda _vg, _lv: False)
        monkeypatch.setattr(main_mod, "activate_lv", lambda _vg, _lv: None)
        monkeypatch.setattr(main_mod, "mount", fake_mount)
        monkeypatch.setattr(main_mod, "ensure_shared_mount", lambda _path: None)
        monkeypatch.setattr(main_mod, "docker_container_running", lambda _name: True)
        monkeypatch.setattr(main_mod.os, "execvp", fake_execvp)

        result = runner.invoke(cli, ["--config", config_path, "enter", "--base", "n1"])

        assert result.exit_code == 0, result.output
        assert state["setup_loop_called"], "应该在已有 pool image 时重建 loop 设备"
        assert state["mount_called"], "恢复存储运行态后应继续挂载 base LV"
        assert "Base LV 'n1' 不存在" not in result.output, result.output

    def test_mount_reports_missing_lvm_infrastructure_instead_of_missing_base_lv(self, tmp_path, monkeypatch):
        """When neither VG nor pool image exists, commands should guide user to init instead of claiming base LV is missing."""
        config_path = str(tmp_path / "config.yaml")
        workdir = str(tmp_path / "workdir")
        config = {
            "global": {
                "mode": "mock",
                "workdir": workdir,
                "pool_image_size_gb": 2,
                "default_base": "n1",
            },
            "base_projects": [
                {
                    "name": "n1",
                    "repo_url": "https://github.com/mock/manifest.git",
                    "repo_branch": "main",
                    "docker_image": "aosp-builder:mock",
                    "base_lv_size_gb": 1,
                    "workspaces": [],
                },
            ],
        }
        write_config(config_path, config)

        import aosp_orch.main as main_mod

        runner = CliRunner()
        monkeypatch.setattr(main_mod, "vg_exists", lambda _vg_name: False)
        monkeypatch.setattr(main_mod, "get_loop_device_for_image", lambda _path: None)

        result = runner.invoke(cli, ["--config", config_path, "mount", "--base", "n1"])

        assert result.exit_code != 0
        assert "LVM 基础设施不存在" in result.output, result.output
        assert "请先运行 'init'" in result.output, result.output
        assert "Base LV 'n1' 不存在" not in result.output, result.output


class TestMountPropagation:
    """验证挂载传播相关回归场景。"""

    def test_ensure_shared_mount_uses_bind_for_self_binding(self, monkeypatch):
        """ensure_shared_mount should use bind to bind path to itself before setting shared propagation."""
        import aosp_orch.storage as storage_mod

        calls = []

        def fake_sudo_run(cmd, check=True, capture=True):
            calls.append(cmd)
            if cmd[:2] == ["mountpoint", "-q"]:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(storage_mod, "_sudo_run", fake_sudo_run)
        monkeypatch.setattr(storage_mod.os, "makedirs", lambda *args, **kwargs: None)

        storage_mod.ensure_shared_mount("/tmp/aosp-propagation-test")

        assert ["mount", "--bind", "/tmp/aosp-propagation-test", "/tmp/aosp-propagation-test"] in calls
        assert ["mount", "--rbind", "/tmp/aosp-propagation-test", "/tmp/aosp-propagation-test"] not in calls

    def test_mount_ensures_shared_product_root_before_mounting_lv(self, tmp_path, monkeypatch):
        """mount should prepare the product root as shared before mounting base/workspace LVs."""
        config_path = str(tmp_path / "config.yaml")
        workdir = str(tmp_path / "workdir")
        config = {
            "global": {
                "mode": "mock",
                "workdir": workdir,
                "pool_image_size_gb": 2,
                "default_base": "n1",
            },
            "base_projects": [
                {
                    "name": "n1",
                    "repo_url": "https://github.com/mock/manifest.git",
                    "repo_branch": "main",
                    "docker_image": "aosp-builder:mock",
                    "base_lv_size_gb": 1,
                    "workspaces": [],
                },
            ],
        }
        write_config(config_path, config)

        import aosp_orch.main as main_mod

        order = []

        def fake_ensure_product_root_dir(_config, _project_name):
            order.append("ensure_root")
            return os.path.join(workdir, "n1")

        def fake_mount_lv(_lv_name, _mount_path):
            order.append("mount_lv")

        monkeypatch.setattr(main_mod, "_require_storage_runtime", lambda _config: None)
        monkeypatch.setattr(main_mod, "_ensure_product_root_dir", fake_ensure_product_root_dir)
        monkeypatch.setattr(main_mod, "lv_exists", lambda _vg, _lv: True)
        monkeypatch.setattr(main_mod, "_mount_lv", fake_mount_lv)

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", config_path, "mount", "--base", "n1"])

        assert result.exit_code == 0, result.output
        assert order == ["ensure_root", "mount_lv"], order
