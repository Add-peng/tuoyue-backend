from crewai import Agent, Task, Crew
import os

# ================= 拓岳专属 API 配置区 =================
# 这里已经配置好你提供的 NewAPI 密钥和地址
os.environ["OPENAI_API_KEY"] = "sk-NNkUVKrc2gcPwwjvJmzvPglyCQtjW6Cc7SKh0NZjGJrt1VOb"
os.environ["OPENAI_API_BASE"] = "http://api.tuoyue-tech.shop/v1"
os.environ["OPENAI_MODEL_NAME"] = "gpt-4o-mini"
# =======================================================

def run_copywriter(user_topic: str):
    """
    核心执行函数：接收一个主题，返回 CrewAI 处理后的文案结果
    """
    
    # 1. 设定爆款文案专家角色
    copywriter = Agent(
        role='小红书爆款文案专家',
        goal='根据用户的主题，写出极具网感的种草文案',
        backstory='你深谙流量密码，精通各类标题公式和排版节奏。',
        verbose=True,
        allow_delegation=False
    )

    # 2. 设定任务，这里的 {user_topic} 会动态替换
    write_post = Task(
        description=f'为主题 "{user_topic}" 撰写一篇不少于 300 字的种草文案。包含 3 个吸睛的备选标题。',
        expected_output='3个标题 + 带有 emoji 排版的高质量正文',
        agent=copywriter
    )

    # 3. 组建 SaaS 核心团队
    ecommerce_crew = Crew(
        agents=[copywriter],
        tasks=[write_post],
        verbose=True
    )

    # 4. 执行并返回结果（注意：这里返回的是字符串格式的结果）
    result = ecommerce_crew.kickoff()
    
    # 转换成字符串返回，确保 FastAPI 能正常识别
    return str(result)

# 如果你手动在终端跑这个脚本，它依然可以执行测试
if __name__ == "__main__":
    print("正在进行内部测试...")
    test_result = run_copywriter("夏季无痕防晒衣")
    print(test_result)