//
//  TouchCanvas.swift
//  全屏原生触摸采集画布：这是**默认采集通道**（plain，非 PencilKit）。
//
//  为什么改用 plain 而不是 PencilKit 作唯一通道：
//    PencilKit 的 stroke 生命周期依赖"接触稳定且未被取消"，恰好是最脆的一环 ——
//    真机症状就是"手指能点、机械臂电容笔点不动"，因为它的回调只在"一笔画完"时才触发。
//    而本需求的本质只是"拿到触点坐标"，所以在 plain UIView 的 touchesBegan 里直接取坐标最稳。
//    PencilKit 作为**对照通道**仍在（见 PencilCanvas.swift，可在设置里切换，默认关闭）。
//
//  本视图**必须铺满整个窗口（含安全区）**：SwiftUI 侧 .ignoresSafeArea()，
//  否则坐标系原点会跑到安全区下方，整屏坐标整体偏移。
//
//  免责：本文件在 Windows 上无法编译验证。
//

import SwiftUI
import UIKit

final class TouchView: UIView {

    /// 每个原始触摸样本（含全相位）都从这里出去，由上层送进 TouchHub
    var onSample: ((TouchSample) -> Void)?
    /// 尺寸回调：(画布尺寸, 窗口尺寸)——用于自检"有没有铺满全屏"
    var onMeta: ((CGSize, CGSize?) -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        isOpaque = true
        isMultipleTouchEnabled = false          // 只认单点：机械臂一次只压一个点
        isUserInteractionEnabled = true
        installGestureFallback()
    }

    required init?(coder: NSCoder) {
        fatalError("TouchView 只支持代码创建")
    }

    /// 兜底识别器：万一某类输入没走到 touchesBegan（历史上怀疑过"笔被按指针投递"），
    /// 这个 0 延迟长按识别器也会在其 .began 时报一次；allowedTouchTypes 放开到三类输入。
    private func installGestureFallback() {
        let g = UILongPressGestureRecognizer(target: self, action: #selector(onPress(_:)))
        g.minimumPressDuration = 0
        g.allowableMovement = .greatestFiniteMagnitude
        g.cancelsTouchesInView = false          // 不干扰 touches* 系列
        g.delaysTouchesBegan = false
        g.delaysTouchesEnded = false
        g.allowedTouchTypes = [
            NSNumber(value: UITouch.TouchType.direct.rawValue),
            NSNumber(value: UITouch.TouchType.pencil.rawValue),
            NSNumber(value: UITouch.TouchType.indirectPointer.rawValue),
        ]
        addGestureRecognizer(g)
    }

    @objc private func onPress(_ g: UILongPressGestureRecognizer) {
        guard g.state == .began else { return }
        emit(point: g.location(in: self), type: "gesture", phase: "began",
             source: .gesture, radius: 0, force: 0)
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for t in touches { emit(t, phase: .began) }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for t in touches { emit(t, phase: .moved) }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for t in touches { emit(t, phase: .ended) }
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        // 取消也记录（诊断用）："接触不稳被系统打断"时它是唯一证据；
        // 但它不是 began，所以不会被采信上报。
        for t in touches { emit(t, phase: .cancelled) }
    }

    private func emit(_ t: UITouch, phase: UITouch.Phase) {
        emit(point: t.location(in: self), type: TouchNaming.type(t.type),
             phase: TouchNaming.phase(phase), source: .view,
             radius: Double(t.majorRadius), force: Double(t.force))
    }

    private func emit(point: CGPoint, type: String, phase: String,
                      source: TouchSource, radius: Double, force: Double) {
        onMeta?(bounds.size, window?.bounds.size)
        let vp = window?.bounds.size ?? bounds.size
        onSample?(TouchSample(x: Double(point.x), y: Double(point.y),
                              type: type, phase: phase, source: source,
                              majorRadius: radius, force: force,
                              viewport: vp, at: Date()))
    }
}

struct TouchCanvas: UIViewRepresentable {
    var onSample: (TouchSample) -> Void
    var onMeta: (CGSize, CGSize?) -> Void

    func makeUIView(context: Context) -> TouchView {
        let v = TouchView()
        v.onSample = onSample
        v.onMeta = onMeta
        return v
    }

    func updateUIView(_ uiView: TouchView, context: Context) {
        uiView.onSample = onSample
        uiView.onMeta = onMeta
    }
}
