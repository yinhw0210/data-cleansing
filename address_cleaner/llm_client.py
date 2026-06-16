"""
llm_client.py — LLM 地址清洗客户端

基于 OpenAI 兼容 SDK，支持 DeepSeek、Qwen、GLM、Doubao 等任意模型一键切换。
System Prompt 从外部文件 prompt_template.txt 加载，可随时编辑无需改代码。
"""

import json
import logging
from pathlib import Path
from typing import Optional

from openai import OpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from address_cleaner.config import settings

logger = logging.getLogger(__name__)


class LLMClient:
    """
    LLM 客户端工厂模式。

    使用 OpenAI 兼容 SDK 调用任意大模型，支持 JSON Mode 结构输出。

    Usage:
        client = LLMClient(settings)
        candidates = client.clean_address("原始地址字符串")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        prompt_template_path: Optional[Path] = None,
    ) -> None:
        """
        初始化 LLM 客户端。

        Args:
            base_url: API Base URL（默认从 settings 读取）
            api_key: API Key
            model: 模型名称
            temperature: 温度参数
            prompt_template_path: System Prompt 模板文件路径（默认从 settings 读取）
        """
        self.base_url = base_url or settings.llm_base_url
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model_name
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.prompt_template_path = prompt_template_path or settings.prompt_template_path

        # 加载 System Prompt
        self.system_prompt = self._load_prompt()

        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )

        logger.info("LLM 客户端初始化完成: model=%s, base_url=%s", self.model, self.base_url)

    def _load_prompt(self) -> str:
        """从外部文件加载 System Prompt 模板。"""
        path = Path(self.prompt_template_path)
        if not path.exists():
            logger.warning("Prompt 模板文件不存在: %s，使用内置默认 Prompt", path)
            return self._default_prompt()
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                logger.warning("Prompt 模板文件为空: %s，使用内置默认 Prompt", path)
                return self._default_prompt()
            logger.info("已加载 Prompt 模板: %s (%d 字符)", path, len(content))
            return content
        except Exception as e:
            logger.error("读取 Prompt 模板失败: %s — %s，使用内置默认 Prompt", path, e)
            return self._default_prompt()

    @staticmethod
    def _default_prompt() -> str:
        """内置最小兜底 Prompt（外部文件丢失时的最后防线）。"""
        return """你是一个物流地址清洗专家。从包含噪音的地址文本中提取 3 个标准地址（精确度从高到低）。
去除商品名称、无关标点、复杂找路描述。保留省市区街道、路名、交叉口、标志性建筑、门牌号。
输出格式：严格输出 JSON 对象 {"candidates": ["地址1", "地址2", "地址3"]}，无其他文字。"""

    @retry(
        stop=stop_after_attempt(settings.llm_max_retries),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception_type((Exception,)),
        reraise=True,
    )
    def _call_llm(self, user_message: str) -> str:
        """
        调用 LLM，带指数退避重试。

        Args:
            user_message: 用户输入的原始地址

        Returns:
            LLM 响应的文本内容
        """
        logger.debug("正在调用 LLM: model=%s", self.model)
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        logger.debug("LLM 原始响应: %s", content[:200])
        return content

    def clean_address(self, raw_address: str) -> list[str]:
        """
        清洗原始地址，提取 3 个候选标准地址。

        Args:
            raw_address: 用户的原始地址（含噪音）

        Returns:
            3 个候选标准地址列表，精确度从高到低
        """
        try:
            content = self._call_llm(raw_address)
            parsed = json.loads(content)

            # 兼容不同返回格式：可能是数组或包含数组的 dict
            if isinstance(parsed, list):
                candidates = parsed
            elif isinstance(parsed, dict):
                # 尝试从常见 key 提取
                candidates = (
                    parsed.get("addresses")
                    or parsed.get("candidates")
                    or parsed.get("results")
                    or list(parsed.values())[0]
                )
                if not isinstance(candidates, list):
                    candidates = []
            else:
                candidates = []

            # 确保返回 3 个元素
            while len(candidates) < 3:
                candidates.append(candidates[-1] if candidates else raw_address)

            logger.info("地址清洗完成: %s → %d 个候选", raw_address[:30], len(candidates[:3]))
            return candidates[:3]

        except json.JSONDecodeError as e:
            logger.warning("LLM 返回非 JSON 内容，尝试降级解析: %s", e)
            # 尝试从文本中提取 JSON 数组
            return self._fallback_parse(content)
        except Exception as e:
            logger.error("LLM 调用失败: %s", e)
            # 兜底：返回原始地址 3 次
            return [raw_address, raw_address, raw_address]

    def _fallback_parse(self, text: str) -> list[str]:
        """降级解析：从非 JSON 文本中尽力提取地址数组。"""
        import re

        # 尝试匹配 JSON 数组
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            try:
                arr = json.loads(match.group())
                if isinstance(arr, list) and len(arr) > 0:
                    while len(arr) < 3:
                        arr.append(arr[-1])
                    return arr[:3]
            except json.JSONDecodeError:
                pass

        # 最终兜底
        logger.warning("无法解析 LLM 响应，使用原始地址兜底")
        return [text, text, text]
