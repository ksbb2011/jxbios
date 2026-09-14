#!/usr/bin/env bash
# ============================================================================
#  ios_calib/build_ipa.sh —— 云端一键出包（未签名 ipa），2026-09-13
#
#  为什么是"未签名"：云 Mac / GitHub Actions 里**手机插不进去**，没法像本机那样
#  ⌘R 直接装。所以这里只负责编出 .app 并打成 .ipa，签名留给 Windows 上的
#  Sideloadly（用你自己的免费 Apple ID 重签，7 天有效）。
#
#  用法（Mac 上，仓库任意位置）：
#      bash ios_calib/build_ipa.sh
#  产物：
#      ios_calib/build/ipa/OrderPickerCalib-unsigned.ipa
#
#  唯一构建入口：本仓库其它地方（CI / 云 Mac 文档）都调这个脚本，不复制编译参数，
#  避免"两套构建流程慢慢漂移"。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_NAME="OrderPickerCalib"
TARGET_NAME="OrderPickerCalib"
BUILD_DIR="$HERE/build"
OBJ_DIR="$BUILD_DIR/Release-iphoneos"
OUT_DIR="$BUILD_DIR/ipa"
IPA="$OUT_DIR/${PROJ_NAME}-unsigned.ipa"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 环境自检
# 先打印再干活：云端按小时计费，"跑到一半才发现环境不对"最亏。
log "环境自检"
sw_vers || true
xcodebuild -version || true
printf 'xcode-select -p  = %s\n' "$(xcode-select -p 2>/dev/null || echo '(缺失)')"
printf 'iOS SDK 版本     = %s\n' "$(xcrun --sdk iphoneos --show-sdk-version 2>/dev/null || echo '(缺失)')"
df -h "$HERE" | tail -n 1 || true

command -v xcodebuild >/dev/null 2>&1 || die "找不到 xcodebuild：这台机器没装 Xcode（要 Xcode，不是只有 Command Line Tools）"
command -v xcodegen   >/dev/null 2>&1 || die "找不到 xcodegen：先执行  brew install xcodegen"
[ -f "$HERE/Info.plist" ] || die "缺 Info.plist：$HERE/Info.plist"

# Info.plist 语法自检（比编译早暴露，报错信息也更好读）
plutil -lint "$HERE/Info.plist" || die "Info.plist 格式非法"

# ------------------------------------------------------------------ 生成工程
log "生成 Xcode 工程（xcodegen）"
rm -rf "$HERE/$PROJ_NAME.xcodeproj" "$BUILD_DIR"
xcodegen generate --spec "$HERE/project.yml" --project "$HERE"

# ------------------------------------------------------------------ 编译
# 刻意用 `-target`（不依赖 scheme，最稳）；万一年代较新的 Xcode 对 -target 有意见，
# 再用 -scheme 兜一次（xcodegen 已按 project.yml 生成 scheme）。
# CODE_SIGNING_ALLOWED=NO：出未签名包，Sideloadly 那边再签。
COMMON_ARGS=(
  -project "$HERE/$PROJ_NAME.xcodeproj"
  -configuration Release
  -sdk iphoneos
  CODE_SIGNING_ALLOWED=NO
  CODE_SIGNING_REQUIRED=NO
  CODE_SIGN_IDENTITY=""
  CONFIGURATION_BUILD_DIR="$OBJ_DIR"
)

log "编译（Release / 未签名）"
if ! xcodebuild "${COMMON_ARGS[@]}" -target "$TARGET_NAME" build; then
  log "-target 方式失败，改用 -scheme 重试"
  xcodebuild "${COMMON_ARGS[@]}" -scheme "$TARGET_NAME" build
fi

APP="$OBJ_DIR/$PROJ_NAME.app"
[ -d "$APP" ] || die "编译过了但找不到 .app：$APP（看上面 xcodebuild 的输出定位）"

# ------------------------------------------------------------------ 构建戳
# 把 commit 短哈希 + 构建时间写进 .app 的 Info.plist；屏上诊断面板会显示它，
# 用来确认"手机上装的到底是不是我刚编的那版"（之前排查最大的黑洞就在这）。
# 任何一步失败都不阻断出包，只是屏上少一个戳。
log "写入构建戳（BuildStamp）"
STAMP_COMMIT="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo nogit)"
if ! git -C "$HERE" diff --quiet 2>/dev/null; then
  STAMP_COMMIT="${STAMP_COMMIT}+dirty"
fi
STAMP="$(date -u +%Y-%m-%dT%H:%MZ) ${STAMP_COMMIT}"
if ! plutil -replace BuildStamp -string "$STAMP" "$APP/Info.plist" 2>/dev/null; then
  plutil -insert BuildStamp -string "$STAMP" "$APP/Info.plist" 2>/dev/null \
    || printf '⚠️ 构建戳写入失败（不阻断出包）\n'
fi
printf 'BuildStamp = %s\n' "$STAMP"

# ------------------------------------------------------------------ 打包 ipa
log "打包 ipa"
rm -rf "$BUILD_DIR/Payload"
mkdir -p "$BUILD_DIR/Payload" "$OUT_DIR"
cp -R "$APP" "$BUILD_DIR/Payload/"
( cd "$BUILD_DIR" && zip -qry "$IPA" Payload )
rm -rf "$BUILD_DIR/Payload"

[ -f "$IPA" ] || die "打包失败：$IPA 没生成"

log "完成"
printf 'ipa  = %s\n' "$IPA"
printf '体积 = %s\n' "$(du -h "$IPA" | cut -f1)"
printf '\n⚠️ 这个包是**未签名**的，手机直接装不了：\n'
printf '   把它下载到 Windows，用 Sideloadly + 免费 Apple ID 安装（见 ios_calib/README.md）。\n'
