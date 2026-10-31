# oracle-keepalive

让 Oracle Cloud **Always Free** 的免费实例不被判定为“空闲”而被回收。

一个文件（`oracle-keepalive.py`）、一个 systemd 单元、一个安装脚本，纯标准库、无网络、无子进程。它做的只有两件事：

- **CPU**：周期性爆发一小段纯整数计算（`SCHED_IDLE` 优先级），按占空比让采样到的 CPU 利用率稳定高于 20% 线；
- **内存**：用**匿名 mmap**（不是 tmpfs）持有并触碰若干页，让 "used" 内存的比例高于 20%。只在内存条件真的算数的机型上启用。

---

## 1. 规则原文（以及日期）

Oracle 对 Always Free 计算实例的“空闲”判定，7 天内**同时**满足以下全部条件才可能回收：

| 条件 | 阈值 | 适用于 |
|---|---|---|
| CPU 利用率 95 分位 | < 20% | 所有机型 |
| 网络利用率 | < 20% | 所有机型 |
| 内存利用率 | < 20% | **仅 A1（ARM）机型** |

来源：<https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>
（**2026-10-31 查阅**；请以你阅读时的最新版本为准，Oracle 改过措辞。）

关键点：这是 **AND**，所以**打破任意一个条件就够**。本项目只打破 CPU（所有机型）和内存（仅 A1），**完全不产生网络流量**——按文档字面意思，这已经足够，但见第 5 节的诚实说明。

## 2. 机型与策略（为什么不一样）

| 机型 | 架构 | 默认模式 | 原因 |
|---|---|---|---|
| `VM.Standard.E2.1.Micro` | x86_64 / 1 vCPU / 1 GB | **仅 CPU** | 内存条件对它**不适用**，占内存纯属浪费，还可能把自己 OOM 掉；1 GB 机器上 15 秒级的反应速度也太慢 |
| `VM.Standard.A1.Flex` | aarch64 / 例如 2 vCPU / 12 GB | **CPU + 内存** | 内存条件对它适用，只烧 CPU 不够（单条也够，但两条都破更稳；且 A1 有突发额度，见风险） |

`KEEPALIVE_MODE=auto` 就是按上表选的：aarch64 → `cpu+mem`，其它 → `cpu`。
即使你强制 `mem`，只要 `MemTotal < 2 GiB`，内存部分也会被自动关闭并打日志。

## 3. 安装 / 卸载 / 查看

```bash
# 安装（会拒绝在检测到 MoeClub 的 eat.service 时继续）
sudo ./install.sh --dry-run          # 先看它要做什么，不动系统
sudo ./install.sh

# 如果装了老的 eat.service，需要显式同意替换
sudo ./install.sh --replace-eat

# 状态：模式、样本覆盖时长、7 天 p95、当前持有量、结论
python3 /usr/local/lib/oracle-keepalive/oracle-keepalive.py status

# 只做检测与算数，不落任何文件
python3 /usr/local/lib/oracle-keepalive/oracle-keepalive.py plan

# 约 5 秒自检：解析 /proc、1 个 worker 烧 2 秒、持有并释放 32 MB
python3 /usr/local/lib/oracle-keepalive/oracle-keepalive.py selftest

# 卸载（保留配置）；加 --purge 连配置与状态目录一起删
sudo ./install.sh --uninstall
sudo ./install.sh --uninstall --purge
```

安装脚本做的事（幂等，可重复执行）：装程序到 `/usr/local/lib/oracle-keepalive/`（root:root 0755）、单元到 `/etc/systemd/system/`、**仅在不存在时**写 `/etc/oracle-keepalive.conf`、按需写 `MemoryMax` drop-in、`daemon-reload` + `enable --now`、最多等 15 秒变 active、然后跑一次 `status`。它只装它旁边的文件，**从不下载任何东西**。

### 一键安装（单文件安装器）

仓库是私有的，所以没有匿名的 `curl | bash`。`make-bundle.sh` 会把全部文件打成**一个自解压脚本**（`dist/oracle-keepalive-installer.sh`）：脚本头里写着载荷的 SHA-256，运行时先校验再解包，然后把参数原样交给里面的 `install.sh`；**全程不联网**。

```bash
# 在有仓库访问权限的机器上构建（要求已提交，不允许带着本地改动打包）
./make-bundle.sh

# 在目标机器上：先看版本和校验值，再干跑，再安装
bash oracle-keepalive-installer.sh --show-info
sudo bash oracle-keepalive-installer.sh --dry-run
sudo bash oracle-keepalive-installer.sh
sudo bash oracle-keepalive-installer.sh --uninstall
```

注意：脚本里的 SHA-256 只能发现文件损坏或被截断，**不是签名**。以 root 运行之前，请把 `--show-info` 打印的 `file sha256` 与你**另外渠道**拿到的值核对一致。不要用管道方式运行（`curl ... | sudo bash`），它需要读自己的文件。

## 4. 它不做什么

- **不产生网络流量。** 网络是第三个条件，但按文档的 AND 逻辑打破任意一个就够了，所以这里没做。如果你想要“第三条也破掉”的保险，需要另外的东西（比如定时的小流量），本项目不提供，也不会假装提供。
- 不改内核参数、不装额外依赖、不写 `/run/shm`、不碰别人的服务。
- 不做任何“反检测”的事：没有伪装、没有随机化特征、没有绕过计费。它就是老老实实占用一点资源。

## 5. 安全设计

一台免费实例上的“保活”进程最怕两件事：把真实业务挤掉，或者把机器搞挂。所以：

| 机制 | 作用 |
|---|---|
| worker 内 `SCHED_IDLE` | 只要有任何普通优先级任务要跑，CPU 立刻让出来；爆发只在真空闲时吃满一个核 |
| `Nice=19` / `CPUSchedulingPolicy=idle` / `IOSchedulingClass=idle` | 系统层面的第二道保险 |
| `OOMScoreAdjust=1000` | 内存紧张时内核**第一个**杀它，而不是你的业务进程 |
| `MemoryMax` drop-in（45% of MemTotal） | cgroup 硬上限，越界直接被限，不会拖垮整机；仅在内存模式生效时安装 |
| `MemAvailable/MemTotal < 10%` → 立刻释放全部持有 | 5 秒级的反应，比 tmpfs 方案快得多 |
| 匿名 mmap，非 tmpfs | 计入普通 used 内存，不做共享记账，也不会被当作可回收页缓存清掉 |
| 无网络、无子进程、`RestrictAddressFamilies=AF_UNIX` | 攻击面接近零 |
| `DynamicUser=yes` + `StateDirectory` + `ProtectSystem=strict` + 空 `CapabilityBoundingSet` | 以一次性用户运行，只读系统，无任何 capability |
| 只在状态目录写文件 | 除了 `/var/lib/oracle-keepalive/` 不落任何东西 |

## 6. 怎么验证它真的有用（重要）

> **关于"升级到 Pay As You Go 就不会被回收"的传闻：** 论坛里常见这个说法。官方 Always Free 页面（2026-06-12 更新）没有写明这一点，闲置条件仍写在那一页上；有用户引用过一封 Oracle 邮件，措辞是 "from Always Free customers only"，但这是转述，本项目没有核实原文。所以这里把它当作**可能**，不当作保证。升级需要绑卡，**超出 Always Free 额度的部分会收费**。

**在 OCI 控制台**：Instance details → Metrics → 选 **`CpuUtilization`**（和 A1 上的 **`MemoryUtilization`**），时间窗口选 **7 天**，看 **p95**。这是 Oracle 自己算的、也是判定用的那份数据。

**在本机**：`status` 会打印 guest 视角的 7 天 p95（CPU、内存、steal）和一行结论。

怎么看这两个数字是否对得上：爆发期间 guest 视角的 CPU **大约是 `workers / nproc`**（例如 2 vCPU 上 1 个 worker ≈ 50%），而 7 天 p95 会靠近这个值——**前提是** Oracle 的采样足够细（见下文的前提），此时 duty 12% 意味着约 12% 的采样点落在爆发里，而判定只要求“超过 5% 的采样点高于 20%”。这也是 duty 下限设 0.06 的原因：刚好过 5% 的线，边际很薄。开发机上我实测过一次：8 核 Debian 13，1 个 worker 烧 6 秒，整机 CPU 读出 12.63%，与 1/8 核的预期（12.5%）一致（共享机器，只作量级参考，不是 Oracle 实例上的结果）。

诚实说明：

- guest 视角的数字只是**代理指标**。`/proc/stat` 的采样间隔（默认 60 秒）和 Oracle 侧采样/聚合方式未必一致，p95 的算法也未必一样（这里用 nearest-rank）。
- **Oracle 具体怎么测、怎么聚合，本项目没有验证过**，也没有权限验证。所以 `status` 的结论一律写成“估计（ESTIMATE）”，控制台指标才是权威。
- 任何“装了就不会被回收”的说法都是吹牛。这里能说的是：按文档的判定条件，它把 CPU（以及 A1 的内存）的观测量推到了 20% 以上。

**一个前提：采样分辨率必须短于一次爆发。** 上面这套占空比只有在 Oracle 的指标采样间隔短于一次爆发（默认 180 秒）时才起作用——采样点随机落进爆发里，p95 才会被抬上去。如果 Oracle 用的其实是长窗口平均值，那么在 1 核机器上 12% 的占空比平均下来就是大约 12%，低于 20% 这条线，装了也不达标。因此建议装好 24 小时后到 OCI 控制台看 `CpuUtilization` 的 p95：如果低于 20，就调高 `KEEPALIVE_CPU_DUTY`（夹取上限 0.40），或者增加 `KEEPALIVE_CPU_WORKERS`。Oracle 具体的测量方式本项目没有验证过。

## 7. 已知风险

- **E2 Micro 是突发型（burstable）实例。** 持续烧 CPU 会消耗 CPU 积分/突发额度，额度耗尽后性能被限制；对“只跑保活”的空机器无所谓，但如果这台机器还在干正事，请把 `KEEPALIVE_CPU_DUTY` 调低（最低 0.06，不能再低）或干脆别用。
- **swap。** 持有页可能被换出。换出后 guest 视角的“已用内存”会下降，还可能带来 I/O 抖动。`status` 和 `plan` 在检测到 swap 时会提醒。想在 A1 上稳住内存指标，考虑对该实例 `swapoff`（自行评估）。
- **steal。** 宿主机超卖时，guest 看到的利用率会包含 steal，数字会“虚高”，看起来达标其实不一定是你的功劳。所以采样里单独记了 `steal_pct`，`status` 也会打印，方便人工复核。
- **未在 Oracle Linux 上验证。** 开发和测试都在 Debian 13 / systemd 257 上做（见第 9 节），Oracle Linux 8/9 的 systemd 版本、`DynamicUser` 行为、`/proc/meminfo` 字段都没实测过。理论兼容（systemd 245+ 的指令集），但没有证据。
- **这台开发机从未真正安装过本单元**，只跑了单元测试和 `plan`/`selftest`/`status`。第一次真机安装请先 `--dry-run`。

## 8. 与 MoeClub `eat.service` 的差异

| 维度 | MoeClub `eat.service` | 本项目 |
|---|---|---|
| 手段 | 只占内存：每 15 s 往 `/run/shm/MemoryEat.bin` 写零 | CPU 爆发 + 匿名 mmap 内存持有 |
| 对 E2 Micro | **无效**（内存条件不适用，且不做 CPU 工作） | 有效（只烧 CPU） |
| 目标比例 | 注释说 15%–20%，代码是 20%–24% | 目标 25%（可调 22%–40%），默认值明确高于 20% 线 |
| 载体 | tmpfs，不可回收且可被 swap | 匿名 mmap，`MemAvailable` 低于 10% 立即全放 |
| 反应速度 | 15 s 循环 | 5 s tick（可调到 1 s） |
| 空闲让路 | 无 | worker `SCHED_IDLE` + `Nice=19` + `CPUSchedulingPolicy=idle` |
| OOM 保护 | 无 | `OOMScoreAdjust=1000` + `MemoryMax` cgroup 上限 |
| 安装方式 | `curl` 拉一个未固定版本的脚本以 root 执行 | 只装本目录的文件，幂等，支持 `--dry-run` |
| 卸载 | 缺 `--now` 与 `daemon-reload`，容易残留 | `disable --now` + 删单元/drop-in/程序 + `daemon-reload`，逐项打印删了什么 |
| 观测 | 无 | `samples.csv`（7 天）+ `status` 的 p95 估计与结论 |
| 冲突处理 | — | 检测到 `eat.service` 直接**拒绝安装**，除非 `--replace-eat` |

## 9. 配置

全部通过环境变量（systemd `EnvironmentFile=-/etc/oracle-keepalive.conf`）。全部可选，越界会被**夹到合法区间并打警告日志**。完整带注释的样例见 `oracle-keepalive.conf.example`；改完 `systemctl restart oracle-keepalive` 生效（或先 `plan` 看数字）。

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `KEEPALIVE_MODE` | `auto` | `auto/cpu/mem/cpu+mem/off` | 做什么 |
| `KEEPALIVE_CPU_DUTY` | `0.12` | `0.06..0.40` | 爆发占墙钟时间的比例 |
| `KEEPALIVE_CPU_BURST_SECONDS` | `180` | `60..600` | 单次爆发时长；周期 = burst / duty |
| `KEEPALIVE_CPU_WORKERS` | `ceil(nproc/2)` | `1..nproc` | 每轮几个 worker |
| `KEEPALIVE_MEM_TARGET` | `0.25` | `0.22..0.40` | 希望“已用”达到 MemTotal 的比例 |
| `KEEPALIVE_MEM_MAX_HOLD` | `0.30` | `0.05..0.40` | 自身持有量的硬上限 |
| `KEEPALIVE_MEM_FLOOR` | `0.10` | `0.02..0.50` | 低于此可用比例就全部释放 |
| `KEEPALIVE_MEM_STEP_MB` | `64` | `8..1024` | 迟滞：差值小于它就不动；增长每 tick 最多 256 MB，缩减立即 |
| `KEEPALIVE_TICK_SECONDS` | `5` | `1..60` | 内存循环间隔 |
| `KEEPALIVE_SAMPLE_SECONDS` | `60` | `10..3600` | 采样间隔（一行 CSV） |
| `KEEPALIVE_STATE_DIR` | `/var/lib/oracle-keepalive` | — | 状态目录（单元里的 `StateDirectory` 就是它） |

`status` 和 `plan` 读的是**环境变量**（和单元里的 `EnvironmentFile` 完全一致）。所以手动跑它们时不会自动读 `/etc/oracle-keepalive.conf`；安装脚本在安装完调用 `status` 时会带上配置文件里的值。想手动按配置跑：`sudo env $(grep -Ev '^\s*(#|$)' /etc/oracle-keepalive.conf | xargs) python3 /usr/local/lib/oracle-keepalive/oracle-keepalive.py status`。

状态目录内容：`samples.csv`（`epoch,cpu_util_pct,mem_util_pct,steal_pct,held_mb,burning`，保留 7 天，启动时与每小时原子裁剪）、`daemon.pid`、`heartbeat`。`status` 只读这些，**不调用 systemctl**。

`duty` 的下限 0.06 是硬性的：判定要求“超过 5% 的采样点高于 20%”，再低就不成立了。

## 10. 测试

```bash
python3 -m unittest discover -s tests -v
# 或
python3 -m pytest tests -q
```

覆盖：`/proc/meminfo` 与 `/proc/stat` 解析、`desired_hold_mb` 的纯函数算例（空闲 12 GB、业务已超目标、低于 floor、上限）、迟滞与增长限速、模式检测、配置夹取、p95、CSV 追加与 7 天裁剪、爆发调度的边界与 2 天模拟占空比（±25%）、`selftest`、`bash -n install.sh`、有 `systemd-analyze` 时的单元校验。全部不需要 root、不联网、不写状态目录。

## 11. 许可

MIT（见 `LICENSE`）。
