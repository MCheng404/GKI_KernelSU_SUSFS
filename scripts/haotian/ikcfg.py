#!/usr/bin/env python3
"""从内核 Image 中提取内嵌 IKCFG（CONFIG_IKCONFIG），刷机前预检配置。

为什么需要它：
    bazel 分支的 ksu.fragment 由 `diff defconfig.orig defconfig | grep '^>'` 生成，
    只捞新增行。删行不生效，改值行也会被静默丢弃。所以「workflow 里写了」
    不等于「真的编进去了」。刷之前直接读 Image 里的配置，能省一轮刷机。

用法：
    python ikcfg.py <Image> [过滤关键字]

    默认过滤 KSU / SUSFS 相关行；传入关键字则按该关键字过滤；
    传入 ALL 则输出完整配置。

haotian 6.6.118 的 IKCFG_ST offset 恒为 17840288。
"""

import gzip
import io
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = sys.argv[1]
    needle = sys.argv[2] if len(sys.argv) > 2 else None

    with open(path, "rb") as f:
        blob = f.read()

    start = blob.find(b"IKCFG_ST")
    print("IKCFG_ST offset:", start)
    if start < 0:
        raise SystemExit("未找到 IKCFG_ST：该内核可能未开启 CONFIG_IKCONFIG")

    end = blob.find(b"IKCFG_ED", start)
    raw = blob[start + 8 : end]

    try:
        cfg = gzip.decompress(raw).decode("utf-8", "replace")
    except Exception:
        # 尾部可能带填充字节，用流式解压容忍 trailing garbage
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            cfg = gz.read().decode("utf-8", "replace")

    lines = cfg.splitlines()

    if needle == "ALL":
        selected = lines
        title = "full config"
    elif needle:
        selected = [l for l in lines if needle in l]
        title = f"config matching {needle!r}"
    else:
        selected = [l for l in lines if "KSU_SUSFS" in l or "CONFIG_KSU" in l]
        title = "KSU/SUSFS config"

    print(f"--- {title} ({len(selected)} lines) ---")
    for line in selected:
        print(" ", line)

    return 0


if __name__ == "__main__":
    sys.exit(main())