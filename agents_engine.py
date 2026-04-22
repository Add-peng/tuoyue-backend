from crewai import Agent, Task, Crew, Process
import logging
import os
import time

# ================= 拓岳专属 API 配置区 =================
os.environ["OPENAI_API_KEY"] = "sk-NNkUVKrc2gcPwwjvJmzvPglyCQtjW6Cc7SKh0NZjGJrt1VOb"
os.environ["OPENAI_API_BASE"] = "http://api.tuoyue-tech.shop/v1"
os.environ["OPENAI_MODEL_NAME"] = "gpt-4o-mini"
# =======================================================

logger = logging.getLogger("tuoyue")


def run_copywriter_crew(user_topic: str):
    """
    核心执行函数：接收一个主题，返回 (final_result, trace_data)
    - final_result: 最终生成的完整文案（字符串）
    - trace_data: 协作流程轨迹列表，每个元素为 {agent, input_summary, output_summary, duration_ms}
    """
    trace_data = []

    # ================= Agent 1: 需求分析师 =================
    requirement_analyst = Agent(
        role='需求分析师',
        goal='深入理解用户主题，拆解出目标人群、核心痛点、风格要求、关键卖点',
        backstory='你是资深营销策划，擅长从一句话主题中挖掘出最有效的传播角度。',
        verbose=True,
        allow_delegation=False
    )

    analysis_task = Task(
        description=f'请分析主题：“{user_topic}”。提炼出目标人群、核心痛点、风格建议、必提卖点。输出简洁的要点，不超过200字。',
        expected_output='一段简短的营销需求分析报告',
        agent=requirement_analyst
    )

    # ================= Agent 2: 标题生成师 =================
    title_creator = Agent(
        role='标题创作专家',
        goal='根据需求分析报告，产出5个极具吸引力的爆款标题',
        backstory='你掌握全网标题公式，擅长用数字、悬念、情绪词抓住眼球。',
        verbose=True,
        allow_delegation=False
    )

    title_task = Task(
        description='根据需求分析报告，创作5个备选标题。每个标题独占一行，标上序号1-5。',
        expected_output='5个爆款标题，每行一个，格式：1. 标题内容',
        agent=title_creator,
        context=[analysis_task]
    )

    # ================= Agent 3: 正文撰写师 =================
    content_writer = Agent(
        role='资深文案写手',
        goal='根据需求分析和选中的标题，撰写一篇高质量种草文案',
        backstory='你擅长讲故事、用emoji排版、营造场景感，让读者忍不住点赞收藏。',
        verbose=True,
        allow_delegation=False
    )

    content_task = Task(
        description='基于需求分析和标题列表，撰写一篇不少于300字的小红书风格种草文案。请包含生动的场景描述、真实使用感受、以及呼吁行动的结尾。',
        expected_output='完整的种草文案正文，包含emoji和段落分隔',
        agent=content_writer,
        context=[analysis_task, title_task]
    )

    # ================= 组建 Crew（顺序执行） =================
    crew = Crew(
        agents=[requirement_analyst, title_creator, content_writer],
        tasks=[analysis_task, title_task, content_task],
        process=Process.sequential,
        verbose=True
    )

    # 记录整体开始时间
    crew_start = time.time()
    logger.info(
        "crew execution started",
        extra={"task_id": None, "topic": user_topic, "started_at": crew_start},
    )
    final_result = crew.kickoff()
    total_duration = int((time.time() - crew_start) * 1000)
    logger.info(
        "crew execution completed",
        extra={"task_id": None, "topic": user_topic, "duration_ms": total_duration},
    )

    # 尝试从任务对象中提取真实输出摘要
    analysis_summary = "分析完成"
    title_summary = "标题生成完成"
    try:
        if hasattr(analysis_task, 'output') and analysis_task.output:
            raw = analysis_task.output.raw
            analysis_summary = raw[:100] + "..." if len(raw) > 100 else raw
        if hasattr(title_task, 'output') and title_task.output:
            raw = title_task.output.raw
            title_summary = raw[:100] + "..." if len(raw) > 100 else raw
    except:
        pass

    trace_data = [
        {
            "agent": "需求分析师",
            "input_summary": user_topic[:50] + "..." if len(user_topic) > 50 else user_topic,
            "output_summary": analysis_summary,
            "duration_ms": 1500  # 模拟耗时，后续可通过更精细的回调获取真实值
        },
        {
            "agent": "标题创作专家",
            "input_summary": "需求分析报告",
            "output_summary": title_summary,
            "duration_ms": 2000
        },
        {
            "agent": "资深文案写手",
            "input_summary": "需求分析+标题列表",
            "output_summary": "最终文案正文",
            "duration_ms": 2500
        }
    ]

    final_output = str(final_result)
    return final_output, trace_data


# 兼容旧函数名，保证 main.py 调用不报错
def run_copywriter(user_topic: str):
    """兼容旧版单 Agent 调用，返回纯文案字符串"""
    result, _ = run_copywriter_crew(user_topic)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("manual crew test started", extra={"task_id": None, "topic": "夏季无痕防晒衣"})
    final, trace = run_copywriter_crew("夏季无痕防晒衣")
    logger.info("manual crew test output", extra={"task_id": None, "result_length": len(final), "trace_length": len(trace)})