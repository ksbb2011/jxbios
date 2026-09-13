# 整屏标定 App（iOS 传感端）—— 编译、安装与使用

配合电脑端 `tools/calibrate_full_grid.py` 使用。App 只做三件事：**显示靶心、上报触摸坐标、显示状态**。

> ⚠️ **我无法在 Windows 上编译验证 Swift 代码**（本项目开发环境是 Windows，手上也没有能跑 Xcode 16 的 Mac）。
> 源码与协议严格对齐、逻辑自洽，但**首次编译若报错是预期内的**，请把报错原文按下面模板回贴，我按报错改。
> 电脑端（Python）部分是经过离线自测的：`py -3.11 tools\calib_mock_phone.py` 断言全过（假机械臂 + 假手机真跑 HTTP 协议）。

---

## 一、基本信息

| 项 | 值 |
|---|---|
| 语言/框架 | Swift 5 + SwiftUI（含一个 UIKit 触摸画布） |
| 最低系统 | **iOS 15.0** |
| 设备 | iPhone（逻辑分辨率 **375×812** 的机型最匹配，如 iPhone X/8 Plus；换机型必须同步改电脑端 `--width/--height`，否则 App 会拒绝上报） |
| 网络 | 手机与电脑必须在**同一 Wi-Fi 网段**；电脑端监听 `0.0.0.0:8767` |
| 签名 | **未签名 ipa + Sideloadly + 免费 Apple ID**（7 天有效，到期重装一次）；不需要买证书 |
| 工程生成 | XcodeGen（`project.yml`）—— 云端编译无人手点 GUI，工程必须可生成 |

---

## 二、编译 → 安装：三条路线

三条路线产出的都是**同一个未签名 ipa**，装到手机的那半段完全一样（见第三节）。

| 路线 | 需要什么 | 耗时 | 适用 |
|---|---|---|---|
| **A. GitHub 云端编译（推荐）** | GitHub 账号 + 能访问 GitHub | 首次约 5~8 分钟 | 手上没有 Mac，且不想花钱 |
| **B. 租云 Mac** | 云 Mac（macOS ≥14.5、已装 Xcode、磁盘 ≥25GB） | 约 10~20 分钟 | 想远程桌面盯着日志改代码 |
| **C. 本机 Mac + 数据线** | 能装 Xcode 16 的 Mac + 数据线 | 约 10 分钟 | 以后买了 M 芯片 Mac 时的常驻方案 |

> 为什么都要"未签名 ipa"：云端**手机插不进去**，无法像本机 Xcode 那样 ⌘R 直装。所以云端只负责编出 `.app` 打成 `.ipa`，签名交给 Windows 上的 Sideloadly 用你自己的 Apple ID 完成。

### 路线 A：GitHub 云端编译（免费，无需 Mac）

1. 仓库已设为 **Public**（公开仓库才能免额度；私有仓库 macOS 构建按 10 倍扣免费额度，也能用但不宽裕）。
2. 打开仓库页 → **Actions** 标签 → 左侧选 **「编译 iOS 标定 App（未签名 ipa）」** → 右侧 **Run workflow** → 绿色按钮。
   （改了 `ios_calib/**` 并推到 `main` 也会自动触发，无需手动点。）
3. 等 5~8 分钟，运行页面变成绿勾 → 页面**最底部 Artifacts** 里下载 **`OrderPickerCalib-unsigned-ipa`**（是个 zip）。
4. 解压得到 `OrderPickerCalib-unsigned.ipa` → 走第三节安装。
5. **失败时**：点开失败的步骤，把红色报错按下面的模板发我。

> 想确认 runner 的 Xcode 版本档位：看运行的「环境自检」步骤输出（`xcodebuild -version`）。

### 路线 B：租云 Mac（远程桌面）

要求（选机时按这三条卡，否则编不了或装不上）：

| 项 | 要求 |
|---|---|
| macOS | **≥ 14.5**（要装 Xcode 16） |
| Xcode | 已安装 **Xcode（不是只有 Command Line Tools）** |
| 磁盘 | 空闲 **≥ 25GB** |

在云 Mac 的终端里：

```bash
# 1) 拉代码（公开仓库，免登录）
git clone https://gitee.com/ksbb2011/jxbios.git          # 或 https://github.com/ksbb2011/jxbios.git
cd jxbios

# 2) 装工程生成器
brew install xcodegen

# 3) 一条命令出包
bash ios_calib/build_ipa.sh
#    产物：ios_calib/build/ipa/OrderPickerCalib-unsigned.ipa
```

然后把 ipa 从云 Mac **下载到本机**（云服务商一般都有"下载/传输文件"功能；或先传到网盘再下）。

> 省钱提示：脚本第一步会打印 `sw_vers`/`xcodebuild -version`/SDK 版本/磁盘，**先只编译、别急着打包**——环境不对在这里就能看出来。编译报错比打包失败早暴露。

### 路线 C：本机 Mac + Xcode GUI + 数据线（兜底）

1. **新建工程**：Xcode → `File` → `New` → `Project…` → **iOS** → **App**
   - Product Name：`OrderPickerCalib`；Interface：**SwiftUI**；Language：**Swift**
   - 取消勾选 Core Data / Tests
2. **删掉自动生成的两个文件**（否则 `@main` 会重复）：`OrderPickerCalibApp.swift`、`ContentView.swift`
3. **拖入本目录 4 个源文件**（`ios_calib/OrderPickerCalib/`）：`CalibApp.swift`、`CalibClient.swift`、`TouchCanvas.swift`、`CalibView.swift`
   —— 勾选 *Copy items if needed*，且 **Add to targets: OrderPickerCalib 一定要打勾**
4. **改最低系统**：target → `General` → *Minimum Deployments* = **iOS 15.0**
5. **Info.plist 别手点**：直接把仓库里的 `ios_calib/Info.plist` 内容贴进 target 的 Info 页签（或把 `INFOPLIST_FILE` 指过去）。
   重点是这几项，**少任何一项都会"连不上"或"坐标整体错位"**：
   `UILaunchScreen`（空字典即可）、`App Transport Security Settings → Allow Local Networking = YES`、
   `Privacy - Local Network Usage Description`、`Status bar is initially hidden = YES`、
   `View controller-based status bar appearance = NO`、`Supported interface orientations` 只留 `Portrait`
6. **签名**：`Signing & Capabilities` → 勾 *Automatically manage signing* → Team 选你的 Apple ID
7. **手机准备**：设置 → 隐私与安全性 → **开发者模式** → 打开（要求重启一次）；数据线连 Mac，弹"信任此电脑"点信任
8. **运行**：选你的 iPhone → ⌘R

---

## 三、装到手机（Sideloadly + 免费 Apple ID）

> 前提：电脑上装 **Sideloadly**（sideloadly.io），并建议装一次 **iTunes**（提供 Apple 移动设备驱动；只装 Microsoft Store 版也行）。手机用数据线连电脑。

1. 手机连上电脑，确认 Sideloadly 顶部能识别到设备。
2. 把 `OrderPickerCalib-unsigned.ipa` 拖进 Sideloadly 的 IPA 框。
3. 在 **Apple ID** 处填你的**免费 Apple ID**（会弹 2FA 验证码就填验证码；不会保存在仓库里，别发给我）。
4. 点 **Start**。首次会自动帮你注册设备、申请 7 天开发签名。
5. 手机上：设置 → 通用 → **VPN与设备管理** → 信任你的 Apple ID（描述文件）。
6. 手机上：设置 → 隐私与安全性 → **开发者模式** → 打开并重启（iOS 16 及以上**必须**，否则装好了也打不开）。
7. 桌面出现 **标定** 图标 → 打开。

**7 天后过期怎么办**：重新走一遍第 3~4 步（同一个 Apple ID、同一个 ipa 即可）。
想省事：在 Windows 上装 **AltStore**（AltServer 常驻），可自动续签，不必手动重装。

> Sideloadly 失败时的兜底：让云 Mac 在 Xcode 里登录同一个免费 Apple ID，用 `Product → Archive → Distribute → Development` 导出一份 development ipa（需要先把手机 UDID 注册进账号）。

---

## 四、使用（配合电脑端）

1. 手机连上与电脑同一个 Wi-Fi。
2. 电脑上先看清单（不驱动机械臂）：
   ```powershell
   $env:PYTHONPATH=(Get-Location).Path
   py -3.11 tools\calibrate_full_grid.py --list
   ```
   记下输出里的 **建议 radius_px / 预计耗时 / 电脑局域网 IP**。
3. **先验连通性**（最省事的排错手段，不驱臂）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --dry
   ```
   然后 App 里点右上角齿轮，填电脑地址（形如 `192.168.1.23:8767`）→ 完成。
   - 顶部变蓝并显示"等待电脑开始…" → 通了 ✓
   - 一直灰色 + 底部红字 → 按 App 里"连不上时依次检查"四条排
   - **橙色 + "视口不符"** → 机型逻辑分辨率与电脑端 `--width/--height` 不一致，App 会拒绝上报（见第五节）
4. **正式跑一轮**（默认只测量、不写配置）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py
   ```
   - 手机显示"进行中 n/45"，机械臂逐点压下；白环 = 本次瞄准点，红点 = 靶点
   - **期间不要用手碰屏幕**（手点的坐标会被当成机械臂落点）
   - 手机"自动锁定"设永不（App 也会主动禁止锁屏，双保险）
5. **验收**：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --verify
   ```
   目标：第 2 轮（新模型未补偿）残差明显变小；第 3 轮（补偿后）**max ≤ 3pt**。
6. **写入配置**（确认数据合理后再做，会自动备份）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --verify --apply
   ```
   ⚠️ 会更新 `config/hardware.json` 的 `actuation` / `arm_range` / `tap_correction`，
   并**丢弃别的工具在旧映射下测的锚点**（refit 后它们方向已失效）——想保留加 `--keep-foreign-anchors`。

---

## 五、联调前须知（协议 + 已知风险）

**协议**（`CalibClient.swift` 头注释与电脑端一致）：

| 方向 | 内容 |
|---|---|
| `GET /state` | `phase`(`waiting`/`running`/`done`/`error`)、`note`、`id`(本 pass 内 0 基序号)、`seq`(跨轮次不重复)、`tx/ty`(靶点)、`aim_x/aim_y`(本次实际瞄准点，含补偿)、`cmd_ax/cmd_ay`(真正下发的机械臂坐标)、`total`、`done`、`expect_viewport`、`last_ack`(可能为 null) 等 |
| `POST /touch` | `id`、`x`、`y`、`t`(毫秒)、`viewport:[w,h]`、`kind`(`began`) |

**去重责任**：电脑端"该点第一条上报即采信、`id` 必须等于当前点序号"；手机端只上报 `began`，并对**已成功上报过的 `seq`** 不重报（失败的会在下一次触摸自动重试）。

**已知风险（已在 App 侧兜底 / 待电脑端收尾）**：

| # | 风险 | 现状 |
|---|---|---|
| 1 | 机型视口 ≠ 375×812 时，电脑端**只打 warning 不阻断**，会静默写出错误配置 | ✅ App 已兜底：视口不符时**拒绝上报**并橙色报警（在设置面板还能看到两个尺寸对照） |
| 2 | 电脑端"每点第一条上报即采信、无时间窗"，`settle`(1.5s) 期间的手碰会被当成机械臂落点 | ⚠️ 操作上规避：**过程中不要碰屏幕**；后续可加时间窗（电脑端待改） |
| 3 | 电脑端 `touch_event.clear()` 与 `last_touch = {}` 的顺序有极小竞态，可能"已收到却判 miss" | ⚠️ 待改（电脑端，影响极小） |
| 4 | 手机端上报失败不重试（原实现） | ✅ 已改为"只在成功后置位 `reportedSeq`"，失败会在下一次触摸自动重试 |
| 5 | `CalibClient` 未标 `@MainActor`，Swift 6 严格并发下会报错 | ⚠️ 当前 `SWIFT_VERSION: 5.0`（仅警告）；将来升 Swift 6 需加注解 |

> 电脑端（`tools/` 下 Python）本轮**刻意冻结未改**：整屏网格工具的菜单接入、产物刷新与文档增补，等真机跑通一轮后再收尾。

---

## 六、常见问题

| 现象 | 原因与处理 |
|---|---|
| GitHub Actions 跑失败 | 点开失败步骤看红色 `error:`；把报错按第七节模板发我。常见：`xcodegen` 没装上（brew 慢）、`macos-latest` 标签不可用（改用 `macos-15`） |
| App 一直"未连接" | ① 防火墙没放行入站 8767 ② 不同网段 ③ `Info.plist` 少了 `Allow Local Networking` 或本地网络用途描述 ④ 手机设置里没允许本 App"本地网络" |
| App 橙色"视口不符" | 机型逻辑分辨率 ≠ 电脑端 `--width/--height`；改电脑端参数或换回 375×812 机型（设置面板里能看到两个尺寸） |
| 电脑端报"未收到手机上报，已中止" | 与上一行同因；另查手机是否熄屏、App 是否在前台、是否被系统弹窗挡住 |
| 电脑端算出的残差异常大（几十 pt） | 多为**画布没铺满**：底部诊断行里"画布/窗口"两个尺寸应一致（都 ≈375×812）。不一致说明被安全区挤了 |
| 手机显示 `viewport_bad` 计数 > 0 | 上报视口与电脑端期望不符 → 换机型时两处要一起改 |
| 第 3 轮误差仍 >3pt | 网格太稀（试 `--cols 6 --rows 10`）；或机械臂本身重复定位差（先跑 `校准工具.exe` 菜单 1/2/4 复检） |
| 装好后打不开 / "未受信任的开发者" | iOS 16+ 要开**开发者模式**（设置 → 隐私与安全性 → 开发者模式 → 重启）；再看 VPN与设备管理里信任描述文件 |
| 7 天后 App 打不开 | 免费签名过期，用 Sideloadly 重装一次（或用 AltStore 自动续签） |

---

## 七、文件说明

| 文件 | 作用 |
|---|---|
| `project.yml` | XcodeGen 工程描述（`xcodegen generate` 生成 `.xcodeproj`，云端无人点 GUI 时必须） |
| `Info.plist` | 权限与显示配置固化：ATS 明文、本地网络授权、**`UILaunchScreen`（缺了坐标会整体错位）**、隐藏状态栏、只竖屏 |
| `build_ipa.sh` | **唯一构建入口**：环境自检 → xcodegen → `xcodebuild` 未签名 Release → 打 `Payload/` 成 ipa（CI 与云 Mac 都调它） |
| `OrderPickerCalib/CalibApp.swift` | App 入口 |
| `OrderPickerCalib/CalibClient.swift` | 网络层：轮询 `GET /state`、上报 `POST /touch`，含视口自检、幂等重报、连接状态 |
| `OrderPickerCalib/CalibView.swift` | 主界面：状态条 + 进度 + 靶心（白环=瞄准点、红点=靶点）+ 底部诊断行 + 设置面板 |
| `OrderPickerCalib/TouchCanvas.swift` | 全屏触摸画布（`touchesBegan` → UIKit 逻辑点 + 当帧尺寸） |
| `../.github/workflows/ios-ipa.yml` | GitHub Actions：装 XcodeGen → 调 `build_ipa.sh` → 上传 ipa 产物 |

### 编译报错回贴模板

```
环境：<粘贴运行的"环境自检"输出，或 sw_vers / xcodebuild -version>
命令：bash ios_calib/build_ipa.sh
报错：<从 "error:" 那一行往前 5 行、往后 5 行原样粘贴>
```
