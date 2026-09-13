import json
import os
import re
from datetime import datetime

from colorama import Fore, Style, init
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from openai import OpenAI

import socket
from pathlib import Path

def patch_connect():
    ip = socket.gethostbyname(socket.gethostname())

    path = Path("/app/ocs.user.js")
    text = path.read_text(encoding="utf-8")

    # 清理旧的动态 @connect
    lines = [
        line for line in text.splitlines()
        if not line.startswith("// @connect      172.")
    ]

    marker = "// ==/UserScript=="
    connect = f"// @connect      {ip}"

    text = "\n".join(lines)

    if connect not in text:
        text = text.replace(
            marker,
            f"{connect}\n{marker}",
            1
        )

    path.write_text(text, encoding="utf-8")
    print(f"Patched userscript @connect -> {ip}", flush=True)

# 初始化 colorama
init(autoreset=True)

# 加载环境变量
load_dotenv()

app = Flask(__name__)

CORS(
    app,
    resources={r"/*": {"origins": "*"}}
)

# 配置 OpenAI 客户端
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")

if not api_key:
    print(Fore.RED + "Warning: OPENAI_API_KEY is not set in environment variables.")

client = OpenAI(api_key=api_key, base_url=base_url if base_url else None)


def log_info(msg):
    print(
        f"{Fore.CYAN}[INFO] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}"
    )


def log_success(msg):
    print(
        f"{Fore.GREEN}[SUCCESS] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}"
    )


def log_error(msg):
    print(
        f"{Fore.RED}[ERROR] {datetime.now().strftime('%H:%M:%S')} {Style.RESET_ALL}{msg}"
    )


def log_request(title, options, q_type):
    print(
        f"\n{Fore.YELLOW}新的请求 [{datetime.now().strftime('%H:%M:%S')}] {Style.RESET_ALL}"
    )
    print(f"{Fore.BLUE}题目:{Style.RESET_ALL} {title}")
    print(f"{Fore.BLUE}类型:{Style.RESET_ALL} {q_type}")
    if options:
        print(f"{Fore.BLUE}选项:{Style.RESET_ALL} \n{options.strip()}")


def log_response(answer, analysis):
    print(f"{Fore.MAGENTA}答案:{Style.RESET_ALL} {answer}")
    print(f"{Fore.MAGENTA}解析:{Style.RESET_ALL} {analysis}")


# 题型映射
TYPE_MAPPING = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
    "unknown": "未知类型",
}


def get_chatgpt_answer(title, options, original_type):
    """
    调用 ChatGPT 获取答案
    """
    # 转换题型为中文
    question_type = TYPE_MAPPING.get(original_type, original_type)

    # 根据题型生成特定指令
    special_instruction = ""
    if original_type == "single":
        special_instruction = "重要提醒：这是一道【单选题】，请从所有选项中选择且仅选择一个最正确、最符合题意的选项。即使有多个选项看起来合理，也必须根据题干限定条件、教材规范表述和题目考查重点，确定唯一最佳答案。"
    elif original_type == "multiple":
        special_instruction = "重要提示：这是一道【多选题】，本题为多选题。请逐项判断每个选项是否正确，选择所有符合题意的正确选项，不得遗漏正确选项，也不得加入错误选项。正确答案数量可能为两个或更多，不要默认固定数量，answer 字段中必须填写所有正确选项的完整内容，并严格按照原选项顺序排列，用 '#' 号分隔（例如：A#C#D）。"
    elif original_type == "completion":
        special_instruction = "重要提示：这是一道【填空题】，请根据题干语义、知识点和上下文，填写最准确、最规范且能够直接代入题目空缺位置的答案。优先使用教材中的标准术语、固定表达、公式、数值或名称，避免不必要的解释和同义改写，不要直接输出填空编号等系统提示，除非题目有要求。"
        options = ""
    elif original_type == "judgement":
        special_instruction = "重要提示：这是一道【判断题】，请判断题干陈述整体是否正确，并从题目提供的判断选项中选择对应答案。判断时应特别注意绝对化表述、适用条件、概念范围、因果关系以及例外情况。answer 字段中只填写对应选项的完整内容，例如“正确”或“错误”，不得添加任何其他内容。"

    # 简单的 Prompt，不做过多修饰，保持核心逻辑
    prompt = f"""
你是一名严谨、专业的学术助教。你的任务是根据题目内容、选项、题目类型以及额外要求，判断并给出最准确的答案。

题目：
{title}

选项：
{options}

题目类型：
{question_type}

额外要求：
{special_instruction}

请按照以下步骤完成判断：
1. 准确理解题意，识别题目考查的知识点。
2. 结合题目类型判断应选择一个还是多个答案。
3. 仔细比较所有选项，排除明显错误、表述不严谨或与题意不符的选项。
4. 如果存在多个看似正确的选项，选择最符合教材、通行学术规范或题目设问方式的答案。
5. 如果“额外要求”与一般规则冲突，优先遵循“额外要求”。

输出时必须严格遵守以下规则：

1. 仅输出一个合法、可直接解析的 JSON 对象，不得输出任何其他文字。
2. 不得使用 Markdown，不得使用代码块，不得在 JSON 前后添加解释、提示、前缀或后缀。
3. JSON 必须包含且只能包含以下两个字段：
   - "answer"
   - "analysis"
4. "answer" 必须填写选项的序号。
5. "analysis" 只填写简短、明确的判断依据，通常控制在 1～3 句话，不展开冗长推导。
6. JSON 字符串中的双引号、换行符、反斜杠等特殊字符必须正确转义，确保 JSON 可以被标准解析器直接解析。
7. 不得输出类似“答案是”“我认为”“根据题目”等额外格式化内容。
8. 即使题目存在歧义，也必须结合题干、选项和常见教材表述选择最可能的答案，不要拒绝作答。

最终输出格式必须严格为：

{{
  "answer": "这里填写最准确的选项完整内容；多选时使用#分隔",
  "analysis": "这里填写简短的解析"
}}
"""
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            messages=[
                {"role": "system", "content": "你是一个只输出 JSON 的专业做题助手。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content.strip()

        # 清洗逻辑
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            content = match.group(0)

        result = json.loads(content)
        return result
    except Exception as e:
        log_error(f"OpenAI 调用或解析失败: {e}")
        return {"answer": "未知", "analysis": "服务器处理出错"}


@app.route("/", methods=["GET", "HEAD"])
def index():
    return jsonify({"code": 1, "msg": "OCS ChatGPT Server is running"}), 200


@app.route("/search", methods=["POST"])
def search_answer():
    try:
        data = request.get_json(force=True, silent=True)
        if data is None:
            try:
                data = json.loads(request.data)
            except Exception:
                return jsonify({"code": 0, "msg": "无法解析 JSON 数据"}), 400

        title = data.get("title", "")
        options = data.get("options", "")
        q_type = data.get("type", "Unknown")

        if options:
            # 清理选项：去除每一行的前后空格，并过滤掉空行，重新组合
            options = "\n".join(
                [line.strip() for line in options.split("\n") if line.strip()]
            )

        if not title:
            return jsonify({"code": 0, "msg": "题目为空"}), 400

        # 获取中文题型名称用于日志显示
        display_type = TYPE_MAPPING.get(q_type, q_type)
        log_request(title, options, display_type)

        result = get_chatgpt_answer(title, options, q_type)

        answer = result.get("answer", "未知")
        analysis = result.get("analysis", "无解析")

        log_response(answer, analysis)

        return jsonify(
            {"code": 1, "question": title, "answer": answer, "analysis": analysis}
        )

    except Exception as e:
        log_error(f"服务器内部错误: {e}")
        return jsonify({"code": 0, "msg": str(e)}), 500

@app.route("/ocs.user.js")
def get_plugin() :
    return send_file(
        "/app/ocs.user.js",
        mimetype="application/javascript",
        as_attachment=False
    )



if __name__ == "__main__":
    patch_connect()
    log_info(f"服务启动在 http://0.0.0.0:5000")
    app.run(host="0.0.0.0", 
        port=5000, 
        ssl_context=(
            "/certs/ocs-llm-answer.crt",
            "/certs/ocs-llm-answer.key"
        ),
        debug=False)
