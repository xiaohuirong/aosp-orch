# AOSP 块设备编译容器编排器 —— 项目文档

## 一、 项目概述

基于 Python + Click 的 CLI 编排器，使用 LVM Thin-Pool 块设备快照为 AOSP 并行开发提供物理隔离的工作区。每个工作区对应一个快照卷，享有原生 NVMe 性能，且仅为差异块付费。

**双模引擎：** Mock 模式（轻量测试）和 Prod 模式（真实 AOSP）。

---

## 二、 工程结构

```
aosp-env/
├── AGENT.md              # 本文档（项目上下文）
├── config.yaml           # YAML 配置文件
├── pyproject.toml        # Python 包配置（依赖、入口点）
├── aosp_orch/
│   ├── __init__.py       # 包初始化（版本号）
│   ├── __main__.py      # 支持 python -m aosp_orch
│   ├── main.py           # CLI 入口（Click），所有命令
│   └── storage.py        # 底层 LVM/Loop/Mount/Docker 操作封装
└── test_orchestrator.py  # E2E 自动化测试（10 个用例）
```

---

## 三、 配置文件 (`config.yaml`)

配置路径优先级：`--config` 参数 > `AOSP_ORCH_CONFIG` 环境变量 > `~/.config/aosp-orch/config.yaml`

配置修改必须原子化（先写 `.tmp` 再 `rename`）。

```yaml
global:
  mode: mock
  workdir: /tmp/aosp_workspaces
  pool_image_size_gb: 2
  default_base: xxx

base_projects:
  - name: xxx
    repo_url: https://github.com/mock/manifest.git
    repo_branch: main
    docker_image: aosp-builder:mock
    base_lv_size_gb: 1
    build_config:
      setup_commands:
        - source build/envsetup.sh
        - lunch mock_target-eng
      compile_command: m -j$(nproc)
      env_vars:
        USE_CCACHE: "1"
    workspaces:
      - name: a
```

### 设计原则：配置文件只存用户需要关心的内容

**硬编码常量**（不存配置、不问用户）：
- `lvm_vg_name` = `vg0`
- `thin_pool_name` = `pool0`
- `pool_image_filename` = `pool.img`

**自动推导路径**（从 `workdir` + 项目名/工作区名计算，不存配置）：

| 字段 | 生成规则 | 示例（workdir=/tmp/aosp_workspaces, project=xxx） |
|---|---|---|
| pool image | `{workdir}/pool.img` | `/tmp/aosp_workspaces/pool.img` |
| base_lv_name | `{project}` | `xxx` |
| base_mount_path | `{workdir}/{project}/base_mount` | `/tmp/aosp_workspaces/xxx/base_mount` |
| snapshot_lv_name | `s-{workspace}` | `s-a` |
| workspace mount | `{workdir}/{project}/{workspace}` | `/tmp/aosp_workspaces/xxx/a` |

**workspace 状态不存配置**：active/inactive 由 `docker_container_exists` 实时判断，配置文件中 workspace 只有 `name` 字段。

---

## 四、 命令参考

### 全局选项

```
--config PATH    指定配置文件路径（默认 ~/.config/aosp-orch/config.yaml）
```

### `--base` 参数与 `default_base` 机制

所有需要指定 base project 的命令都支持 `--base` 参数。`--base` 为可选参数，解析优先级：

1. **`--base` 显式指定** → 使用指定值
2. **`global.default_base`** → 使用配置中的默认项目
3. **两者都没有** → 报错退出，提示用户使用 `--base` 或先 `add` 一个项目并设为默认

`add` 命令交互模式下会询问"是否将此项目设为默认?"（默认 Y），确认后写入 `global.default_base`。

### `workspace_name` 参数与 base LV 操作

`mount`、`unmount`、`activate`、`deactivate`、`enter` 这 5 个命令的 `workspace_name` 参数为**可选**：

- **指定 workspace_name** → 操作该 workspace 的快照卷
- **不指定 workspace_name** → 操作 base LV 本身（挂载到 `base_mount`，容器名为 `{project}_default`）

`new` 和 `del` 的 `workspace_name` 仍为必填，因为它们只针对 workspace 操作。

### `init` —— 初始化全局配置 + 创建 LVM 磁盘

交互式或全参数初始化 `global` 配置，并**立即创建** LVM 磁盘基础设施：pool image、loop device、VG、thin pool。设置 `mode`、`workdir`、`pool_image_size_gb`。

```bash
# 交互式
aosp-orch init

# 全参数
aosp-orch init --mode mock --workdir /tmp/aosp_workspaces --pool-image-size-gb 2
```

### `add` —— 添加 base project 并创建 base LV

交互式或全参数配置 base project，并**立即创建** base LV：格式化 + 填充内容。支持 `--sync-type` 选择 repo/git 同步方式。

前提：LVM 磁盘已通过 `init` 创建。

交互模式下会询问是否设为默认项目（写入 `global.default_base`）。

若 base LV 已存在，检查是否已填充（通过 `.aosp_base_initialized` 标记），未填充则自动填充。

```bash
# 交互式
aosp-orch add

# 全参数
aosp-orch add --name xxx --repo-url ... --repo-branch main \
  --docker-image aosp-builder:mock --base-lv-size-gb 1 --sync-type repo
```

### `remove` —— 删除 base project（销毁所有资源 + 删除配置）

销毁该 base project 下所有 workspace（停容器 + 卸载 + 删快照）、销毁 base LV、从配置中移除。若该项目是 `default_base`，自动清除。需二次确认（默认 N）。

`base_name` 为可选位置参数：不指定则使用 `global.default_base`，指定则操作该项目。

```bash
aosp-orch remove xxx
aosp-orch remove              # 使用 default_base
```

### `default` —— 查看/设置/清空 default base project

管理 `global.default_base` 配置。无参数时清空 default base，指定项目名时设为 default base（需项目已通过 `add` 添加）。

```bash
aosp-orch default xxx         # 将 xxx 设为 default base
aosp-orch default             # 清空 default base
```

### `new` —— 创建工作区（写配置 + 创建快照 LV）

在配置文件中添加 workspace 条目，并**立即创建**快照 LV。幂等：若 workspace 已存在于配置中但快照 LV 不存在，仅创建快照。

前提：base LV 已通过 `add` 创建。

```bash
aosp-orch new <workspace_name> --base <project_name>
```

### `mount` —— 挂载（不启动容器）

激活 LV + 挂载 + 修复权限，**不启动容器**。适合仅需访问文件系统的场景。

前提：LV 已存在（base LV 通过 `add`，快照通过 `new`）。

```bash
# 挂载 workspace 快照
aosp-orch mount <workspace_name> --base <project_name>

# 挂载 base LV
aosp-orch mount --base <project_name>
```

### `unmount` —— 卸载（不停止容器）

卸载快照/base LV，**不停止容器**。适合需要保持容器运行但释放挂载点的场景。

```bash
# 卸载 workspace 快照
aosp-orch unmount <workspace_name> --base <project_name>

# 卸载 base LV
aosp-orch unmount --base <project_name>
```

### `activate` —— 激活（mount + 启动容器）

挂载 + 启动容器。不指定 workspace 时激活 base LV（挂载 + 启动 default 容器）。

前提：workspace 已 `new`，base LV 已 `add`。

```bash
# 激活 workspace
aosp-orch activate <workspace_name> --base <project_name>

# 激活 base LV（挂载 + 启动 default 容器）
aosp-orch activate --base <project_name>
```

### `enter` —— 进入容器

进入容器交互 Shell。容器未激活时询问是否自动激活（默认 Y）。

前提：workspace 已 `new`，base LV 已 `add`。

```bash
# 进入 workspace 容器
aosp-orch enter <workspace_name> --base <project_name>

# 进入 base LV 的 default 容器
aosp-orch enter --base <project_name>
```

### `deactivate` —— 去激活（停容器 + 卸载）

停容器、卸载，数据保留在快照卷/base LV 中。幂等操作（已 inactive 时安全返回）。

```bash
# 去激活 workspace
aosp-orch deactivate <workspace_name> --base <project_name>

# 去激活 base LV（停 default 容器 + 卸载）
aosp-orch deactivate --base <project_name>
```

### `del` —— 彻底销毁工作区

去激活 + 销毁快照卷 + 从配置中移除。

```bash
aosp-orch del <workspace_name> --base <project_name>
```

### `sync` —— 基底强制更新（清盘流）

销毁所有子工作区的快照卷和容器，重新拉取/编译基底。**保留配置文件中的 workspace 条目**，下次需先 `new` 重建快照再 `activate`。支持 repo/git 两种同步方式。

前提：base LV 已通过 `add` 创建。

```bash
aosp-orch sync --base <project_name>
```

### `compile` —— 别名，等同 `sync`

```bash
aosp-orch compile --base <project_name>
```

### `rebase` —— 删除 workspace 快照（保留配置和 base LV）

销毁指定 workspace 的快照卷和容器，**保留配置条目和 base LV**。下次 `new` 可重建快照。

`workspace_name` 为可选：不指定则删除该 base 下所有 workspace。操作需二次确认（默认 N）。

```bash
# 删除指定 workspace
aosp-orch rebase <workspace_name> --base <project_name>

# 删除所有 workspace
aosp-orch rebase --base <project_name>
```

### `destroy` —— 销毁所有资源并删除配置

彻底销毁所有资源：所有容器、LV、VG、loop device、pool image、工作目录，最后删除配置文件。需二次确认（默认 N）。

销毁顺序：`_destroy_lvm_infrastructure`（停容器+卸载+删 LV+删 VG+解绑 loop device）→ 删 pool image → 删工作目录 → 删配置文件。

```bash
aosp-orch destroy
```

---

## 五、 命令职责与复用

每个命令有明确职责，无懒加载。用户需按正确顺序调用命令：

- **`init`** → 创建 LVM 磁盘基础设施（pool image/loop device/VG/thin pool）
- **`add`** → 创建 base LV + 格式化 + 填充内容（前提：`init` 已创建磁盘）
- **`new`** → 创建快照 LV（前提：base LV 已通过 `add` 创建）
- **`mount`** → 激活 LV + 挂载 + 修复权限（前提：LV 已存在）
- **`activate`** = `mount` + 启动容器
- **`deactivate`** = 停容器 + `unmount`
- **`sync`** = `_destroy_all_workspaces` + 重新填充 base LV
- **`rebase`** = 删除指定/所有 workspace 快照（保留配置，需确认）
- **`remove`** = 删除 base project（位置参数，需确认）
- **`default`** = 设置/清空 default_base
- **`compile`** → `sync` 的别名

使用 `.aosp_base_initialized` 标记文件判断 base LV 是否已首次填充。

### 命令复用关系

```
activate  = mount + _start_container
deactivate = docker_rm + unmount
del       = deactivate + lvremove + 删配置
rebase    = _destroy_workspace（保留配置，需确认）
sync      = _destroy_all_workspaces + _populate_base(force=True)
remove    = _destroy_all_workspaces + lvremove(base) + 删配置
destroy   = _destroy_lvm_infrastructure + 删 pool image + 删 workdir + 删配置
```

`_destroy_workspace` 是内部函数，执行停容器 + 卸载 + 删快照（不删配置）。`_destroy_all_workspaces` 遍历所有 workspace 调用 `_destroy_workspace`，被 `sync`、`rebase`（不指定 workspace 时）和 `remove` 复用。

---

## 六、 关键技术细节

### 硬编码常量与自动推导

LVM 卷组名、精简池名、所有 LV 名和挂载路径均由代码自动推导，不写入配置文件，减轻用户阅读负担。参见第三节"自动推导路径"表。

### workspace 状态判断

workspace 的 active/inactive 状态**不存储在配置文件中**，而是通过 `docker_container_exists(container_name)` 实时判断。容器存在 = active，容器不存在 = inactive。这避免了配置与实际状态不同步的问题。

### LVM Thin Snapshot 激活

Thin snapshot 默认带 `activation skip` 标志（`k` 属性），必须用 `lvchange -K -ay` 才能激活，否则 mount 报 `Can't lookup blockdev` 错误。

### Docker 容器持久化

容器使用 `--entrypoint /bin/sh -c "tail -f /dev/null"` 保持后台运行，而非依赖镜像默认 CMD。`docker_exec` 使用 `/bin/sh`（非 `/bin/bash`）以兼容 Alpine 镜像。

### Git 同步模式

`sync_type: git` 时，代码同步到容器内 `/{project_name}/git-repo` 子目录（非挂载点根目录）：
- **首次**：`git init` + `git remote add` + `git fetch` + `git checkout`
- **已有 .git**：`git pull`；失败时询问用户是否清除重建
- **挂载点根目录**保持干净，避免 git clone 到已存在目录的冲突

### 权限处理

- 所有 LVM/mount 操作通过 `sudo` 执行
- `sudo mount` 后目录属 root，需 `sudo chown -R` 修复权限
- Pool image 由 `sudo fallocate` 创建，清理时需 `sudo rm`

### 配置验证

每个命令执行前应检查配置文件的正确性（`global` 必需字段、`base_projects` 结构等）。workspace 只需 `name` 字段，不再需要 `status`。

### sync_type 支持

base_project 支持 `sync_type` 字段：`repo`（默认）或 `git`。
- repo: `repo init -u <url> -b <branch>` + `repo sync`
- git: `git init` + `git remote add origin <url>` + `git fetch` + `git checkout -b <branch> origin/<branch>`

### sync 不删除配置

`sync` 命令只销毁快照卷和容器（物理资源），**保留配置文件中的 workspace 条目**。用户下次需先 `new` 重建快照再 `activate`，无需重新在配置中添加 workspace。

---

## 七、 测试

```bash
python3 -m pytest test_orchestrator.py -v    # 必须输出 10 passed
```

### 测试架构

测试使用 **pytest `tmp_path` fixture** 为每个测试创建独立的临时配置文件，不同测试使用不同的 workdir（`/tmp/aosp_test_mock` 和 `/tmp/aosp_test_prod`），实现完全隔离。

- Mock 测试：workdir = `/tmp/aosp_test_mock`，项目名 `xxx`，Docker 镜像 `aosp-builder:mock`
- Prod/Git 测试：workdir = `/tmp/aosp_test_prod`，项目名 `aosp`，Docker 镜像 `alpine/git`，sync_type = git

### 10 个测试用例

| 类 | 用例 | 验证内容 |
|---|---|---|
| 断言1 | `test_link_writes_config_and_creates_infrastructure` | add 写入配置 + 创建 LVM 基础设施 |
| 断言1 | `test_link_creates_base_lv_with_mock_output` | add 创建 base LV 含 mock 产物 |
| 断言2 | `test_workspace_isolation` | 工作区 a 写入的文件在 b 中不可见（块设备级物理隔离） |
| 断言3 | `test_deactivate_unmounts_and_removes_container` | deactivate 后快照已卸载、容器已删除 |
| 断言4 | `test_sync_destroys_workspaces_and_refreshes_base` | sync 销毁所有快照 + 配置保留 + 需重新 new 再 activate |
| 空间 | `test_snapshot_data_percent_is_low` | 新快照 data_percent 低（共享基座） |
| 空间 | `test_snapshot_only_stores_deltas` | 写入后 data_percent 增长（仅存增量） |
| 空间 | `test_multiple_snapshots_share_base` | 多快照共享基座数据 |
| Git | `test_git_clone_to_git_repo_dir` | git sync 模式下 clone 到 /{project}/git-repo 子目录 |
| Git | `test_git_pull_on_reactivate` | 重新 activate 时 git pull 而非重新 clone |

### 测试环境 Mock

- Docker 镜像：`alpine:latest` + bash，命名为 `aosp-builder:mock`
- Pool 大小：2GB（测试用）
- Base LV 大小：1GB
- Mock 工作目录：`/tmp/aosp_test_mock/`
- Prod 工作目录：`/tmp/aosp_test_prod/`

### 测试清理

`_force_cleanup_all()` 清理策略（每个测试前后自动执行）：

1. **容器清理**：删除所有 `aosp_` 前缀的 Docker 容器
2. **卸载**：先按 workdir 和 VG 设备路径匹配卸载，再 `umount -l` 懒卸载残留
3. **dmsetup 强制清理**：`dmsetup remove --force` 清除 device mapper 条目，解决 LV "filesystem in use" 问题
4. **LV 清理**：先 `lvchange -an` 去激活所有 LV，再 `lvremove -ff -y` 强制删除（快照优先于 base LV）
5. **VG/PV 清理**：`vgremove -ff -y`
6. **Loop 设备**：`losetup -d` 解绑
7. **文件清理**：`sudo rm -rf` 删除 pool image 和 workdir

关键：必须先 `dmsetup remove --force` + `lvchange -an` 再 `lvremove`，否则 base LV 因 "filesystem in use" 无法删除。

---

## 八、 已解决的问题

### 容器启动失败（Alpine 兼容性）

`alpine/git` 镜像无 `/bin/bash`，`docker_exec` 改用 `/bin/sh`。容器持久化改用 `--entrypoint /bin/sh -c "tail -f /dev/null"`。

### git clone 到已存在目录

git clone 目标目录已存在时会失败。解决方案：clone 到 `/{project_name}/git-repo` 子目录而非挂载点根目录。

### workspace 状态不同步

原方案将 `status: active/inactive` 存入配置文件，容易与实际状态不一致。改为通过 `docker_container_exists()` 实时判断，配置文件中 workspace 只保留 `name` 字段。

### 测试清理 "filesystem in use"

`lvremove` 报 "Logical volume contains a filesystem in use"，原因是 device mapper 条目未清除。解决：清理时先 `dmsetup remove --force` 清除 DM 条目，再 `lvchange -an` 去激活，最后 `lvremove -ff -y` 强制删除。

### 测试间 VG 残留

不同测试共享同一个 VG 名 `vgaosp_pool`，若前一个测试清理不彻底会导致 `vgcreate` 报 "already exists"。解决：使用 `tmp_path` fixture 为每个测试创建独立配置，不同测试使用不同 workdir；清理函数覆盖所有 workdir。

### default_base 机制

多个 base project 时每次都要传 `--base` 很繁琐。解决：在 `global` 中增加 `default_base` 字段，`add` 交互时询问是否设为默认，`--base` 参数改为可选，未指定时自动使用 `default_base`。

### mount/unmount 细粒度控制

原 `activate`/`deactivate` 将挂载和容器绑定在一起，无法单独操作。解决：拆出 `mount`/`unmount` 命令，`activate` = `mount` + 启动容器，`deactivate` = 停容器 + `unmount`。

### 无 workspace 时操作 base LV

用户经常需要直接操作 base LV（查看源码、手动编译等），之前必须创建一个 workspace。解决：`mount`/`unmount`/`activate`/`deactivate`/`enter` 的 `workspace_name` 改为可选，不传时操作 base LV 本身，容器名为 `{project}_default`。

### init 创建 LVM 磁盘

原方案中 `init` 只写配置文件，LVM 磁盘（pool image/loop device/VG/thin pool）的创建延迟到 `add` 中执行。这导致 `add` 职责过重，且用户无法在 `add` 之前确认磁盘基础设施是否就绪。解决：将 LVM 磁盘创建移到 `init` 命令中，`init` 保存配置后立即调用 `_ensure_pool_and_vg()` 创建磁盘基础设施。`add` 只负责创建 base LV + 格式化 + 填充内容。典型工作流变为 `init` → `add` → `new` → `activate`。

### 独立分发的 CLI 工具

原方案为裸脚本调用（`python3 src/main.py`），依赖 `sys.path.insert` hack 导入模块，无法直接分发。解决：重构为标准 Python 包：

- `src/` → `aosp_orch/`，添加 `__init__.py`、`__main__.py`
- `from storage import` → `from .storage import`（相对导入）
- 新增 `pyproject.toml`，声明依赖（`click`、`pyyaml`）和入口点 `aosp-orch = "aosp_orch.main:cli"`
- 安装后直接使用 `aosp-orch` 命令，也支持 `python -m aosp_orch`

安装方式：
```bash
pip install -e .          # 开发模式
pip install .             # 正式安装
pip install aosp-orch     # 从 PyPI（未来）
```

### init 覆盖已有 pool image

`init` 创建 LVM 磁盘前会检查 pool image 是否已存在。若存在，询问用户是否覆盖（默认 N）。覆盖时先调用 `_destroy_lvm_infrastructure()` 销毁所有容器、卸载、删除所有 LV/VG/loop device，再删除 pool image 文件，最后重新创建。不覆盖则跳过磁盘创建，保留现有数据。

`_destroy_lvm_infrastructure()` 是内部函数，遍历所有 base project 执行停容器 + 卸载 + 删 LV，然后删除 VG 和 loop device。被 `init` 覆盖流程复用。

### enter 自动激活

`enter` 命令在容器未激活时不再直接报错退出，而是询问用户"是否立即激活?"（默认 Y）。确认后自动调用 `activate` 激活，再进入容器 Shell。对 base LV 和 workspace 均适用。

### rebase 命令

`rebase` 用于删除 workspace 快照但保留配置条目，方便下次 `new` 重建。与 `sync` 和 `del` 的区别：

- **`rebase`**：只删快照 + 保留配置 + 保留 base LV + 需确认
- **`sync`**：删快照 + 保留配置 + 重新填充 base LV（不删 base LV）
- **`del`**：删快照 + 删配置（针对单个 workspace）

`rebase` 支持可选 `workspace_name`：不指定则删除所有 workspace 快照（需确认），指定则只删除该 workspace 快照（需确认）。确认默认为 N，防止误操作。

### 命令重命名

为使命令更简洁直观，进行了以下重命名：

- `link` → `add`：添加 base project
- `unlink` → `remove`：删除 base project
- `create` → `new`：创建 workspace
- `remove` → `del`：删除 workspace

注意：`del` 和 `new` 是 Python 关键字/内置函数，不能直接用作函数名。实现中使用 Click 的 `@cli.command("del")` 指定命令名，函数名改为 `del_cmd` / `new_cmd`。

### enter 自动创建

`enter` 命令在 workspace 不存在时不再直接报错退出，而是询问用户"是否立即创建?"（默认 Y）。确认后自动调用 `new` 创建 + `activate` 激活，再进入容器 Shell。

### remove 使用位置参数 + 二次确认

`remove` 命令改用位置参数代替 `--base` 选项。不传参数时使用 `global.default_base`，传参则操作指定项目。由于该操作会销毁所有数据，添加了二次确认（默认 N）。

### default 命令

新增 `default` 命令用于管理 `global.default_base` 配置。无参数时清空 default base，指定项目名时设为 default base（需项目已通过 `add` 添加）。这比之前只能在 `add` 交互模式下设置更灵活，用户可以随时切换或清空默认项目。

---

## 九、 代码重构与优化

### 公共函数提取

#### `_resolve_bp(ctx, base) -> (config, bp, base)`

9 个命令（`new`、`mount`、`unmount`、`activate`、`enter`、`deactivate`、`del`、`sync`、`rebase`）都重复了"加载配置 → 解析 base → 查找项目 → 不存在则退出"的样板代码。提取为 `_resolve_bp` 统一处理，返回三元组 `(config, bp, base)`。**注意返回值包含解析后的 `base`**（原始参数可能是 `None`，解析后是 `default_base` 的值），调用者必须使用返回的 `base` 而非原始参数。

#### `_mount_lv(lv_name, mount_path)`

`mount_cmd` 中 base LV 和 workspace 分支都重复 activate+mount+chown 逻辑。提取为 `_mount_lv(lv_name, mount_path)` 统一处理。

#### `_start_container(c_name, mount_path, project_name, docker_image)`

`activate` 中 base 和 workspace 分支都重复 docker_rm+docker_run 逻辑。提取为 `_start_container` 统一处理。

#### `_destroy_workspace(config, base, ws_name)`

提取单个 workspace 的停容器+卸载+删快照逻辑。`_destroy_all_workspaces` 内部循环调用它，`rebase` 单 workspace 分支也直接调用，消除了重复的 docker_rm/umount/remove_lv 逻辑。

### `sync` 复用 `_populate_base`

`sync` 命令中 ~40 行的 mock/prod 分支填充逻辑与 `_populate_base` 几乎完全重复。给 `_populate_base` 加了 `force=True` 参数（先删 marker 再填充），`sync` 直接调用 `_populate_base(config, bp, force=True)` 即可。同时 `_populate_base` 增加了幂等性：挂载前检查 `is_lv_mounted`，卸载前检查 `is_lv_mounted`。

### `compile` 命令简化

`compile` 命令不再冗余地加载配置和解析 base（`sync` 自己会做），直接 `ctx.invoke(sync, base=base)`。函数名改为 `compile_cmd`（避免与内置函数冲突）。

### `remove_cmd` 复用 `_resolve_base`

`remove_cmd` 使用位置参数 `base_name` 而非 `--base`，但解析逻辑与 `_resolve_base` 一致。简化为 `base = base_name or _resolve_base(config, None)`。

### 死代码清理

- **storage.py**: 删除未使用的 `deactivate_lv` 和 `mock_compile` 函数（整个项目中无任何调用者）
- **main.py**: 从 import 中移除 `deactivate_lv`、`get_lv_data_percent`、`get_lv_size_info`、`mock_compile`（`get_lv_data_percent` 和 `get_lv_size_info` 仍被测试文件直接从 storage 导入使用）

### `remove_lv` 自动去激活

`lvremove` 在 LV 仍处于激活状态时会报 "Logical volume contains a filesystem in use" 错误。修改 `remove_lv` 在删除前先执行 `lvchange -an`（`check=False` 确保已 inactive 时也不报错），再执行 `lvremove`。

### `new` 命令自动创建 base LV

`new` 命令发现 base LV 不存在时，不再报错退出或调 `add_cmd`（会进入交互模式），而是直接用已有配置自动创建：`_ensure_pool_and_vg` + `create_thin_lv` + `format_ext4` + `_populate_base`。这样 `enter` 的自动创建链路（enter → new → 自动创建 base LV）可以无交互完成。

### 旧命令名清理

所有提示信息、注释、docstring 中的旧命令名已统一替换：
- `link` → `add`（提示信息、注释）
- `unlink` → `remove`（注释）
- `create` → `new`（提示信息、docstring、输出消息）

### `destroy` 命令

新增 `destroy` 命令，一键销毁所有资源并删除配置文件。销毁顺序：`_destroy_lvm_infrastructure`（停所有容器 + 卸载 + 删所有 LV + 删 VG + 解绑 loop device）→ 删 pool image → 删工作目录 → 删配置文件。需二次确认（默认 N），确认前显示将销毁的内容清单。
