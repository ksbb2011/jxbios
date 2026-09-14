//
//  CalibApp.swift
//  OrderPickerCalib —— 运满满抢单机器人「整屏网格标定」手机传感端
//
//  配合电脑端 tools/calibrate_full_grid.py（默认端口 8767）。
//  App 只做三件事：显示靶心、上报触摸坐标、显示状态；不参与任何计算。
//
//  2026-09-14 起：改为**多通道采集**（窗口钩子 + 全屏原生视图 + 0 延迟手势 + PencilKit 对照），
//  任一通道拿到"按下"坐标即上报，不再依赖 PencilKit 是否合成出一整笔。
//
//  ⚠️ 编译前请先读 ios_calib/README.md（Info.plist 的 ATS / 本地网络权限必须配，
//     否则 iOS 会**静默**拒绝连接电脑，现象是"一直连不上"）。
//

import SwiftUI

@main
struct CalibApp: App {

    init() {
        // 最早时机装窗口级触摸钩子：它必须在任何触摸发生前生效（见 WindowProbe.swift）。
        WindowProbe.install()
    }

    var body: some Scene {
        WindowGroup {
            CalibView()
                .preferredColorScheme(.dark)
        }
    }
}
