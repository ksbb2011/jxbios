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

struct CalibView: View {

    @StateObject private var client = CalibClient()
    @AppStorage("serverURL") private var serverURL: String = "192.168.1.100:8767"
    @State private var showSettings = false
    @State private var canvasSize: CGSize = .zero
    @State private var windowSize: CGSize = .zero
    @State private var lastEnded = "-"
    @State private var didAutoOpenSettings = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        GeometryReader { _ in
            ZStack {
                // ① 全屏触摸画布（最底层，吃掉所有触摸）
                TouchCanvas(
                    onTouch: { point, size, kind in
                        canvasSize = size
                        // 任何一次触摸都记一笔（含抬起、含电脑端不在 running 的情况）：
                        // 现场判断"App 到底有没有收到触摸"就靠它
                        client.noteTouch(x: Double(point.x), y: Double(point.y))
                        if kind == "began" {
                            Task {
                                await client.report(x: Double(point.x), y: Double(point.y),
                                                    kind: kind,
                                                    w: Double(size.width),
                                                    h: Double(size.height))
                            }
                        } else {
                            lastEnded = String(format: "%.1f, %.1f", point.x, point.y)
                        }
                    },
                    onMeta: { viewSize, winSize in
                        canvasSize = viewSize
                        if let w = winSize { windowSize = w }
                    }
                )
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

                // ④ 底部诊断行（平时压暗，出问题时它是第一手证据）
                VStack {
                    Spacer()
                    Text(footerText)
                        // 放大加亮 + 补"触摸 N / 最近触摸"：现场判断"App 有没有收到触摸"
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
            SettingsView(serverURL: $serverURL, client: client, canvasText: canvasText)
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
        // 触摸数**始终显示**（未连接时也显示）——"屏到底有没有收到触摸"是现场第一问题
        line1 += "   触摸 \(client.touchSeen)   上报 \(client.reports)"
        if client.connected {
            // 触摸 = 屏上真实收到的触摸次数（含抬起；电脑端未在 running 时也计）
            // 上报 = 成功发给电脑的次数（只在电脑端 running 时才会涨）
            line1 += "   轮询 \(client.polls)"
            line1 += "\n最近触摸 \(client.lastTouchAt) @ \(client.lastTouchText)" +
                     "   电脑收到 \(client.lastAckText)   抬起 \(lastEnded)"
        } else {
            line1 += "\n未连接：\(client.lastError)"
        }
        return line1
    }
}

// MARK: - 设置（填电脑地址）

private struct SettingsView: View {
    @Binding var serverURL: String
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
