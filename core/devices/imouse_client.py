"""iMouse 取帧源：在新项目里承担「感知」的唯一入口。

边界（用户 2026-09-09 明确）：
    * 只替代摄像头做"看屏幕"（截图 / 图色 / OCR）。
    * 所有"点击 / 滑动 / 长按 / 物理键"一律仍由机械臂执行。
    * 唯一允许的操作类接口是 **send_key**（键盘输入），用于线上沟通打字。
      但 send_key 官方注明**不支持中文**——线上沟通的中文走剪贴板 + 粘贴（见 B2）。

本模块只是**客户端**（HTTP/JSON 封装 + 探测 + 取图），不实现 FrameSource。
取帧源协议实现在 `imouse_source.py`，复用 `core.devices.frame_source` 已有的
FrameSet / FrameSource / FrameSourceError / crop_letterbox / resize_keep_aspect，
业务层（RobotTask / ActionKit / FlowContext）完全无感。

为什么客户端与 FrameSource 分开两个文件：
    客户端可在没有 FrameSource 的场景下单独使用（标定工具、手动探针），
    也便于 mock 测试时注入自定义 `screenshot_fn`。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import requests

HTTP_TIMEOUT = 8.0
PRO_FUN_DEVICE_LIST = ("get_device_list", "get_device", "device_list")

_LOG = logging.getLogger("imouse.client")


# ---------------------------------------------------------------- 错误
class ImouseError(RuntimeError):
    """iMouse 协议层错误（探测失败 / 设备离线 / 接口异常）。"""


# ---------------------------------------------------------------- 数据
@dataclass(frozen=True)
class DeviceInfo:
    """专业版 /device/get 单条设备记录。

    注意：专业版返回值是 dict（id -> info）而非 list；客户端在探测时已统一
    拍平为 DeviceInfo 列表，并回填原 id 到 `_key` 字段。
    """

    id: str
    name: str
    width: int       # 屏幕逻辑宽（iPhone X = 375）
    height: int      # 屏幕逻辑高（iPhone X = 812）
    imgw: int        # 设备自报截图宽（专业版 ≈ 逻辑宽，XP 版 ≈ 2 倍）
    imgh: int
    rotate: int
    state: int       # 0 不在线 / 非0 在线
    model: str
    version: str
    raw: dict[str, Any]


# ---------------------------------------------------------------- 客户端
class ImouseClient:
    """iMouse 内核服务的极简 HTTP/JSON 客户端，兼容 XP 版与专业版。

    主要能力：取帧（jpg/bmp, 截区域）+ 设备列表 + 找色 + 键盘。
    不封装：找图 / OCR —— 实测发现这两个是**插件**且插件默认未安装，
    用不到。如果后续装上插件再补 fun。
    """

    def __init__(self, host: str = "127.0.0.1", edition: str | None = None,
                 http_port: int | None = None,
                 log: Callable[[str, str], None] | None = None,
                 timeout: float = HTTP_TIMEOUT) -> None:
        self.host = host
        self.timeout = timeout
        self.edition = edition
        self.port = http_port
        self._log = log or (lambda lvl, msg: _LOG.log(
            {"info": logging.INFO, "warn": logging.WARNING,
             "error": logging.ERROR, "debug": logging.DEBUG}.get(lvl, logging.INFO),
            msg))
        self._session = requests.Session()
        self._msgid = 0

    # ---- 探测
    def detect(self) -> tuple[str, int]:
        """探测当前 iMouse 版本与端口。返回 (edition, port)。"""
        evidence: dict[str, Any] = {}
        for port in (9911, 9912):
            try:
                r = requests.get(f"http://{self.host}:{port}/api/device/get", timeout=3)
                body = r.json()
                if (r.status_code == 200 and isinstance(body.get("data"), dict)
                        and "list" in body["data"]):
                    self._log("info", f"iMouse 探测：XP 版 @ {port}")
                    self.edition, self.port = "xp", port
                    return "xp", port
            except Exception as exc:  # noqa: BLE001
                evidence[f"xp_{port}"] = type(exc).__name__
        for port in (9912, 9911):
            for fun in PRO_FUN_DEVICE_LIST:
                try:
                    r = requests.post(
                        f"http://{self.host}:{port}/api",
                        json={"fun": fun, "msgid": 1, "data": {}}, timeout=3)
                    if int(r.json().get("status", -1)) == 0:
                        self._log("info", f"iMouse 探测：专业版 @ {port}（fun={fun}）")
                        self.edition, self.port = "pro", port
                        return "pro", port
                except Exception as exc:  # noqa: BLE001
                    evidence[f"pro_{port}_{fun}"] = type(exc).__name__
        raise ImouseError(
            f"未能识别 iMouse 版本（{self.host}）。请确认：内核已启动、手机已投屏在线、主机正确。"
            f"证据：{json.dumps(evidence, ensure_ascii=False)}"
        )

    def _base(self) -> str:
        assert self.edition and self.port, "请先调用 detect()"
        return f"http://{self.host}:{self.port}/api"

    def call(self, fun: str, data: dict[str, Any] | None = None,
             timeout: float | None = None) -> dict:
        data = data or {}
        self._msgid += 1
        to = timeout or self.timeout
        if self.edition == "xp":
            body: dict[str, Any] = {"fun": fun, "data": data}
        else:
            body = {"fun": fun, "msgid": self._msgid, "data": data}
        r = self._session.post(self._base(), json=body, timeout=to)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def ok(resp: dict) -> bool:
        top = resp.get("status")
        if top == 200:
            return int(resp.get("data", {}).get("code", -1)) == 0
        return int(top) == 0

    @staticmethod
    def msg(resp: dict) -> str:
        return str(resp.get("message") or resp.get("data", {}).get("message") or "")

    # ---- 设备
    def list_devices(self) -> list[DeviceInfo]:
        assert self.edition, "请先调用 detect()"
        if self.edition == "xp":
            resp = self.call("/device/get")
            if not self.ok(resp):
                raise ImouseError(f"/device/get 失败：{self.msg(resp)}")
            raw_list = self._payload(resp).get("list") or []
            return [self._parse_dev(d) for d in raw_list]
        for fun in PRO_FUN_DEVICE_LIST:
            resp = self.call(fun)
            if self.ok(resp):
                data = self._payload(resp)
                raw_list: list[dict] = []
                for key in ("list", "devices", "device_list"):
                    if isinstance(data.get(key), list):
                        raw_list = list(data[key])
                        break
                if not raw_list and all(isinstance(v, dict) for v in data.values()):
                    raw_list = [dict(v, _key=k) for k, v in data.items()]
                if raw_list:
                    return [self._parse_dev(d) for d in raw_list]
        raise ImouseError("专业版获取设备列表失败")

    @staticmethod
    def _parse_dev(d: dict) -> DeviceInfo:
        return DeviceInfo(
            id=str(d.get("deviceid") or d.get("_key") or d.get("id") or d.get("mac") or ""),
            name=str(d.get("device_name") or d.get("name") or ""),
            width=int(d.get("width") or 0),
            height=int(d.get("height") or 0),
            imgw=int(d.get("imgw") or 0),
            imgh=int(d.get("imgh") or 0),
            rotate=int(d.get("rotate") or 0),
            state=int(d.get("state") or 0),
            model=str(d.get("model") or ""),
            version=str(d.get("version") or ""),
            raw=d,
        )

    @staticmethod
    def _payload(resp: dict) -> dict:
        d = resp.get("data")
        return d if isinstance(d, dict) else {}

    def pick_online(self, devices: list[DeviceInfo] | None = None) -> DeviceInfo:
        devs = devices or self.list_devices()
        if not devs:
            raise ImouseError("设备列表为空——请先在控制台里把手机投屏连上")
        for d in devs:
            if d.state != 0:
                return d
        return devs[0]

    # ---- 截图
    def screenshot(self, device_id: str, jpg: bool = True,
                   rect: list[int] | None = None) -> np.ndarray:
        """取一帧并直接解码为 numpy BGR 数组。

        为什么默认 jpg：实测 bmp 138ms vs jpg 62ms（专业版，iPhone X），
        jpg 压缩对模板匹配 / OCR 在 375×812 分辨率上**无可见损失**。
        若后续验证 jpg 影响阈值/OCR 召回率，再回退 bmp。
        """
        raw = self.screenshot_bytes(device_id, jpg=jpg, rect=rect)
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ImouseError(f"截图解码失败（前 16 字节：{raw[:16]!r}）")
        return img

    def screenshot_bytes(self, device_id: str, jpg: bool = True,
                         rect: list[int] | None = None) -> bytes:
        """原始字节，未解码。供标定页的 WIA 等场景使用。"""
        assert self.edition, "请先调用 detect()"
        if self.edition == "xp":
            data: dict[str, Any] = {"id": device_id, "jpg": jpg}
            if rect:
                data["rect"] = rect
            r = self._session.get(f"{self._base()}/pic/screenshot",
                                  params=data, timeout=self.timeout)
            r.raise_for_status()
            return r.content

        data = {"deviceid": device_id, "isjpg": jpg, "binary": False, "original": False}
        if rect:
            data["rect"] = self._pro_rect(rect)
        resp = self.call("get_device_screenshot", data)
        if not self.ok(resp):
            raise ImouseError(f"get_device_screenshot 失败：{self.msg(resp)}")
        p = self._payload(resp)
        img = p.get("img") or p.get("image")
        if not img:
            raise ImouseError("截图返回里没有 img/image 字段")
        if isinstance(img, str) and len(img) < 4096 and ("/" in img or "\\" in img):
            return Path(img).read_bytes()
        return base64.b64decode(img)

    @staticmethod
    def _pro_rect(rect: list[int]) -> list[list[int]]:
        """[左,上,右,下] → 专业版四点格式 [[lt],[lb],[rt],[rb]]。"""
        l, t, r, b = (int(v) for v in rect)
        return [[l, t], [l, b], [r, t], [r, b]]

    # ---- 找色（多点比色）
    def find_multi_color(self, device_id: str, color_spec: str,
                         similarity: float = 0.85,
                         rect: list[int] | None = None) -> list[int] | None:
        """返回第一个命中点的 centre [x, y]，未命中返回 None。

        color_spec: "x|y|RRGGBB-偏色,x|y|RRGGBB-偏色,..."
        """
        if self.edition == "xp":
            payload: dict[str, Any] = {
                "id": device_id,
                "list": [{"first_color": color_spec, "similarity": float(similarity)}],
                "same": False, "all": False,
            }
            if rect:
                payload["rect"] = rect
            resp = self.call("/pic/find-multi-color", payload)
        else:
            payload = {
                "deviceid": device_id,
                "list": [{"first_color": color_spec, "similarity": float(similarity)}],
                "same": False, "all": False,
            }
            if rect:
                payload["rect"] = self._pro_rect(rect)
            resp = self.call("find_multi_color", payload)

        if not self.ok(resp):
            raise ImouseError(f"find_multi_color 失败：{self.msg(resp)}")
        p = self._payload(resp)
        if self.edition == "pro":
            if p.get("code") == 0:
                r = p.get("result")
                if r and len(r) >= 2:
                    return [int(r[0]), int(r[1])]
            return None
        lst = p.get("list") or []
        if lst and lst[0].get("centre"):
            c = lst[0]["centre"]
            return [int(c[0]), int(c[1])]
        return None

    # ---- 键盘（用户唯一授权的操作类接口）
    def send_key(self, device_id: str, key: str = "", fn_key: str = "") -> None:
        """发送按键。

        ⚠️ 官方限制：send_key **只支持英文/数字/英文字符，不支持中文**。
        中文方案：先把文本写入手机剪贴板（iOS 快捷指令 / Bark / 自研 H5），
        再用 `fn_key="WIN+v"` 粘贴。
        """
        resp = self.call("send_key" if self.edition == "pro" else "/keyboard/input",
                         {"deviceid": device_id, "key": key, "fn_key": fn_key})
        if not self.ok(resp):
            raise ImouseError(f"send_key 失败：{self.msg(resp)}")


# ---------------------------------------------------------------- 一站式探活
def probe(host: str = "127.0.0.1", timeout: float = HTTP_TIMEOUT) -> dict:
    """一站式探活：探测 + 列出设备 + 取一帧 + 给出坐标空间结论。"""
    cli = ImouseClient(host=host, timeout=timeout)
    edition, port = cli.detect()
    devs = cli.list_devices()
    online = cli.pick_online(devs)
    img = cli.screenshot(online.id, jpg=True)
    h, w = img.shape[:2]
    return {
        "edition": edition,
        "port": port,
        "device": online,
        "actual_pixels": [w, h],
        "declared_img": [online.imgw, online.imgh],
        "declared_logical": [online.width, online.height],
        "pixel_equals_logical": w == online.width and h == online.height,
        "scale_x": round(w / online.width, 4) if online.width else None,
        "scale_y": round(h / online.height, 4) if online.height else None,
    }


if __name__ == "__main__":
    t0 = time.perf_counter()
    out = probe()
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    print(json.dumps(
        {k: (v if not isinstance(v, DeviceInfo) else v.__dict__)
         for k, v in out.items()},
        ensure_ascii=False, indent=2, default=str))