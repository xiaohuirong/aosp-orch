"""Storage layer: LVM, loop device, mount, and Docker operations."""

import subprocess
import os
import logging
import time

logger = logging.getLogger(__name__)


def _run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command, optionally with sudo prefix."""
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _sudo_run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command with sudo."""
    return _run(["sudo"] + cmd, check=check, capture=capture)


# ── Loop device ──────────────────────────────────────────────

def setup_loop_device(image_path: str) -> str:
    """Attach an image file as a loop device, return device path."""
    result = _sudo_run(["losetup", "-f", "--show", image_path])
    device = result.stdout.strip()
    logger.info("Loop device created: %s -> %s", image_path, device)
    return device


def detach_loop_device(device: str) -> None:
    """Detach a loop device."""
    _sudo_run(["losetup", "-d", device])
    logger.info("Loop device detached: %s", device)


def get_loop_device_for_image(image_path: str) -> str | None:
    """Find existing loop device for an image, or None."""
    result = _sudo_run(["losetup", "-j", image_path], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Output format: /dev/loopX: [xxxx]: (/aosp_pool.img)
    return result.stdout.split(":")[0].strip()


# ── LVM operations ──────────────────────────────────────────

def vg_exists(vg_name: str) -> bool:
    """Check if a volume group exists."""
    result = _sudo_run(["vgs", vg_name], check=False, capture=True)
    return result.returncode == 0


def create_pool_image(path: str, size_gb: int) -> None:
    """Create the pool image file using fallocate."""
    _sudo_run(["fallocate", "-l", f"{size_gb}G", path])
    logger.info("Pool image created: %s (%d GB)", path, size_gb)


def init_lvm_thin_pool(device: str, vg_name: str, thin_pool_name: str) -> None:
    """Initialize LVM physical volume, volume group, and thin pool on a device."""
    _sudo_run(["pvcreate", "-ff", "-y", device])
    logger.info("PV created on %s", device)

    _sudo_run(["vgcreate", vg_name, device])
    logger.info("VG '%s' created", vg_name)

    # Create thin pool (use 95% of VG free space for data, minimal metadata)
    _sudo_run([
        "lvcreate", "-l", "100%FREE",
        "-T", f"{vg_name}/{thin_pool_name}",
    ])
    logger.info("Thin pool '%s/%s' created", vg_name, thin_pool_name)


def create_thin_lv(vg_name: str, thin_pool_name: str, lv_name: str, size_gb: int) -> None:
    """Create a thin-provisioned logical volume."""
    _sudo_run([
        "lvcreate", "-V", f"{size_gb}G",
        "-T", f"{vg_name}/{thin_pool_name}",
        "-n", lv_name,
    ])
    logger.info("Thin LV '%s/%s' created (%d GB)", vg_name, lv_name, size_gb)


def create_snapshot(vg_name: str, base_lv_name: str, snapshot_lv_name: str) -> None:
    """Create a snapshot of an existing LV."""
    _sudo_run([
        "lvcreate", "-s",
        "-n", snapshot_lv_name,
        f"/dev/{vg_name}/{base_lv_name}",
    ])
    logger.info("Snapshot '%s' created from '%s'", snapshot_lv_name, base_lv_name)


def lv_exists(vg_name: str, lv_name: str) -> bool:
    """Check if a logical volume exists."""
    result = _sudo_run(["lvs", f"{vg_name}/{lv_name}"], check=False, capture=True)
    return result.returncode == 0


def remove_lv(vg_name: str, lv_name: str) -> None:
    """Remove a logical volume."""
    _sudo_run(["lvremove", "-f", f"/dev/{vg_name}/{lv_name}"])
    logger.info("LV '%s/%s' removed", vg_name, lv_name)


def activate_lv(vg_name: str, lv_name: str) -> None:
    """Activate a logical volume. Uses -K to handle activation skip on thin snapshots."""
    _sudo_run(["lvchange", "-K", "-ay", f"/dev/{vg_name}/{lv_name}"])
    logger.info("LV '%s/%s' activated", vg_name, lv_name)


def deactivate_lv(vg_name: str, lv_name: str) -> None:
    """Deactivate a logical volume."""
    _sudo_run(["lvchange", "-an", f"/dev/{vg_name}/{lv_name}"])
    logger.info("LV '%s/%s' deactivated", vg_name, lv_name)


def get_lv_data_percent(vg_name: str, lv_name: str) -> float:
    """Get the data usage percentage of a thin LV."""
    result = _sudo_run([
        "lvs", "--noheadings", "--nosuffix",
        "-o", "data_percent",
        f"{vg_name}/{lv_name}",
    ], check=False)
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (ValueError, IndexError):
        return 0.0


def get_lv_size_info(vg_name: str, lv_name: str) -> dict:
    """Get LV size information including data percent for snapshot space saving verification."""
    result = _sudo_run([
        "lvs", "--noheadings", "--nosuffix", "--units", "m",
        "-o", "lv_size,data_percent,origin",
        f"{vg_name}/{lv_name}",
    ], check=False)
    if result.returncode != 0:
        return {}
    parts = result.stdout.strip().split()
    info = {}
    try:
        info["lv_size_mb"] = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        info["lv_size_mb"] = 0.0
    try:
        info["data_percent"] = float(parts[1].replace(",", "."))
    except (ValueError, IndexError):
        info["data_percent"] = 0.0
    try:
        info["origin"] = parts[2] if len(parts) > 2 else ""
    except (ValueError, IndexError):
        info["origin"] = ""
    return info


# ── Filesystem ───────────────────────────────────────────────

def format_ext4(device: str) -> None:
    """Format a device with ext4."""
    _sudo_run(["mkfs.ext4", "-F", device])
    logger.info("Formatted %s as ext4", device)


# ── Mount ────────────────────────────────────────────────────

def mount(device: str, mount_path: str) -> None:
    """Mount a device to a path."""
    os.makedirs(mount_path, exist_ok=True)
    _sudo_run(["mount", device, mount_path])
    logger.info("Mounted %s -> %s", device, mount_path)


def umount(mount_path: str) -> None:
    """Unmount a path."""
    _sudo_run(["umount", mount_path], check=False)
    logger.info("Unmounted %s", mount_path)


def is_mounted(mount_path: str) -> bool:
    """Check if a path is currently mounted."""
    result = _run(["mount"], capture=True)
    return mount_path in result.stdout


def is_lv_mounted(vg_name: str, lv_name: str) -> bool:
    """Check if an LV device is currently mounted."""
    result = _run(["mount"], capture=True)
    return f"/dev/{vg_name}/{lv_name}" in result.stdout or f"/dev/mapper/{vg_name}-{lv_name}" in result.stdout


# ── Docker ────────────────────────────────────────────────────

def docker_build_mock(image_name: str) -> None:
    """Build a minimal mock Docker image."""
    dockerfile = """FROM alpine:latest
RUN apk add --no-cache bash
CMD ["/bin/bash"]
"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        df_path = os.path.join(tmpdir, "Dockerfile")
        with open(df_path, "w") as f:
            f.write(dockerfile)
        _run(["docker", "build", "-t", image_name, tmpdir])
    logger.info("Mock Docker image '%s' built", image_name)


def docker_image_exists(image_name: str) -> bool:
    """Check if a Docker image exists."""
    result = _run(["docker", "image", "inspect", image_name], check=False, capture=True)
    return result.returncode == 0


def docker_run(name: str, mount_path: str, volume_dest: str, image: str, uid: int | None = None, gid: int | None = None) -> None:
    """Run a detached container."""
    cmd = ["docker", "run", "-d", "--name", name]
    cmd.extend(["-v", f"{mount_path}:{volume_dest}"])
    if uid is not None and gid is not None:
        cmd.extend(["-u", f"{uid}:{gid}"])
    cmd.extend(["--entrypoint", "/bin/sh"])
    cmd.append(image)
    cmd.extend(["-c", "tail -f /dev/null"])
    _run(cmd)
    logger.info("Container '%s' started", name)


def docker_exec(name: str, command: str, interactive: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Execute a command in a running container."""
    cmd = ["docker", "exec"]
    if interactive:
        cmd.append("-it")
    cmd.extend([name, "/bin/sh", "-c", command])
    return _run(cmd, capture=not interactive, check=check)


def docker_rm(name: str) -> None:
    """Force remove a container."""
    _run(["docker", "rm", "-f", name], check=False)
    logger.info("Container '%s' removed", name)


def docker_container_exists(name: str) -> bool:
    """Check if a container exists (running or stopped)."""
    result = _run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"], check=False, capture=True)
    return name in result.stdout.strip()


# ── Mock operations ──────────────────────────────────────────

def mock_populate_base(mount_path: str) -> None:
    """Populate base LV with mock content in mock mode."""
    build_dir = os.path.join(mount_path, "build")
    out_dir = os.path.join(mount_path, "out")
    os.makedirs(build_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    envsetup_path = os.path.join(build_dir, "envsetup.sh")
    with open(envsetup_path, "w") as f:
        f.write('#!/bin/bash\necho "mock env setup"\n')
    os.chmod(envsetup_path, 0o755)

    # Create a ~50MB mock_system.img
    mock_img = os.path.join(out_dir, "mock_system.img")
    _sudo_run(["fallocate", "-l", "50M", mock_img])
    logger.info("Mock base populated at %s", mount_path)


def mock_compile(mount_path: str) -> None:
    """Simulate compilation in mock mode."""
    out_dir = os.path.join(mount_path, "out")
    os.makedirs(out_dir, exist_ok=True)
    mock_img = os.path.join(out_dir, "mock_system.img")
    if not os.path.exists(mock_img):
        _sudo_run(["fallocate", "-l", "50M", mock_img])
    logger.info("Mock compile done at %s", mount_path)
