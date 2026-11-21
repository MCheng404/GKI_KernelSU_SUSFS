#!/usr/bin/env python3
"""以原厂 boot.img 为模板，只替换 kernel 段，生成可直接 fastboot 刷入的镜像。

为什么需要它：
    Actions 产物里的 AnyKernel3 boot.img 是 64MB，而 haotian 原厂 boot 分区是
    96MB（100663296 B），header / ramdisk 布局也不一致。直接刷产物镜像会变砖。
    本脚本保留原厂 boot 的全部结构，只把 kernel 段换成自编译的 Image，
    并修正 header 中的 kernel size。

用法：
    python bake_boot.py <stock_boot.img> <Image> <out.img>

haotian 6.6.118 的正常输出：
    stock=100663296 hdr=1584 v4 old_kernel=36866560 ramdisk=0
    new_image=38111744 delta=1245184

刷入：
    adb shell su -c "reboot bootloader"
    fastboot flash boot_a <out.img>
    fastboot reboot
"""

import os
import struct
import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2

    stock_path, image_path, out_path = sys.argv[1:4]

    with open(stock_path, "rb") as f:
        data = bytearray(f.read())
    with open(image_path, "rb") as f:
        img = f.read()

    if data[:8] != b"ANDROID!":
        raise SystemExit("bad magic: not an Android boot image")

    old_kernel_size, ramdisk_size = struct.unpack_from("<II", data, 8)
    header_size = struct.unpack_from("<I", data, 20)[0]
    header_version = struct.unpack_from("<I", data, 40)[0]

    print(
        f"stock={len(data)} hdr={header_size} v{header_version} "
        f"old_kernel={old_kernel_size} ramdisk={ramdisk_size}"
    )
    print(f"new_image={len(img)} delta={len(img) - old_kernel_size}")

    if header_version >= 3 and ramdisk_size != 0:
        raise SystemExit(
            f"boot header v{header_version} 且带 ramdisk（{ramdisk_size} B），"
            "布局与纯 kernel 的 GKI boot 不同，请人工核对后再改"
        )

    # v3/v4 固定 4096 对齐；v0-v2 由 header_size 向上取整到页边界。
    # haotian 是 v4 + ramdisk=0 的纯 kernel GKI boot，两者算出来都是 4096。
    page = 4096 if header_version >= 3 else header_size
    koff = (page + 4095) // 4096 * 4096
    old_end = koff + old_kernel_size
    need_end = koff + len(img)

    # 留出后续段（ramdisk / second / dtb 等）的安全余量
    if need_end > len(data) - 65536:
        raise SystemExit("image too large for this boot template")

    data[koff : koff + len(img)] = img

    # 新内核比旧内核短时，清掉尾部残留，避免旧字节被误当作有效内容
    if old_end > need_end:
        data[need_end:old_end] = b"\x00" * (old_end - need_end)

    struct.pack_into("<I", data, 8, len(img))

    with open(out_path, "wb") as f:
        f.write(data)

    print("written:", out_path, os.path.getsize(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())