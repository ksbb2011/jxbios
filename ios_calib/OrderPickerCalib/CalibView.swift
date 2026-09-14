//
//  CalibView.swift
//  整屏标定的手机端界面（黑底仪表风：整屏只有靶心 + 顶部状态 + 底部一行诊断信息）
//
//  设计约束（别改）：
//    · 屏幕上除了靶心**不放任何可点元素**：机械臂会实体压在这块屏幕上，
//      多余控件既可能被误触，也会让人分不清"是机械臂压的还是手点的"。
//    · 不做点击反馈动画：视觉反馈会和"机械臂真的压下来了"混淆。
//    · 触摸被 TouchCanvas 全屏消费；设置面板只在非运行状态下才建议打开。
//
//  免责：本文件在 Windows 上无法编译验证（无 Mac/Xcode 环境），只保证逻辑与协议自洽。

import SwiftUI
import UIKit          // UIApplication.shared.isIdleTimerDisabled 需要（只 import SwiftUI 时可能编不过）

/// 采集通道选择（排查用，设置里可切；默认 plain 原生视图）
enum CaptureMode: String {
    case plain      // 全屏原生 UIView：直接取触点坐标，不依赖 PencilKit 合成笔迹
    case pencil     // PencilKit 对照通道：能画出笔迹，是"系统认不认这支笔"的直观旁证
}

struct CalibView: View {

    @StateObject private var client = CalibClient()
    /// 多通道原始触摸汇聚中心（窗口钩子 + 原生视图 + 手势 + PencilKit）
    @ObservedObject private var hub = TouchHub.shared
    @AppStorage("serverURL") private var serverURL: String = "192.168.1.100:8767"
    @AppStorage("captureMode") private var captureModeRaw: String = CaptureMode.plain.rawValue
    @State private var showSettings = false
    @State private var canvasSize: CGSize = .zero
    @State private var windowSize: CGSize = .zero
    @State private var didAutoOpenSettings = false
    @Environment(\.scenePhase) private var scenePhase

    private var captureMode: CaptureMode { CaptureMode(rawValue: captureModeRaw) ?? .plain }

    var body: some View {
        GeometryReader { _ in
            ZStack {
                // ① 全屏采集画布：默认走 **plain 原生视图**（最稳），可在设置里切到 PencilKit 对照。
                //    两条通道、0 延迟手势识别器以及**窗口级钩子**都会把原始事件送进 TouchHub，
                //    由 TouchHub 合并去重后回调上报（见 TouchDiagnostics.swift）。
                Group {
                    if captureMode == .pencil {
                        PencilCanvas(
                            onSample: { hub.ingest($0) },
                            onMeta: { viewSize, winSize in
                                canvasSize = viewSize
                                if let w = winSize { windowSize = w }
                            }
                        )
                    } else {
                        TouchCanvas(
                            onSample: { hub.ingest($0) },
                            onMeta: { viewSize, winSize in
                                canvasSize = viewSize
                                if let w = winSize { windowSize = w }
                            }
                        )
                    }
                }
                .ignoresSafeArea()

                // ② 靶心：白环 = 本次瞄准点，红点 = 本轮靶点
                if client.phase == "running" {
                    Circle()
                        .stroke(Color.white.opacity(0.9), lineWidth: 3)
                        .frame(width: 80, height: 80)
                        .position(x: client.aimX, y: client.aimY)
                    Circle()
                        .fill(Color(red: 1.0, green: 0.22, blue: 0.22))
                        .frame(width: 14, height: 14)
                        .position(x: client.tx, y: client.ty)
                }

                // ②b 最近一次**实际收到**的触摸位置（青色小点，纯诊断）
                //     它回答"屏幕认为你点在哪儿"；与白环（应到位置）一对比，
                //     立刻就能分辨是"根本没压到屏"还是"压到了但偏了"。不参与任何计算。
                if client.touchSeen > 0 {
                    Circle()
                        .fill(Color.cyan.opacity(0.5))
                        .frame(width: 9, height: 9)
                        .position(x: client.lastTouchX, y: client.lastTouchY)
                        .allowsHitTesting(false)
                }

                // ③ 顶部状态条 + 细进度条 + 右上角设置
                VStack(spacing: 6) {
                    HStack(spacing: 10) {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 10, height: 10)
                            .allowsHitTesting(false)
                        Text(statusText)
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundColor(.white)
                            .lineLimit(1)
                            .allowsHitTesting(false)
                        Spacer(minLength: 8)
                        // 运行中把齿轮藏起来：一是标定期间不必改地址，二是要保证顶部这条
                        // **完全没有任何可点元素**——网格最高一行正好压在这里，任何可交互
                        // 元素都会把触摸吃掉（真机实测：第 1 个点就是这么丢的）。
                        // 另外：**未连接电脑时也要显示**（否则电脑端退出后 phase 会卡在
                        // running，齿轮永远不出现，想改地址都点不到）。
                        if !client.connected || client.phase != "running" {
                            Button {
                                showSettings = true
                            } label: {
                                Image(systemName: "gearshape.fill")
                                    .font(.system(size: 16))
                                    .foregroundColor(.white.opacity(0.55))
                                    // 点击区放大到 44×44（iOS 最小可点尺寸）：
                                    // 原来只有图标那么大约 16pt，手指很难点中。
                                    .frame(width: 44, height: 44)
                                    .contentShape(Rectangle())
                            }
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    // 背景色也要显式关掉 hit-testing：SwiftUI 的 Color 默认可点，会吃掉触摸
                    .background(Color.black.opacity(0.55).allowsHitTesting(false))
                    .cornerRadius(12)

                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Rectangle().fill(Color.white.opacity(0.12))
                            Rectangle().fill(statusColor)
                                .frame(width: g.size.width * progress)
                        }
                    }
                    .frame(height: 3)
                    .allowsHitTesting(false)
                    .cornerRadius(1.5)

                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.top, client.phase == "running" ? 6 : 0)

                // ④ 底部：诊断面板（**仅非运行态**）+ 一行诊断信息
                //    运行中必须让整屏干净：网格最高一行正好压在顶部胶囊区，
                //    底部这块也要避免遮挡机械臂落点（历史真机实测：第 1 个点就是这么丢的）。
                VStack(spacing: 6) {
                    Spacer()
                    if client.phase != "running" {
                        DiagnosticPanel(hub: hub)
                            .padding(.horizontal, 10)
                    }
                    Text(footerText)
                        // 放大加亮 + 补"采信 N / 最近触摸"：现场判断"App 有没有收到触摸"
                        // 全靠这一行，原来 11pt/0.42 太暗，调试时根本看不清。
                        .font(.system(size: 14, weight: .medium, design: .monospaced))
                        .foregroundColor(.white.opacity(0.8))
                        .lineLimit(3)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 10)
                        .padding(.bottom, 6)
                }
                .allowsHitTesting(false)   // 底部整块只是显示，绝不能截住触摸
            }
        }
        .background(Color.black)
        .ignoresSafeArea()
        .statusBar(hidden: true)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true      // 防自动锁屏（整轮标定要几分钟）
            // TouchHub 的"首个按下样本"是**唯一**的上报来源：
            // 无论窗口钩子 / 原生视图 / 手势哪条通道先拿到，都在这里统一计数与上报，
            // 因此不再依赖 PencilKit 是否合成出一整笔。
            hub.onFirstSample = { s in
                client.noteTouch(x: s.x, y: s.y)                 // 现场计数 + 最近触摸显示
                Task {
                    await client.report(x: s.x, y: s.y, kind: "began",
                                        w: Double(s.viewport.width),
                                        h: Double(s.viewport.height))
                }
            }
            client.start(base: serverURL)
            // 兜底入口：地址没填对 / 电脑端还没跑时，右上角齿轮又小又难戳。
            // 12 秒后若仍未连上、且从未成功上报过，就把设置面板自动打开一次。
            Task {
                try? await Task.sleep(nanoseconds: 12_000_000_000)
                if !client.connected && client.reports == 0 && !didAutoOpenSettings {
                    didAutoOpenSettings = true
                    showSettings = true
                }
            }
        }
        .onChange(of: serverURL) { _ in
            client.start(base: serverURL)
        }
        .onChange(of: scenePhase) { phase in
            // 回前台重新点亮常亮并保证轮询在跑；退后台就交还系统（否则整机耗电）
            if phase == .active {
                UIApplication.shared.isIdleTimerDisabled = true
                client.start(base: serverURL)
            } else if phase == .background {
                UIApplication.shared.isIdleTimerDisabled = false
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(serverURL: $serverURL,
                         captureModeRaw: $captureModeRaw,
                         client: client,
                         canvasText: canvasText)
        }
    }

    // MARK: - 文案与状态

    private var progress: Double {
        guard client.total > 0, client.phase == "running" else { return client.phase == "done" ? 1 : 0 }
        return min(max(Double(client.index + 1) / Double(client.total), 0), 1)
    }

    private var statusText: String {
        if !client.connected { return "未连接电脑（点右上角齿轮填地址）" }
        // 视口不符优先报警：这时继续上报只会把错误坐标写进配置，必须让人先看见
        if client.viewportMismatch {
            return "视口不符 \(canvasText) ≠ 期望 \(expectText)（已停止上报）"
        }
        switch client.phase {
        case "running": return "进行中 \(client.index + 1)/\(client.total)"
        case "done":    return "标定完成 ✓"
        case "error":   return "电脑端已中止：" + client.note
        default:        return client.note.isEmpty ? "等待电脑开始…" : client.note
        }
    }

    private var statusColor: Color {
        if !client.connected { return Color(white: 0.45) }
        if client.viewportMismatch { return Color(red: 1.0, green: 0.58, blue: 0.0) }
        switch client.phase {
        case "done":  return Color(red: 0.19, green: 0.82, blue: 0.35)
        case "error": return Color(red: 0.84, green: 0.0, blue: 0.0)
        case "running": return Color(red: 0.04, green: 0.52, blue: 1.0)
        default:        return Color(white: 0.62)   // waiting 用灰色，和"进行中"的蓝色区分开
        }
    }

    private var canvasText: String {
        "\(Int(canvasSize.width))×\(Int(canvasSize.height))"
    }

    private var expectText: String {
        "\(client.expectViewport.first ?? 0)×\(client.expectViewport.last ?? 0)"
    }

    private var footerText: String {
        var line1 = "\(client.baseURL.isEmpty ? "-" : client.baseURL)   " +
                    "画布 \(canvasText)   " +
                    "窗口 \(Int(windowSize.width))×\(Int(windowSize.height))   " +
                    "期望 \(expectText)"
        // 采信数**始终显示**（未连接时也显示）——"屏到底有没有收到触摸"是现场第一问题
        line1 += "   采信 \(client.touchSeen)   上报 \(client.reports)"
        if client.connected {
            // 采信 = TouchHub 判定为"一次按下"的次数（=最终会被上报的次数）
            // 上报 = 成功发给电脑的次数（只在电脑端 running 时才会涨）
            line1 += "   轮询 \(client.polls)"
            line1 += "\n最近触摸 \(client.lastTouchAt) @ \(client.lastTouchText)" +
                     "   电脑收到 \(client.lastAckText)"
        } else {
            line1 += "\n未连接：\(client.lastError)"
        }
        return line1
    }
}

// MARK: - 现场诊断面板（只在非运行态显示，且完全不响应点击）

/// 它回答现场最关键的两个问题：
///   ① 系统到底有没有把触摸交给本 App（看各通道计数：窗口/视图/手势/Pencil）
///   ② 交来的是什么（type / phase / 坐标 / 半径 / 力度），据此判断该用哪条通道上报
private struct DiagnosticPanel: View {
    @ObservedObject var hub: TouchHub

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("版本 \(BuildStamp.text)")
                .foregroundColor(.yellow.opacity(0.9))
            Text("通道 \(hub.countsText)   采信 \(hub.pressCount)")
                .foregroundColor(.green.opacity(0.95))
            if hub.recent.isEmpty {
                Text("暂无触摸事件：用机械臂点屏，这里会出现原始事件")
                    .foregroundColor(.white.opacity(0.5))
            } else {
                // 用下标做 id：TouchSample 不是 Identifiable，且元组元素不能当 key path，
                // 所以按 indices + \.self 遍历（数组元素少，开销可忽略）。
                ForEach(hub.recent.indices, id: \.self) { i in
                    Text(hub.recent[i].line).foregroundColor(.cyan.opacity(0.95))
                }
            }
        }
        .font(.system(size: 10, weight: .medium, design: .monospaced))
        .lineLimit(1)
        .minimumScaleFactor(0.6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
        .background(Color.black.opacity(0.6))
        .cornerRadius(8)
        .allowsHitTesting(false)   // 诊断面板绝不能截住触摸
    }
}

// MARK: - 设置（填电脑地址）

private struct SettingsView: View {
    @Binding var serverURL: String
    @Binding var captureModeRaw: String
    @ObservedObject var client: CalibClient
    var canvasText: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationView {
            Form {
                Section(header: Text("电脑地址（跑 calibrate_full_grid.py 的机器）")) {
                    TextField("192.168.1.100:8767", text: $serverURL)
                        .keyboardType(.numbersAndPunctuation)
                        .autocorrectionDisabled(true)
                        .textInputAutocapitalization(.never)
                }
                Section(header: Text("采集通道（排查用）")) {
                    Picker("采集通道", selection: $captureModeRaw) {
                        Text("原生视图（推荐）").tag(CaptureMode.plain.rawValue)
                        Text("PencilKit 对照").tag(CaptureMode.pencil.rawValue)
                    }
                    .pickerStyle(.segmented)
                    Text("默认「原生视图」：直接把触点坐标上报，不依赖 PencilKit 是否合成出一整笔。切到「PencilKit 对照」会在屏上真的画出笔迹，用于判断系统认不认这支笔。改完回主界面即生效（会重建画布）。")
                        .font(.footnote).foregroundColor(.secondary)
                }
                Section(header: Text("状态")) {
                    Text(client.connected ? "已连接" : "未连接")
                        .foregroundColor(client.connected ? .green : .red)
                    Text("端口必须与电脑端 --port 一致（默认 8767）")
                        .font(.footnote).foregroundColor(.secondary)
                    if !client.lastError.isEmpty {
                        Text(client.lastError).font(.footnote).foregroundColor(.red)
                    }
                }
                Section(header: Text("视口自检（坐标口径）")) {
                    Text("本机画布：\(canvasText)")
                    Text("电脑端期望：\(client.expectViewport.first ?? 0)×\(client.expectViewport.last ?? 0)")
                        .font(.footnote)
                    if client.viewportMismatch {
                        Text("⚠️ 两者不一致，本 App 已停止上报。请把电脑端的 --width / --height 改成与「本机画布」一致，或换回 375×812 的机型再跑。")
                            .font(.footnote).foregroundColor(.orange)
                    } else {
                        Text("两者一致才会上报触摸坐标（不一致会被电脑端静默算出错误配置）")
                            .font(.footnote).foregroundColor(.secondary)
                    }
                }
                Section(header: Text("连不上时依次检查")) {
                    Text("1. 手机与电脑在同一 Wi-Fi（关掉手机蜂窝数据试试）")
                    Text("2. Windows 防火墙放行入站 8767（首次运行会弹窗，要点允许）")
                    Text("3. 本 App 的 Info.plist 已配 ATS 与本地网络权限（见 ios_calib/README.md）")
                    Text("4. 手机设置里允许了本 App 访问「本地网络」")
                }
                .font(.footnote)
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") {
                        client.start(base: serverURL)
                        dismiss()
                    }
                }
            }
        }
    }
}
