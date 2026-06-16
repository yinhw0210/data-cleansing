"""
main.py — 物流地址智能解析与补码 CLI 工具

使用 Typer 框架，提供开箱即用的命令行体验。

Usage:
    python main.py                    # 全量处理
    python main.py --sample 3         # 试跑前 3 条
    python main.py -i data.xlsx -o result.xlsx -w 16  # 自定义参数
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from address_cleaner.config import settings
from address_cleaner.pipeline import Pipeline

app = typer.Typer(
    name="address-pipeline",
    help="物流地址智能解析与补码 — LLM 清洗 + 高德 AutoComplete + 围栏命中",
    add_completion=False,
)


# ── 日志配置 ──
def setup_logging(verbose: bool = False) -> Path:
    """
    配置双通道日志：
      - 控制台：INFO 级别（verbose 时 DEBUG）
      - 文件：DEBUG 级别，每次运行生成独立文件 logs/pipeline_YYYY-MM-DD_HHMMSS.log
    """
    # 确保日志目录存在
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    # 本次运行的日志文件（按时间戳命名）
    run_ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = settings.logs_dir / f"pipeline_{run_ts}.log"

    # 日志格式
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-18s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 文件 handler — DEBUG 级别，记录所有细节
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_fmt)

    # 控制台 handler — 可调级别
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(console_fmt)

    # 根 logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return log_file


# ── CLI 命令 ──
@app.command()
def main(
    input_file: Optional[Path] = typer.Option(
        None, "--input", "-i",
        help="输入 Excel 路径（默认: ./input.xlsx）",
        exists=True,
        dir_okay=False,
    ),
    output_file: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="输出 Excel 路径（默认: ./output.xlsx）",
        dir_okay=False,
    ),
    workers: int = typer.Option(
        settings.max_workers, "--workers", "-w",
        help="并发线程数",
        min=1,
        max=64,
    ),
    sample: Optional[int] = typer.Option(
        None, "--sample", "-s",
        help="只处理前 N 行（用于试跑验证）",
        min=1,
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="输出 DEBUG 级别日志",
    ),
) -> None:
    """
    执行物流地址智能解析与补码流水线。

    流程: LLM 清洗 → 高德 AutoComplete (+ Geocoder 兜底) → 围栏命中 → 三段码回填
    """
    # ── 日志 ──
    log_file = setup_logging(verbose=verbose)
    logger = logging.getLogger("main")

    # ── 参数解析 ──
    input_path = input_file or settings.input_path
    output_path = output_file or settings.output_path

    # ── 启动横幅 ──
    logger.info("=" * 60)
    logger.info("  物流地址智能解析与补码")
    logger.info("  输入:     %s", input_path)
    logger.info("  输出:     %s", output_path)
    logger.info("  线程数:   %d", workers)
    logger.info("  日志文件: %s", log_file)
    logger.info("  模型:     %s @ %s", settings.llm_model_name, settings.llm_base_url)
    if sample:
        logger.info("  模式:     试跑 (%d 行)", sample)
    logger.info("=" * 60)

    # ── 执行 ──
    pipeline = Pipeline(max_workers=workers)
    df = pipeline.run(
        input_path=input_path,
        output_path=output_path,
        max_rows=sample,
    )

    # ── 结果摘要 ──
    # 统计成功数（有命中三段码的视为成功）
    success = (df["命中三段码"] != "").sum() if "命中三段码" in df.columns else 0
    total = len(df)
    logger.info("")
    logger.info("🎉 完成！ 成功 %d/%d (%.1f%%)", success, total, 100 * success / total if total else 0)
    logger.info("   结果文件: %s", output_path.resolve())
    logger.info("   日志文件: %s", log_file.resolve())


# ── 入口 ──
if __name__ == "__main__":
    app()
