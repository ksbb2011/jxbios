//
//  PencilCanvas.swift
//  对照通道：用 PencilKit（**备忘录画笔用的同一套系统组件**）接收输入。
//
//  历史：2026-09-14 之前它是唯一采集通道，但真机反馈"手指能点、机械臂电容笔点不动"——
//  因为它的回调只在"一笔画完（canvasViewDidEndUsingTool）"时才触发，
//  接触若被判为不稳定/被取消，就永远不回调。于是降级为**设置里可切换的对照通道**，默认关闭。
//
//  仍保留它的价值：
//    · 它会在屏上真正画出笔迹，是"系统到底认不认这支笔"的直观旁证；
//    · 它的样本只进诊断计数，**不参与上报**（窗口级钩子看得比它更全）。
//
//  免责：本文件在 Windows 上无法编译验证；工程由 project.yml 的目录源自动收进。
//

import SwiftUI
import PencilKit

final class PencilCanvasView: PKCanvasView {

    /// 送进 TouchHub 的诊断样本（source = .pencil）
    var onSample: ((TouchSample) -> Void)?
    /// 尺寸回调：(画布尺寸, 窗口尺寸)
    var onMeta: ((CGSize, CGSize?) -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        isOpaque = true
        isScrollEnabled = false                 // 固定画布，不滚动
        alwaysBounceVertical = false
        alwaysBounceHorizontal = false
        contentInset = .zero
        // 手指与笔都能画（默认 .pencilOnly 会忽略手指——我们需要手指自检可用）
        drawingPolicy = .anyInput
        // 对照通道：留可见笔迹更直观（能否画出笔迹本身就是关键证据）
        tool = PKInkingTool(.pen, color: .white, width: 2)
        delegate = self
    }

    required init?(coder: NSCoder) {
        fatalError("PencilCanvasView 只支持代码创建")
    }
}

extension PencilCanvasView: PKCanvasViewDelegate {

    func canvasViewDidBeginUsingTool(_ canvasView: PKCanvasView) {
        onMeta?(bounds.size, window?.bounds.size)
        emit(point: nil, phase: "pencil-began")
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        onMeta?(bounds.size, window?.bounds.size)
        if let stroke = canvasView.drawing.strokes.last, let first = stroke.path.first {
            // stroke.path 的 location 就是画布视图坐标系，与上报口径一致
            emit(point: first.location, phase: "stroke")
        }
        canvasView.drawing = PKDrawing()        // 清掉，避免糊屏（诊断只看"有没有出现笔迹"）
    }

    private func emit(point: CGPoint?, phase: String) {
        let vp = window?.bounds.size ?? bounds.size
        let p = point ?? CGPoint(x: -1, y: -1)
        onSample?(TouchSample(x: Double(p.x), y: Double(p.y),
                              type: "pencil", phase: phase, source: .pencil,
                              majorRadius: 0, force: 0,
                              viewport: vp, at: Date()))
    }
}

struct PencilCanvas: UIViewRepresentable {
    var onSample: (TouchSample) -> Void
    var onMeta: (CGSize, CGSize?) -> Void

    func makeUIView(context: Context) -> PencilCanvasView {
        let v = PencilCanvasView()
        v.onSample = onSample
        v.onMeta = onMeta
        return v
    }

    func updateUIView(_ uiView: PencilCanvasView, context: Context) {
        uiView.onSample = onSample
        uiView.onMeta = onMeta
    }
}
