import json
import os
import re
from typing import List

from app.core.config import get_settings

import asyncio
import logging

import httpx
from langchain_openai import ChatOpenAI

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
        if file.startswith("http"):
            self.headers["Content-Type"] = "application/json"
            payload = {
                "fileUrl": file,
                "model": self.model_name,
                "optionalPayload": self.optional_payload
            }
            async with httpx.AsyncClient() as client:
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
                async with httpx.AsyncClient() as client:
                    job_resp = await client.post(self.job_url, headers=self.headers, data=data, files=files)

        if job_resp.status_code != 200:
            logger.error(f"文件上传失败：{job_resp.text}, file: {file}")
            raise ValueError(f"文件上传失败：{job_resp.text}")

        job_id = job_resp.json()["data"]["jobId"]
        return job_id

    async def poll_for_state(self, job_id: str) -> str | None:
        """
        轮询任务状态
        :param job_id: 解析任务id
        :return: 返回解析文件内容地址
        """
        while True:
            async with httpx.AsyncClient() as client:
                url = f"{self.job_url}/{job_id}"
                job_result_response = await client.get(url, headers=self.headers)
                if job_result_response.status_code != 200:
                    raise ValueError(f"获取任务：{job_id}状态错误！")
                state = job_result_response.json()["data"]["state"]
                if state == 'pending':
                    logger.info(f"{job_id}peding...")
                elif state == 'running':
                    try:
                        total_pages = job_result_response.json()['data']['extractProgress']['totalPages']
                        extracted_pages = job_result_response.json()['data']['extractProgress']['extractedPages']
                        logger.info(f"任务：{job_id}运行中，{extracted_pages}/{total_pages}")
                    except KeyError:
                        logger.info("The current status of the job is running...")
                elif state == 'done':
                    extracted_pages = job_result_response.json()['data']['extractProgress']['extractedPages']
                    start_time = job_result_response.json()['data']['extractProgress']['startTime']
                    end_time = job_result_response.json()['data']['extractProgress']['endTime']
                    logger.info(f"任务{job_id}执行完成，总共提取{extracted_pages}，开始时间：{start_time}，结束时间：{end_time}")
                    jsonl_url = job_result_response.json()['data']['resultUrl']['jsonUrl']
                    return jsonl_url
                elif state == "failed":
                    error_msg = job_result_response.json()['data']['errorMsg']
                    logger.error(f"任务：{job_id}失败，错误信息：{error_msg}")
                    raise ValueError(error_msg)
            await asyncio.sleep(2)

    async def fetch_parsed_contents(self, jsonl_url: str) -> List[str]:
        logger.debug(f"jsonl_url:{jsonl_url}")
        contents = []
        async with httpx.AsyncClient() as client:
            jsonl_response = await client.get(jsonl_url)
            if jsonl_response.status_code != 200:
                raise ValueError(f"获取内容失败：{jsonl_response.text}")
            lines = jsonl_response.text.strip().split('\n')

            header = self._extract_block(jsonl_response.text, "header")
            footer = self._extract_block(jsonl_response.text, "footer")
            logger.debug(f"doc_title:{header}")
            logger.debug(f"footer:{footer}")

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
    def _extract_block(text: str, label: str) ->  List[str]:
        """从 OCR JSONL 文本中提取指定 block_label 的 block_content"""
        pattern = rf'"block_label"\s*:\s*"{re.escape(label)}"[^}}]*"block_content"\s*:\s*"([^"]*)"'
        return re.findall(pattern, text)