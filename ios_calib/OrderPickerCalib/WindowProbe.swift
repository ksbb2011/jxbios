//
//  WindowProbe.swift
//  给 UIWindow.sendEvent(_:) 装一个"只读钩子"：这是唯一能绕过全部手势识别器与 PencilKit、
//  看到「系统到底有没有把触摸事件投递给本 App」的位置。
//
//  为什么必须用它（而不是只靠 UIView.touchesBegan）：
//    · 手势识别器可以先"抢走"触摸，PencilKit 也可能把不稳定接触直接丢弃；
//    · 但如果系统要投递，事件必然先过 UIWindow.sendEvent —— 在这里枚举 allTouches 一定能看到；
//    · 记录 touch.type 能直接回答"笔到底被归为 direct / pencil / pointer"。
//
//  实现方式：方法交换（不替换 window 结构，风险最小），只在 App 启动时装一次。
//  两种已知的理论上限（现场遇到时按此判断）：
//    · 若连本钩子都收不到任何 touched 事件 → 系统级未投递到本 App，退路是全屏 WKWebView canvas；
//    · 若 type 是 pointer → 需改走指针事件通道。
//
//  免责：本文件在 Windows 上无法编译验证。
//

import UIKit
import ObjectiveC

enum WindowProbe {

    private static var installed = false

    /// 在 App 启动时调用一次（见 CalibApp.init）。
    static func install() {
        guard !installed else { return }
        installed = true
        let cls: AnyClass = UIWindow.self
        guard let original = class_getInstanceMethod(cls, #selector(UIWindow.sendEvent(_:))),
              let probe    = class_getInstanceMethod(cls, #selector(UIWindow.probe_sendEvent(_:)))
        else { return }
        method_exchangeImplementations(original, probe)
    }
}

extension UIWindow {

    /// 交换后：本方法体内调用 probe_sendEvent 实际执行的是**系统原实现**（标准 swizzle 写法）。
    @objc func probe_sendEvent(_ event: UIEvent) {
        probe_sendEvent(event)   // 先按原流程分发，保证 App 行为与未装钩子时完全一致

        guard let touches = event.allTouches else { return }
        let vp = bounds.size
        for t in touches {
            let p = t.preciseLocation(in: self)
            TouchHub.shared.ingest(TouchSample(
                x: Double(p.x),
                y: Double(p.y),
                type: TouchNaming.type(t.type),
                phase: TouchNaming.phase(t.phase),
                source: .window,
                majorRadius: Double(t.majorRadius),
                force: Double(t.force),
                viewport: vp,
                at: Date()))
        }
    }
}
