# 整屏标定 App（iOS 传感端）—— 编译与使用

配合电脑端 `tools/calibrate_full_grid.py` 使用。App 只做三件事：**显示靶心、上报触摸坐标、显示状态**。

> ⚠️ **我无法在 Windows 上编译验证 Swift 代码**（本项目开发环境是 Windows）。这 4 个源文件与协议严格对齐、逻辑自洽，但**首次编译若报错，请把报错原文发我**，我按报错改。
> 电脑端（Python）的部分是经过离线自测的：`py -3.11 tools\calib_mock_phone.py` 13 项断言全过。

---

## 一、基本信息

| 项 | 值 |
|---|---|
| 语言/框架 | Swift 5 + SwiftUI（含一个 UIKit 触摸画布） |
| 最低系统 | **iOS 15.0** |
| 设备 | iPhone（iPhone X 及以上，逻辑分辨率 375×812 最匹配；换机型要同步改电脑端 `--width/--height`） |
| 网络 | 手机与电脑必须在**同一 Wi-Fi 网段**；电脑端监听 `0.0.0.0:8767` |
| 签名 | 用**免费 Apple ID** 即可真机运行（7 天有效，到期重新 Run 一次）；不需要买证书 |

---

## 二、编译步骤（Xcode，约 5 分钟）

1. **新建工程**：Xcode → `File` → `New` → `Project…` → **iOS** → **App**
   - Product Name：`OrderPickerCalib`
   - Interface：**SwiftUI**，Language：**Swift**
   - 取消勾选 Core Data / Tests（不需要）
2. **删掉自动生成的两个文件**（否则 `@main` 会重复）：
   - `OrderPickerCalibApp.swift`
   - `ContentView.swift`
3. **拖入本目录的 4 个源文件**（`ios_calib/OrderPickerCalib/` 下）：勾选 *Copy items if needed*，且 **Add to targets: OrderPickerCalib 一定要打勾**
   - `CalibApp.swift`
   - `CalibClient.swift`
   - `TouchCanvas.swift`
   - `CalibView.swift`
4. **改最低系统**：选中工程 → target `OrderPickerCalib` → `General` → *Minimum Deployments* 设为 **iOS 15.0**
5. **配置 Info.plist**（target → `Info` 页签，逐条加）：
   | Key | Type | Value | 为什么必须加 |
   |---|---|---|---|
   | `App Transport Security Settings` | Dictionary | 见下一行 | iOS 默认禁止明文 HTTP，不加**连不上** |
   | └ `Allow Local Networking` | Boolean | **YES** | 允许访问局域网明文 HTTP |
   | `Privacy - Local Network Usage Description` | String | `用于与电脑上的标定工具通信，采集触摸坐标` | iOS 14+ 访问局域网会弹权限框，不填会**静默失败** |
   | `Status bar is initially hidden` | Boolean | **YES** | 让界面真全屏（本 App 是"整屏压测"用途） |
   | `View controller-based status bar appearance` | Boolean | **NO** | 与上一行配套 |
   | `Supported interface orientations` | Array | 只留 `Portrait` | 防止旋转导致坐标口径变化 |
6. **签名**：target → `Signing & Capabilities` → 勾 *Automatically manage signing* → Team 选你的 Apple ID（没加过就 `Add an Account…` 登录）→ 若提示 bundle id 冲突，把 `Bundle Identifier` 改成 `com.你的名字.OrderPickerCalib`
7. **手机准备**：设置 → 隐私与安全性 → **开发者模式** → 打开（会要求重启一次）；用数据线连电脑，弹"信任此电脑"点信任
8. **运行**：Xcode 顶部选你的 iPhone → ⌘R
   - 首次运行手机可能提示"不受信任的开发者"：设置 → 通用 → VPN与设备管理 → 信任你的 Apple ID
   - **7 天后过期**：重新 ⌘R 一次即可

---

## 三、使用（配合电脑端）

1. 手机连上与电脑同一个 Wi-Fi。
2. 电脑上跑（先看清单不驱动机械臂）：
   ```powershell
   $env:PYTHONPATH=(Get-Location).Path
   py -3.11 tools\calibrate_full_grid.py --list
   ```
   记下输出里的 **建议 radius_px / 预计耗时 / App 里填的地址**。
3. **先验连通性**（不驱臂，最省事的排错手段）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --dry
   ```
   然后在 App 里点右上角齿轮，填电脑地址（形如 `192.168.1.23:8767`），点"完成"。
   - 顶部变蓝并显示"等待电脑开始…" → 通了 ✓
   - 一直灰色 + 底部红字 → 按 App 里"连不上时依次检查"那四条排；
     手机没编译好时也可用假手机验证电脑侧：`py -3.11 tools\calib_mock_phone.py --client http://127.0.0.1:8767`
4. **正式跑一轮**（默认只测量、不写配置）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py
   ```
   - 手机会显示"进行中 n/45"，机械臂逐点压下；屏幕上白环=本次瞄准点、红点=靶点
   - **手机屏自动锁定必须关掉**（设置 → 显示与亮度 → 自动锁定 → 永不；App 也会主动禁止锁屏，双保险）
   - 期间**不要用手碰屏幕**（手点的坐标会被当成机械臂落点）
5. **验收**（再复压一轮，给出补偿后误差）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --verify
   ```
   目标：第 2 轮（新模型，未补偿）残差明显变小、第 3 轮（补偿后）**max ≤ 3pt**。
6. **写入配置**（确认数据合理后再做，会自动备份）：
   ```powershell
   py -3.11 tools\calibrate_full_grid.py --verify --apply
   ```
   ⚠️ 会更新 `config/hardware.json` 的 `actuation` / `arm_range` / `tap_correction`，
   并**丢弃别的工具在旧映射下测的锚点**（refit 后它们方向已失效）——想保留加 `--keep-foreign-anchors`。

---

## 四、常见问题

| 现象 | 原因与处理 |
|---|---|
| App 一直"未连接" | ① 防火墙没放行入站 8767（首次运行会弹窗）② 不同网段 ③ `Info.plist` 少了 `Allow Local Networking` 或 `本地网络用途描述` ④ 手机设置里没允许本 App"本地网络" |
| 电脑端报"未收到手机上报，已中止" | 与上一行同因；另查手机是否熄屏、App 是否被切到后台 |
| 电脑端算出的残差异常大（几十 pt） | 多为**画布没铺满**：底部诊断行里"画布/窗口"两个尺寸应一致（都 ≈375×812）。不一致说明被安全区挤了，检查 `.ignoresSafeArea()` |
| 手机显示 `viewport_bad` 计数 > 0 | 说明上报的视口尺寸与电脑端 `--width/--height` 不符 → 换机型时两处要一起改 |
| 第 3 轮误差仍然 >3pt | 网格太稀（试 `--cols 6 --rows 10`，耗时线性增加）；或机械臂本身重复定位差（先跑 `校准工具.exe` 菜单 1/2/4 复检） |
| 手机会自己黑屏 | 自动锁定没关；App 已设 `isIdleTimerDisabled`，但被系统强杀/切后台后会失效 |
| 7 天后 App 打不开 | 免费签名过期，重新 ⌘R 一次 |

---

## 五、文件说明

| 文件 | 作用 |
|---|---|
| `CalibApp.swift` | App 入口 |
| `CalibClient.swift` | 网络层：轮询 `GET /state`、上报 `POST /touch`，含连接状态与错误提示 |
| `CalibView.swift` | 主界面：状态条 + 进度 + 靶心 + 底部诊断行 + 设置面板 |
| `TouchCanvas.swift` | 全屏触摸画布（`touchesBegan` → UIKit 逻辑点坐标） |
