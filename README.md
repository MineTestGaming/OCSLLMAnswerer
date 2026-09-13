# OCS AI Answerer Server

一个基于 Python Flask 和 OpenAI 接口（支持 DeepSeek/Qwen 等）的 OCS 网课助手题库服务器。

本项目旨在为 [OCS 网课助手](https://docs.ocsjs.com/) 提供一个本地化的、高智能的 AI 查题后端。它接收 OCS 发送的题目，通过调用大模型（LLM）进行推理，并将答案格式化返回给 OCS 脚本自动答题。

## ✨ 特性

-   **🤖 多模型支持**: 兼容 OpenAI 格式接口，支持 GPT-3.5/4, DeepSeek, Qwen (通义千问) 等模型。
-   **🧠 智能推理**: 专门针对推理模型优化，自动去除 `<think>` 标签，提取核心 JSON 答案。
-   **🎨 炫彩日志**: 控制台实时显示彩色日志，清晰展示题目、选项、AI 推理结果及解析。
-   **🧹 智能清洗**: 自动去除选项中的多余空行和格式杂质，提高 AI 识别准确率。
-   **🧩 多题型适配**: 针对单选、多选、判断、填空题定制不同的 Prompt，大幅提升准确率。


## 🛠️ 安装与运行

## 直接运行

下载Release包，解压后复制 `.env.template` 为 `.env` ，并修改其中的`OPENAI_API_KEY`和`OPENAI_BASE_URL`，然后运行`OCSAnswererWrapper.exe`即可。

### 1. 克隆或下载本项目
```bash
git clone https://github.com/FengZi-lv/OCSLLMAnswerer.git
cd OCSAnswererWrapper
```

### 2. 安装依赖
建议使用 Python 3.8+ 环境。
```bash
pip install -r requirements.txt
```

### 3. 配置环境变量
复制 `.env.template` 为 `.env` (如果不存在则新建)，并填入您的 API Key

**启动成功后，服务器默认运行在 `http://0.0.0.0:5000`。**

## 模型升级与独立复核

原有各题型 Prompt、system 消息、JSON `answer` / `analysis` 要求和
`temperature=0.3` 保持不变。L1、复核和 L2 都使用完全相同的 Prompt，
不注入前一次答案，不追加置信度指令。服务不将答案转换为字母或完整选项，
而是直接返回最终选中响应的 `answer` 和 `analysis`。

在 `.env` 中设置供应商实际支持的模型名称：

```dotenv
OPENAI_MODEL_MAIN=你的现有主模型
OPENAI_MODEL_L1=你的低成本模型
OPENAI_MODEL_L2=你的强模型
```

未填写 L1 时使用 `OPENAI_MODEL_MAIN`；未填写 L2 时也使用主模型。
旧的 `OPENAI_MODEL` 仍作为主模型的备用配置。若 L1 和 L2 相同，
系统仍可复核，但不会在日志里声称升级到了更强模型。
`.env.template` 是配置示例，不会覆盖现有 `.env`。

| 情况 | 路由 |
| --- | --- |
| 普通单选、判断、填空 | L1 回答后，再用 L1 独立调用一次；答案一致时返回第一次结果 |
| 普通多选 | L1 独立调用两次；选项集合一致时返回第一次的原始答案和解析 |
| 两次答案不一致，或 L1 / 复核输出不合法 | 调用 L2，返回 L2 结果 |
| 多选选项达到 6 个、未知题型 | 直接调用 L2 |
| 超过 150 字、命中否定 / 最佳答案 / 材料题关键词 | 直接调用 L2 |
| L2 输出无效或供应商调用失败 | `/search` 返回 HTTP 502、`code: 0`，不将错误标成有效答案 |

普通题通常有 2 次调用，升级时最多 3 次；直接使用 L2 的题目有 1 次。
这些是路由层调用次数，OpenAI SDK 自身的网络重试另计。
静态关键词是初始启发规则，可以在 `model_router.py` 的 `static_risk` 中调整。

为遵守原 Prompt，本版本不请求 `logprobs`，也不把 JSON token 的生成概率
或模型自报数字当成答对概率。独立调用可能重复同一个错误，一致性不能保证正确。
单选、判断、填空的答案比较只忽略首尾空白。
多选在内部按原题选项解析答案集合，忽略集合顺序、分隔符两侧空白和全角 `＃`；
纯字母列表也支持逗号、顿号和分号。完整选项文本中的逗号不拆分。
只有能唯一对应原选项时，序号与完整选项文本才视为等价。
重复选项、少于两个选项、不存在的选项或无法唯一解析的答案触发 L2；
L2 仍无法通过校验则返回失败。按原 Prompt，多选要求至少两个正确选项。
选项输入沿用每行一个选项的约定，支持无标号选项以及 `A. 文本`、`1. 文本` 等标号。
所有规范化仅用于校验和比较，最终 `answer`、`analysis` 不做转换。
原 Prompt 内部对序号 / 完整内容的不同表述按要求原样保留。

控制台记录模型、路由、升级原因和尝试记录。`get_chatgpt_answer()` 的
内部结果包含 `_meta`，但 `/search` 成功响应保持原有字段，OCS 配置无需更改。

### 离线测试

安装项目依赖后，在项目根目录运行：

```bash
python -m unittest discover -s tests -v
```

测试模拟付费 API 边界，覆盖路由、无效输出、失败响应和各题型 Prompt
与重构前的逐字一致性，不会发送真实题目或消费 API 额度。

## 🖥️ OCS 脚本配置

打开 OCS 网课助手的【全局设置】 -> 【题库配置】，点击【新建】，粘贴以下配置：

```json
[
    {
        "name": "AI题库",
        "homepage": "http://localhost:5000",
        "url": "http://localhost:5000/search",
        "method": "post",
        "type": "GM_xmlhttpRequest",
        "contentType": "json",
        "data": {
            "title": "${title}",
            "options": "${options}",
            "type": "${type}"
        },
        "handler": "return (res) => res.code === 1 ? [res.question, res.answer, {ai: res.analysis}] : undefined"
    }
]
```
> **注意**: 如果您部署在云服务器上，请将 `localhost` 替换为服务器 IP。



## ⚠️ 免责声明

本项目仅供学习交流使用，请勿用于违反学校规定或法律法规的用途。开发者不对使用本项目产生的任何后果负责。
