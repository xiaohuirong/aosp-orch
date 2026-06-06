为了让 AI 编码工具（如 Claude、GPT-4 或 Cline/Cursor 等 Agent）能够**独立、安全地实现这个项目并完成闭环验证**，这份需求文档（PRD）引入了“双模引擎”设计：

1. **测试/验证模式（Mock 模式）：** 使用几百兆的轻量级镜像和空目录模拟 AOSP 编译和 `repo` 树，让 AI 可以在几秒钟内完成端到端的自动化测试，大幅节省硬盘和测试时间。
2. **生产模式（Prod 模式）：** 切换为全量 AOSP、庞大逻辑卷和真实的 Docker 镜像。

以下是为你整理的、面向 AI 闭环开发的《AOSP 块设备编译容器编排器需求文档（PRD）》。

---

# AOSP 块设备编译容器编排器 —— 需求文档 (PRD)

## 一、 项目概述

### 1.1 背景与痛点

在 AI 密集参与 AOSP 开发的背景下，多个 AI Agent 并行开发会导致代码相互污染。采用传统的物理复制（Copy）或 OverlayFS 架构，面临海量小文件 Copy-Up 带来的 **I/O 性能雪崩**、**磁盘空间暴涨**、以及**文件系统权限乱象**。

### 1.2 核心解决方案

本工具是一个基于 Python 开发的 CLI 编排器。它抛弃了文件系统级联合挂载，**统一使用内核级块设备快照（LVM Thin-Pool）管理一切**。
源码与编译产物（`out/`）无缝融为一体。每个动态工作区（Workspace）对应一个物理隔离的轻量化快照卷，互不干扰，享有 $95\%+$ 的原生 NVMe 性能，且仅为代码/编译产物的“差异块”买单。

---

## 二、 核心架构与实体定义

系统完全基于一个在宿主机 `ext4` 根目录下虚拟出来的 `aosp_pool.img` 镜像文件运行（LVM-on-File），其内部划分为两层：

1. **Base 卷 (`xxx_base_lv`):** 黄金基底逻辑卷。包含最干净的源码和由 `default` 环境完成的首次全局整编产物。
2. **Workspace 快照卷 (`<a>_snapshot_lv`):** 激活工作区时秒级派生的快照卷。**源码与编译差异统统以增量块形式写入此卷。**

---

## 三、 核心配置协议设计 (`config.toml`)

AI 编写的程序必须保证对该配置文件的修改是**原子化**的（先写 `.tmp` 文件，再通过系统调用 `rename` 覆盖），以防并发操作导致配置损坏。

```toml
[global]
version = "3.2.0"
mode = "mock"                               # 运行模式: mock (测试) | prod (生产)
pool_image_path = "/aosp_pool.img"           # LVM-on-File 镜像物理路径
pool_image_size_gb = 10                      # 存储池大小 (Mock模式默认为10, Prod模式建议400+)
lvm_vg_name = "vgaosp_pool"                 # 虚拟卷组名称
thin_pool_name = "aosp_thin_pool"            # LVM精简配置池名称

[[base_projects]]
name = "xxx"
repo_url = "https://github.com/mock/manifest.git" # 远程清单仓库地址
repo_branch = "main"
docker_image = "aosp-builder:mock"
base_lv_name = "xxx_base_lv"                 # 基底逻辑卷名称
base_lv_size_gb = 5                         # 基底卷大小限制
base_mount_path = "/home/developer/aosp_workspaces/xxx/base_mount"

    [base_projects.build_config]
    setup_commands = [
        "source build/envsetup.sh",
        "lunch mock_target-eng"
    ]
    compile_command = "m -j$(nproc)"
    env_vars = { USE_CCACHE = "1" }

    [[base_projects.workspaces]]
    name = "a"
    status = "inactive"                      # active | inactive
    snapshot_lv_name = "a_snapshot_lv"       # 工作区专属的快照卷
    mount_path = "/home/developer/aosp_workspaces/xxx/a"

```

---

## 四、 核心命令与底层系统行为规范

> ⚠️ **权限前置要求：** 本工具内的 LVM/Mount 操作需要 Root 权限。AI 在编写代码时，执行系统命令（`subprocess`）前必须能够根据配置自动补全 `sudo`。

### 1. `link` —— 初始化与拉取基底

* **命令：** `aosp-orch link --name <name>`
* **底层动作流：**
1. 检查 TOML。若 `vgaosp_pool` 卷组不存在，则先创建 `pool_image_path`（使用 `fallocate`），通过 `losetup` 绑定为环回设备，并初始化为 LVM Thin-Pool（精简池）。
2. 在池内划分容量为 `base_lv_size_gb` 的 `xxx_base_lv` 逻辑卷，格式化为 `ext4`，并挂载到 `base_mount_path`。
3. **Mock/Prod 分流执行：**
* **Prod 模式：** 拉起真实的 Docker 镜像，在挂载点内部执行标准的 `repo init -u <url> -b <branch>` 和 `repo sync`，随后串联执行 `build_config` 中的编译命令。
* **Mock 模式：** 绕过网络。直接在挂载点内 `mkdir -p build/ out/`，并在 `build/envsetup.sh` 写入 `echo "mock env setup"`。模拟整编命令时，仅在 `out/` 目录下生成一个 50MB 的 `mock_system.img` 测试文件。


4. 卸载（`umount`）并冻结 Base 卷。



### 2. `create` —— 创建轻量工作区

* **命令：** `aosp-orch create <a> --base <xxx>`
* **底层动作流：**
1. 这是一个**纯元数据操作**。
2. 校验 `xxx` 是否存在，若存在，直接在 TOML 的 `workspaces` 数组中追加工作区 `a` 的节点信息，将 `status` 设为 `inactive`。不引发任何实际的磁盘克隆。



### 3. `activate` —— 激活工作区（懒加载机制）

* **命令：** `aosp-orch activate <a>`
* **底层动作流：**
1. **快照懒加载：** 检查系统是否存在 `/dev/vgaosp_pool/a_snapshot_lv`。若不存在，瞬间执行快照克隆命令（耗时 < 0.5 秒）：
```bash
sudo lvcreate -s --name a_snapshot_lv /dev/vgaosp_pool/xxx_base_lv

```


2. **挂载块设备：** 创建 `mount_path` 物理目录，执行 `sudo mount /dev/vgaosp_pool/a_snapshot_lv <mount_path>`。
3. **拉起长驻容器：**
```bash
docker run -d --name aosp_xxx_a \
  -v <mount_path>:/xxx \
  -u $(id -u):$(id -g) \
  <docker_image> tail -f /dev/null

```


4. 将 TOML 中该工作区的状态更新为 `active`。



### 4. `enter` —— 进入工作区交互

* **命令：** `aosp-orch enter <a>`
* **底层动作流：**
1. 直接对接到目标容器的 Shell：`docker exec -it aosp_xxx_a /bin/bash`。



### 5. `deactivate` —— 去激活工作区

* **命令：** `aosp-orch deactivate <a>`
* **底层动作流：**
1. 停止并删除容器：`docker rm -f aosp_xxx_a`。
2. 卸载块设备：`sudo umount <mount_path>`。
3. 更新 TOML 状态为 `inactive`。工作区内的所有增量代码和编译产物完好锁死在快照卷中。



### 6. `remove` —— 彻底销毁工作区

* **命令：** `aosp-orch remove <a>`
* **底层动作流：**
1. 隐式调用 `deactivate <a>` 确保环境卸载干净。
2. 物理销毁快照卷，将差异空间彻底释放回精简资源池：
```bash
sudo lvremove -f /dev/vgaosp_pool/a_snapshot_lv

```


3. 从 TOML 配置中剔除该工作区。



### 7. `sync / compile` —— 基底强制更新（强力清盘流）

* **命令：** `aosp-orch sync --base <xxx>`
* **底层动作流：**
1. **无条件清盘：** 遍历 TOML 中属于 `xxx` 的所有子工作区。无条件强制调用 `docker rm -f` 强杀容器，调用 `umount` 解挂，并运行 `lvremove -f` **批量格式化销毁所有子快照卷**，回收所有增量空间。
2. **刷新基底：** 重新挂载 Base 卷 `xxx_base_lv` 到 `base_mount_path`。
3. 启动 `default` 容器，在容器内根据模式（Mock 或 Prod）重新执行 `repo sync`，并读取 TOML 里的 `build_config` **重新进行全局整编**，更新基础产物。
4. 卸载 Base 卷。子工作区在下次调用 `activate` 时，会自动触发懒加载机制重获新生。



---

## 五、 AI 自动化自闭环测试与验证规范（核心）

为了让 AI 编码工具在没有真实 AOSP 代码的大环境下能够进行**自我重构、自我测试与自我验证**，项目必须包含一套全自动化的测试脚本 `test_orchestrator.py`。

### 5.1 测试大环境 Mock（桩函数设计）

AI 在编写自动化测试用例时，应遵循以下 Mock 规则：

1. **Docker 镜像 Mock：** 测试开始前，AI 应自动动态构建一个极小的测试镜像：
```dockerfile
FROM alpine:latest
RUN apk add --no-cache bash
CMD ["/bin/bash"]

```


将其命名为 `aosp-builder:mock`。
2. **容量压缩：** 在测试配置文件中，`pool_image_size_gb` 设为 `2`（逻辑大小），使整个测试流程在几秒内即可建立虚拟磁盘，不吃硬盘。

### 5.2 必须通过的端到端（E2E）测试断言

AI 编写完核心代码后，运行测试脚本必须完美通过以下断言链路：

1. **断言 1 (初始化验证):** 执行 `link` 后，验证 `/aosp_pool.img` 文件是否存在，验证 `vgaosp_pool` 卷组是否处于 active 状态，验证 Base 卷内是否成功生成了 Mock 编译产物文件。
2. **断言 2 (快照独立性验证):** * 创建并激活工作区 `a`，进入容器在 `/xxx/` 目录下写入一个新文件 `ai_code.txt`。
* 创建并激活工作区 `b`。
* **断言：** 读取工作区 `b` 容器内的 `/xxx/` 目录，**必须判定 `ai_code.txt` 不存在**（验证块设备级物理隔离）。


3. **断言 3 (去激活幂等性验证):** * 执行 `deactivate a`。
* **断言：** 宿主机执行 `mount | grep a_snapshot_lv` 必须返回空（验证成功卸载），且 Docker 容器列表里没有 `aosp_xxx_a`。


4. **断言 4 (强力清盘流验证):**
* 保持工作区 `b` 处于激活状态。
* 运行 `sync --base xxx`。
* **断言：** 检查 LVM 系统，`b_snapshot_lv` 必须已经被销毁（返回未找到错误），且 Base 卷的修改时间或内容已更新。
* 再次运行 `activate b`。
* **断言：** 检查系统，`b_snapshot_lv` 重新被懒加载创建出来，且处于清爽状态。



---

## 六、 交付标准

AI Agent 最终提交的工程结构必须满足：

1. `src/main.py`: 核心 CLI 入口，采用 `argparse` 或 `click` 库管理命令。
2. `src/storage.py`: 封装所有 `losetup`, `lvcreate`, `mount` 等底层 Shell 交互。
3. `config.toml`: 标准配置文件。
4. `test_orchestrator.py`: 包含上述 **5.2 节** 所有断言的自动化测试脚本。一键运行 `pytest test_orchestrator.py` 必须输出 `100% Passed`。
