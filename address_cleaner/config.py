"""
config.py — 项目配置中心
=======================

所有配置项通过 **.env 文件** 或 **环境变量** 注入，提供开箱即用的默认值。
用户无需修改代码，只需编辑项目根目录下的 `.env` 文件即可切换模型、API Key、路径等。

配置优先级：.env 文件 > 环境变量 > 代码默认值（.env 始终优先，避免 shell 残留变量干扰）

目录结构约定：
    data/input/   ← 输入文件（运单 Excel、片区围栏 JSON）
    data/output/  ← 输出文件（补码结果 Excel）
    logs/         ← 运行日志（每次运行一个独立文件）
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 项目根目录：自动推导为 address_cleaner/ 的上一级
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加载 .env 文件（override=True：.env 中的值覆盖已有的环境变量，保证 .env 为唯一配置来源）
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _project_path(env_key: str, default: str) -> Path:
    """
    将配置中的相对路径转为项目根目录下的绝对路径。

    若用户在 .env 中写了绝对路径（如 /data/batch1.xlsx），则直接使用；
    若写的是相对路径（如 data/input/input.xlsx），则拼到 PROJECT_ROOT 下。
    """
    raw = os.getenv(env_key, default)
    p = Path(raw)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


@dataclass
class Settings:
    """
    全局配置单例。

    所有字段均可在 .env 中覆盖，字段名即环境变量名（大写）。
    例如：LLM_BASE_URL → .env 中写 LLM_BASE_URL=https://...

    使用方式：
        from address_cleaner.config import settings
        print(settings.llm_model_name)
    """

    # ═══════════════════════════════════════════
    # 项目根目录
    # ═══════════════════════════════════════════
    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)
    """项目根目录的绝对路径，所有相对路径均以此为基准。"""

    # ═══════════════════════════════════════════
    # LLM 大模型配置（OpenAI 兼容协议）
    # ═══════════════════════════════════════════
    llm_base_url: str = field(
        default_factory=lambda: os.getenv(
            "LLM_BASE_URL",
            # 默认使用 DeepSeek。注意：OpenAI SDK 会自动追加 /v1，此处不要加 /v1 后缀
            "https://api.deepseek.com",
        )
    )
    """LLM API 地址，兼容 OpenAI 协议的任意厂商。
    
    常用 Base URL（直接复制到 .env）：
        DeepSeek:        https://api.deepseek.com
        阿里百炼 Qwen:   https://dashscope.aliyuncs.com/compatible-mode/v1
        智谱 GLM:        https://open.bigmodel.cn/api/paas/v4
        字节豆包:        https://ark.cn-beijing.volces.com/api/v3
        OpenAI:          https://api.openai.com/v1
    """

    llm_api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "sk-placeholder")
    )
    """LLM API Key。在对应厂商控制台创建，形如 sk-xxxx 或 $DEEPSEEK_API_KEY。"""

    llm_model_name: str = field(
        default_factory=lambda: os.getenv(
            "LLM_MODEL_NAME",
            # deepseek-v4-flash: 快速、便宜（1元/百万token入，2元/百万token出）
            # deepseek-v4-pro:  更强推理（3元/百万token入，6元/百万token出）
            # 旧名 deepseek-chat / deepseek-reasoner 将于 2026-07-24 弃用
            "deepseek-v4-flash",
        )
    )
    """模型名称，需与 LLM_BASE_URL 对应厂商匹配。"""

    llm_temperature: float = 0.0
    """LLM 温度参数。
    
    0.0 = 最稳定输出（推荐，适合数据清洗场景）
    0.3~0.7 = 更有创造性（适合文案生成）
    """

    llm_max_retries: int = 3
    """LLM 调用失败时的最大重试次数。配合 tenacity 指数退避（2s → 4s → 8s）。"""

    # ═══════════════════════════════════════════
    # 高德地图 API 配置
    # ═══════════════════════════════════════════
    amap_api_key: str = field(
        default_factory=lambda: os.getenv("AMAP_API_KEY", "1a1a3665ce0986b49aa6be79cc42711d")
    )
    """高德地图 Web 服务 API Key。
    
    注意：必须是「Web 服务」类型，不能是 JS API 类型。
    在 https://console.amap.com/dev/key/app 创建。
    JS API Key 调用 REST API 会返回 USERKEY_PLAT_NOMATCH 错误。
    """

    amap_city: str = field(
        default_factory=lambda: os.getenv("AMAP_CITY", "0539")
    )
    """高德 API 城市限定码。
    
    0539 = 山东省临沂市（区号）
    全国 = 空字符串或省略此参数
    AutoComplete 接口会配合 citylimit=true 严格限定在此城市内搜索。
    """

    amap_max_retries: int = 3
    """高德 API 调用失败时的最大重试次数。"""

    # ═══════════════════════════════════════════
    # 文件路径（输入 / 输出 / 日志）
    # ═══════════════════════════════════════════
    input_path: Path = field(
        default_factory=lambda: _project_path(
            "INPUT_PATH",
            # 默认读取 data/input/ 下的运单 Excel
            "data/input/input.xlsx",
        )
    )
    """待处理的运单 Excel 文件路径。需包含「省市区街道」和「详细地址」列。"""

    output_path: Path = field(
        default_factory=lambda: _project_path(
            "OUTPUT_PATH",
            # 默认输出到 data/output/
            "data/output/output.xlsx",
        )
    )
    """补码结果 Excel 输出路径。保留原始所有列，追加 candidates_json、lng、lat、补码状态。"""

    areas_path: Path = field(
        default_factory=lambda: _project_path(
            "AREAS_PATH",
            # 默认读取 data/input/ 下的片区围栏 JSON
            "data/input/areas.json",
        )
    )
    """网点片区围栏数据（WKT 多边形数组）。需包含 wkt 和 san_duan_ma 字段。"""

    logs_dir: Path = field(
        default_factory=lambda: _project_path("LOGS_DIR", "logs")
    )
    """运行日志输出目录。每次运行生成独立文件 pipeline_YYYY-MM-DD_HHMMSS.log。"""

    prompt_template_path: Path = field(
        default_factory=lambda: _project_path(
            "PROMPT_TEMPLATE_PATH",
            "prompt_template.txt",
        )
    )
    """LLM System Prompt 模板文件。可随时编辑，下次运行自动生效，无需改代码。"""

    # ═══════════════════════════════════════════
    # areas.json 字段映射
    # ═══════════════════════════════════════════
    wkt_field: str = field(
        default_factory=lambda: os.getenv("WKT_FIELD", "wkt")
    )
    """areas.json 中 WKT 多边形几何字段的键名。"""

    segment_code_field: str = field(
        default_factory=lambda: os.getenv("SEGMENT_CODE_FIELD", "san_duan_ma")
    )
    """areas.json 中三段码字段的键名。命中片区后取此字段值回填到 Excel。"""

    # ═══════════════════════════════════════════
    # 并发控制
    # ═══════════════════════════════════════════
    max_workers: int = field(
        default_factory=lambda: int(os.getenv("MAX_WORKERS", "8"))
    )
    """ThreadPoolExecutor 并发线程数。
    
    建议值：
        8  — 通用默认，适合大多数网络环境
        16 — 网络延迟较高时可增加
        1  — 调试时使用，避免并发日志混乱
    注意：过高可能触发 LLM/高德 API 的限流。
    """


# ──────────────────────────────────────────────
# 全局单例 — 整个项目共用一个 Settings 实例
# ──────────────────────────────────────────────
settings = Settings()
