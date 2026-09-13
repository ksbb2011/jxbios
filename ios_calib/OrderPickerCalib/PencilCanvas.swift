//
//  PencilCanvas.swift
//  用 PencilKit（**备忘录画笔用的同一套系统组件**）接收机械臂电容笔的输入。
//
//  为什么换成它（2026-09-14 真机实测）：
//    · 机械臂的电容笔在 iOS **备忘录的画笔**里能正常画出笔迹 ✓
//    · 但在我们自绘的 UIView（touchesBegan）上**完全收不到** ✗（手指却正常 ✗）
//    · 已试过：删掉 UIApplicationSupportsIndirectInputEvents、把 allowedTouchTypes
//      放开到手指/手写笔/指针三类 —— 都无效 ✗
//  所以不再自绘：**既然备忘录收得到，就用备忘录那套组件** PKCanvasView ✓
//
//  坐标来源：每一笔的**起点**（stroke.path.first.location），在“抬笔”回调里上报。
//  笔迹用透明墨水且每次立刻清空 —— 屏幕上不留痕迹，只取坐标。
//
//  免责：本文件在 Windows 上无法编译验证；工程由 project.yml 的目录源自动收进。
//

import SwiftUI
import PencilKit

final class PencilCanvasView: PKCanvasView {

    /// 一次落笔完成时回调：(落笔起点坐标（视图坐标系）, 当时画布尺寸)
    var onStroke: ((CGPoint, CGSize) -> Void)?
    /// 尺寸回调：(画布尺寸, 窗口尺寸)——用于自检“有没有铺满全屏”
    var onMeta: ((CGSize, CGSize?) -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .clear
        isOpaque = false
        isScrollEnabled = false                 // 固定画布，不滚动
        alwaysBounceVertical = false
        alwaysBounceHorizontal = false
        contentInset = .zero
        // 手指与笔都能画（默认 .pencilOnly 会忽略手指——我们需要手指自检可用）
        drawingPolicy = .anyInput
        // 透明墨水：只为拿坐标，不要视觉痕迹（抬笔后立刻清空）
        tool = PKInkingTool(.pen, color: .clear, width: 1)
        delegate = self
    }

    required init?(coder: NSCoder) {
        fatalError("PencilCanvasView 只支持代码创建")
    }
}

extension PencilCanvasView: PKCanvasViewDelegate {

    func canvasViewDidBeginUsingTool(_ canvasView: PKCanvasView) {
        onMeta?(bounds.size, window?.bounds.size)
    }

    func canvasViewDidEndUsingTool(_ canvasView: PKCanvasView) {
        if let stroke = canvasView.drawing.strokes.last,
           let first = stroke.path.first {
            // stroke.path 的 location 就是画布视图坐标系，与我们上报的口径一致
            onStroke?(first.location, bounds.size)
        }
        canvasView.drawing = PKDrawing()        // 立刻清空，屏幕不留痕
    }
}

struct PencilCanvas: UIViewRepresentable {
    var onStroke: (CGPoint, CGSize) -> Void
    var onMeta: (CGSize, CGSize?) -> Void

    func makeUIView(context: Context) -> PencilCanvasView {
        let v = PencilCanvasView()
        v.onStroke = onStroke
        v.onMeta = onMeta
        return v
    }

    func updateUIView(_ uiView: PencilCanvasView, context: Context) {
        uiView.onStroke = onStroke
        uiView.onMeta = onMeta
    }
}
