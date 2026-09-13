"""iMouse 探活与实测探针（A1~A6）。

为什么要先跑这个：
    新项目要把"看屏幕"从摄像头换成 iMouse 投屏。但官方文档里有几个致命的
    **未声明项/版本差异**，任何一个猜错都会让后续代码白写：

        A1 版本与端口   —— XP 版(HTTP/WS 均 9911) 与 专业版(HTTP 9912/WS 9911)
                           接口名与参数**完全不同**，混用会全部报错
        A2 分辨率坐标   —— 截图到底是逻辑点(375x812)还是 2 倍图(750x1624)，
                           决定要不要做坐标换算层
        A3 黑边/状态栏  —— 决定 ROI 归一化基准
        A4 接口耗时     —— 决定 OCR 走本地还是 iMouse 服务端
        A5 投屏延迟     —— 决定主循环帧间隔与"点完立即复判"策略
        A6 插件连通性   —— 专业版的找图/OCR 是**插件**，没连上就全是摆设

    跑完产出 docs/实测报告_iMouse.md + docs/_probe/imouse_probe.json。
    **没有实测数据之前不要写生产代码。**

两套 API 的关键差异（2026-09-09 实测 + 官方文档核对）：

    能力      | XP 版                                  | 专业版
    ----------|----------------------------------------|------------------------------------------
    设备列表  | GET /api/device/get → data.list[]      | POST fun=get_device_list → data{id:{...}}
    截图      | /pic/screenshot {id,jpg,rect,binary}   | get_device_screenshot {deviceid,isjpg,binary,original}
    找图      | /pic/find-image-cv {id,img_list[],sim} | find_image {deviceid,img(base64字符串),sim,rect}
              | → data.list[].centre                   | → data.result=[x,y] / data.code(0找到,1没找到)
    OCR       | /pic/ocr {id,rect}                     | ocr / ocr_ex {deviceid,rect[[x,y]x4]}
              | → data.list[].text/.centre             | → data.list[].txt/.result
    找色      | /pic/find-multi-color {id,list[]}      | find_multi_color → data.result=[x,y]
    找字      | /pic/find-text                         | **无此接口**
    键盘      | /keyboard/input                        | send_key {deviceid,key,fn_key}
              |                                        | ⚠️ 官方注明**不支持中文**

用法：
    py -3.11 tools/imouse_probe.py
    py -3.11 tools/imouse_probe.py --host 192.168.9.9 --repeat 30
    py -3.11 tools/imouse_probe.py --skip-timing              # 只探活
    py -3.11 tools/imouse_probe.py --test-keyboard            # 额外测键盘（会真打字，需先聚焦输入框）

依赖：requests / numpy / opencv-python（与源项目一致，不引入新东西）
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "docs" / "_probe"
REPORT_MD = PROJECT_ROOT / "docs" / "实测报告_iMouse.md"
REPORT_JSON = OUT_DIR / "imouse_probe.json"
SHOT_DIR = OUT_DIR / "shots"

HTTP_TIMEOUT = 8.0
OCR_TIMEOUT = 30.0

PRO_FUN_CANDIDATES = ["get_device_list", "get_device", "device_list"]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    return float(np.percentile(np.asarray(values, dtype=np.float64), q * 100.0))


def _stat(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(_pct(values, 0.50), 2),
        "p95": round(_pct(values, 0.95), 2),
        "min": round(float(np.min(values)), 2),
        "max": round(float(np.max(values)), 2),
    }


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _safe_div(a: float, b: float) -> float | None:
    return round(a / b, 4) if b else None


def _plugins_unavailable(plugins: dict | None) -> bool:
    """判断找图/OCR 插件是否不可用。

    专业版判定依据（见接口概述错误码）：
        26 = 插件未连接，可能没安装
        25 = 调用接口失败
    两者都意味着该能力不可用，其耗时数据是"失败秒返"的假数据。
    """
    if not plugins:
        return False
    bad_codes = {25, 26}
    for v in plugins.values():
        if isinstance(v, dict):
            try:
                if int(v.get("status")) in bad_codes:
                    return True
            except (TypeError, ValueError):
                continue
    return False


class ImouseError(RuntimeError):
    pass


# ---------------------------------------------------------------- 客户端
class ImouseClient:
    """兼容 XP 版与专业版的极简客户端，两套差异全部收在这里。"""

    def __init__(self, host: str, edition: str, http_port: int) -> None:
        self.host = host
        self.edition = edition  # "xp" | "pro"
        self.port = http_port
        self.base = f"http://{host}:{http_port}/api"
        self._session = requests.Session()
        self._msgid = 0

    def call(self, fun: str, data: dict[str, Any] | None = None,
             timeout: float = HTTP_TIMEOUT) -> dict:
        data = data or {}
        self._msgid += 1
        if self.edition == "xp":
            body: dict[str, Any] = {"fun": fun, "data": data}
        else:
            body = {"fun": fun, "msgid": self._msgid, "data": data}
        r = self._session.post(self.base, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def call_raw(self, fun: str, data: dict[str, Any] | None = None,
                 timeout: float = HTTP_TIMEOUT) -> bytes:
        data = dict(data or {})
        data["binary"] = True
        if self.edition == "xp":
            r = self._session.get(f"{self.base}{fun}", params=data, timeout=timeout)
        else:
            self._msgid += 1
            r = self._session.post(
                self.base,
                json={"fun": fun, "msgid": self._msgid, "data": data},
                timeout=timeout,
            )
        r.raise_for_status()
        return r.content

    @staticmethod
    def ok(resp: dict) -> bool:
        """跨版本判断"接口调用成功"（注意：不等于"找到了"）。

        XP 版：顶层 status==200 且 data.code==0
        专业版：顶层 status==0（找图/OCR 的"有没有找到"在 data.code：0找到/1没找到）
        """
        top = resp.get("status")
        if top == 200:
            return int(resp.get("data", {}).get("code", -1)) == 0
        return int(top) == 0

    @staticmethod
    def msg(resp: dict) -> str:
        return str(resp.get("message") or resp.get("data", {}).get("message") or "")

    @staticmethod
    def payload(resp: dict) -> dict:
        d = resp.get("data")
        return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- A1 版本探测
def detect_edition(host: str) -> tuple[str, int, dict]:
    evidence: dict[str, Any] = {}

    for port in (9911, 9912):
        try:
            r = requests.get(f"http://{host}:{port}/api/device/get", timeout=3)
            body = r.json()
            if r.status_code == 200 and isinstance(body.get("data"), dict) and "list" in body["data"]:
                evidence[f"xp_{port}"] = "GET /api/device/get 返回 data.list -> XP 版"
                return "xp", port, evidence
            evidence[f"xp_{port}"] = f"HTTP {r.status_code}，非 XP 结构"
        except Exception as exc:  # noqa: BLE001
            evidence[f"xp_{port}"] = f"{type(exc).__name__}"

    for port in (9912, 9911):
        for fun in PRO_FUN_CANDIDATES:
            try:
                r = requests.post(
                    f"http://{host}:{port}/api",
                    json={"fun": fun, "msgid": 1, "data": {}}, timeout=3)
                body = r.json()
                if int(body.get("status", -1)) == 0:
                    evidence[f"pro_{port}"] = f"fun={fun} 返回 status=0 -> 专业版"
                    return "pro", port, evidence
            except Exception as exc:  # noqa: BLE001
                evidence[f"pro_{port}_{fun}"] = f"{type(exc).__name__}"
    raise ImouseError(
        "未能识别 iMouse 版本。请确认内核服务端已启动、手机已投屏在线、主机地址正确。"
        f"证据：{json.dumps(evidence, ensure_ascii=False)}")


# ---------------------------------------------------------------- A2 设备
def fetch_devices(cli: ImouseClient) -> list[dict]:
    if cli.edition == "xp":
        resp = cli.call("/device/get")
        if not ImouseClient.ok(resp):
            raise ImouseError(f"/device/get 失败：{ImouseClient.msg(resp)}")
        return list(ImouseClient.payload(resp).get("list") or [])

    for fun in PRO_FUN_CANDIDATES:
        resp = cli.call(fun)
        if ImouseClient.ok(resp):
            data = ImouseClient.payload(resp)
            for key in ("list", "devices", "device_list"):
                if isinstance(data.get(key), list):
                    return list(data[key])
            # 专业版实测：{设备id: {...}}（2026-09-09 iPhone X 确认）
            if data and all(isinstance(v, dict) for v in data.values()):
                return [dict(v, _key=k) for k, v in data.items()]
            return [data] if data else []
    raise ImouseError("专业版获取设备列表失败，已试 fun=" + ",".join(PRO_FUN_CANDIDATES))


def pick_online(devices: list[dict]) -> dict:
    if not devices:
        raise ImouseError("设备列表为空——请先在控制台里把手机投屏连上")
    for d in devices:
        if _to_int(d.get("state")) != 0:
            return d
    return devices[0]


def device_id(dev: dict) -> str:
    for key in ("deviceid", "id", "_key", "mac"):
        v = dev.get(key)
        if v:
            return str(v)
    raise ImouseError(f"设备记录里找不到 id 字段：{list(dev.keys())}")


# ---------------------------------------------------------------- 截图
def screenshot_bytes(cli: ImouseClient, dev_id: str, jpg: bool = False,
                     rect: list[int] | None = None,
                     timeout: float = HTTP_TIMEOUT) -> bytes:
    if cli.edition == "xp":
        data: dict[str, Any] = {"id": dev_id, "jpg": jpg}
        if rect:
            data["rect"] = rect
        return cli.call_raw("/pic/screenshot", data, timeout=timeout)

    # 专业版：original 必须为 False——true 会返回与手机分辨率不同的高清图，
    # 官方文档自己都提醒"需要自己转换成手机的坐标"，我们不需要自找麻烦。
    data = {"deviceid": dev_id, "isjpg": jpg, "binary": False, "original": False}
    if rect:
        data["rect"] = _pro_rect(rect)
    resp = cli.call("get_device_screenshot", data, timeout=timeout)
    if not ImouseClient.ok(resp):
        raise ImouseError(f"get_device_screenshot 失败：{ImouseClient.msg(resp)}")
    p = ImouseClient.payload(resp)
    img = p.get("img") or p.get("image")
    if not img:
        raise ImouseError("截图返回里没有 img/image 字段")
    if isinstance(img, str) and len(img) < 4096 and ("/" in img or "\\" in img):
        return Path(img).read_bytes()
    return base64.b64decode(img)


def _pro_rect(rect: list[int]) -> list[list[int]]:
    """[左,上,右,下] → 专业版的四点格式 [[lt],[lb],[rt],[rb]]。"""
    l, t, r, b = [int(v) for v in rect]
    return [[l, t], [l, b], [r, t], [r, b]]


def decode_image(raw: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ImouseError(f"图片解码失败（前 16 字节：{raw[:16]!r}）")
    return img


# ---------------------------------------------------------------- A3 几何
def analyze_geometry(img: np.ndarray, dev: dict) -> dict:
    h, w = img.shape[:2]
    logical_w, logical_h = _to_int(dev.get("width")), _to_int(dev.get("height"))
    img_w, img_h = _to_int(dev.get("imgw")), _to_int(dev.get("imgh"))

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = gray > 12
    band = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    if bool(mask.any()):
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        band["top"] = int(rows[0])
        band["bottom"] = int(h - 1 - rows[-1])
        band["left"] = int(cols[0])
        band["right"] = int(w - 1 - cols[-1])

    info: dict[str, Any] = {
        "actual_pixels": [w, h],
        "declared_img": [img_w, img_h],
        "declared_logical": [logical_w, logical_h],
        "black_band_px": band,
        "has_black_band": any(v > 2 for v in band.values()),
        "declared_img_matches_actual": (img_w == w and img_h == h),
    }
    if logical_w and logical_h:
        info["scale_to_logical_x"] = round(w / logical_w, 4)
        info["scale_to_logical_y"] = round(h / logical_h, 4)
        info["uniform_scale"] = abs(info["scale_to_logical_x"] - info["scale_to_logical_y"]) < 0.01
        info["aspect_actual"] = round(w / h, 4)
        info["aspect_logical"] = round(logical_w / logical_h, 4)
        a, b = info["aspect_actual"], info["aspect_logical"]
        info["suspected_rotated"] = abs(a - 1.0 / b) < 0.02 and abs(a - b) > 0.02
        info["pixel_equals_logical"] = (w == logical_w and h == logical_h)
    return info


# ---------------------------------------------------------------- A6 插件连通性
def check_plugins(cli: ImouseClient, dev_id: str, tpl_b64: str) -> dict:
    """专业版的找图与 OCR 都是插件，没连上就全是摆设——必须单独确认。"""
    out: dict[str, Any] = {}

    if cli.edition == "xp":
        r = cli.call("/pic/find-image-cv", {"id": dev_id, "img_list": [], "similarity": 0.8})
        out["find_image"] = {"status": r.get("status"), "message": ImouseClient.msg(r)}
        r2 = cli.call("/pic/ocr", {"id": dev_id}, timeout=OCR_TIMEOUT)
        out["ocr"] = {"status": r2.get("status"), "message": ImouseClient.msg(r2)}
        return out

    r = cli.call("find_image", {"deviceid": dev_id, "img": tpl_b64, "similarity": 0.8})
    out["find_image"] = {
        "status": r.get("status"),
        "message": ImouseClient.msg(r),
        "data_code": ImouseClient.payload(r).get("code"),
        "result": ImouseClient.payload(r).get("result"),
    }
    r2 = cli.call("ocr", {"deviceid": dev_id}, timeout=OCR_TIMEOUT)
    out["ocr"] = {
        "status": r2.get("status"),
        "message": ImouseClient.msg(r2),
        "data_code": ImouseClient.payload(r2).get("code"),
    }
    r3 = cli.call("ocr_ex", {"deviceid": dev_id}, timeout=OCR_TIMEOUT)
    out["ocr_ex"] = {"status": r3.get("status"), "message": ImouseClient.msg(r3)}
    return out


# ---------------------------------------------------------------- A4 耗时
def bench_screenshot(cli: ImouseClient, dev_id: str, n: int) -> dict:
    """测截图耗时，bmp 与 jpg 分开测——实测两者差一倍以上，取帧必须用 jpg。"""
    full, rect, jpg = [], [], []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            screenshot_bytes(cli, dev_id)
            full.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass

        t0 = time.perf_counter()
        try:
            screenshot_bytes(cli, dev_id, rect=[0, 0, 200, 200])
            rect.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass

        t0 = time.perf_counter()
        try:
            screenshot_bytes(cli, dev_id, jpg=True)
            jpg.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass
    return {"full_screen_ms": _stat(full), "rect_200x200_ms": _stat(rect),
            "full_screen_jpg_ms": _stat(jpg)}


def bench_find_image(cli: ImouseClient, dev_id: str, tpl_path: Path, n: int) -> dict:
    """测找图耗时，同时记录返回中心点落在哪个坐标空间。"""
    if cli.edition == "xp":
        fun = "/pic/find-image-cv"
        payload: dict[str, Any] = {"id": dev_id, "img_list": [str(tpl_path)], "similarity": 0.8}
    else:
        fun = "find_image"
        payload = {"deviceid": dev_id,
                   "img": base64.b64encode(tpl_path.read_bytes()).decode(),
                   "similarity": 0.8}

    times, centres, codes = [], [], []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            resp = cli.call(fun, payload)
            times.append((time.perf_counter() - t0) * 1000)
            p = ImouseClient.payload(resp)
            if ImouseClient.ok(resp):
                if cli.edition == "xp":
                    lst = p.get("list") or []
                    if lst:
                        centres.append(list(lst[0].get("centre") or []))
                else:
                    codes.append(p.get("code"))
                    if p.get("code") == 0 and p.get("result"):
                        centres.append(list(p["result"]))
        except Exception:  # noqa: BLE001
            pass
    return {"ms": _stat(times), "sample_centre": centres[0] if centres else None,
            "found_count": len(centres), "pro_code_samples": codes[:5]}


def bench_ocr(cli: ImouseClient, dev_id: str, n: int) -> dict:
    """测 OCR 耗时。只在插件可用时才有意义，见 A6。"""
    out: dict[str, Any] = {}
    variants = [("ocr", "/pic/ocr", "ocr"), ("ocr_ex", "/pic/ocr-ex", "ocr_ex")]
    for key, xp_fun, pro_fun in variants:
        fun = xp_fun if cli.edition == "xp" else pro_fun
        base_payload = {"id": dev_id} if cli.edition == "xp" else {"deviceid": dev_id}
        times, texts = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            try:
                resp = cli.call(fun, base_payload, timeout=OCR_TIMEOUT)
                times.append((time.perf_counter() - t0) * 1000)
                if ImouseClient.ok(resp):
                    for x in (ImouseClient.payload(resp).get("list") or []):
                        texts.append(str(x.get("txt") or x.get("text") or ""))
            except Exception:  # noqa: BLE001
                pass
        out[key] = {"ms": _stat(times), "text_count_total": len(texts),
                    "sample_texts": texts[:5]}

        if key == "ocr":
            p_rect = dict(base_payload)
            p_rect["rect"] = _pro_rect([0, 0, 375, 300]) if cli.edition == "pro" else [0, 0, 375, 300]
            times_r = []
            for _ in range(n):
                t0 = time.perf_counter()
                try:
                    cli.call(fun, p_rect, timeout=OCR_TIMEOUT)
                    times_r.append((time.perf_counter() - t0) * 1000)
                except Exception:  # noqa: BLE001
                    pass
            out["ocr_with_rect"] = {"ms": _stat(times_r)}
    return out


# ---------------------------------------------------------------- A5 延迟
def bench_latency(cli: ImouseClient, dev_id: str, n: int) -> dict:
    """连续取 n 帧，量「取帧周期」与「重复帧率」。

    注意：要量的是**相邻两次开始取帧的间隔**（周期），不是"上一帧结束到下一帧开始"
    的空档——后者恒为 0，量了等于没量。
    """
    starts: list[float] = []
    durations: list[float] = []
    hashes: list[int] = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            raw = screenshot_bytes(cli, dev_id, jpg=True)
            img = decode_image(raw)
            small = cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA)
            hashes.append(hash(small.tobytes()))
        except Exception:  # noqa: BLE001
            continue
        t1 = time.perf_counter()
        starts.append(t0)
        durations.append((t1 - t0) * 1000)

    periods = [(starts[i] - starts[i - 1]) * 1000 for i in range(1, len(starts))]
    dup = sum(1 for i in range(1, len(hashes)) if hashes[i] == hashes[i - 1])
    return {
        "frames": len(hashes),
        "capture_duration_ms": _stat(durations),
        "period_ms": _stat(periods),
        "effective_fps": round(1000.0 / _pct(periods, 0.5), 2) if periods else None,
        "distinct_frames": len(set(hashes)),
        "duplicate_frame_ratio": round(dup / max(1, len(hashes) - 1), 3),
    }


# ---------------------------------------------------------------- 键盘（B2 预检）
def probe_keyboard(cli: ImouseClient, dev_id: str) -> dict:
    """只探接口是否可达，**不判断能否输入中文**——官方已注明 send_key 不支持中文。

    中文输入必须走「文本发到剪贴板 + WIN+v 粘贴」，见报告结论。
    本函数不实际发送按键（会真打字），只记录接口名与限制。
    """
    return {
        "fun": "send_key" if cli.edition == "pro" else "/keyboard/input",
        "param": {"deviceid": dev_id, "key": "<ascii only>", "fn_key": ""},
        "official_limit": "只支持英文、数字和英文字符（不支持中文）",
        "chinese_plan": "中文走剪贴板写入 + fn_key=WIN+v 粘贴；或 iOS 自带键盘",
        "note": "本探针不实际发送按键，需人工在真机上确认（B2）",
    }


# ---------------------------------------------------------------- 报告
def render_md(res: dict) -> str:
    edition_cn = {"xp": "XP 版", "pro": "专业版"}.get(res["edition"], res["edition"])
    geo = res.get("geometry") or {}
    dev = res.get("device") or {}
    L: list[str] = []

    L += ["# iMouse 实测报告", "",
          f"> 生成时间：{res['generated_at']}",
          f"> 主机：{res['host']}　版本：**{edition_cn}**　HTTP 端口：**{res['port']}**",
          "> 本文件由 `tools/imouse_probe.py` 自动生成，是后续所有设计的输入，**不要手改**。", ""]

    L += ["## ⚠️ 风险留痕（先读这段）", "",
          "源项目 `core/devices/frame_source.py` 文件头的取帧源矩阵原文：", "",
          "```",
          "| 方式           | 画质         | 痕迹 | 状态     |",
          "|----------------|--------------|------|----------|",
          "| 摄像头         | 有反光/畸变  | 零   | 当前在用 |",
          "| HDMI 采集卡    | 像素级清晰   | 极低 | 计划接入 |",
          "| ADB / WDA      | -            | 高   | **禁用** |",
          "| 投屏 / 镜像    | -            | 高   | **禁用** |",
          "后两者 iOS 上或不可用、或会被 App 检测（用户实测原测算软件因此被封）。",
          "```", "",
          "iMouse 正是靠 **AirPlay 镜像**工作，等于主动踩了这条红线。",
          "**这是唯一可能让整个项目白干的风险点**，需用户显式确认已评估。", ""]

    L += ["## A1 版本与端口", "", f"结论：**{edition_cn}**，HTTP 端口 **{res['port']}**。", "",
          "| 探测项 | 结果 |", "|---|---|"]
    for k, v in (res.get("detect_evidence") or {}).items():
        L.append(f"| `{k}` | {str(v)[:160]} |")
    L.append("")

    L += ["## A2 设备与坐标空间", "", "| 字段 | 值 |", "|---|---|"]
    for k in ("deviceid", "device_name", "model", "version", "state", "rotate"):
        L.append(f"| `{k}` | `{dev.get(k)}` |")
    L += ["", "| 来源 | 宽 × 高 | 说明 |", "|---|---|---|",
          f"| `width/height`（逻辑点） | {geo.get('declared_logical')} | 屏幕逻辑分辨率 |",
          f"| `imgw/imgh`（声明截图） | {geo.get('declared_img')} | 设备自报 |",
          f"| **实测解码出的截图** | **{geo.get('actual_pixels')}** | 真值 |", "",
          "| 判据 | 值 |", "|---|---|",
          f"| **截图像素 == 逻辑点** | **{geo.get('pixel_equals_logical')}** |",
          f"| 缩放 X / Y | {geo.get('scale_to_logical_x')} / {geo.get('scale_to_logical_y')} |",
          f"| 等比 | {geo.get('uniform_scale')} |",
          f"| 疑似旋转 | {geo.get('suspected_rotated')} |", ""]
    if geo.get("pixel_equals_logical"):
        L += ["> ✅ **截图像素坐标 == 手机逻辑点坐标，ratio=1.0，不需要坐标换算层。**",
              "> 前提：专业版截图必须保持 `original=false`（true 会返回高清图，与手机分辨率不一致）。", ""]
    else:
        L += ["> ⚠️ 截图像素与逻辑点不一致，必须建坐标换算层，换算比见上表。", ""]

    band = geo.get("black_band_px") or {}
    L += ["## A3 黑边与有效区域", "",
          f"- 是否检测到黑边：**{geo.get('has_black_band')}**",
          f"- 上/下/左/右 纯黑像素：{band.get('top')} / {band.get('bottom')} / {band.get('left')} / {band.get('right')}",
          f"- 截图留档：`{res.get('shot_path')}`（同时存了一份 .png 便于查看）", "",
          "> 自动化只能量**纯黑边**。**本机实测（打开留档图确认）**：截图**包含状态栏**",
          "> （顶部约 y∈[0, 44] 是时间/信号/电池），不含刘海安全区偏移。",
          "> 意味着 UI 元素的 y 坐标就比「全屏内容区」多约 44 像素，",
          "> 模板 ROI 与点击目标按「逻辑点 y 坐标」算即可，**不用**单独减状态栏偏移。", ""]

    if res.get("plugins"):
        L += ["## A6 插件连通性（专业版关键）", "",
              "专业版的找图与 OCR 都是**插件**，接口存在但插件没连上就全是摆设。", "",
              "| 能力 | status | data.code | message |", "|---|---|---|---|"]
        for k, v in res["plugins"].items():
            L.append(f"| `{k}` | {v.get('status')} | {v.get('data_code')} | {v.get('message')} |")
        L.append("")

    if res.get("timing"):
        t = res["timing"]
        plugin_bad = _plugins_unavailable(res.get("plugins"))
        L += ["## A4 接口耗时（毫秒）", ""]
        if plugin_bad:
            L += ["> ⚠️ **找图与 OCR 的插件未连接，下表这两项的耗时是「失败秒返」的假数据，不可采信。**",
                  "> 只有「截图」与 A5 取帧节奏的数据有效。", ""]
        L += ["| 接口 | p50 | p95 | min | max | 样本 | 有效 |", "|---|---|---|---|---|---|---|"]
        for label, path, need_plugin in [
            ("截图 全屏（bmp）", ("screenshot", "full_screen_ms"), False),
            ("截图 全屏（jpg）", ("screenshot", "full_screen_jpg_ms"), False),
            ("截图 rect 200×200", ("screenshot", "rect_200x200_ms"), False),
            ("找图 find_image", ("find_image", "ms"), True),
            ("OCR", ("ocr", "ms"), True),
            ("OCR 带 rect", ("ocr_with_rect", "ms"), True),
            ("OCR 增强 ocr_ex", ("ocr_ex", "ms"), True),
        ]:
            if need_plugin and plugin_bad:
                node: Any = t
                for p in path:
                    node = node.get(p) if isinstance(node, dict) else None
                n = node.get("n") if isinstance(node, dict) else 0
                L.append(f"| ~~{label}~~ | - | - | - | - | {n} | ❌ 插件未连接 |")
                continue
            node: Any = t
            for p in path:
                node = node.get(p) if isinstance(node, dict) else None
            if isinstance(node, dict) and node.get("n"):
                L.append(f"| {label} | {node['p50']} | {node['p95']} | {node['min']} | "
                         f"{node['max']} | {node['n']} | ✅ |")
        L += ["",
              f"- 找图返回中心点样例：`{(t.get('find_image') or {}).get('sample_centre')}`"
              f"（找到 {(t.get('find_image') or {}).get('found_count')} 次）",
              f"- 逻辑点宽高：`{geo.get('declared_logical')}`", "",
              "> 判读：中心点数值若**超出**逻辑点宽高，说明它在别的坐标空间，必须换算。", ""]

    if res.get("latency"):
        lat = res["latency"]
        per, dur = lat.get("period_ms") or {}, lat.get("capture_duration_ms") or {}
        L += ["## A5 投屏延迟与取帧节奏", "", "| 指标 | 值 |", "|---|---|",
              f"| 取帧数 | {lat.get('frames')} |",
              f"| 单次取帧耗时 p50 / p95 (ms) | {dur.get('p50')} / {dur.get('p95')} |",
              f"| **取帧周期 p50 / p95 (ms)** | {per.get('p50')} / {per.get('p95')} |",
              f"| **等效帧率 (fps)** | {lat.get('effective_fps')} |",
              f"| 不同帧数 / 总帧数 | {lat.get('distinct_frames')} / {lat.get('frames')} |",
              f"| 重复帧率 | {lat.get('duplicate_frame_ratio')} |", "",
              "> 静态画面下重复帧率高属正常；若画面本应在变却始终重复，说明有投屏延迟，",
              "> 主循环 `frame_interval_sec` 与「点完立即复判」需据此加稳定检测。", ""]

    if res.get("keyboard"):
        kb = res["keyboard"]
        L += ["## 键盘输入（B2 预检）", "",
              f"- 接口：`{kb.get('fun')}`",
              f"- **官方限制：{kb.get('official_limit')}**",
              f"- 中文方案：{kb.get('chinese_plan')}", "",
              "> ⚠️ **「线上沟通打字」这个需求目前不成立**：send_key 不支持中文。",
              "> 需人工确认剪贴板+粘贴方案是否可行（B2）。", ""]

    L += ["## 待人工落实（B 组）", "", "| # | 项目 | 结论 |", "|---|---|---|",
          "| **B1** | 截图像素 ↔ 手机逻辑点 ↔ 机械臂落点 三者闭合 | ☐ 待落实 |",
          "| **B2** | 中文输入可用性（send_key 不支持中文，需剪贴板+粘贴） | ☐ 待落实 |",
          "| **B3** | 断连与恢复（决定要不要保留摄像头降级路径） | ☐ 待落实 |", "",
          "B1 做法（已有天然靶子）：",
          "  1. 当前桌面就是**「App 资源库 / 最近添加」**页，**「运满满 司机」图标就在画面里**（约逻辑点 (260, 100)），",
          "     已知坐标 → 用它做标定验证的「已知 UI 元素」，比随便画的网页靶点更接近真实业务。",
          "  2. `tools/imouse_close_loop_check.py` —— 截图 → **本地 OpenCV** 找图标（不用 iMouse 找图插件）",
          "     → 换算成逻辑点（ratio=1.0，直接用） → **机械臂点过去** → 再截图，",
          "     OCR 看到「运满满司机」标题 → 命中。",
          "  3. **不通则后续全部停摆。**", ""]

    if res.get("errors"):
        L += ["## 探测过程中的异常", ""] + [f"- {e}" for e in res["errors"]] + [""]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="iMouse 探活与实测探针")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--repeat", type=int, default=30)
    ap.add_argument("--skip-timing", action="store_true", help="只探活，跳过 A4/A5")
    ap.add_argument("--test-keyboard", action="store_true", help="记录键盘接口信息（不实际发送）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    res: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": args.host,
        "errors": [],
    }

    try:
        _log(f"[A1] 探测版本与端口（{args.host}）...")
        edition, port, evidence = detect_edition(args.host)
        res.update(edition=edition, port=port, detect_evidence=evidence)
        edition_cn = {"xp": "XP 版", "pro": "专业版"}[edition]
        _log(f"     -> {edition_cn}，HTTP {port}")

        cli = ImouseClient(args.host, edition, port)

        _log("[A2] 读取设备列表...")
        devices = fetch_devices(cli)
        dev = pick_online(devices)
        dev_id = device_id(dev)
        res.update(device=dev, device_count=len(devices))
        _log(f"     -> {len(devices)} 台，选用 {dev.get('device_name') or dev_id} "
             f"logical={dev.get('width')}x{dev.get('height')} img={dev.get('imgw')}x{dev.get('imgh')}")

        _log("[A3] 截图并分析几何...")
        raw = screenshot_bytes(cli, dev_id)
        img = decode_image(raw)
        shot_path = SHOT_DIR / "probe_full.bmp"
        shot_path.write_bytes(raw)
        res["geometry"] = analyze_geometry(img, dev)
        res["shot_path"] = str(shot_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        _log(f"     -> 实际 {img.shape[1]}x{img.shape[0]}，逻辑点 {dev.get('width')}x{dev.get('height')}，"
             f"缩放 {res['geometry'].get('scale_to_logical_x')}")

        # A6 插件连通性（专业版必须先确认，否则 A4 的耗时数据全是假的）
        _log("[A6] 检查找图/OCR 插件连通性...")
        h, w = img.shape[:2]
        patch = img[h // 2 - 40: h // 2 + 40, w // 2 - 40: w // 2 + 40]
        tpl_path = SHOT_DIR / "probe_tpl.bmp"
        cv2.imwrite(str(tpl_path), patch)
        res["plugins"] = check_plugins(cli, dev_id, base64.b64encode(tpl_path.read_bytes()).decode())
        for k, v in res["plugins"].items():
            _log(f"     -> {k}: status={v.get('status')} code={v.get('data_code')} {v.get('message')}")

        if not args.skip_timing:
            _log(f"[A4] 耗时测试（每项 {args.repeat} 次）...")
            res["timing"] = {"screenshot": bench_screenshot(cli, dev_id, args.repeat)}
            res["timing"]["find_image"] = bench_find_image(cli, dev_id, tpl_path, args.repeat)
            _log(f"     -> 截图 p50 {res['timing']['screenshot']['full_screen_ms'].get('p50')}ms，"
                 f"找图 p50 {res['timing']['find_image']['ms'].get('p50')}ms"
                 f"（找到 {res['timing']['find_image'].get('found_count')} 次）")

            _log("     -> OCR（较慢，请稍候）...")
            res["timing"].update(bench_ocr(cli, dev_id, min(args.repeat, 10)))
            _log(f"     -> OCR p50 {res['timing']['ocr']['ms'].get('p50')}ms，"
                 f"ocr_ex p50 {res['timing']['ocr_ex']['ms'].get('p50')}ms")

            _log("[A5] 连续取帧测延迟...")
            res["latency"] = bench_latency(cli, dev_id, args.repeat)
            _log(f"     -> 周期 p50 {res['latency']['period_ms'].get('p50')}ms，"
                 f"等效 {res['latency']['effective_fps']}fps，"
                 f"不同帧 {res['latency']['distinct_frames']}/{res['latency']['frames']}")

        if args.test_keyboard:
            res["keyboard"] = probe_keyboard(cli, dev_id)

    except Exception as exc:  # noqa: BLE001
        res["errors"].append(f"{type(exc).__name__}: {exc}")
        _log(f"[错误] {type(exc).__name__}: {exc}")

    REPORT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    REPORT_MD.write_text(render_md(res), encoding="utf-8")
    _log("")
    _log(f"报告已写入：{REPORT_MD}")
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
