"""
pipeline.py — 数据清洗流水线

编排 LLM 清洗 → 高德两步解析 → 空间匹配的完整流程。
使用 ThreadPoolExecutor 实现多线程并发处理。
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

from address_cleaner.config import settings
from address_cleaner.llm_client import LLMClient
from address_cleaner.amap_client import AmapClient
from address_cleaner.geo_matcher import GeoMatcher

logger = logging.getLogger(__name__)


class Pipeline:
    """
    物流地址智能解析与补码流水线。

    组合 LLM 清洗、高德解析、空间匹配三个模块，
    对 Excel 运单数据逐行处理，输出补码结果。

    Usage:
        pipeline = Pipeline()
        pipeline.run(input_path="./input.xlsx", output_path="./output.xlsx")
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        amap: Optional[AmapClient] = None,
        matcher: Optional[GeoMatcher] = None,
        max_workers: Optional[int] = None,
    ) -> None:
        """
        初始化流水线。

        Args:
            llm: LLM 客户端实例
            amap: 高德客户端实例
            matcher: 空间匹配器实例
            max_workers: 并发线程数（默认从 settings 读取）
        """
        self.llm = llm or LLMClient()
        self.amap = amap or AmapClient()
        self.matcher = matcher or GeoMatcher()
        self.max_workers = max_workers or settings.max_workers

        logger.info("流水线初始化完成: workers=%d, areas=%d", self.max_workers, self.matcher.polygon_count)

    def process_row(self, row: pd.Series) -> dict:
        """
        处理单行运单数据。

        执行流程：
          1. 拼接「省市区街道」+「详细地址」作为 LLM 输入
          2. LLM 清洗 → 3 个候选标准地址
          3. AmapClient 按优先级解析经纬度
          4. GeoMatcher 查询坐标所属片区 → 三段码

        Args:
            row: DataFrame 的一行（Series）

        Returns:
            {
                "candidates_json": str,     # 3 个候选地址的 JSON 字符串
                "lng": float | None,        # 经度
                "lat": float | None,        # 纬度
                "san_duan_ma": str | None,  # 三段码
                "status": str,              # "成功" / "失败: 原因"
            }
        """
        # Step 0: 提取字段
        province_city_street = str(row.get("省市区街道", "") or "")
        detail_address = str(row.get("详细地址", "") or "")
        waybill_no = str(row.get("运单号", ""))

        # 拼接完整地址
        full_address = f"{province_city_street}{detail_address}".strip()
        if not full_address:
            return {
                "waybill_no": waybill_no,
                "original_address": "",
                "candidate_1": "",
                "candidate_2": "",
                "candidate_3": "",
                "location": "",
                "san_duan_ma": "",
                "status": "失败: 地址为空",
            }

        # Step 1: LLM 清洗
        try:
            candidates = self.llm.clean_address(full_address)
        except Exception as e:
            logger.error("LLM 清洗失败 [%s]: %s", full_address[:30], e)
            return {
                "waybill_no": waybill_no,
                "original_address": full_address,
                "candidate_1": "",
                "candidate_2": "",
                "candidate_3": "",
                "location": "",
                "san_duan_ma": "",
                "status": f"失败: LLM异常 - {str(e)[:50]}",
            }

        # Step 2: 高德坐标解析
        coord = self.amap.resolve(candidates)
        if coord is None:
            return {
                "waybill_no": waybill_no,
                "original_address": full_address,
                "candidate_1": candidates[0] if len(candidates) > 0 else "",
                "candidate_2": candidates[1] if len(candidates) > 1 else "",
                "candidate_3": candidates[2] if len(candidates) > 2 else "",
                "location": "",
                "san_duan_ma": "",
                "status": "失败: 高德未命中坐标",
            }

        lng, lat = coord
        loc_str = f"{lng},{lat}"

        # Step 3: 空间匹配
        code = self.matcher.match(lng, lat)
        if code is None:
            return {
                "waybill_no": waybill_no,
                "original_address": full_address,
                "candidate_1": candidates[0] if len(candidates) > 0 else "",
                "candidate_2": candidates[1] if len(candidates) > 1 else "",
                "candidate_3": candidates[2] if len(candidates) > 2 else "",
                "location": loc_str,
                "san_duan_ma": "",
                "status": "失败: 未命中片区",
            }

        return {
            "waybill_no": waybill_no,
            "original_address": full_address,
            "candidate_1": candidates[0] if len(candidates) > 0 else "",
            "candidate_2": candidates[1] if len(candidates) > 1 else "",
            "candidate_3": candidates[2] if len(candidates) > 2 else "",
            "location": loc_str,
            "san_duan_ma": code,
            "status": "成功",
        }

    def run(
        self,
        input_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        执行完整流水线。

        Args:
            input_path: 输入 Excel 路径
            output_path: 输出 Excel 路径
            max_rows: 最多处理行数（None=全部, 用于试跑验证）

        Returns:
            处理后的 DataFrame
        """
        input_path = input_path or settings.input_path
        output_path = output_path or settings.output_path

        # ── 读取数据 ──
        logger.info("正在读取输入文件: %s", input_path)
        df = pd.read_excel(input_path)
        total = len(df)
        logger.info("读取完成: %d 行, %d 列", total, len(df.columns))

        if max_rows:
            df = df.head(max_rows)
            logger.info("限制处理行数: %d", max_rows)

        # ── 处理 ──
        # 只处理「可处理」的行，不可处理的行跳过
        processable = df["是否可处理"] == "可处理"
        to_process = df[processable]
        skip_count = total - len(to_process)

        logger.info(
            "待处理: %d 行, 跳过(不可处理): %d 行, 并发线程: %d",
            len(to_process),
            skip_count,
            self.max_workers,
        )

        start_time = time.time()
        results = []

        if len(to_process) > 0:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self.process_row, row): idx
                    for idx, row in to_process.iterrows()
                }

                completed = 0
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        logger.error("行 %s 处理异常: %s", idx, e)
                        result = {
                            "waybill_no": "",
                            "original_address": "",
                            "candidate_1": "",
                            "candidate_2": "",
                            "candidate_3": "",
                            "location": "",
                            "san_duan_ma": "",
                            "status": f"失败: 处理异常 - {str(e)[:50]}",
                        }
                    results.append((idx, result))
                    completed += 1
                    if completed % 10 == 0 or completed == len(to_process):
                        logger.info("进度: %d/%d", completed, len(to_process))

        # ── 合并结果 ──
        # 初始化结果列
        df["原始地址"] = df.apply(
            lambda r: str(r.get("省市区街道", "") or "") + str(r.get("详细地址", "") or ""), axis=1
        )
        df["清洗地址1"] = ""
        df["清洗地址2"] = ""
        df["清洗地址3"] = ""
        df["命中经纬度"] = ""
        df["命中三段码"] = ""
        df["_status"] = "跳过: 不可处理"

        for idx, result in results:
            df.at[idx, "清洗地址1"] = result["candidate_1"]
            df.at[idx, "清洗地址2"] = result["candidate_2"]
            df.at[idx, "清洗地址3"] = result["candidate_3"]
            df.at[idx, "命中经纬度"] = result["location"]
            df.at[idx, "命中三段码"] = result["san_duan_ma"]
            df.at[idx, "_status"] = result["status"]

        # ── 精简输出列 ──
        output_columns = ["运单号", "原始地址", "清洗地址1", "清洗地址2", "清洗地址3", "命中经纬度", "命中三段码"]
        out_df = df[output_columns].copy()

        # ── 统计 ──
        elapsed = time.time() - start_time
        # 从 results 统计（而不是从 DataFrame）
        statuses = [r["status"] for _, r in results]
        success_count = statuses.count("成功")
        fail_count = len(statuses) - success_count

        logger.info("=" * 60)
        logger.info("流水线执行完成")
        logger.info("  总行数: %d", total)
        logger.info("  成功: %d / 失败: %d", success_count, fail_count)
        logger.info("  成功率: %.1f%%", 100 * success_count / len(results) if results else 0)
        logger.info("  总耗时: %.2f 秒", elapsed)
        logger.info("  平均耗时: %.2f 秒/行", elapsed / len(results) if results else 0)
        logger.info("=" * 60)

        # ── 写出 ──
        logger.info("正在写出结果: %s", output_path)
        out_df.to_excel(output_path, index=False)
        logger.info("写出完成: %d 行 × %d 列", len(out_df), len(out_df.columns))

        return out_df
