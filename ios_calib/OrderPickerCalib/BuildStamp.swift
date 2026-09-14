//
//  BuildStamp.swift
//  屏上显示"当前装的是哪一版"：版本号 + 构建号 + 构建脚本注入的 commit 短哈希与构建时间。
//
//  为什么需要它：排查时最大的黑洞是"手机上装的到底是不是我刚编的那版"——
//  之前几轮修复无法判定生效与否，很大程度就卡在这里。把构建戳显示出来，一眼可辨。
//  （值由 build_ipa.sh 在打包前用 plutil 写进 .app 的 Info.plist；本机 Xcode 直编时为 dev。）
//

import Foundation

enum BuildStamp {

    static var text: String {
        let info = Bundle.main.infoDictionary
        let v = (info?["CFBundleShortVersionString"] as? String) ?? "?"
        let b = (info?["CFBundleVersion"] as? String) ?? "?"
        let stamp = (info?["BuildStamp"] as? String) ?? ""
        return stamp.isEmpty ? "v\(v)(\(b))" : "v\(v)(\(b)) · \(stamp)"
    }
}
