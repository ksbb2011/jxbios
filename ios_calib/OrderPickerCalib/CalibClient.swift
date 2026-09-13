//
//  CalibClient.swift
//  与电脑端 tools/calibrate_full_grid.py 的通信（轮询 GET /state、上报 POST /touch）
//
//  协议（JSON）：
//    GET  /state  → {"phase","note","id","seq","tx","ty","aim_x","aim_y",
//                    "cmd_ax","cmd_ay","total","done","app_seen","expect_viewport",
//                    "touches","stale","dup","viewport_bad","last_ack"}
//                    · tx/ty    本轮靶点（要它落在这里）
//                    · aim_x/y  本次实际瞄准的屏幕点（电脑已含补偿；按它画标记最直观）
//                    · seq      单调递增的下压序号（跨轮次不重复，仅用于“同点只报一次”的幂等；
//                               **电脑端按 id 校验，不校验 seq**）
//                    · cmd_ax/ay 真正下发给机械臂的坐标（手机端不读；假手机自测用它反解真值）
//                    · last_ack 电脑端最近一次采信的上报，**可能是 null**（尚未收到任何上报）
//                    本文件只消费其中 11 个键，其余是诊断量。
//    POST /touch  ← {"id","x","y","t","viewport":[w,h],"kind"}
//
//  去重责任（哪边负责，别改错）：
//    · 电脑端：“该点第一条上报即采信，其余计 dup 丢弃”，且 `id` 必须等于当前点序号（否则计 stale）；
//    · 手机端：只上报 began（不上报 ended/moved），并对**已成功上报过的 seq** 不再重报。
//      两者叠加的结果：正常路径每点恰好一条；偶发丢包时手机端会在下一次触摸自动重试（见 report 注释）。
//
//  免责：本文件在 Windows 上无法编译验证，只保证与协议一致、逻辑自洽。

import Foundation

final class CalibClient: ObservableObject {

    // ---- 服务端广播的状态
    @Published var phase: String = "waiting"
    @Published var note: String = ""
    @Published var seq: Int = -1
    @Published var index: Int = 0
    @Published var total: Int = 0
    @Published var tx: Double = 0
    @Published var ty: Double = 0
    @Published var aimX: Double = 0
    @Published var aimY: Double = 0
    @Published var expectViewport: [Int] = [375, 812]

    // ---- 本地状态（自检用）
    @Published var connected = false
    @Published var polls = 0
    @Published var reports = 0
    @Published var lastError = ""
    @Published var lastReportText = "-"
    @Published var lastAckText = "-"
    /// 已**成功**上报过的 seq：只在成功后置位，所以上报失败会在下一次触摸时自动重试（幂等重发）
    @Published var reportedSeq: Int = -1
    /// 机型逻辑分辨率与电脑端期望不符：这是最危险的静默失效，一旦为真就停止上报并高亮报警
    @Published var viewportMismatch = false

    private var base = ""
    private var running = false

    // MARK: - 生命周期

    func start(base: String) {
        let trimmed = base.trimmingCharacters(in: .whitespacesAndNewlines)
        self.base = trimmed.hasPrefix("http") ? trimmed : "http://\(trimmed)"
        guard !running else { return }
        running = true
        Task { await pollLoop() }
    }

    func stop() { running = false }

    var baseURL: String { base }

    // MARK: - 轮询

    private func pollLoop() async {
        while running {
            do {
                let st = try await getJSON(base + "/state")
                await MainActor.run { self.apply(st); self.connected = true; self.polls += 1; self.lastError = "" }
            } catch {
                let msg = error.localizedDescription
                await MainActor.run { self.connected = false; self.lastError = msg }
            }
            try? await Task.sleep(nanoseconds: 350_000_000)   // 0.35s
        }
    }

    private func apply(_ st: [String: Any]) {
        phase = (st["phase"] as? String) ?? "?"
        note = (st["note"] as? String) ?? ""
        seq = intOf(st["seq"])
        index = intOf(st["id"])
        total = intOf(st["total"])
        tx = doubleOf(st["tx"])
        ty = doubleOf(st["ty"])
        aimX = doubleOf(st["aim_x"])
        aimY = doubleOf(st["aim_y"])
        if let vp = st["expect_viewport"] as? [Int], vp.count == 2 { expectViewport = vp }
        // last_ack 可能是 null（电脑端还没采信过任何上报）→ 这时要清空，
        // 否则底部一直显示上一轮的旧坐标，联调时会误判“电脑还没收到”。
        if let ack = st["last_ack"] as? [String: Any] {
            lastAckText = String(format: "%.1f, %.1f", doubleOf(ack["x"]), doubleOf(ack["y"]))
        } else {
            lastAckText = "-"
        }
    }

    // MARK: - 上报

    /// 上报一次触摸。w/h 为本 App 画布的逻辑尺寸（应与电脑端 --width/--height 一致）。
    func report(x: Double, y: Double, kind: String, w: Double, h: Double) async {
        // 只在“进行中”上报：未开始/已结束时按屏产生的杂散触摸对电脑端无用
        guard running, phase == "running" else { return }
        // 一次按压会同时产生 began/ended，只认 began（电脑端以“每点第一条上报”为准）
        if kind != "began" { return }
        // 同一个点只上报一次；但因为 reportedSeq 只在**成功**后置位，
        // 失败的那次不会挡住下一次触摸——等于免费拿到了“丢包自动重试”。
        if reportedSeq >= 0 && seq == reportedSeq { return }

        let vp = [Int(w.rounded()), Int(h.rounded())]
        // 视口自检（最危险的静默失效）：机型逻辑分辨率必须等于电脑端期望值，
        // 否则所有坐标整体错位，而电脑端只会打印一行 warning 就照常拟合、甚至写盘。
        // w/h 尚未量出（0）时跳过，避免第一个点被误判。
        if vp[0] > 0 && vp[1] > 0 && (vp[0] != expectViewport[0] || vp[1] != expectViewport[1]) {
            let expect = expectViewport
            await MainActor.run {
                self.viewportMismatch = true
                self.lastError = "视口不符：本机 \(vp[0])×\(vp[1])，电脑端期望 \(expect[0])×\(expect[1])"
            }
            return   // 拒绝上报：宁可跑不动，也不要静默写出错误配置
        }
        await MainActor.run { self.viewportMismatch = false }

        let body: [String: Any] = [
            "id": index,
            "x": x, "y": y,
            "t": Int(Date().timeIntervalSince1970 * 1000),
            "viewport": vp,
            "kind": kind,
        ]
        do {
            _ = try await postJSON(base + "/touch", body)
            await MainActor.run {
                self.reports += 1
                self.reportedSeq = self.seq
                self.lastReportText = String(format: "%.1f, %.1f", x, y)
            }
        } catch {
            let msg = error.localizedDescription
            await MainActor.run { self.lastError = "上报失败：\(msg)" }
        }
    }

    // MARK: - HTTP 小工具

    private func getJSON(_ url: String) async throws -> [String: Any] {
        guard let u = URL(string: url) else { throw URLError(.badURL) }
        var req = URLRequest(url: u)
        req.timeoutInterval = 2.5
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, _) = try await URLSession.shared.data(for: req)
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw URLError(.cannotParseResponse)
        }
        return obj
    }

    private func postJSON(_ url: String, _ body: [String: Any]) async throws -> [String: Any] {
        guard let u = URL(string: url) else { throw URLError(.badURL) }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        req.timeoutInterval = 2.5
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, _) = try await URLSession.shared.data(for: req)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    // MARK: - 取值容错（JSON 数字可能是 Int / Double / NSNumber）

    private func intOf(_ any: Any?) -> Int {
        if let i = any as? Int { return i }
        if let d = any as? Double { return Int(d) }
        if let n = any as? NSNumber { return n.intValue }
        return 0
    }

    private func doubleOf(_ any: Any?) -> Double {
        if let d = any as? Double { return d }
        if let i = any as? Int { return Double(i) }
        if let n = any as? NSNumber { return n.doubleValue }
        return 0
    }
}
