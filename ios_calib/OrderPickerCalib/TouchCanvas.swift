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

    /// 触摸回调：(视图内坐标, 类型 "began"/"ended")
    var onTouch: ((CGPoint, String) -> Void)?
    /// 尺寸回调：(画布尺寸, 窗口尺寸)——用于自检"有没有铺满全屏"
    var onMeta: ((CGSize, CGSize?) -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        isMultipleTouchEnabled = false     // 只认单指：机械臂一次只压一个点
        isUserInteractionEnabled = true
    }

    required init?(coder: NSCoder) {
        fatalError("TouchView 只支持代码创建")
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let t = touches.first else { return }
        onMeta?(bounds.size, window?.bounds.size)
        onTouch?(t.location(in: self), "began")
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let t = touches.first else { return }
        onTouch?(t.location(in: self), "ended")
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        // 刻意不处理：系统打断（来电/手势）不应该被当成一次有效标定点
    }
}

struct TouchCanvas: UIViewRepresentable {
    var onTouch: (CGPoint, String) -> Void
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
