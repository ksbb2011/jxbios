# Xcode 全英文界面操作指南（编译「整屏标定 App」）

> 用途：在 Mac 上从零把 `ios_calib/OrderPickerCalib/` 编进 iPhone。
> **Xcode 官方从来没有中文界面**，别去设置里找了 —— 本文把"英文按钮 → 你要做什么"逐条对上。
> 目标产物：手机上出现一个全屏黑的「标定」App，能连上电脑的 `calibrate_full_grid.py`。

---

## 0. 先认清 4 个位置（后面全靠它们）

| 英文 | 在哪 | 干什么 |
|---|---|---|
| **File → New → Project…** | 屏幕顶部菜单栏 | 新建工程 |
| **Navigator（⌘1）** | 窗口最左侧竖条，第一个图标 | 看文件 / 拖文件进来 |
| **General / Info / Signing & Capabilities** | 左侧点**蓝色工程图标**后，中间出现的页签 | 改系统版本 / 加 Info 键 / 配签名 |
| **顶栏中间那个设备下拉框** | 窗口最上一行 | 选真机（你的 iPhone） |

> 找不到欢迎窗口时：`File → New → Project…`；想调出欢迎窗口：`Window → Welcome to Xcode`。

---

## 1. 新建工程

1. `File → New → Project…`（或欢迎窗口点 **Create New Project**）
2. 顶部选 **iOS** 页签 → 选 **App** → 点 `Next`
3. 填三项（其余保持默认）：
   - **Product Name**：`OrderPickerCalib`
   - **Interface**：**SwiftUI**
   - **Language**：**Swift**
   - **Core Data / Tests 全部不勾**
4. 点 `Next` → 保存位置选 **`~/jxb/jxbios/ios_calib/`**
   （放仓库里，这样工程也在 git 里，两边都能看到、能改）
5. 等它把界面画出来。首次启动可能提示安装组件 / 输密码 —— 输 **Mac 登录密码**即可。

---

## 2. 删掉 Xcode 自带的两个文件（不删会因 `@main` 重复编译失败）

在左侧 **Navigator** 里：

1. 右键 `OrderPickerCalibApp.swift` → **Delete** → 选 **Move to Trash**
2. 右键 `ContentView.swift` → **Delete** → **Move to Trash**

---

## 3. 拖入 4 个源文件

1. Finder 打开 `~/jxb/jxbios/ios_calib/OrderPickerCalib/`，能看到 4 个 `.swift`：
   `CalibApp.swift`、`CalibClient.swift`、`CalibView.swift`、`TouchCanvas.swift`
2. 全选 → **拖进 Xcode 左侧 Navigator**
3. 弹出的对话框里**必须勾两处**：
   - ✅ **Copy items if needed**
   - ✅ **Add to targets** 里的 `OrderPickerCalib`
   然后 `Finish`

---

## 4. 设最低系统版本

左侧点**蓝色工程图标** → 中间 **General** 页签 → **Minimum Deployments** 改成 **iOS 15.0**。

---

## 5. 加 Info 键（最容易卡的一步）

先在左侧选中 **TARGET**（在工程图标**下面**，名字也叫 `OrderPickerCalib`，图标是白色小方块）→ 点 **Info** 页签。

加一条的方法：把鼠标移到列表区域 → 会出现 **`＋`** 圆形按钮 → 点它 → 输入 Key → 选 Type → 填 Value。

| Key（照抄） | Type | Value |
|---|---|---|
| `App Transport Security Settings` | **Dictionary** | （不用填值，加完点它左边 **▸** 展开） |
| └ `Allow Local Networking` | **Boolean** | **YES** |
| `Privacy - Local Network Usage Description` | **String** | `用于与电脑上的标定工具通信，采集触摸坐标` |
| `Status bar is initially hidden` | **Boolean** | **YES** |
| `View controller-based status bar appearance` | **Boolean** | **NO** |
| `Supported interface orientations` | **Array** | 只留一项 `Portrait` |

> 展开 Dictionary 后加子项：点 `App Transport Security Settings` 左边的 **▸**，把鼠标移到展开出来的空白行 → 点出现的 **`＋`**。
> **三条为什么必须加**：① 不加 ATS 例外 → iOS 拒绝明文 HTTP，永远连不上；② 不加本地网络用途描述 → iOS 14+ 会**静默**拒绝局域网访问（现象是"一直连不上"，最难查）；③ 不隐藏状态栏 → 界面不是真全屏，坐标口径会偏。

---

## 6. 签名 + 跑真机

1. 左侧 TARGET → **Signing & Capabilities** 页签 → 勾 **Automatically manage signing**
2. **Team** 下拉 → `Add an Account…` → 用 **Apple ID** 登录（**免费账号即可**）→ 选它
3. 若提示 bundle id 冲突 → 把 **Bundle Identifier** 改成 `com.ksbb2011.OrderPickerCalib`
4. iPhone 用**数据线**连 Mac → 手机上点「信任此电脑」
5. **iOS 16 及以上**：手机 → 设置 → 隐私与安全性 → 最下面 **开发者模式** → 打开 → **重启手机**
6. Xcode 顶栏中间的设备下拉 → 选你的 **iPhone** → 按 **⌘R**
7. 首次运行手机提示「不受信任的开发者」→ 手机 → 设置 → 通用 → **VPN与设备管理** → 信任你的 Apple ID
8. App 启动后：点右上角**齿轮** → 填电脑地址（形如 `192.168.1.23:8767`）→ 完成 → 顶部变蓝 = 已连上 ✓

> 免费 Apple ID 签名的 App **7 天后过期**，重新按 ⌘R 一次即可，不用买证书。

---

## 7. 常见红字对照

| Xcode 报错 | 意思 | 怎么办 |
|---|---|---|
| `Signing for "..." requires a development team` | 没选团队 | 回到第 6 节第 1~2 步 |
| `Failed to register bundle identifier` | bundle id 被占用 | 改 Bundle Identifier（加名字/数字） |
| `Cannot find 'CalibView' in scope` | 少文件 / 没加到 target | 回到第 3 节，确认 4 个文件都在 Navigator 且勾了 target |
| `Multiple commands produce` / `@main` 重复 | 自带文件没删 | 回到第 2 节 |
| `The request was denied by service delegate` 之类 | 手机没连好/没信任 | 重插线、重信任、确认设备下拉选的是真机 |
| App 起来后一直"未连接" | 网络/权限 | ① 电脑防火墙放行入站 8767 ② 手机与电脑同一 Wi-Fi ③ 第 5 节的 Info 键是否齐 ④ 手机设置里允许本 App「本地网络」 |

---

## 8. 编完之后怎么标定

在**电脑上**（Windows）：

```powershell
$env:PYTHONPATH=(Get-Location).Path
py -3.11 tools\calibrate_full_grid.py --dry      # 只验连通（不驱臂），App 顶部应变蓝
py -3.11 tools\calibrate_full_grid.py            # 正式一轮（约 6 分钟，只测不写配置）
py -3.11 tools\calibrate_full_grid.py --verify --apply   # 验收 + 写回配置
```

详细说明见 `docs\新电脑_校准指南.md` 第 4.1 节；协议与端口（**8767**）见 `tools\calibrate_full_grid.py` 头部注释。
