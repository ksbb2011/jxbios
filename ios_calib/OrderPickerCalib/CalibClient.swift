//
//  CalibClient.swift
//  与电脑端 tools/calibrate_full_grid.py 的通信（轮询 GET /state、上报 POST /touch）
//
//  协议（JSON）：
//    GET  /state  → {"phase","note","id","seq","tx","ty","aim_x","aim_y","total",
//                    "done","expect_viewport","last_ack"}
//                    · tx/ty   本轮靶点（要它落在这里）
//                    · aim_x/y 本次实际瞄准的屏幕点（电脑已含补偿；按它画标记最直观）
//                    · seq     单调递增的下压序号（跨轮次不重复，判断"新点"用它，不要用 id）
//    POST /touch  ← {"id","x","y","t","viewport":[w,h],"kind"}
//
//  免责：本文件在 Windows 上无法编译验证，只保证与协议一致、逻辑自洽。
//

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
    /// 已经上报过的 seq：同一个点只上报一次（touchesEnded 只用于本地显示）
    @Published var reportedSeq: Int = -1

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
        if let ack = st["last_ack"] as? [String: Any] {
            lastAckText = String(format: "%.1f, %.1f", doubleOf(ack["x"]), doubleOf(ack["y"]))
        }
    }

    // MARK: - 上报

    /// 上报一次触摸。w/h 为本 App 画布的逻辑尺寸（应与 375×812 一致）。
    func report(x: Double, y: Double, kind: String, w: Double, h: Double) async {
        // 只在"进行中"上报：未开始/已结束时按屏产生的杂散触摸对电脑无用
        guard running, phase == "running" else { return }
        // 同一个点（seq）只上报一次：一次按压会同时产生 began/ended
        if kind == "ended" { return }
        let body: [String: Any] = [
            "id": index,
            "x": x, "y": y,
            "t": Int(Date().timeIntervalSince1970 * 1000),
            "viewport": [Int(w.rounded()), Int(h.rounded())],
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
