//
//  TouchDiagnostics.swift
//  原始触摸事件的统一汇聚点（多通道：窗口钩子 / 全屏原生视图 / 手势识别器 / PencilKit 对照）。
//
//  为什么要有它（2026-09-14）：
//    现状是「手指点有反应、机械臂电容笔点不动」，而此前三个版本（ca008ee / 247c6fc / b2484f0）
//    都没拿到「底层到底收到了什么事件」的硬证据，全在推测。
//    本文件把所有通道的原始事件归一化成 TouchSample，既能现场判读，也能作为**唯一**的上报来源：
//    任一条通道拿到「按下（began）」坐标就立刻回调上报，从此不再依赖 PencilKit 是否合成出完整笔迹。
//
//  两条硬规则（改之前先想清楚）：
//    1) 只认 began：UIKit 里任何一次接触，其**第一个**被投递的相位必然是 began
//       （即便后来被取消，也是 began → cancelled；不存在"没见过 began 却有 ended/cancelled"）。
//       所以只按 began 采信，逻辑最简，也绝不会误采 moved/ended。
//    2) 0.8s 内只采信一次：同一次物理按压会被多条通道（窗口/视图/手势）各报一遍，
//       而电脑端「每点第一条上报即采信、其余计 dup」，所以必须在 App 侧先合并。
//       实测两次按压之间至少有「抬起 0.15s + 移动 + settle 1.5s」的间隔，0.8s 足够安全。
//
//  免责：本文件在 Windows 上无法编译验证；只用 iOS 15 可用 API。
//

import Foundation
import UIKit
import Combine          // ObservableObject / @Published（只 import SwiftUI 时才有，UIKit 不带）

/// 事件来自哪条通道（现场判读靠它区分"是谁收到的"）
enum TouchSource: String, CaseIterable {
    case window   // UIWindow.sendEvent 钩子：最底层，绕过一切手势识别器与 PencilKit
    case view     // 全屏原生 UIView 的 touchesBegan/...
    case gesture  // 0 延迟长按识别器
    case pencil   // PencilKit 画布（对照通道，默认关闭）

    var label: String {
        switch self {
        case .window:  return "窗口"
        case .view:    return "视图"
        case .gesture: return "手势"
        case .pencil:  return "Pencil"
        }
    }
}

/// 一个原始触摸样本（字段都是"现场判读 + 上报"需要的最小集）
struct TouchSample {
    let x: Double
    let y: Double
    let type: String            // direct / pencil / pointer / unknown / gesture
    let phase: String           // began / moved / stationary / ended / cancelled / stroke
    let source: TouchSource
    let majorRadius: Double
    let force: Double
    let viewport: CGSize        // 触摸发生时的窗口尺寸（=上报用的 viewport 口径）
    let at: Date

    /// 本次采样是不是"按下那一刻"（只有它会被采信上报）
    var isBegan: Bool { phase == "began" }

    var posText: String { String(format: "%.1f, %.1f", x, y) }

    /// 一行紧凑文本，给屏上诊断面板用
    var line: String {
        String(format: "%@ %@/%@ (%@) r%.1f f%.1f %@",
               source.label, type, phase, posText, majorRadius, force,
               TouchSample.clock.string(from: at))
    }

    static let clock: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f
    }()
}

/// UITouch 的类型/相位没有现成字符串，这里给可读名。
/// 类型区分是排查关键：若笔被系统按 indirectPointer 投递，普通 touchesBegan 收不到，必须走指针事件。
enum TouchNaming {
    static func type(_ t: UITouch.TouchType) -> String {
        switch t {
        case .direct:          return "direct"
        case .indirectPointer: return "pointer"
        case .pencil:          return "pencil"
        @unknown default:      return "unknown"
        }
    }

    static func phase(_ p: UITouch.Phase) -> String {
        switch p {
        case .began:      return "began"
        case .moved:      return "moved"
        case .stationary: return "stationary"
        case .ended:      return "ended"
        case .cancelled:  return "cancelled"
        @unknown default: return "unknown"
        }
    }
}

/// 多通道汇聚中心（单例）。所有 UIKit 触摸回调都在主线程，故不作额外加锁。
final class TouchHub: ObservableObject {

    static let shared = TouchHub()

    /// 每个"物理按压"只回调一次（内部已按 mergeGap 合并多通道）。回调在主线程。
    var onFirstSample: ((TouchSample) -> Void)?

    /// 每条通道的原始事件条数（现场第一问题"到底有没有收到"看它）
    @Published private(set) var counts: [TouchSource: Int] = [:]
    /// 每条通道最近一条事件（诊断面板用）
    @Published private(set) var lastBySource: [TouchSource: TouchSample] = [:]
    /// 最近事件环形缓冲（最新在后）
    @Published private(set) var recent: [TouchSample] = []
    /// 已"采信"（触发过回调）的按压次数
    @Published private(set) var pressCount: Int = 0

    /// 合并窗口：同一次物理按压的多通道上报都落在它之内
    private let mergeGap: TimeInterval = 0.8
    private var lastEmitAt = Date.distantPast
    private let recentLimit = 6

    /// 所有通道的统一入口（必须在主线程调用）。
    func ingest(_ s: TouchSample) {
        counts[s.source, default: 0] += 1
        lastBySource[s.source] = s
        recent.append(s)
        if recent.count > recentLimit { recent.removeFirst(recent.count - recentLimit) }

        // 只有"按下"才采信；同一次按压的多通道重复上报按 mergeGap 合并
        guard s.isBegan else { return }
        guard s.at.timeIntervalSince(lastEmitAt) >= mergeGap else { return }
        lastEmitAt = s.at
        pressCount += 1
        onFirstSample?(s)
    }

    /// 清空诊断计数（换轮次时可选调用）
    func reset() {
        counts = [:]
        lastBySource = [:]
        recent = []
        pressCount = 0
        lastEmitAt = .distantPast
    }

    /// 屏上一行：窗口N 视图N 手势N PencilN
    var countsText: String {
        TouchSource.allCases.map { "\($0.label)\(counts[$0] ?? 0)" }.joined(separator: " ")
    }
}
