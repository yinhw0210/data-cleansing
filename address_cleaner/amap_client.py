"""
amap_client.py — 高德地图两步客户端

工作流：
  1. AutoComplete（输入提示 /v3/assistant/inputtips）：关键词 → POI 候选
  2. 若 AutoComplete 返回了坐标 → 直接使用
  3. 若自动补全未返回坐标 → Geocoder 兜底（/v3/geocode/geo）

城市限定：city=0539（临沂），AutoComplete 加 citylimit=true
"""

import logging
from typing import Optional

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from address_cleaner.config import settings

logger = logging.getLogger(__name__)

# 高德 API 端点
INPUTTIPS_URL = "https://restapi.amap.com/v3/assistant/inputtips"
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


class AmapClient:
    """
    高德地图两步客户端。

    封装 AutoComplete（输入提示）和 Geocoder（地理编码），
    对 LLM 清洗后的 3 个候选地址按优先级解析坐标。

    Usage:
        client = AmapClient(api_key=settings.amap_api_key, city=settings.amap_city)
        lng, lat = client.resolve(["候选地址1", "候选地址2", "候选地址3"])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        city: Optional[str] = None,
    ) -> None:
        """
        初始化高德客户端。

        Args:
            api_key: 高德 API Key
            city: 城市限定码（临沂为 0539）
        """
        self.api_key = api_key or settings.amap_api_key
        self.city = city or settings.amap_city
        logger.info("高德客户端初始化: city=%s", self.city)

    @retry(
        stop=stop_after_attempt(settings.amap_max_retries),
        wait=wait_exponential(multiplier=2, min=1, max=4),
        retry=retry_if_exception_type((requests.RequestException,)),
        reraise=True,
    )
    def _get(self, url: str, params: dict) -> dict:
        """带重试的 HTTP GET 请求。"""
        params["key"] = self.api_key
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "1":
            raise requests.RequestException(
                f"高德 API 返回错误: status={data.get('status')}, info={data.get('info')}"
            )
        return data

    def auto_complete(self, keyword: str) -> Optional[dict]:
        """
        调用高德输入提示 API，返回最佳匹配 POI。

        Args:
            keyword: 搜索关键词（候选地址）

        Returns:
            {'name': str, 'location': 'lng,lat' | None} 或 None
        """
        try:
            data = self._get(
                INPUTTIPS_URL,
                {
                    "keywords": keyword,
                    "city": self.city,
                    "citylimit": "true",
                    "datatype": "all",
                },
            )
            tips = data.get("tips", [])
            if not tips:
                logger.debug("AutoComplete 无结果: '%s'", keyword[:40])
                return None

            best = tips[0]
            result = {
                "name": best.get("name", ""),
                "location": best.get("location"),  # "lng,lat" 或 None
                "address": best.get("address", ""),
            }
            logger.debug(
                "AutoComplete 命中: '%s' → '%s' (坐标: %s)",
                keyword[:40],
                result["name"],
                result["location"],
            )
            return result
        except Exception as e:
            logger.warning("AutoComplete 调用失败: '%s' — %s", keyword[:40], e)
            return None

    def geocode(self, address: str) -> Optional[tuple[float, float]]:
        """
        调用高德地理编码 API，将地址转换为经纬度。

        Args:
            address: 标准地址字符串

        Returns:
            (lng, lat) 或 None
        """
        try:
            data = self._get(
                GEOCODE_URL,
                {
                    "address": address,
                    "city": self.city,
                },
            )
            geocodes = data.get("geocodes", [])
            if not geocodes:
                logger.debug("Geocoder 无结果: '%s'", address[:40])
                return None

            location = geocodes[0].get("location", "")
            if not location:
                return None

            lng_str, lat_str = location.split(",")
            lng, lat = float(lng_str), float(lat_str)
            logger.debug("Geocoder 命中: '%s' → (%.6f, %.6f)", address[:40], lng, lat)
            return (lng, lat)
        except Exception as e:
            logger.warning("Geocoder 调用失败: '%s' — %s", address[:40], e)
            return None

    def _resolve_one(self, candidate: str) -> Optional[tuple[float, float]]:
        """
        为单个候选地址解析坐标：AutoComplete → Geocoder fallback。

        Args:
            candidate: 单个候选地址

        Returns:
            (lng, lat) 或 None
        """
        # Step 1: AutoComplete
        tip = self.auto_complete(candidate)
        if tip is None:
            return None

        # Step 2: 检查是否有坐标
        if tip.get("location"):
            lng_str, lat_str = tip["location"].split(",")
            return (float(lng_str), float(lat_str))

        # Step 3: 无坐标 → Geocoder 兜底
        name = tip.get("name") or candidate
        return self.geocode(name)

    def resolve(self, candidates: list[str]) -> Optional[tuple[float, float]]:
        """
        按优先级解析坐标，首个成功即返回。

        Args:
            candidates: 3 个候选标准地址（精确度从高到低）

        Returns:
            (lng, lat) 或 None
        """
        for i, candidate in enumerate(candidates):
            logger.info("尝试候选 %d/3: '%s'", i + 1, candidate[:50])
            coord = self._resolve_one(candidate)
            if coord is not None:
                logger.info("候选 %d 命中: (%.6f, %.6f)", i + 1, *coord)
                return coord

        logger.warning("全部 3 个候选地址均未命中坐标")
        return None

    def resolve_all(self, candidates: list[str]) -> list[Optional[tuple[float, float]]]:
        """
        并行解析所有候选地址的坐标，不短路。

        对 3 个候选分别调用 AutoComplete → Geocoder fallback，
        返回与输入等长的列表，未命中的位置为 None。

        Args:
            candidates: 3 个候选标准地址

        Returns:
            坐标列表，如 [(118.2, 35.0), None, (118.3, 35.1)]
        """
        results: list[Optional[tuple[float, float]]] = []
        for i, candidate in enumerate(candidates):
            logger.info("并行候选 %d/3: '%s'", i + 1, candidate[:50])
            coord = self._resolve_one(candidate)
            if coord is not None:
                logger.info("候选 %d 命中: (%.6f, %.6f)", i + 1, *coord)
            else:
                logger.info("候选 %d 未命中", i + 1)
            results.append(coord)
        return results
