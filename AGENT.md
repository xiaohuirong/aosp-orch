# AOSP 块设备编译容器编排器 —— 项目文档

## 一、 项目概述

基于 Python + Click 的 CLI 编排器，使用 LVM Thin-Pool 块设备快照为 AOSP 并行开发提供物理隔离的工作区。每个工作区对应一个快照卷，享有原生 NVMe 性能，且仅为差异块付费。

**双模引擎：** Mock 模式（轻量测试）和 Prod 模式（真实 AOSP）。

---

## 二、 工程结构

```
aosp-env/
├── AGENT.md              # 本文档（项目上下文）
├── config.toml           # 标准配置文件
├── src/
│   ├── main.py           # CLI 入口（Click），所有命令
│   └── storage.py        # 底层 LVM/Loop/Mount/Docker 操作封装
└── test_orchestrator.py  # E2E 自动化测试（9 个用例）
```

---

## 三、 配置文件 (`config.toml`)

配置路径优先级：`--config` 参数 > `AOSP_ORCH_CONFIG` 环境变量 > `~/.config/aosp-orch/config.toml`

配置修改必须原子化（先写 `.tmp` 再 `rename`）。

```toml
[global]
version = "3.2.0"
mode = "mock"
pool_image_path = "/aosp_pool.img"
pool_image_size_gb = 2
lvm_vg_name = "vgaosp_pool"
thin_pool_name = "aosp_thin_pool"

[[base_projects]]
name = "xxx"
repo_url = "https://github.com/mock/manifest.git"
repo_branch = "main"
docker_image = "aosp-builder:mock"
base_lv_name = "xxx_base_lv"
base_lv_size_gb = 1
base_mount_path = "/tmp/aosp_workspaces/xxx/base_mount"

[base_projects.build_config]
setup_commands = ["source build/envsetup.sh", "lunch mock_target-eng"]
compile_command = "m -j$(nproc)"

[base_projects.build_config.env_vars]
USE_CCACHE = "1"
```

**TOML 格式要点：** `base_projects` 必须用 `[[base_projects]]` 数组项语法，嵌套子表用 `[base_projects.build_config]`，不能用内联数组 `base_projects = [...]`。代码中使用 `tomlkit.aot()` 而非 `[]` 初始化。

---

## 四、 命令参考

### 全局选项

```
--config PATH    指定配置文件路径
```

### `init` —— 初始化全局配置

交互式或全参数初始化 `[global]` 配置。

```bash
# 交互式
python3 src/main.py init

# 全参数
python3 src/main.py init --mode mock --pool-image-path /aosp_pool.img \
  --pool-image-size-gb 2 --lvm-vg-name vgaosp_pool --thin-pool-name aosp_thin_pool
```

### `link` —— 配置 base project（仅写配置，不触发 LVM）

交互式或全参数配置 base project。**纯元数据操作**，不创建任何 LVM 资源。LVM 操作延迟到 `activate`/`sync`/`compile` 时懒加载执行。

```bash
# 交互式
python3 src/main.py link

# 全参数
python3 src/main.py link --name xxx --repo-url ... --repo-branch main \
  --docker-image aosp-builder:mock --base-lv-size-gb 1 \
  --base-mount-path /tmp/aosp_workspaces/xxx/base_mount
```

### `create` —— 创建工作区（纯元数据）

```bash
python3 src/main.py create <workspace_name> --base <project_name>
```

### `activate` —— 激活工作区（懒加载）

触发懒加载：若 base LV 不存在则自动创建池、VG、LV、格式化、填充内容。然后创建快照、挂载、启动容器。

```bash
python3 src/main.py activate <workspace_name>
```

### `enter` —— 进入工作区容器

```bash
python3 src/main.py enter <workspace_name>
```

### `deactivate` —— 去激活工作区

停容器、卸载，数据保留在快照卷中。

```bash
python3 src/main.py deactivate <workspace_name>
```

### `remove` —— 彻底销毁工作区

去激活 + 销毁快照卷 + 从配置中移除。

```bash
python3 src/main.py remove <workspace_name>
```

### `sync` —— 基底强制更新（清盘流）

销毁所有子工作区，重新拉取/编译基底。

```bash
python3 src/main.py sync --base <project_name>
```

### `compile` —— 别名，等同 `sync`

```bash
python3 src/main.py compile --base <project_name>
```

---

## 五、 懒加载机制

`link` 只写配置，不触发任何 LVM/Docker 操作。实际基础设施在需要时按需创建：

- **`activate`** → 发现 base LV 不存在 → 自动创建池/VG/LV/格式化/填充 → 创建快照 → 挂载 → 启动容器
- **`sync`** → 同样懒加载确保基础设施就绪 → 清盘 → 重新填充
- **`compile`** → `sync` 的别名

使用 `.aosp_base_initialized` 标记文件判断 base LV 是否已首次填充。

---

## 六、 关键技术细节

### LVM Thin Snapshot 激活

Thin snapshot 默认带 `activation skip` 标志（`k` 属性），必须用 `lvchange -K -ay` 才能激活，否则 mount 报 `Can't lookup blockdev` 错误。

### 权限处理

- 所有 LVM/mount 操作通过 `sudo` 执行
- `sudo mount` 后目录属 root，需 `sudo chown -R` 修复权限
- Pool image 由 `sudo fallocate` 创建，清理时需 `sudo rm`

### 配置验证

每个命令执行前应检查配置文件的正确性（`[global]` 必需字段、`base_projects` 结构等）。

---

## 七、 测试

```bash
pytest test_orchestrator.py -v    # 必须输出 9 passed
```

### 9 个测试用例

| 类 | 用例 | 验证内容 |
|---|---|---|
| 断言1 | `test_link_writes_config` | link 写入配置 + TOML 格式正确（`[[base_projects]]` 嵌套子表） |
| 断言1 | `test_activate_creates_pool_image` | activate 懒加载创建 pool image |
| 断言1 | `test_activate_creates_vg_and_base_lv_with_mock_output` | activate 懒加载创建 VG + base LV 含 mock 产物 |
| 断言2 | `test_workspace_isolation` | 工作区 a 写入的文件在 b 中不可见（块设备级物理隔离） |
| 断言3 | `test_deactivate_unmounts_and_removes_container` | deactivate 后快照已卸载、容器已删除、状态为 inactive |
| 断言4 | `test_sync_destroys_workspaces_and_refreshes_base` | sync 销毁所有快照 + 重新 activate 后懒加载重建且干净 |
| 空间 | `test_snapshot_data_percent_is_low` | 新快照 data_percent 低（共享基座） |
| 空间 | `test_snapshot_only_stores_deltas` | 写入后 data_percent 增长（仅存增量） |
| 空间 | `test_multiple_snapshots_share_base` | 多快照共享基座数据 |

### 测试环境 Mock

- Docker 镜像：`alpine:latest` + bash，命名为 `aosp-builder:mock`
- Pool 大小：2GB（测试用）
- Base LV 大小：1GB
- 挂载路径：`/tmp/aosp_workspaces/`
