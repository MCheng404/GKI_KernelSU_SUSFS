# 小米 15 Pro（haotian）HyperOS 4 / Android 17 自编译 GKI + BakaSU + SUSFS 全特性实录

本文记录在 **小米 15 Pro（`haotian` / SM8750 / Android 17 / HyperOS 4）** 上，把自编译 GKI `6.6.118` 内核 + 内置 BakaSU root + **SUSFS 十项特性全开**做到系统完全可用的全过程，包含三个真实根因、可复现的修复、以及一批**已证伪、不要重复走**的弯路。

> 结论先行：**「HyperOS 4 刷自编译 SUSFS 内核大概率开不了机」这个流传结论是不成立的。**
> 卡二屏和桌面崩溃是两个**独立**的、可定位可修复的缺陷，不是 OS4 本身不兼容。
> 本仓库已实测做到 SUSFS 10/10 全开、桌面正常、root 可用、GPU 正常。

适用基线：

| 项 | 值 |
| --- | --- |
| 设备 | `haotian` / `2410DPN6CC` / SM8750 |
| 系统 | HyperOS 4，fingerprint `Xiaomi/haotian/haotian:17/CP2A.260605.016/OS4.0.4.0.XOBCNXM:user/release-keys` |
| 内核 | `6.6.118-android15-8-gf72a02f47f4b-abogki551678766-4k`（52 字符） |
| `uname -v` | `#1 SMP PREEMPT Sun Dec 01 08:10:00 UTC 2024`（43 字符） |
| root | BakaSU（**不是** SukiSU；SukiSU 上游仓库已被封） |
| SUSFS | `gitlab.com/simonpunk/susfs4ksu` 分支 `gki-android15-6.6`，运行时 `ksu_susfs show version` = `v2.4.0` |
| 槽位 | `boot_a` = `/dev/block/sde14`，`boot_b` = `/dev/block/sde49`（两槽原厂内容相同） |

---

## 一、刷机方法论：必须用原厂 boot 做模板替换 kernel 段

**直接刷 Actions 产物里那个 64 MB 的 AnyKernel3 `boot.img` 会变砖。** 原厂 boot 分区是 96 MB（`100663296` B），产物镜像的 header / ramdisk 布局与原厂不一致。

正确做法：**保留原厂 boot 的全部结构，只把 kernel 段换成自编译的 `Image`**，并修正 header 里的 kernel size。本仓库提供了脚本：

```bash
python scripts/haotian/bake_boot.py <stock_boot.img> <Image> <out.img>
```

正常输出应恒定为：

```
stock=100663296 hdr=1584 v4 old_kernel=36866560 ramdisk=0
new_image=38111744 delta=1245184
```

刷入：

```bash
adb shell su -c "reboot bootloader"
fastboot flash boot_a <out.img>
fastboot reboot
```

注意事项（都实际踩过）：

- `fastboot reboot` 后**有可能又回到 fastboot**，重刷一次再 reboot 即可救回。
- 降级到 `6.6.77` / `6.6.102` 会卡 Android logo 且 adbd 离线，**不要试**。
- 回滚优先级：已验证可用的自编译镜像 → 无 SUSFS 干净基线 → `boot_stock.img`。

---

## 二、坑一：`CONFIG_KSU_SUSFS_SPOOF_UNAME` 导致卡二屏（已解决）

### 现象

刷入后停在 HyperOS 开机动画，`sys.boot_completed` 永远为 0，但 **adb 可用、root 可用、CPU 大量空闲**（678~758% idle），不是死锁也不是 Watchdog。

### 定位过程（对照实验，全部实测）

| 构建 | 配置 | 结果 |
| --- | --- | --- |
| 纯内核（无 KSU） | — | 正常 |
| BakaSU，**禁用 SUSFS** | — | 正常进桌面 + root（干净基线） |
| BakaSU + SUSFS 全开 | 10/10 | 卡二屏 |
| 关 4 个「全进程」特性 | SUS_MOUNT / SPOOF_UNAME / HIDE_SYMBOLS / BOOTCONFIG = off | **启动成功** |
| **只关 SPOOF_UNAME** | 其余 9 项全开 | **启动成功** ← 锁定唯一元凶 |

### 根因

`SPOOF_UNAME` 只 hook **`newuname` 系统调用**，**不改 `/proc/version`**。

perf HAL 用 `uname()` 识别 target，拿到被改写的版本串后 `Target Init` 永不完成 → 走不到 `WMS.systemBooted()` → `mSystemBooted` 永远 false → 开机动画常驻。

抓 `system_server` 全线程栈可证：主线程名是 `"system_server"`（不是 `"main"`），栈停在 `epoll_pwait` **完全空转**，不是死锁。

### 三个必须排除的错误解释

早期在这上面浪费了很多轮，记下来避免重蹈：

1. **「版本串内容不对」——不是。** ksud 持久化配置 `/data/adb/ksu/.susfs.json` 里 `uname` 是 `{"release":"default","version":"default"}`，按 `susfs_set_uname` 的实现，`default` 就是照抄真实 `utsname()`，内容上等于没改。把版本串改成真实串重新构建，**照样卡**。
2. **「ABI 不匹配」——不是。** 内核侧 `struct st_susfs_uname` 与 ksud 侧 Rust `SusfsUname` 完全一致（`[u8;65] + [u8;65] + i32`）。
3. **「字符串超长被截断」——不是。** 实际 release 串长 52、version 长 43，都没到 `strscpy(..., __NEW_UTS_LEN)` 的 63 上限。

**问题不在内容，而在作用范围。** 另外，`set_uname` 的调用者是 **ksud 自己**，设备上没有任何模块脚本调它。

### 修复

给 hook 加 uid 门槛，与 SUS_PATH / SUS_KSTAT / SUS_MAP 本来的生效范围对齐。改 `common/kernel/sys.c`：

```c
if (static_branch_likely(&susfs_is_uname_spoof_buffer_set) &&
    current_uid().val >= 10000)
        susfs_spoof_uname(&tmp);
```

并确保该文件 `#include <linux/cred.h>`。

本仓库 `build.yml` 中「限制 SPOOF_UNAME 只作用于应用进程」一步已自动完成该改写。

**效果**：系统服务（uid < 10000）拿到真实 uname，perf HAL 正常初始化；应用（uid ≥ 10000）仍看到伪装串。实测 `bc=1`、PERFHAL 报错 0、桌面正常、**SUSFS 10/10 全开**。

---

## 三、坑二：`SUS_MOUNT` × 冗余 GPU 驱动模块导致桌面崩溃（已解决）

### 现象

能进系统，但**桌面每 60~70 秒重启一次**；**Flutter + Rust 应用全部打不开**；小爱同学反复闪退。**三方 Flutter 应用却完全正常**——这一点非常关键，说明不是「Flutter/Rust 通用问题」。

无 tombstone、无 Java FATAL，因为 MIUI 的 `ActivityLeakMonitor` 先把桌面杀了。

### 完整失败链

```
vndksupport: Could not load vulkan.adreno.so from sphal namespace:
  dlopen failed: cannot locate symbol "gsl_syncobj_external_signal"
  referenced by "/vendor/lib64/hw/vulkan.adreno.so"
    → context_vk.cc(326) Impeller validation: No valid Vulkan device found
    → Context is invalid
    → platform_view_android.cc(263) → android_shell_holder.cc(157) → shell.cc(366)
    → <hyper_os_flutter_backend::flutter_scene:609> Create engine failed, just finish
Rust 侧：once_cell::imp::initialize_or_wait+252 → park+220 永久阻塞
MIUI 侧：ActivityLeakMonitor: ...has create 200 activities more than 200 → 杀桌面
```

### 根因：符号来自两套不同的 Adreno 驱动

设备上装了 **`adreno_gpu_driver` 模块**，往 `/vendor/lib64` 覆盖挂载了一整套新版 Adreno 驱动。

| 来源 | `libgsl.so` | `vulkan.adreno.so` | 导出 `gsl_syncobj_external_signal` |
| --- | --- | --- | --- |
| **真原厂**（停用模块后露出） | 2284152 B | 5350296 B | **是**（dynsym idx 15, addr 846384），且原厂 vulkan 引用它 |
| 模块覆盖版 | 401904 B | 7040329 B | **否**（两边都没有，raw 字节计数 0） |

两套各自**自洽**。`SUS_MOUNT` 让应用解析出「**原厂 vulkan + 模块 libgsl**」的混搭组合 → 正好就是上面那条符号缺失报错，连路径都对得上。

> **排查陷阱**：直接 `pull /vendor/lib64/libgsl.so` 拿到的是**模块覆盖版**，所以怎么查都找不到那个符号，会误判成「原厂库本身缺符号」。必须先停用模块才能看到真原厂库。

另外查清（不是本次崩溃的直接原因，但值得清理）：

- `ro.gfx.driver.0` = `com.xiaomi.ugd`，其 APK 内装的是 **`vulkan.kona.so`（骁龙 865 / kona 驱动）**，是**错代驱动**。
- `ro.gfx.driver.1` = `com.qualcomm.qti.gpudrivers.sun.api35` 才是对的代次。
- 交叉比对：sun 版 vulkan 需要 34 个 gsl 符号，ugd 版 libgsl 全不导出。
- **`adreno_gpu_driver` 模块与 sun.api35 APK 提供的是同一份驱动，完全冗余。**

### 修复：让模块与 SUS_MOUNT 共存（不是停用模块）

依据 ksud `init_event.rs` 的执行顺序——公共 `post-fs-data.d`（L48）、模块 `post-fs-data.sh`（L117）都在 `metamodule::exec_mount_script`（L127）**之前**。

因此安装 `/data/adb/post-fs-data.d/00_susfs_sdcard_early.sh`（`chmod 755`，`chcon u:object_r:adb_data_file:s0`），在挂载发生前访问：

```
/data/media/0
/data/media/0/Android
/data/media/0/Android/data
```

意图是触发 fsnotify 关闭 `susfs_is_sdcard_android_data_not_decrypted`（`DEFINE_STATIC_KEY_TRUE`）。

**实测结果**：模块恢复启用 + `SUS_MOUNT=y`，`gsl_err=0 / no_vulkan=0 / engine_fail=0 / leak=0 / root_uid=0`，`/vendor/lib64/libgsl.so` 为模块版 401904 B（覆盖生效），跨两个不同构建稳定复现。

> **诚实标注：机制未完全坐实。** dmesg 显示真正的解密事件在 **78.9 s**（`'/data/media/0/Android' detected, mask: 0x48010000` → 延迟 5 s → 83.9 s `/sdcard is decrypted`），远晚于模块挂载；且某相机模块的挂载仍有高位 mnt_id。经验有效，但因果链还没闭环。

旁证：`ksu_module_susfs` 的 `boot-completed.sh` L327-334 有注释「Starting in SUSFS v1.5.8, it needs to set the sdcard and android data root paths. This will start the sus_path process. Without this check, sus_path will not work」，并 `until [ -d "/sdcard/Android/data" ]` 轮询最多 100 次——与本修复思路同源。

### SUS_MOUNT 读侧隐藏的源码机制

`fs/namespace.c`：

- `mnt_alloc_group_id` 对 ksu 域分配 `DEFAULT_KSU_MNT_GROUP_ID` 起的 id；
- `mnt_free_id` 对 `VFSMOUNT_MNT_FLAGS_KSU_UNSHARED_MNT` **跳过 `ida_free`**；
- `clone_mnt()` 有一条**对所有进程无条件生效**的 `if (old->mnt_id >= DEFAULT_KSU_MNT_ID)` 分支；
- `copy_mnt_ns` 加 `CL_COPY_MNT_NS BIT(25)`。

读侧隐藏受 static key 门控，`mounts_open` / `mountinfo_open` / `mountstats_open` 都是：

```c
if (static_branch_likely(&susfs_is_hide_sus_mnts_for_non_su_procs_enabled)) {
        if (!susfs_is_current_ksu_domain()) ...
}
```

被隐藏的判据是 `if (r->mnt_id >= DEFAULT_KSU_MNT_ID) return 0;`。

**在该 key 为 true 的状态下已实测 GPU / 桌面全绿**，不需要为了桌面而关掉 SUS_MOUNT。

---

## 四、坑三：`selinux_hide` 对应用空转，且「关 compat 宏」会导致链接失败

### 4.1 空转的实测证据

检测应用报告 `selinux_hide` 显示 enabled / running，但实际不生效。用普通应用 uid 直接探针：

```bash
# uid >= 10000 满足门控
busybox setuidgid 10464 sh -c 'printf u:r:ksu:s0 > /sys/fs/selinux/context'
#  → 成功，无报错（本应 EINVAL）
busybox setuidgid 10464 sh -c 'printf u:r:zzsentinel:s0 > /sys/fs/selinux/context'
#  → Invalid argument（哨兵对照，正常）
```

同时 `untrusted_app -> ksu_file:file read`、`untrusted_app -> lsposed_file:file read`、`system_server -> system_server:process execmem` 全部 **Allowed**。

ksud 自抓的 bootlog `/data/adb/ksu/log/dmesg.log` 显示初始化链**完全正常、无任何错误**：

```
[0.372]  registered handler for selinux_hide (id=4)
[1.360]  selinux_hide: fake status need late initialization
[1.370]  backup sepolicy success! latest_granting=1
[15.643] selinux_hide: set to 1
[15.643] init selinux hide
```

即 `ksu_selinux_hide_enable()` 返回 0、`running=true`、钩子已装——**但查出来还是脏策略的结果**。

> 提示：`dmesg` 环形缓冲会把早期启动冲掉，一定要读 ksud 落盘的 `dmesg.log`，不要只看 `dmesg`。

### 4.2 空转的根因（真实的集成缺陷）

`kernel/tools/susfs_compat.mk` 只要

```make
grep -q "ksu_selinux_hide_running" $(srctree)/security/selinux/hooks.c
```

成功，就 `ccflags-y += -DKSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE`。

该宏一旦定义，BakaSU 把**自己那套基于 `backup_sepolicy` 的实现整段 `#ifndef` 编译掉**，改用 SUSFS 的 inline hook。而本版 SUSFS 的 `susfs_security_compute_av_user` 与 `susfs_security_context_to_sid_core` 取的都是：

```c
rcu_dereference(selinux_state.policy)   // 当前生效的「脏」策略
```

**不是**施加 KSU 规则之前的原厂备份。所以查出来必然 allowed。

SUSFS 侧的门控本身是对的（`handle_sel_write_access` / `handle_sel_write_context` 都是 `if (likely(current_uid().val >= 10000 && ksu_selinux_hide_running))`），问题纯粹在**取错了策略对象**。

### 4.3 错误的修法：关掉 compat 宏 → vmlinux 链接失败

曾尝试改 `susfs_compat.mk` 让该宏不被定义，好让 BakaSU 编译并安装自己的实现（`selinux_hide.c` 中直接改写 `write_op[SEL_CONTEXT]` / `[SEL_ACCESS]`，全部走 `backup_sepolicy`）。

**该做法已证实会导致构建失败**（run `44934445437`）：

```
ld.lld: error: undefined symbol: ksu_selinux_hide_running
>>> referenced by hooks.c:3468 ... vmlinux.o:(handle_selinux_inode_setxattr)
>>> did you mean: ksu_selinux_hide_running
>>> defined in: vmlinux.o
ld.lld: error: undefined symbol: fake_status
ld.lld: error: undefined symbol: initialize_fake_status
ld.lld: error: undefined symbol: fake_status_initialize_key
```

注意那个诡异的「**同名符号明明 defined in vmlinux.o，却报 undefined**」。原因在 `kernel/feature/selinux_hide.c` L49-53：

```c
#ifdef KSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE
#define __maybe_static
#else
#define __maybe_static static
#endif
```

这四个符号都用 `__maybe_static` 修饰。**宏一旦不定义，它们全部变成 `static`**，在 ThinLTO 下被私有化 / 内联；而 SUSFS 打过补丁的 `security/selinux/hooks.c`、`selinuxfs.c` 仍然**无条件 extern 引用**它们 → 链接期找不到。

**结论：`KSU_COMPAT_HAS_SUSFS_FEATURE_SELINUX_HIDE` 必须保持定义。** 该 step 已从 `build.yml` 移除，并在原处留注释说明。

### 4.4 正确修法（已上机验证）：只伪造探测入口，内部路径保持实时策略

**第一版（全局换策略）是错的，且已用刷机证伪。** 那版把 `susfs_security_context_to_sid_core` 与 `susfs_security_compute_av_user` 的策略取值整体换成 `backup_sepolicy`。结果：`selinux_hide=1` 时 zygote 给应用设上下文全部 `-EINVAL`，`selinux_android_setcontext` 失败 58 次、波及 10 个包，zygote 逐个 SIGABRT，system_server 被判死反复重启，卡在开机动画。

根因是**作用范围**，不是策略内容：SUSFS 自己 hook 了 `security_setprocattr`，zygote 给应用设上下文正走 `hooks.c` → `susfs_security_context_to_sid()` → core；`backup_sepolicy` 的 sidtab 只有 initial SID，应用类型不在里面，必然 `-EINVAL`。

定案方案（`build.yml` 步骤「让 selinux_hide 只伪造 selinuxfs 探测结果」）：

```c
extern bool ksu_selinux_hide_running;
extern struct selinux_policy *backup_sepolicy;

/* core 加 hide 参数：只有探测入口传 1 */
static int susfs_security_context_to_sid_core(..., int force, int hide)
{
	...
	policy = (hide && backup_sepolicy) ?
		backup_sepolicy : rcu_dereference(selinux_state.policy);
	...
}

/* 新增变体，仅供 selinuxfs 探测使用 */
int susfs_security_context_to_sid_hide(...)
{
	return susfs_security_context_to_sid_core(..., 0, 1);
}
```

- 三处内部包装（`_default` / `_force` / 原函数）一律 `hide=0`，`hooks.c` 的 setxattr/setprocattr 完全不受影响；
- `selinuxfs.c` 的 `selinux_context` 写入入口改调 `_hide` 变体；
- `susfs_security_compute_av_user` 只被 selinuxfs 的 access 入口使用，按 `current_uid().val >= 10000 && ksu_selinux_hide_running && backup_sepolicy` 门控。

**验收结果（r10 实机，`selinux_hide=1`）**

| 写入主体 | 上下文 | 结果 |
| --- | --- | --- |
| 应用 uid 10464 | `u:r:ksu:s0` | `EINVAL`（已隐藏） |
| 应用 uid 10464 | `u:object_r:ksu_file:s0` | `EINVAL`（已隐藏） |
| 应用 uid 10464 | `u:object_r:lsposed_file:s0` | `EINVAL`（已隐藏） |
| 应用 uid 10464 | `u:object_r:system_file:s0` | 接受（合法上下文正常） |
| root uid 0 | `u:r:ksu:s0` / `ksu_file` | 接受（实时策略未被削弱） |

同时：`sys.boot_completed=1`、`bootanim=stopped`、tombstone 0、`selinux_android_setcontext` 报错 0 次、Vulkan 相关报错 0 次。Duck Detector 的 SELinux 卡片由 `DANGER` 变为 `[✔ CLEAR]`（`Dangerous types: None`、`Permissive domains: None`），其原始 selinuxfs 探针对两个 KSU 上下文均记录 `errno=22`。

**补丁脚本自身的四个坑（都是实测踩出来的）**

1. 替换串必须**从 SUSFS core 之后**开始找，否则会命中原生 `security_context_to_sid_default/_force`（签名只有 6 个参数），编译报 `too many arguments`。
2. 改完前面的文本后不能再复用旧偏移插入 `extern`，否则会插进 `retur` 中间变成 `returextern`。
3. 三元表达式的另一半不能把 `policy = ...` 整句拼进去，否则得到 `policy = ... : policy = rcu_dereference(...)`，报 `expression is not assignable`。
4. 幂等标记要用**新符号**（`susfs_security_context_to_sid_hide`），否则会和上一版补丁混淆、跳过不打。

**探针写法**：`printf '<ctx>' | toybox dd of=/sys/fs/selinux/context bs=<len> count=1`。用 `bs=1` 会逐字节写，每次都是残片，全部 `EINVAL`，看起来像"成功隐藏"，其实是假象。

---

## 五、SUSFS 运行时配置：装了模块 ≠ 开启了隐藏

### `ksu_module_susfs.zip` 不要盲装

审查后有三条硬理由：

1. `sus_path.txt` / `sus_path_loop.txt` / `sus_maps.txt` / `sus_mount.txt` / `sus_open_redirect.txt` / `try_umount.txt` **全是注释模板、零实际条目**。装上不会自动隐藏任何东西。
2. 模块 `module.prop` 是 `v1.5.2-CI-*`，自带 `tools/ksu_susfs_arm64` 也是 1.5.2 代；而设备上 `/data/adb/ksu/bin/ksu_susfs` 是 **v2.4.0**，版本对不上。
3. `config.sh` 里 `force_hide_lsposed` 那段要求 `SUSFS_DECIMAL_MAIN >= 2`，本内核 SUSFS `1.5.2+` 不满足，根本不执行。另外 `config.sh` 默认 `avc_log_spoofing=0` 会把已有的 true 关掉。

### 推荐做法：用设备上版本匹配的 ksud 直接填持久化配置

```bash
KSUD=/data/adb/ksu/bin/ksud
$KSUD susfs config backup                     # 先导出回退点
$KSUD susfs config sus_path add /data/adb
$KSUD susfs config sus_path add /debug_ramdisk
$KSUD susfs add_sus_path /data/adb            # 立即生效
$KSUD susfs add_sus_path /debug_ramdisk
```

内核确认真正落地（`dmesg`）：

```
susfs_add_sus_path] flagged AS_FLAGS_SUS_PATH on pathname: '/data/adb', ino: '1104'
susfs_add_sus_path] flagged AS_FLAGS_SUS_PATH on pathname: '/debug_ramdisk', ino: '122'
CMD_SUSFS_ADD_SUS_PATH -> ret: 0
```

`/data/adb` 会自动覆盖全部子路径（`modules` / `ksu` / `lspd` 等）。

**验证局限要讲清楚**：SUS_PATH 对 **ksu 域豁免**，所以无法从 root shell 直接演示隐藏效果；证据只能是内核标记成功 + 持久化，真实效果需检测应用复测。

### 哪些列表有意留空

`sus_map` / `open_redirect` / `sus_kstat` 保持为空，是**实测后有意为之**，不是遗漏：

- 实测应用进程 maps 里 `/data/adb` 条目**一条都没有**（`kernel_umount=1` 已卸载，LSPosed 走 memfd 匿名注入）；
- `/memfd:jit-zygote-cache (deleted)`、`/memfd:jit-cache (deleted)` 是**标准 ART 产物**，不是痕迹；
- `lspd` 目录下无 `.so`，zygisk 的 `.so` 未映射进应用。

没有真实目标时填猜测条目只是凑数，还扩大误伤面。`/data/local/tmp` 也**不建议**加（合法用途多）。

---

## 六、可选特性：哪些能加、哪些不用加

| 特性 | 结论 | 依据 |
| --- | --- | --- |
| **Ptrace Leak Fix** | **不适用** | 上游 `.github/actions/ptrace/action.yml` 原文 `Apply GKI ptrace compatibility patch for kernels < 5.16`，`MIN_VERSION="5.16"`，else 分支打印 `Kernel >= $MIN_VERSION, skipping ptrace patch`。6.6 用不上。 |
| **Unicode Fix** | **已内置生效** | `build.yml`「应用 Unicode 绕过修复」步骤 `if: inputs.enable_susfs`，每一版都带；补丁 `Action-Build/patches/unicode_bypass_fix_6.1+.patch`。 |
| **BTF / eBPF / FUSE-BPF** | **原厂已全开，无需加** | stock config 实测 `BPF / BPF_SYSCALL / BPF_JIT / BPF_JIT_ALWAYS_ON / BPF_LSM / CGROUP_BPF / BPF_EVENTS / FUSE_FS / FUSE_BPF / DEBUG_INFO_BTF / DEBUG_INFO_BTF_MODULES` 全 `=y`。上游还把该 action **主动注释掉**，理由是剩下的只是 tracing 管线和 experimental FUSE-over-eBPF，"nothing here uses"。 |
| **Networking + NTSync** | **可加**，NTSync 依赖 Droidspaces | 补丁链取自 `github.com/ravindu644/Droidspaces-OSS`；NTSync 自动选 `ntsync_compat_android15-6.6.patch`，最终写 `CONFIG_NTSYNC=y`。 |
| **Baseband Guard** | 开关存在（`use_bbg`），本轮未启用 | — |

### 两条必须记住的风险

1. **Droidspaces 会动 kABI。** 它启用 `SYSVIPC / POSIX_MQUEUE / IPC_NS / PID_NS / USER_NS / DEVTMPFS`，**padding 补丁必需**，否则 vendor 模块拒载 → bootloop。补丁：`001.GKI-below-6.12-fix_sysvipc_kabi_${SLOT_NAME}.patch`、`002.5.10_or_lower_use_android_abi_padding_for_posix_mqueue.patch`。槽位 **678 不行就换 345**；**123 在 android15-6.6 上打不上**。
2. **`use_zram` 必须保持 `false`。** 6.6 上与原厂 `vendor_dlkm` 的 `zram.ko` 打架。某些第三方指南默认 true，**别照抄**。

---

## 七、HMA-OSS 与 SUSFS 不兼容（独立问题，已坐实）

### 现象

`zygote64` 每秒一次 `signal 6 SIGABRT`，`Abort message: '!(fd != -1)'`，`Process uptime: 1s`。

### 归因

ELF 解析 `hma_oss_zygisk/zygisk/arm64-v8a.so`（13480 B）三项精确吻合：PT_LOAD 总跨度 `0xb000` = 45056 与崩溃帧区间相等、首个 PT_LOAD 是 `r-x`、含一字不差的 `!(fd != -1)` 与标签 `ZygoteLoader[Native]`。

机制：ZygoteLoader 遍历 `/proc/self/fd` + 名字匹配 + `openat` 重开 dex，而 SUSFS 改了三处它依赖的识别依据：

- `fs/readdir.c` —— SUS_PATH 过滤目录项
- `fs/proc/fd.c` —— SUS_KSTAT 伪造 `mnt_id` / `ino`
- `fs/proc/base.c` —— OPEN_REDIRECT 改写 readlink

→ `fd = -1` → abort。移除 / 禁用 HMA 后 tombstone 归零。

### SUSFS 能不能替代 HMA？**架构上不能**

- HMA 拦的是 **PackageManager API 结果**，数据由 **system_server（uid 1000）经 Binder** 从内存包注册表返回，调用方进程**不碰文件系统**。
- SUSFS 全部作用在文件系统 / proc 层，且**只对 uid ≥ 10000 生效**，够不着 system_server，也没有 Binder hook。
- 真把 `/data/app` 对 system_server 隐藏会直接搞崩 PackageManager。

SUSFS 能覆盖的是**另一半**：直接 `stat /data/app/...`、探测 `/data/data/<pkg>`、读 `/proc/self/maps`、挂载痕迹 —— 分别对应 SUS_PATH / SUS_KSTAT / SUS_MAP / SUS_MOUNT。

**因此「28 个 Xposed 模块包名可见」这类 PackageManager 层泄露，内核侧无解。**

### 可能的替代路线（未实测）

原版 HMA `com.tsng.hidemyapplist`（Dr-TSNG）自 **V3.0.6 起是纯 Xposed 模块**，发布说明原文「不再需要 Magisk 模块，请将其卸载，否则会卡开机」，靠 LSPosed 在目标进程 hook PackageManager，**不经过 ZygoteLoader**，理论上绕开本冲突。

风险：原版**已停止更新**，Android 17 / API 37 兼容性无实测保证；framework hook 本身留 Xposed 痕迹，属于「用一种检测面换另一种」。

---

## 八、NFQUEUE：原厂已全开，不需要改内核

用于本地流量过滤 / 去广告时常被问到。实测 stock 与自编译内核**均已内建**：

```
CONFIG_NETFILTER_NETLINK=y
CONFIG_NETFILTER_NETLINK_QUEUE=y       # nfnetlink_queue
CONFIG_NETFILTER_XT_TARGET_NFQUEUE=y   # xt_NFQUEUE
CONFIG_NETFILTER_XT_MATCH_OWNER=y      # 按 uid 分应用
CONFIG_NETFILTER_XT_MATCH_STRING=y
CONFIG_NETFILTER_XT_MATCH_U32=y
CONFIG_IP_NF_IPTABLES=y / IP6_NF_IPTABLES=y
```

全是 `=y` 而非 `=m`，**不需要额外 ko，也就没有 kABI / vendor 模块拒载风险**。

限制：**`CONFIG_NF_TABLES is not set`**，nftables 用不了，只能走 iptables legacy（设备 `/system/bin/iptables` 为 v1.8.11 legacy，`ip6tables` 是其软链）。

端到端验证方法（安全、可立即回滚）：只拦回环 ICMP、无监听者，看规则计数器与丢包，然后删规则。

```bash
iptables -w -I OUTPUT 1 -p icmp -d 127.0.0.1 -j NFQUEUE --queue-num 65000
ping -c 2 -W 2 127.0.0.1          # 预期 100% loss
iptables -w -L OUTPUT 1 -v -n     # 预期计数器 2 包
iptables -w -D OUTPUT 1           # 立即删除，回环恢复
```

实用提醒：HTTPS / QUIC 加密，`-m string` 匹配 payload 对广告无效；能看到的明文只有 DNS 与 TLS ClientHello 的 SNI（ECH 普及后 SNI 也会没）。每包一次内核↔用户态往返，延迟与耗电显著高于 DNAT 方案，守护进程必须配 `--queue-bypass` 做 fail-open。另外 Android 的 netd 自己管 iptables，网络切换会重建规则链，规则需能自动重下发。

---

## 九、构建系统的坑（这几条不知道会白跑很多轮）

1. **BakaSU Kconfig 里所有 SUSFS 子选项 `default y`。** 不写 `# CONFIG_X is not set` 是**关不掉**的。
2. **bazel 分支的 `ksu.fragment` 由 `diff "$DEFCONFIG.orig" "$DEFCONFIG" | grep '^>'` 生成，只捞新增行。** 删行不生效；只有**追加**的 `# CONFIG_X is not set` 才会进 fragment。
3. 承上，**改值行（如 `CONFIG_LOCALVERSION`）不产生 `>` 行，会被静默丢弃**。版本串伪装必须靠 dispatch 参数直传才生效。此问题尚未修复。
4. `CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER` 在 BakaSU 未声明，workflow 会自动跳过（某些 fork 自行补了该 Kconfig）。
5. 「应用 Stock Config 伪装」步骤要读 `config/stock_defconfig`，本仓库不存在该文件 → 一直跳过，**只影响 `/proc/config.gz` 的显示**。实际编译配置恒为 `common/arch/arm64/configs/gki_defconfig`。
6. `ksu_mode` 三个选项语义反直觉：`关闭` = **启用 SUSFS**；`禁用SUSFS` = 保留 KSU；`禁用KSU` = 纯内核。
7. `cancel_susfs=true` 会报 `SukiSU Kconfig SUSFS section missing`，不要用。
8. 给 KSU 所有含 `susfs_` 的 `.c` 首行插 `susfs.h` 会早于 `cred.h`，导致隐式 int + array-bounds 编译崩溃。
9. **编译步骤的重试会清空 `ksu.fragment`。** 该步骤先生成 fragment 再执行 `cp "$DEFCONFIG.orig" "$DEFCONFIG"`；一旦编译失败被 retry，第二次 `diff` 两侧已经完全相同，fragment 被改写成空文件，KSU/SUSFS/NTSYNC/NoMount 配置全丢（表现为 `ntsync.ko` modpost 报 `__vfs_setxattr_noperm undefined`）。修法：diff 先写 `$FRAG.tmp`，非空才覆盖 `$FRAG`，空的就沿用上一次生成的 fragment。

---

## 十、验证清单

### 刷机前（省一轮刷机）

从产物 AnyKernel3.zip 解出 `Image`，直接读内嵌 IKCFG：

```bash
python scripts/haotian/ikcfg.py <Image>
```

`IKCFG_ST` offset 在本机型恒为 `17840288`。确认十项 `CONFIG_KSU_SUSFS*` 状态符合预期再刷。

### 刷机后（**只看 `bc=1` 不足以判定可用**）

```bash
adb shell getprop sys.boot_completed                       # 期望 1
adb shell dumpsys window | grep -m1 mCurrentFocus           # 期望 com.miui.home/...Launcher
adb shell su -c "id -u"                                     # 期望 0
adb shell su -c "zcat /proc/config.gz | grep KSU_SUSFS"
adb shell su -c "ls /data/tombstones/ | wc -l"              # 期望 0
# 四个计数都应为 0：
adb logcat -d -b all | grep -c "No valid Vulkan device found"
adb logcat -d -b all | grep -c "Create engine failed"
adb logcat -d -b all | grep -c "gsl_syncobj_external_signal"
adb logcat -d -b all | grep -c "ActivityLeakMonitor.*has create"
```

焦点为 `null` 或停在其他 app，即桌面未接管。

---

## 十一、已证伪 / 已排除清单（不要重复走）

**启动侧**：SELinux `status.c` seqlock（bug 真实、已修，但非根因）；版本串内容；`services.c` latest_granting（已修，仍卡）；Zygisk 本体 / `kernel_umount` / `selinux_hide`；`systemui_mount`（init 与 SystemUI mountinfo 均 6926 条）；CPU 抢占 / Watchdog / ANR；app uid 文件可见性；bootanim（强制 `setprop service.bootanim.exit 1` 后 stopped 但 `mSystemBooted` 仍 false）；LSPosed hook system_server（正常版与 SUSFS 版都有）；`DarkModeStatusTracker.init` 警告（基线噪声）。

**桌面侧**：

- `mm/memory.c` 的 `__access_remote_vm` SUSFS 改动是**死代码**（`vma = NULL` 后立即 `if (vma && ...)` 恒假）。
- `susfs_priv_app_sid` 仅 3 处，全在 `security/selinux/avc.c` 的 avc 日志伪装，纯显示且受未开启的 static key 控制。
- `fs/proc/bootconfig.c` 的 spoof 受 `susfs_is_fake_cmdline_or_bootconfig_buffer_set` 控制，未配置。
- `kernel_umount=0` **并未修好桌面**。
- 小爱崩溃**不是**桌面泄漏的驱动源（tombstone 为 0 时桌面照样泄漏）。
- 清空 `ro.gfx.driver.0/1`（resetprop）后符号错误消失，但 `No valid Vulkan device found` 仍在、桌面仍起不来，重启后属性自动恢复。
- **「Flutter/Rust 通用问题」是错的**：三方 Flutter 应用实测渲染完全正常。

**尚未深究的线索**：小爱 tombstone 中 `x0 = 0xf81e0ffe_d503233f`，低 32 位 `0xd503233f` 正好是 ARM64 `paciasp` 指令编码，疑似 JIT 把指令字节当指针（`libllvm-qgl.so` 是 Adreno 着色器 JIT）。

**其他未验证项**：`fs/open.c` 的 SUSFS 版 `do_faccessat` 缺 `IS_ERR(fname)` 检查、`getname_flags` / `filename_lookup` 的 `empty` 参数传 NULL（可能破坏 `AT_EMPTY_PATH`）；`TIF 33-35` / `AS_FLAGS 33-39` 位号是否与高通 `sched_walt` / Xiaomi 自定位冲突（缺小米内核源码，无法核对）。

---

## 十二、构建对照表

| run | 说明 | 结果 |
| --- | --- | --- |
| `44877664888` | 纯内核 | 正常 |
| `44879978653` | BakaSU，无 SUSFS | 正常 + root（干净基线） |
| `44883626569` / `44885694422` / `44914450006` | status.c 修复 / 真实版本串 / services.c 修复 | 均仍卡二屏 |
| `44919777486` | 关 4 个全进程特性 | 启动成功 |
| `44922019341` | **只关 SPOOF_UNAME** | 启动成功（锁定元凶） |
| `44927662932` | 关 SUS_MOUNT + SPOOF_UNAME（8/10） | 桌面正常，Vulkan 三项全 0 |
| **`44930971760`** | **SPOOF_UNAME 加 uid 门槛（10/10）** | **全绿，长期运行版** |
| `44933335577` | + Networking + NTSync + Droidspaces 678 | 构建成功，未刷机验证 |
| `44934445437` | + 关 selinux_hide compat 宏 | **链接失败**，见第四节 |

---

## 十三、NoMount：VFS 路径重定向（已集成并实机生效）

NoMount 是 **VFS 层的路径重定向框架**，不是 mount 方案：不产生挂载项、不污染 `/proc/mounts`，注入/whiteout/opaque 全部在 RAM 里完成，用户态与内核通过 keyring（`add_key`）通信，还带 UID 隔离（把银行类/检测类应用放回原厂文件系统视图）。

**集成方式**

- `build.yml` 的 `use_nomount` 默认改为 `true`；步骤 30 拉取 `maxsteeel/nomount` 的 `dev` 分支（实测 `2e696f6`），建立 `common/fs/nomount -> ../NoMount/kernel/src` 软链，并注册 `fs/Makefile`、`fs/Kconfig`。
- 追加 `CONFIG_NOMOUNT=y`，**编进 Image**（不是 LKM）。产物 `Image` 比不带 NoMount 的版本大 65536 B。
- 本地构建要给该步骤加代理（`curl raw.githubusercontent.com` 直连不通）。

**实机验证（r10，6.6.118 GKI）**

```text
[   22.765138] nomount: Injection started
[   22.765235] nomount: loading module BuildingtheLightofHopefortheFuture
NoMount: [DEBUG] Superblock successfully hijacked for dev: 0xfe00033
NoMount: [DEBUG] xattr handlers successfully hijacked for dev: 0xfe00004
NoMount: [DEBUG] Successfully hijacked VFS ops for parent dir (ino: 350)
```

即开机后子系统已接管，且设备上已安装的模块被它接走——不是把代码编进去空转。

**两点提醒**

- 内核侧只是子系统；要真正用起来还得刷 NoMount 元模块 zip（KSU/APatch 模块 + `nm` CLI）。
- 它与 SUSFS 的 `sus_mount` 是两套独立的路径重定向，编译期不冲突，**运行时是否互抢 hook 需长期观察**。

---

## 附：本文配套脚本

- `scripts/haotian/bake_boot.py` —— 以原厂 boot 为模板替换 kernel 段
- `scripts/haotian/ikcfg.py` —— 从 `Image` 提取内嵌 IKCFG，刷机前预检配置

## 参考

- 上游：`github.com/WildKernels/GKI_KernelSU_SUSFS`、`github.com/zzh20188/GKI_KernelSU_SUSFS`
- SUSFS：`gitlab.com/simonpunk/susfs4ksu`（分支 `gki-android15-6.6`）
- BakaSU：`github.com/Baka-SU/BakaSU`
- Droidspaces 补丁：`github.com/ravindu644/Droidspaces-OSS`
- 同机型踩坑记录：`github.com/Jgckm/GKI_KernelSU_SUSFS`（其指南结论仍为「OS4 大概率不能开机」，本文已推翻）
- 相关 issue：`zzh20188/GKI_KernelSU_SUSFS#251`、`#252`