import asyncio
import json
import logging
import os
import re
from typing import List

from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.http_client import get_http_client

logger = logging.getLogger(__name__)

settings = get_settings()


class PaddleOcr:
    def __init__(self):
        self.job_url = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
        self.access_token = settings.PADDLE_OCR_ACCESS_TOKEN
        self.model_name = "PaddleOCR-VL-1.6"
        self.headers = {
            "Authorization": f"bearer {self.access_token}",
        }
        self.optional_payload = {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }

    async def create_job(self, file: str) -> str:
        """
        创建解析任务：上传要解析的文件地址 url或者本地文件位置
        :param file: 文件地址
        :return: 解析任务id
        """
        # 复用全局 client（见 app/core/http_client.py），避免每次请求重建连接池
        client = get_http_client()

        if file.startswith("http"):
            self.headers["Content-Type"] = "application/json"
            payload = {
                "fileUrl": file,
                "model": self.model_name,
                "optionalPayload": self.optional_payload
            }
            job_resp = await client.post(self.job_url, json=payload, headers=self.headers)
        else:
            if not os.path.exists(file):
                raise ValueError(f"错误：{file}不存在！")
            data = {
                "model": self.model_name,
                "optionalPayload": json.dumps(self.optional_payload)
            }

            with open(file, "rb") as fp:
                files = {"file": fp}
                job_resp = await client.post(
                    self.job_url, headers=self.headers, data=data, files=files
                )

        if job_resp.status_code != 200:
            logger.error("文件上传失败：%s, file: %s", job_resp.text, file)
            raise ValueError(f"文件上传失败：{job_resp.text}")

        job_id = job_resp.json()["data"]["jobId"]
        return job_id

    async def poll_for_state(self, job_id: str) -> str | None:
        """
        轮询任务状态
        :param job_id: 解析任务id
        :return: 返回解析文件内容地址
        """
        client = get_http_client()
        url = f"{self.job_url}/{job_id}"

        while True:
            job_result_response = await client.get(url, headers=self.headers)
            if job_result_response.status_code != 200:
                raise ValueError(f"获取任务：{job_id}状态错误！")

            data = job_result_response.json()["data"]
            state = data["state"]

            if state == 'pending':
                logger.info("%speding...", job_id)
            elif state == 'running':
                try:
                    total_pages = data['extractProgress']['totalPages']
                    extracted_pages = data['extractProgress']['extractedPages']
                    logger.info("任务：%s运行中，%s/%s", job_id, extracted_pages, total_pages)
                except KeyError:
                    logger.info("The current status of the job is running...")
            elif state == 'done':
                progress = data['extractProgress']
                logger.info(
                    "任务%s执行完成，总共提取%s，开始时间：%s，结束时间：%s",
                    job_id, progress['extractedPages'], progress['startTime'], progress['endTime'],
                )
                return data['resultUrl']['jsonUrl']
            elif state == "failed":
                error_msg = data['errorMsg']
                logger.error("任务：%s失败，错误信息：%s", job_id, error_msg)
                raise ValueError(error_msg)

            await asyncio.sleep(2)

    async def fetch_parsed_contents(self, jsonl_url: str) -> List[str]:
        logger.debug("jsonl_url:%s", jsonl_url)
        contents: List[str] = []

        jsonl_response = await get_http_client().get(jsonl_url)
        if jsonl_response.status_code != 200:
            raise ValueError(f"获取内容失败：{jsonl_response.text}")

        text = jsonl_response.text
        lines = text.strip().split('\n')

        header = self._extract_block(text, "header")
        footer = self._extract_block(text, "footer")
        logger.debug("doc_title:%s", header)
        logger.debug("footer:%s", footer)

        contents.extend(header)

        for line in lines:
            line = line.strip()
            if not line:
                continue
            result = json.loads(line)["result"]
            for res in result["layoutParsingResults"]:
                contents.append(res["markdown"]["text"])

        contents.extend(footer)

        return contents

    @staticmethod
    def _extract_block(text: str, label: str) -> List[str]:
        """从 OCR JSONL 文本中提取指定 block_label 的 block_content"""
        pattern = rf'"block_label"\s*:\s*"{re.escape(label)}"[^}}]*"block_content"\s*:\s*"([^"]*)"'
        return re.findall(pattern, text)
