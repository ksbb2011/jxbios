//
//  CalibApp.swift
//  OrderPickerCalib —— 运满满抢单机器人「整屏网格标定」手机传感端
//
//  配合电脑端 tools/calibrate_full_grid.py（默认端口 8767）。
//  App 只做三件事：显示靶心、上报触摸坐标、显示状态；不参与任何计算。
//
//  ⚠️ 编译前请先读 ios_calib/README.md（Info.plist 的 ATS / 本地网络权限必须配，
//     否则 iOS 会**静默**拒绝连接电脑，现象是"一直连不上"）。
//

import SwiftUI

@main
struct CalibApp: App {
    var body: some Scene {
        WindowGroup {
            CalibView()
                .preferredColorScheme(.dark)
        }
    }
}
