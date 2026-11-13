"""演示数据种子脚本：灌入职位 + 候选人 + AI 评分，让页面一打开就是满的。

覆盖状态：已投递 / AI筛选成功 / 协商超时 / 待面试 / 已入职 / 处理异常

使用方式::

    python -m scripts.demo_seed

幂等：按 email 判重，已存在的候选人会跳过。
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.db import AsyncSessionFactory
from app.models.candidate import (
    CandidateModel,
    CandidateStatusEnum,
    CandidateAIScoreModel,
    ResumeModel,
    GenderEnum,
)
from app.models.position import PositionModel, EducationEnum
from app.repository.user_repo import UserRepo, DepartmentRepo
from app.repository.resume_repo import CandidateRepo, ResumeRepo, CandidateAIScoreRepo
from app.core.config import get_settings

settings = get_settings()

# Fix for Windows psycopg issue with ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ==================== 演示数据定义 ====================

POSITIONS = [
    {
        "title": "高级后端工程师",
        "description": "负责核心交易系统微服务化改造，设计高并发架构方案。",
        "requirements": "5年以上后端开发经验，精通 Python/Java，熟悉微服务、Redis、Kafka。",
        "min_salary": 25000,
        "max_salary": 40000,
        "recruitment_count": 3,
        "education": EducationEnum.BACHELOR,
        "work_year": 5,
        "department": "技术部",
    },
    {
        "title": "前端开发工程师",
        "description": "负责公司前端架构设计与开发，提升用户体验。",
        "requirements": "3年以上前端开发经验，精通 Vue3/React，熟悉 TypeScript。",
        "min_salary": 15000,
        "max_salary": 25000,
        "recruitment_count": 2,
        "education": EducationEnum.BACHELOR,
        "work_year": 3,
        "department": "技术部",
    },
    {
        "title": "产品经理",
        "description": "负责招聘产品线的需求分析与产品规划。",
        "requirements": "5年以上产品经验，有招聘/HR SaaS 产品经验者优先。",
        "min_salary": 20000,
        "max_salary": 35000,
        "recruitment_count": 1,
        "education": EducationEnum.BACHELOR,
        "work_year": 5,
        "department": "运营部",
    },
    {
        "title": "HR 专员",
        "description": "负责招聘流程执行与候选人关系维护。",
        "requirements": "2年以上 HR 经验，熟悉招聘流程，沟通能力强。",
        "min_salary": 8000,
        "max_salary": 15000,
        "recruitment_count": 2,
        "education": EducationEnum.COLLEGE,
        "work_year": 2,
        "department": "人事部",
    },
]


CANDIDATES = [
    # === 已投递 ===
    {
        "name": "王小明",
        "email": "wangxiaoming@example.com",
        "phone": "13800138001",
        "gender": GenderEnum.MALE,
        "work_year": 3,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.APPLICATION,
        "work_experience": "3年 Python 后端开发，熟悉 FastAPI/Django。",
        "education_experience": "某大学 计算机科学与技术 本科",
        "skills": "Python, FastAPI, MySQL, Redis",
    },
    {
        "name": "李小红",
        "email": "lihong@example.com",
        "phone": "13800138002",
        "gender": GenderEnum.FEMALE,
        "work_year": 4,
        "position": "前端开发工程师",
        "status": CandidateStatusEnum.APPLICATION,
        "work_experience": "4年前端开发，精通 Vue3/React。",
        "education_experience": "某大学 软件工程 本科",
        "skills": "Vue3, React, TypeScript, Vite",
    },
    # === AI筛选成功 ===
    {
        "name": "张伟",
        "email": "zhangwei@example.com",
        "phone": "13800138003",
        "gender": GenderEnum.MALE,
        "work_year": 6,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.AI_FILTER_PASSED,
        "work_experience": "6年后端开发，精通微服务架构。",
        "education_experience": "华中科技大学 计算机 本科",
        "skills": "Java, Spring Cloud, Kafka, Redis",
        "ai_score": {
            "work_experience_score": 9,
            "technical_skills_score": 8,
            "soft_skills_score": 7,
            "educational_background_score": 9,
            "project_experience_score": 8,
            "overall_score": 8,
            "summary": "技术扎实，经验丰富，推荐进入面试。",
            "strengths": ["微服务架构", "高并发经验"],
            "weaknesses": ["沟通能力待提升"],
        },
    },
    {
        "name": "刘芳",
        "email": "liufang@example.com",
        "phone": "13800138004",
        "gender": GenderEnum.FEMALE,
        "work_year": 5,
        "position": "产品经理",
        "status": CandidateStatusEnum.AI_FILTER_PASSED,
        "work_experience": "5年产品经验，有 HR SaaS 产品经验。",
        "education_experience": "北京大学 信息管理 硕士",
        "skills": "产品设计, Axure, Figma, 数据分析",
        "ai_score": {
            "work_experience_score": 8,
            "technical_skills_score": 7,
            "soft_skills_score": 9,
            "educational_background_score": 10,
            "project_experience_score": 8,
            "overall_score": 8,
            "summary": "产品经验丰富，有招聘领域经验，推荐面试。",
            "strengths": ["HR SaaS 经验", "沟通能力强"],
            "weaknesses": ["技术背景较弱"],
        },
    },
    # === 协商超时 ===
    {
        "name": "陈杰",
        "email": "chenjie@example.com",
        "phone": "13800138005",
        "gender": GenderEnum.MALE,
        "work_year": 4,
        "position": "前端开发工程师",
        "status": CandidateStatusEnum.NEGOTIATION_EXPIRED,
        "work_experience": "4年前端开发，熟悉 Vue/React。",
        "education_experience": "某大学 计算机科学 本科",
        "skills": "Vue, React, JavaScript",
        "ai_score": {
            "work_experience_score": 7,
            "technical_skills_score": 7,
            "soft_skills_score": 6,
            "educational_background_score": 7,
            "project_experience_score": 7,
            "overall_score": 7,
            "summary": "技术合格，但协商超时未确认面试时间。",
            "strengths": ["前端技术全面"],
            "weaknesses": ["响应速度慢"],
        },
    },
    # === 待面试 ===
    {
        "name": "赵磊",
        "email": "zhaolei@example.com",
        "phone": "13800138006",
        "gender": GenderEnum.MALE,
        "work_year": 5,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.WAITING_FOR_INTERVIEW,
        "work_experience": "5年后端开发，精通分布式系统。",
        "education_experience": "清华大学 计算机 硕士",
        "skills": "Go, Python, K8s, Docker",
        "ai_score": {
            "work_experience_score": 9,
            "technical_skills_score": 9,
            "soft_skills_score": 8,
            "educational_background_score": 10,
            "project_experience_score": 9,
            "overall_score": 9,
            "summary": "技术优秀，学历背景强，强烈推荐。",
            "strengths": ["分布式系统", "高并发"],
            "weaknesses": ["无"],
        },
    },
    {
        "name": "孙丽",
        "email": "sunli@example.com",
        "phone": "13800138007",
        "gender": GenderEnum.FEMALE,
        "work_year": 3,
        "position": "HR 专员",
        "status": CandidateStatusEnum.WAITING_FOR_INTERVIEW,
        "work_experience": "3年 HR 经验，熟悉招聘流程。",
        "education_experience": "某大学 人力资源管理 本科",
        "skills": "招聘, 员工关系, 薪酬福利",
        "ai_score": {
            "work_experience_score": 7,
            "technical_skills_score": 6,
            "soft_skills_score": 9,
            "educational_background_score": 7,
            "project_experience_score": 6,
            "overall_score": 7,
            "summary": "HR 经验丰富，沟通能力好。",
            "strengths": ["沟通能力强"],
            "weaknesses": ["技术背景较弱"],
        },
    },
    # === 已入职 ===
    {
        "name": "周强",
        "email": "zhouqiang@example.com",
        "phone": "13800138008",
        "gender": GenderEnum.MALE,
        "work_year": 7,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.HIRED,
        "work_experience": "7年后端开发，精通高并发架构。",
        "education_experience": "浙江大学 计算机 硕士",
        "skills": "Java, Python, K8s, 微服务",
        "ai_score": {
            "work_experience_score": 10,
            "technical_skills_score": 9,
            "soft_skills_score": 8,
            "educational_background_score": 10,
            "project_experience_score": 9,
            "overall_score": 9,
            "summary": "资深工程师，技术全面，已入职。",
            "strengths": ["高并发", "架构设计"],
            "weaknesses": ["无"],
        },
    },
    {
        "name": "吴敏",
        "email": "wumin@example.com",
        "phone": "13800138009",
        "gender": GenderEnum.FEMALE,
        "work_year": 4,
        "position": "前端开发工程师",
        "status": CandidateStatusEnum.HIRED,
        "work_experience": "4年前端开发，精通 Vue3。",
        "education_experience": "某大学 软件工程 本科",
        "skills": "Vue3, TypeScript, Vite",
        "ai_score": {
            "work_experience_score": 8,
            "technical_skills_score": 8,
            "soft_skills_score": 8,
            "educational_background_score": 7,
            "project_experience_score": 8,
            "overall_score": 8,
            "summary": "前端技术扎实，已入职。",
            "strengths": ["Vue3 专家"],
            "weaknesses": ["无"],
        },
    },
    # === 处理异常 ===
    {
        "name": "郑华",
        "email": "zhenghua@example.com",
        "phone": "13800138010",
        "gender": GenderEnum.MALE,
        "work_year": 5,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.PROCESSING_ERROR,
        "work_experience": "5年后端开发。",
        "education_experience": "某大学 计算机 本科",
        "skills": "Python, Java",
        "error_message": "[scoring] AI 评分调用失败: Connection timeout",
    },
    {
        "name": "黄艳",
        "email": "huangyan@example.com",
        "phone": "13800138011",
        "gender": GenderEnum.FEMALE,
        "work_year": 3,
        "position": "产品经理",
        "status": CandidateStatusEnum.PROCESSING_ERROR,
        "work_experience": "3年产品经验。",
        "education_experience": "某大学 信息管理 本科",
        "skills": "产品设计, 数据分析",
        "error_message": "[confirm] 创建面试预约记录失败: 数据库连接异常",
    },
    # === AI筛选失败 ===
    {
        "name": "林涛",
        "email": "lintao@example.com",
        "phone": "13800138012",
        "gender": GenderEnum.MALE,
        "work_year": 1,
        "position": "高级后端工程师",
        "status": CandidateStatusEnum.AI_FILTER_FAILED,
        "work_experience": "1年后端开发，经验不足。",
        "education_experience": "某大学 计算机 本科",
        "skills": "Python, Django",
        "ai_score": {
            "work_experience_score": 3,
            "technical_skills_score": 4,
            "soft_skills_score": 5,
            "educational_background_score": 6,
            "project_experience_score": 3,
            "overall_score": 4,
            "summary": "经验不足，技术深度不够，未通过筛选。",
            "strengths": ["学习能力强"],
            "weaknesses": ["经验不足", "技术深度不够"],
        },
    },
    # === 已拒绝 ===
    {
        "name": "徐静",
        "email": "xujing@example.com",
        "phone": "13800138013",
        "gender": GenderEnum.FEMALE,
        "work_year": 2,
        "position": "HR 专员",
        "status": CandidateStatusEnum.REJECTED,
        "work_experience": "2年 HR 经验。",
        "education_experience": "某大学 人力资源 本科",
        "skills": "招聘, 员工关系",
    },
]


async def seed_positions():
    """创建演示职位"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            department_repo = DepartmentRepo(session)
            user_repo = UserRepo(session)
            user = await user_repo.get_by_email("boss@qq.com")
            if not user:
                print("错误：找不到 Boss 用户，请先运行 init_data.py")
                return

            for pos_data in POSITIONS:
                dept = await department_repo.get_by_name(pos_data["department"])
                if not dept:
                    print(f"警告：部门 {pos_data['department']} 不存在，跳过")
                    continue

                # 检查是否已存在
                existing = await session.scalar(
                    select(PositionModel).where(PositionModel.title == pos_data["title"])
                )
                if existing:
                    print(f"职位已存在：{pos_data['title']}")
                    continue

                position = PositionModel(
                    title=pos_data["title"],
                    description=pos_data["description"],
                    requirements=pos_data["requirements"],
                    min_salary=pos_data["min_salary"],
                    max_salary=pos_data["max_salary"],
                    recruitment_count=pos_data["recruitment_count"],
                    education=pos_data["education"],
                    work_year=pos_data["work_year"],
                    department_id=dept.id,
                    creator_id=user.id,
                )
                session.add(position)
                print(f"创建职位：{pos_data['title']}")


async def seed_candidates():
    """创建演示候选人"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            user_repo = UserRepo(session)
            user = await user_repo.get_by_email("boss@qq.com")
            if not user:
                print("错误：找不到 Boss 用户")
                return

            for cand_data in CANDIDATES:
                # 检查是否已存在
                existing = await session.scalar(
                    select(CandidateModel).where(CandidateModel.email == cand_data["email"])
                )
                if existing:
                    print(f"候选人已存在：{cand_data['name']} ({cand_data['email']})")
                    continue

                # 查找职位
                position = await session.scalar(
                    select(PositionModel).where(PositionModel.title == cand_data["position"])
                )
                if not position:
                    print(f"警告：职位 {cand_data['position']} 不存在，跳过 {cand_data['name']}")
                    continue

                # 创建简历
                resume = ResumeModel(
                    file_path=f"demo_{cand_data['email'].replace('@', '_')}.pdf",
                    uploader_id=user.id,
                )
                session.add(resume)
                await session.flush()

                # 创建候选人
                candidate = CandidateModel(
                    name=cand_data["name"],
                    email=cand_data["email"],
                    phone_number=cand_data["phone"],
                    gender=cand_data["gender"],
                    work_year=cand_data["work_year"],
                    work_experience=cand_data.get("work_experience"),
                    education_experience=cand_data.get("education_experience"),
                    skills=cand_data.get("skills"),
                    status=cand_data["status"],
                    error_message=cand_data.get("error_message"),
                    position_id=position.id,
                    resume_id=resume.id,
                    creator_id=user.id,
                )
                session.add(candidate)
                await session.flush()
                print(f"创建候选人：{cand_data['name']} - {cand_data['status']}")

                # 创建 AI 评分
                ai_score_data = cand_data.get("ai_score")
                if ai_score_data:
                    ai_score = CandidateAIScoreModel(
                        work_experience_score=ai_score_data["work_experience_score"],
                        technical_skills_score=ai_score_data["technical_skills_score"],
                        soft_skills_score=ai_score_data["soft_skills_score"],
                        educational_background_score=ai_score_data["educational_background_score"],
                        project_experience_score=ai_score_data["project_experience_score"],
                        overall_score=ai_score_data["overall_score"],
                        summary=ai_score_data["summary"],
                        strengths=ai_score_data["strengths"],
                        weaknesses=ai_score_data["weaknesses"],
                        candidate_id=candidate.id,
                    )
                    session.add(ai_score)
                    print(f"  └─ AI 评分：{ai_score_data['overall_score']} 分")


async def main():
    print("=" * 50)
    print("演示数据种子脚本")
    print("=" * 50)

    print("\n[1/2] 创建职位...")
    await seed_positions()

    print("\n[2/2] 创建候选人...")
    await seed_candidates()

    print("\n" + "=" * 50)
    print("种子数据创建完成！")
    print(f"职位数量：{len(POSITIONS)}")
    print(f"候选人数量：{len(CANDIDATES)}")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
