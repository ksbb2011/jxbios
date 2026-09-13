//
//  TouchCanvas.swift
//  全屏触摸画布：抓住每一次触摸，回调 **UIKit 窗口逻辑点** 坐标。
//
//  为什么不用 SwiftUI 的 DragGesture：
//    · 它有延迟与最小移动距离，拿不到"按下的那一刻"的精确坐标；
//    · 它的坐标是相对某个视图的，链式布局里容易偏移。
//  这里直接用 UIKit 的 touchesBegan，坐标口径与运满满运行时一致
//  （iPhone X = 375×812 逻辑点，原点在屏幕左上角）。
//
//  ⚠️ 本视图**必须铺满整个窗口（含安全区）**：在 SwiftUI 侧用 .ignoresSafeArea()，
//     否则坐标系原点会跑到安全区下方，整屏坐标整体偏移。
//     屏幕上会实时显示 view/window 两个尺寸，二者不一致就说明没铺满。
//
//  免责：本文件在 Windows 上无法编译验证。
//

import SwiftUI
import UIKit

final class TouchView: UIView {

    /// 触摸回调：(视图内坐标, **本次触摸时的画布尺寸**, 类型 "began"/"ended")
    ///
    /// 尺寸随触摸一起回传，而不是让上层读 `@State` 里的缓存值：
    /// 第一帧的 `@State` 还是 0×0，会让首个上报带上 `viewport:[0,0]`（电脑端会记一次
    /// viewport_bad），这一帧的尺寸才是权威值。
    var onTouch: ((CGPoint, CGSize, String) -> Void)?
    /// 尺寸回调：(画布尺寸, 窗口尺寸)——用于自检"有没有铺满全屏"
    var onMeta: ((CGSize, CGSize?) -> Void)?

    /// 去重用：同一瞬间、同一个点，`touchesBegan` 与下面的手势识别器可能各报一次
    private var lastFireAt = Date.distantPast
    private var lastFirePoint = CGPoint(x: -9999, y: -9999)

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        isMultipleTouchEnabled = false     // 只认单指：机械臂一次只压一个点
        isUserInteractionEnabled = true
        installAllTouchTypesFallback()
    }

    required init?(coder: NSCoder) {
        fatalError("TouchView 只支持代码创建")
    }

    /// 兜底：机械臂的电容笔在本机不一定被当成"手指触摸"投递
    /// （真机症状：**备忘录画笔能画**、本 App 的 `touchesBegan` 却完全收不到）。
    /// 这里挂一个 0 延迟的长按识别器，并把 `allowedTouchTypes` 放开到
    /// 手指 / 手写笔 / 指针三类，保证无论系统把它归为哪类输入都能采到。
    private func installAllTouchTypesFallback() {
        let g = UILongPressGestureRecognizer(target: self, action: #selector(onPress(_:)))
        g.minimumPressDuration = 0
        g.allowableMovement = .greatestFiniteMagnitude
        g.cancelsTouchesInView = false
        g.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.direct.rawValue),
            NSNumber(value: UITouch.TouchType.pencil.rawValue),
            NSNumber(value: UITouch.TouchType.indirectPointer.rawValue),
        ]
        addGestureRecognizer(g)
    }

    @objc private func onPress(_ g: UILongPressGestureRecognizer) {
        guard g.state == .began else { return }
        fire(g.location(in: self), "began")
    }

    /// 统一出口：同一瞬间同一点只算一次（touchesBegan 与识别器会重复触发）
    private func fire(_ p: CGPoint, _ kind: String) {
        let now = Date()
        if kind == "began",
           now.timeIntervalSince(lastFireAt) < 0.08,
           abs(p.x - lastFirePoint.x) < 2, abs(p.y - lastFirePoint.y) < 2 {
            return
        }
        lastFireAt = now
        lastFirePoint = p
        onMeta?(bounds.size, window?.bounds.size)
        onTouch?(p, bounds.size, kind)
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let t = touches.first else { return }
        fire(t.location(in: self), "began")
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let t = touches.first else { return }
        fire(t.location(in: self), "ended")
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        // 刻意不处理：系统打断（来电/手势）不应该被当成一次有效标定点
    }
}

struct TouchCanvas: UIViewRepresentable {
    var onTouch: (CGPoint, CGSize, String) -> Void
    var onMeta: (CGSize, CGSize?) -> Void

    func makeUIView(context: Context) -> TouchView {
        let v = TouchView()
        v.onTouch = onTouch
        v.onMeta = onMeta
        return v
    }

    func updateUIView(_ uiView: TouchView, context: Context) {
        uiView.onTouch = onTouch
        uiView.onMeta = onMeta
    }
}
