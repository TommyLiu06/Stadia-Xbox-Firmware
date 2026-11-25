# 固件源码

`bruce_pvt_xbox` 的完整可编译源码。

刷入步骤见上一级目录的 [README](../README.md)。

## 环境

Linux，Python 3.10 以上。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

只有一个依赖 `ziglang`，它自带 Zig 编译器，不需要单独装 Zig。构建脚本调用 `python -m ziglang cc` 交叉编译。

## 输入

需要原厂量产镜像 `bruce_pvt_a_prod_signed.bin`（1,050,984 字节，SHA-256 `466ddd2dbcd8d1a66e2826f25849a6e5e45d9bb7e696e3fe0d500af56a5507a3`）。

本仓库里已经有一份，就在刷写工具目录下：

```
../StadiaController-main/stadiatool/data/bruce_pvt_a_prod_signed.bin
```

脚本会校验哈希，传错文件直接报错退出。

## 编译

```bash
python3 build.py ../StadiaController-main/stadiatool/data/bruce_pvt_a_prod_signed.bin
```

输出：

```
payload 2092 B, report map 283 B
wrote   bruce_pvt_xbox.EXPERIMENTAL-INVALID-SIGNATURE.bin (1050984 bytes)
sha256  e2d681121fd9248c2c34e7948f93a96b48249fa1e744552a4885272a04a82c72
```

哈希对上就说明产物和已实测的镜像一致。

产物路径也可以自己指定，加第二个参数即可：`python3 build.py <base.bin> <out.bin>`。

## 文件

| 文件 | 作用 |
|---|---|
| `build.py` | 构建脚本：交叉编译 + 打补丁，一步出镜像 |
| `bruce_direct.c` | 变体主体。USB / BLE 收发钩子，档位切换与记忆 |
| `stadia_xinput.c` | 协议转换。HID ↔ 20 字节 XUSB、HID ↔ 16 字节 Series、震动回译 |
| `stadia_xinput.h` | 报告布局、档位表、公共声明 |
| `bruce_direct.ld` | 连接脚本。payload 落在 `0x6013D5A8`，段首是 8 项回调表 |
| `series_report_map.h` | 283 字节 Xbox Series HID 报告映射 |
| `requirements.txt` | 依赖 |

`build.py` 里固化了镜像布局：payload 位置与上限 `0x840`、签名区 `0xFF000`、以及所有需要改写的函数指针地址。这些值改错会生成跑不起来的镜像，脚本对每处源字节都做了校验，不匹配就抛异常。

## 动了代码会怎样

改 C 源码后重新构建，哈希会变，这是正常的。改之前建议先跑一次确认基线，产物哈希应当是 `e2d68112…`。

payload 有 `0x840` 字节上限，超出时链接脚本的 `ASSERT` 会直接让编译失败。

## 注意

产物的 HAB 签名是失效的（内容被改过）。只有在已经烧掉 `FIELD_RETURN` eFuse 的手柄上才能启动，否则刷进去也开不了机。
