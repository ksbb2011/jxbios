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
//

import SwiftUI

struct CalibView: View {

    @StateObject private var client = CalibClient()
    @AppStorage("serverURL") private var serverURL: String = "192.168.1.100:8767"
    @State private var showSettings = false
    @State private var canvasSize: CGSize = .zero
    @State private var windowSize: CGSize = .zero
    @State private var lastEnded = "-"
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        GeometryReader { geo in
            ZStack {
                // ① 全屏触摸画布（最底层，吃掉所有触摸）
                TouchCanvas(
                    onTouch: { point, kind in
                        if kind == "began" {
                            Task {
                                await client.report(x: Double(point.x), y: Double(point.y),
                                                    kind: kind,
                                                    w: Double(canvasSize.width),
                                                    h: Double(canvasSize.height))
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

                // ② 靶心（唯一图形元素）：白环 = 本次瞄准点，红点 = 本轮靶点
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

                // ③ 顶部状态条 + 细进度条 + 右上角设置
                VStack(spacing: 6) {
                    HStack(spacing: 10) {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 10, height: 10)
                        Text(statusText)
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundColor(.white)
                            .lineLimit(1)
                        Spacer(minLength: 8)
                        Button {
                            showSettings = true
                        } label: {
                            Image(systemName: "gearshape.fill")
                                .font(.system(size: 16))
                                .foregroundColor(.white.opacity(0.55))
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Color.black.opacity(0.55))
                    .cornerRadius(12)

                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Rectangle().fill(Color.white.opacity(0.12))
                            Rectangle().fill(statusColor)
                                .frame(width: g.size.width * progress)
                        }
                    }
                    .frame(height: 3)
                    .cornerRadius(1.5)

                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.top, client.phase == "running" ? 6 : 0)

                // ④ 底部诊断行（平时压暗，出问题时它是第一手证据）
                VStack {
                    Spacer()
                    Text(footerText)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(.white.opacity(0.42))
                        .lineLimit(2)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 10)
                        .padding(.bottom, 6)
                        .onChange(of: client.lastError) { _ in
                            // 出错时就地显示，不弹窗（弹窗会挡住靶心）
                        }
                }
            }
        }
        .background(Color.black)
        .ignoresSafeArea()
        .statusBar(hidden: true)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true      // 防自动锁屏（整轮标定要几分钟）
            client.start(base: serverURL)
        }
        .onChange(of: serverURL) { newValue in
            client.start(base: newValue)
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
            SettingsView(serverURL: $serverURL, client: client)
        }
    }

    // MARK: - 文案与状态

    private var progress: Double {
        guard client.total > 0, client.phase == "running" else { return client.phase == "done" ? 1 : 0 }
        return min(max(Double(client.index + 1) / Double(client.total), 0), 1)
    }

    private var statusText: String {
        if !client.connected { return "未连接电脑（点右上角齿轮填地址）" }
        switch client.phase {
        case "running": return "进行中 \(client.index + 1)/\(client.total)"
        case "done":    return "标定完成 ✓"
        case "error":   return "电脑端已中止：" + client.note
        default:        return client.note.isEmpty ? "等待电脑开始…" : client.note
        }
    }

    private var statusColor: Color {
        if !client.connected { return Color(white: 0.45) }
        switch client.phase {
        case "done":  return Color(red: 0.19, green: 0.82, blue: 0.35)
        case "error": return Color(red: 0.84, green: 0.0, blue: 0.0)
        default:      return Color(red: 0.04, green: 0.52, blue: 1.0)
        }
    }

    private var footerText: String {
        var line1 = "\(client.baseURL.isEmpty ? "-" : client.baseURL)   " +
                    "画布 \(Int(canvasSize.width))×\(Int(canvasSize.height))   " +
                    "窗口 \(Int(windowSize.width))×\(Int(windowSize.height))"
        if !client.connected { line1 += "\n" + client.lastError }
        else {
            line1 += "   上报 \(client.reports)  轮询 \(client.polls)"
            line1 += "\n最近上报 \(client.lastReportText)   电脑收到 \(client.lastAckText)   抬起 \(lastEnded)"
        }
        return line1
    }
}

// MARK: - 设置（填电脑地址）

private struct SettingsView: View {
    @Binding var serverURL: String
    @ObservedObject var client: CalibClient
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
