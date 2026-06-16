"""
geo_matcher.py — 空间匹配引擎

加载 4546 个网点片区 WKT 多边形，构建 STRtree 空间索引，
实现毫秒级 Point-in-Polygon 判断，等价替代 Turf.js booleanPointInPolygon。
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from shapely import from_wkt, Point
from shapely.strtree import STRtree

from address_cleaner.config import settings

logger = logging.getLogger(__name__)


class GeoMatcher:
    """
    空间匹配器。

    从 areas.json 加载多边形，构建 R-Tree 空间索引，
    给定经纬度快速查询所属片区的三段码。

    Usage:
        matcher = GeoMatcher(areas_json_path)
        code = matcher.match(118.370, 35.036)  # → "J21" 或 None
    """

    def __init__(self, areas_path: Optional[Path] = None) -> None:
        """
        初始化空间匹配器。

        Args:
            areas_path: areas.json 路径（默认从 settings 读取）
        """
        self.areas_path = areas_path or settings.areas_path
        self.wkt_field = settings.wkt_field
        self.segment_code_field = settings.segment_code_field

        self._polygons: list = []          # shapely Polygon 对象
        self._segment_codes: list[str] = []  # 与 polygons 一一对应的三段码
        self._tree: Optional[STRtree] = None

        self._load_areas()
        self._build_index()

    def _load_areas(self) -> None:
        """从 JSON 文件加载所有多边形和三段码。"""
        logger.info("正在加载片区数据: %s", self.areas_path)

        with open(self.areas_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError(f"areas.json 格式错误：期望 JSON 数组，实际为 {type(data)}")

        skipped = 0
        for item in data:
            wkt_str = item.get(self.wkt_field, "")
            code = item.get(self.segment_code_field, "")

            if not wkt_str or not code:
                skipped += 1
                continue

            try:
                polygon = from_wkt(wkt_str)
                self._polygons.append(polygon)
                self._segment_codes.append(str(code))
            except Exception as e:
                logger.warning("WKT 解析失败 id=%s: %s", item.get("id", "?"), e)
                skipped += 1

        logger.info(
            "加载完成: %d 个多边形, 跳过 %d 条无效记录, %d 个唯一三段码",
            len(self._polygons),
            skipped,
            len(set(self._segment_codes)),
        )

    def _build_index(self) -> None:
        """构建 STRtree 空间索引。"""
        if not self._polygons:
            logger.warning("无有效多边形，跳过索引构建")
            return

        start = time.time()
        self._tree = STRtree(self._polygons)
        elapsed = time.time() - start
        logger.info("STRtree 索引构建完成: 耗时 %.2f 秒", elapsed)

    def match(self, lng: float, lat: float, tolerance_meters: float = 100.0) -> Optional[str]:
        """
        查询点所属的片区三段码。

        等价替代 Turf.js booleanPointInPolygon：
          1. 用 STRtree.buffer 扩大点后粗筛候选多边形
          2. 精确 contains 判断
          3. 未命中时降级为最近邻匹配（容差内取距离最近的多边形）

        Args:
            lng: 经度
            lat: 纬度
            tolerance_meters: 容差距离（米），点距离多边形小于此值则视为命中。
                              默认 100m，用于解决 GPS 精度和边界偏差问题。

        Returns:
            三段码字符串（如 "J21"），未命中返回 None
        """
        if self._tree is None or not self._polygons:
            return None

        point = Point(lng, lat)

        # 将容差（米）近似转为经纬度度数
        tolerance_deg = tolerance_meters / 111000.0

        # Step 1: R-Tree 粗筛（用 buffer 扩大搜索范围）
        buffered = point.buffer(tolerance_deg)
        candidate_indices = self._tree.query(buffered)
        if len(candidate_indices) == 0:
            logger.debug("R-Tree 无候选: (%.6f, %.6f)", lng, lat)
            return None

        # Step 2: 精确 contains 判断
        best_idx = None
        best_dist = float("inf")

        for idx in candidate_indices:
            polygon = self._polygons[idx]
            if polygon.contains(point):
                code = self._segment_codes[idx]
                logger.debug("命中片区: (%.6f, %.6f) → %s", lng, lat, code)
                return code

            # 同时记录最近距离
            dist = point.distance(polygon)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        # Step 3: 容差兜底 — 未精确命中但距离在容差内
        if best_idx is not None and best_dist < tolerance_deg:
            code = self._segment_codes[best_idx]
            dist_m = best_dist * 111000
            logger.info(
                "容差命中: (%.6f, %.6f) 距最近片区 %d 米 → %s",
                lng, lat, int(dist_m), code,
            )
            return code

        logger.debug("未命中任何片区: (%.6f, %.6f), 最近=%.0fm", lng, lat, best_dist * 111000)
        return None

    @property
    def polygon_count(self) -> int:
        """已加载的多边形数量。"""
        return len(self._polygons)
