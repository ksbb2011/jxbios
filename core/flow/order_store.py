"""订单去重库（SQLite）：结构化字段做稳定去重 key，跨运行持久检索。

为什么需要它（现状痛点）：
    * 旧去重只有 `FingerprintCache`（内存）——
      ① 跨运行不保留：今天点过的单，明天重跑又点；
      ② OCR 文本指纹随抖动变化：同单每帧指纹可能不同，去重失效。
    * 日志里的 `TraceWriter` 只是 JSONL 事件流，不是结构化查询库，不能快速查重。

设计：
    * 去重 key（dup_key）用**结构化稳定字段**——路线 + 始发地 + 目的地 +
      车长 + 车型 + 货重 + 方数。price / distance 会浮动（同单 350→370 元），
      **不进 dup_key**，仅作展示/统计存储。
    * 每次运行启动清理过期记录（ttl_days 可配，0=永久）。
    * 零依赖：Python 自带 sqlite3。

调用点：
    * scanner.scan_once：命中 dup_key 即跳过（进详情前拦，省时间）；
    * detail 处理（抢/弃/拒）：写库标记状态，跨运行不再重复处理。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from typing import List, Optional, Sequence, Tuple


# 「始发地→目的地」提取：OCR 把箭头识别成多种形态都兼容
#   箭头：  →  ➜  ➔  ⇒
#   横杠：  -（半角）  —（中文破折号）  －（全角短横，CJK 最常见，旧正则漏了它）
#   其他：  =>（等于箭头）  到（个别皮肤用「到」连接，少见）
# 分隔符两侧强制要求中文城市名（[一-龥]{2,}），这样「4.2-5米」数字区间、
# 「09－15」日期绝不会被误当成路线。
_DELIM = r"(?:=>|→|➜|➔|⇒|[-—－>]|到)"
_OD_RE = re.compile(rf"([一-龥]{{2,}})\s*{_DELIM}\s*([一-龥]{{2,}})")


def _s(v: object) -> str:
    """归一化成字符串（None→""），用于 SQL 比较。"""
    return "" if v is None else str(v).strip()


def _n(v: object) -> float:
    """归一化成数字（None/非数字→-1），吨位/方数参与 SQL 比较前统一量化。"""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return -1.0


def extract_od(texts: Sequence[str]) -> Tuple[str, str]:
    """从 OCR 文本里提取「始发地 / 目的地」。

    去重 key 的始/目的部分用它，让「同路线同参数不同发货单」也能被区分
    （更细粒度，不误合并）。OCR 抖动落在箭头符号上很常见（→ / -> / - / — /
     全角－ / => / 到 都兼容，见 _OD_RE），落在城市名上才是真问题——但城市名
     抖动极少。

    调用方传入的是**整张卡片**的 OCR 文本（card.texts / detail.texts），路线行
    通常落在同一框内，故先逐框匹配；个别皮肤把 A/箭头/B 拆成多个框，再用
    「空格拼接整卡」做一次兜底匹配。
    """
    if not texts:
        return ("", "")
    for t in texts:
        m = _OD_RE.search(t or "")
        if m:
            return (m.group(1), m.group(2))
    # 兜底：跨框（OCR 把「苏州」「→」「广州」拆成三个框）
    blob = " ".join(t for t in texts if t)
    m = _OD_RE.search(blob)
    if m:
        return (m.group(1), m.group(2))
    return ("", "")


class OrderStore:
    """SQLite 订单去重库。"""

    def __init__(
        self,
        db_path: str,
        ttl_days: float = 3.0,
        enabled: bool = True,
    ) -> None:
        self.db_path = db_path
        self.ttl = float(ttl_days)
        self.enabled = bool(enabled)
        self._conn: Optional[sqlite3.Connection] = None
        # 连接跨线程使用：装配在后台 boot 线程（创建连接），主循环在另一个 worker
        # 线程、stop 时 close 又可能回到 GUI 线程——check_same_thread 必须关，并用
        # 锁串行化访问，否则会抛「SQLite objects created in a thread can only be used
        # in that same thread」。
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self._ensure_schema()
            except Exception as exc:  # noqa: BLE001 建库失败不能阻断主流程
                self.enabled = False
                import logging

                logging.getLogger(__name__).warning("订单库初始化失败，去重降级为内存: %s", exc)

    # ---------------------------------------------------------------- key

    @staticmethod
    def extract_od(texts: Sequence[str]) -> Tuple[str, str]:
        """类内别名：调用方（scanner / detail）统一写 OrderStore.extract_od。"""
        return extract_od(texts)

    @staticmethod
    def dup_key(
        route: str,
        origin: str,
        dest: str,
        che_len: object,
        che_type: object,
        tonnage: object,
        volume: object,
    ) -> str:
        """稳定去重 key：归一化后拼成串再 md5。

        归一化要点：None→""；浮点四舍五入到 2 位（避免 1.999999/2.0 抖动）；
        字符串去首尾空白。price/distance 故意不传入——会浮动。
        """

        def s(v: object) -> str:
            if v is None:
                return ""
            if isinstance(v, float):
                return f"{v:.2f}"
            return str(v).strip()

        raw = "|".join(
            [s(route), s(origin), s(dest), s(che_len), s(che_type), s(tonnage), s(volume)]
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    # ---------------------------------------------------------------- schema

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False：连接会在多个线程间使用，靠 self._lock 串行化。
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        with self._lock:
            conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                dup_key   TEXT NOT NULL,
                route     TEXT,
                origin    TEXT,
                dest      TEXT,
                che_len   TEXT,
                che_type  TEXT,
                tonnage   REAL,
                volume    REAL,
                price     REAL,
                distance  REAL,
                unit_price REAL,
                status    TEXT,
                first_seen REAL,
                last_seen REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_dup   ON orders(dup_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_route ON orders(route)")
        conn.commit()

    # ---------------------------------------------------------------- 查询/写

    def is_seen(self, dup_key: str) -> bool:
        """该订单是否已处理过（未过期）。enabled=False 或空 key 直接返回 False。

        ⚠️ 旧实现把 TTL 写进 SQL 的 `OR ? <= 0` 分支，该分支恒真 → 过期记录也
        被判成 seen，ttl 形同虚设（3 天前的老单永远不再看）。这里改为取回
        last_seen 后在 Python 里判新鲜度，逻辑一眼可验证。
        """
        if not self.enabled or not dup_key:
            return False
        # dry_ 前缀 = 试跑（dry_run）留下的记录，当时并没有真抢/真处理，
        # 不能拿来挡住正式运行——否则试跑一次就把这些真单锁死 3 天。
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT last_seen FROM orders WHERE dup_key=? "
                    "AND IFNULL(status,'') NOT LIKE 'dry\\_%' ESCAPE '\\' LIMIT 1",
                    (dup_key,),
                )
                .fetchone()
            )
        return row is not None and self._fresh(row[0])

    def is_spec_seen(
        self,
        route: str,
        che_len: object,
        che_type: object,
        tonnage: object,
        volume: object,
    ) -> bool:
        """按「路线 + 车型参数」查重（忽略始/目的地与价格）。

        为什么需要第二粒度：列表卡片常常读不到「A→B」（路线行可能落在切卡边界外，
        或被"电议"标签挤掉），于是算不出与详情页一致的 dup_key，已处理过的单
        回到列表又被点进去一次（用户实测「同一个 #1 连点三次」）。车长/车型/
        吨位/方数在列表页稳定可读，用它们做**参数级**兜底判重。

        代价（已知）：同路线 + 同车长车型 + 同吨位方数的**不同**发货单会被合并，
        可能漏看。真遇到漏单就把 runtime.dedup.spec_level 改成 "off"，
        或把 ttl 调小（默认 3 天）。全空参数不判重，避免误杀一大片。
        """
        if not self.enabled:
            return False
        if che_len is None and che_type is None and tonnage is None and volume is None:
            return False
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT last_seen FROM orders WHERE IFNULL(route,'')=? "
                    "AND IFNULL(che_len,'')=? AND IFNULL(che_type,'')=? "
                    "AND ROUND(IFNULL(tonnage,-1),2)=ROUND(?,2) "
                    "AND ROUND(IFNULL(volume,-1),2)=ROUND(?,2) "
                    "AND IFNULL(status,'') NOT LIKE 'dry\\_%' ESCAPE '\\' LIMIT 1",
                    (
                        _s(route),
                        _s(che_len),
                        _s(che_type),
                        _n(tonnage),
                        _n(volume),
                    ),
                )
                .fetchone()
            )
        return row is not None and self._fresh(row[0])

    def _fresh(self, last_seen: object) -> bool:
        """记录是否还在 ttl 内（ttl<=0 表示永久有效）。"""
        if self.ttl <= 0:
            return True
        try:
            return time.time() - float(last_seen) <= self.ttl * 86400.0
        except (TypeError, ValueError):
            return True

    def record(
        self,
        dup_key: str,
        route: str = "",
        origin: str = "",
        dest: str = "",
        che_len: object = None,
        che_type: object = None,
        tonnage: object = None,
        volume: object = None,
        price: object = None,
        distance: object = None,
        unit_price: object = None,
        status: str = "seen",
    ) -> None:
        """写/更新一条订单记录。首次写入 first_seen，之后只更新 last_seen/status/字段。"""
        if not self.enabled or not dup_key:
            return
        now = time.time()
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT id, first_seen FROM orders WHERE dup_key=? LIMIT 1", (dup_key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO orders (dup_key,route,origin,dest,che_len,che_type,tonnage,"
                    "volume,price,distance,unit_price,status,first_seen,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        dup_key, route, origin, dest, che_len, che_type, tonnage, volume,
                        price, distance, unit_price, status, now, now,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE orders SET route=?,origin=?,dest=?,che_len=?,che_type=?,tonnage=?,"
                    "volume=?,price=?,distance=?,unit_price=?,status=?,last_seen=? WHERE dup_key=?",
                    (
                        route, origin, dest, che_len, che_type, tonnage, volume,
                        price, distance, unit_price, status, now, dup_key,
                    ),
                )
            conn.commit()

    def mark_dry_run(self) -> int:
        """把历史记录标记为试跑（加 dry_ 前缀），使它们不再参与去重。

        一次性补救：2026-09-06 之前的 dry_run 试跑把 29 条单写成了 grabbed，
        库是跨运行的，正式跑时这些真单会被当成「已抢过」跳过。跑一次即幂等
        （新写入的 dry_ 记录不会被重复加前缀）。返回受影响行数。
        """
        if not self.enabled:
            return 0
        with self._lock:
            cur = self._connect().execute(
                "UPDATE orders SET status='dry_'||status "
                "WHERE IFNULL(status,'') NOT LIKE 'dry\\_%' ESCAPE '\\'"
            )
            self._connect().commit()
        return int(cur.rowcount or 0)

    def cleanup(self) -> int:
        """清理过期记录（ttl<=0 不清理）。返回删除条数。"""
        if not self.enabled or self.ttl <= 0:
            return 0
        with self._lock:
            conn = self._connect()
            before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            conn.execute(
                "DELETE FROM orders WHERE last_seen < ?", (time.time() - self.ttl * 86400.0,)
            )
            conn.commit()
        return before - conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def close(self) -> None:
        """关闭连接（退出/换库前调用；不关的话 Windows 上文件被占用删不掉）。"""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001 关库失败无所谓
                    pass
                self._conn = None

    def count_for_route(self, route: str) -> int:
        if not self.enabled or not route:
            return 0
        with self._lock:
            return self._connect().execute(
                "SELECT COUNT(*) FROM orders WHERE route=?", (route,)
            ).fetchone()[0]
