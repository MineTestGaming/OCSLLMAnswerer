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

### 日志模式

在 `.env` 中设置 `DEBUG`，修改后重启服务生效：

```dotenv
DEBUG=true
```

- `DEBUG=true`：在当前终端显示完整请求、题目、选项、答案、解析、模型路由和错误详情。
- `DEBUG=false`（默认，Release）：只显示每次调用的模型名和成功或失败，例如 `auto-quality: 成功`；隐藏题目、答案、启动横幅、HTTP 访问日志和第三方库日志。

`DEBUG` 支持 `true/false`、`1/0`、`yes/no`、`on/off`，忽略大小写和首尾空白。
此开关仅控制日志，不开启 Flask 调试器或自动重载；接口响应内容保持原有格式。

### HTTPS / SSL 配置

在 `.env` 中配置 SSL 开关和 PEM 格式证书、私钥文件路径：

```dotenv
SSL_ENABLED=true
SSL_SERVER_CERT_FILE=/certs/ocs-llm-answer.crt
SSL_SERVER_KEY_FILE=/certs/ocs-llm-answer.key
```

默认 `SSL_ENABLED=false`，使用 HTTP。改为 `true` 并重启后，5000 端口使用 HTTPS，
OCS 的 `homepage`、`url` 也应改用 `https://`，例如 `https://ocs-llm-answer:5000/search`。
同一端口只运行选定的一种协议；向 HTTP 端口发送 HTTPS 会产生 TLS 乱码和 400 日志。
启用 SSL 时，路径缺失、文件不可读、证书或私钥无效都会使启动失败，不会退回 HTTP。
关闭 SSL 时不读取证书文件。相对路径以进程工作目录为基准；Windows 可使用
`C:/certs/server.crt` 形式的路径，Docker 中须使用容器内路径并挂载证书文件。
浏览器须信任证书，且访问主机名须与证书匹配。这里配置的是文件路径，不是证书内容。
不要使用 Python/OpenSSL 保留变量 `SSL_CERT_FILE` 指定服务器证书；它会覆盖出站
HTTPS 的 CA 信任库，导致模型 API 证书校验失败。

使用仓库根目录的 Compose 时，答题服务会读取 `LlmAns/.env`，并与桌面容器共享
Tailscale 网络命名空间。OCS 配置中的地址必须保持为
`https://ocs-llm-answer:5000`：证书仅包含该主机名，不能改成 `localhost` 或 IP。
桌面镜像会在 root 初始化阶段安装 CA，并把该主机名映射到共享命名空间的
`127.0.0.1`；不要在 Compose 中添加 `extra_hosts`，它与
`network_mode: service:tailscale` 不兼容。

## 模型升级与独立复核

纯文字题原有各题型 Prompt、system 消息、JSON `answer` / `analysis` 要求和
默认使用 `temperature=0.3`。模型调用使用 OpenAI 兼容的 JSON 响应模式，避免
供应商在合法 JSON 后追加省略号或解释文字。L1、复核和 L2 都使用完全相同的 Prompt，
不注入前一次答案，不追加置信度指令。服务不将答案转换为字母或完整选项，
而是直接返回最终选中响应的 `answer` 和 `analysis`。图片题的 Image ID
会在答案返回时还原为原图片链接，详见下方图片输入说明。

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
| 两次答案不一致，或 L1 / 复核输出不合法、超时、连接失败或 API 报错 | 调用 L2，返回 L2 结果 |
| 多选选项达到 6 个、未知题型 | 直接调用 L2 |
| 超过 150 字、命中否定 / 最佳答案 / 材料题关键词 | 直接调用 L2 |
| L2 输出无效或 L2 供应商调用失败 | 已配置独立备用接口时调用备用模型；没有备用或备用仍失败时返回 HTTP 502、`code: 0` |

普通题通常有 2 个主模型调用阶段，升级时最多 3 个；直接使用 L2 的题目有 1 个。
若接口明确以 HTTP 400 拒绝 `temperature` 参数，当前阶段会去掉该参数重试一次；
其他参数错误、鉴权失败和网络失败不会触发该参数重试。日志记录两次实际调用，
第二次标注 `temperature_omitted: true`。备用调用和 SDK 自身的网络重试另计。
L1 首次调用或独立复核在 SDK 重试后仍失败时，会跳过剩余 L1 步骤并转到 L2；
日志会记录失败阶段和异常类型，L2 仍失败时按下方配置尝试独立备用接口。
静态关键词是初始启发规则，可以在 `model_router.py` 的 `static_risk` 中调整。

### 独立备用接口

L1 和 L2 共用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。若需要在主接口故障时
切换供应商，须另外配置以下三项；仅设置 `OPENAI_MODEL_L2` 不会切换接口：

```dotenv
OPENAI_BACKUP_API_KEY=备用供应商的Key
OPENAI_BACKUP_BASE_URL=https://备用供应商地址/v1
OPENAI_BACKUP_MODEL=备用供应商实际支持的模型ID
OPENAI_BACKUP_VISION_MODELS=备用供应商支持视觉的模型ID
OPENAI_TIMEOUT_SECONDS=30
```

主接口最终 L2 调用失败或输出校验不通过后，程序通过独立客户端调用备用模型一次，
并复用原题目和格式校验。正常主接口响应不会触发备用调用；备用仍失败才返回 502。
日志中的路由为 `backup`，尝试记录标注 `provider: backup`。备用模型的图片能力
只按 `OPENAI_BACKUP_VISION_MODELS` 判断。前三项须全部填写或全部留空，配置缺失时
启动会列出缺失的变量名。修改 `.env` 后需重启服务。

`OPENAI_TIMEOUT_SECONDS` 对主、备用接口均生效，默认 600 秒，是单次 API 尝试
的超时；SDK 自带重试仍可能延长实际等待时间。它与 OCS 脚本的搜题等待预算不同。

服务器启动时，`main.py` 会为 `/app/ocs.user.js` 补上服务域名、当前 IP 的
`@connect`，并调整题库和答题等待超时。默认每次题库请求的预算为
`30 + 2 × 600 + 2 × 600 + 60 = 2490` 秒（41 分 30 秒），分别预留
1 轮 Jev、2 轮 5.6 Terra、2 轮 6 Astra 和 60 秒余量。
这里的轮数用于预留等待时间，不改变实际模型路由和调用次数。
OpenAI SDK 网络重试另计，这不是整个后端请求的严格耗时上限。

题库请求发生网络错误、HTTP 失败或超时后，JS 会等待 30 秒再重试，最多重试
10 次（加上首次请求共 11 次）；成功后立即停止重试。各题库独立重试，正常返回
但没有答案、响应处理器报错时不重试。控制台会显示当前重试次数；全部失败后，
沿用 OCS 的题库错误提示。该补丁只作用于题库请求。
答题等待上限会同步调整为 `每次请求超时 × 11 + 30 × 10 + 10` 秒，
覆盖所有请求、重试间隔和 10 秒余量。

可在 `.env` 设置 `OCS_ANSWER_TIMEOUT_SECONDS=2490` 调整该预算，重启服务器后生效。
补丁会提高 OCS「搜题最大耗时」的默认值、最小值和可设置上限；浏览器保存的旧短超时
会自动提升到预算值，更长的设置仍然生效（单次上限为 195197 秒，避免 JS 总计时器溢出）。
使用 Compose 部署时，修改补丁代码后运行 `docker compose up -d --build ocs-llm-answer`。
已经安装的脚本需要从
`https://ocs-llm-answer:5000/ocs.user.js` 重新安装或更新（未启用 SSL 时使用 HTTP）。

### Jev L0 快速路由

可选的 Jev System One L0 层只处理无图片、未命中静态风险规则的单选题和判断题。
它从题目原始选项中选择答案，并同时给出 `l0_direct`、`verify_l1` 或
`direct_l2` 路由。候选答案和 `l0_direct` 路由的归一化概率都达到阈值时，
服务直接返回原始选项文本；否则沿用 L1 独立复核流程。`direct_l2` 会直接进入 L2。
图片题、多选题、填空题、无法唯一解析选项的题目不会调用 Jev。

```dotenv
JEV_ENABLED=true
TYPESAFE_API_KEY=你的TypeSafe API Key
JEV_MODEL=jev-latest
JEV_DIRECT_CONFIDENCE=0.990
```

Jev 默认关闭。缺少 Key、配置非法、初始化失败、超时、网络错误或响应无效时，
服务会静默回退到原有 L1/L2 路由，不影响启动和 `/search` 的成功/失败语义。
L0 直答的固定解析为 `Jev L0 高置信度直答（99.0%+）`。运行日志只包含模型、
路由及归一化概率，不记录 API Key、Authorization 请求头或完整 SDK 响应。
启用后，符合条件的题干与选项会发送给 TypeSafe AI，图片不会发送。

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

### 图片题干与图片选项

支持题干、选项字符串中的 HTTP/HTTPS 图片链接，例如超星的 `.PNG` 链接。
保持原来的 OCS 请求配置即可，无需增加 JSON 字段。
按 URL 路径后缀识别 png、jpg/jpeg、gif、webp、bmp、svg、avif、tif/tiff，
不区分大小写，保留查询参数和签名。普通网页链接不当作图片；
无图片后缀的下载接口目前不自动识别。

图片请求沿用本次 `/search` 请求的 `User-Agent`；来源未提供 UA 时，
使用 `OCSLLMAnswerer/1.0`。不转发来源请求的 Cookie 或 Authorization。
服务端下载成功后将图片字节编码为 `data:image/...;base64,...`，
通过多模态 `image_url` 内容块发给支持视觉的模型。供应商不再访问原图片链接，
也不接收来源 UA。图片只在当前请求中保存，复核和升级调用复用 Base64 数据，
不会重复下载；原图片 URL 单独保留用于调试和答案回填。

服务端先用 GET 检查图片：响应须为 2xx、Content-Type 为 `image/*`，
内容非空且不超过 20 MiB，网络连接和读取的 socket 超时为 10 秒。
这是下载与响应检查，不进行图片解码或 OCR；供应商支持的图片格式以其接口为准。
同一道题重复链接只检查一次，依次替换为 `[Image 1]`、`[Image 2]` 等。
题干、选项共享编号。任何一张图片请求失败、超时或响应不符合上述条件，
`/search` 返回 **HTTP 501**、`code: 0`，不调用模型。
501 响应同时包含 `question`（原始题干）、`options`（原始选项字符串）和 `type`，
保留图片链接及原始选项空白，方便定位和重放失败请求。

模型调用或答案校验失败时返回 HTTP 502、`code: 0`，并提供以下调试字段：

- `raw_output`：最终失败模型的原始 `message.content`，不清理空白、思考标签或代码块。
- `model`、`finish_reason`：失败模型和输出结束原因。
- `error_type`、`reason`：异常类型和校验或接口失败原因，例如超时、鉴权失败、余额不足。
- `provider`、`http_status`：最终失败的主 / 备用接口标识和上游 HTTP 状态码（如有）。
- `attempts`：按调用顺序列出已尝试的模型、接口、结果和失败原因；包含有效 Jev 路由记录及参数兼容重试。
- `reasons`：路由、升级和参数兼容处理的原因。
- `refusal`：供应商单独返回的拒绝内容（如果有）。

网络错误或没有候选输出时，`raw_output` 为 `null`；空字符串输出保持为空字符串。
网络调用失败时 `model` 仍保留最后调用的模型 ID。控制台在失败时同样输出完整调用记录，
接口错误使用固定说明，不直接输出供应商异常文本或凭证。
这些字段用于调试，不作为有效答案；成功响应格式保持不变。

在 `.env` 中明确列出支持视觉输入的实际模型 ID（逗号分隔）：

```dotenv
OPENAI_VISION_MODELS=你的视觉模型ID,另一个视觉模型ID
```

每次 L1、复核、L2 调用都独立检查当前模型是否在名单中：

- 在名单中：发送替换后的文本，以及标有对应 Image ID、包含 Base64 图片数据的 `image_url` 内容块。
- 不在名单中或未配置：只发送替换后的文本，让模型在没有图片的情况下尝试作答。

模型能力由配置声明，不按模型名称猜测，也不发送付费探测请求。
本功能不安装或调用 OCR，不需要 CUDA。原有路由规则保持不变，
因此纯文本 L1 两次回答一致时仍会直接返回，不保证调用视觉 L2。
若供应商拒绝 Base64 图片输入、图片格式或请求大小，仍按原有模型调用失败
返回 HTTP 502，不静默重试或降级。Base64 会增加约三分之一的数据体积。

图片题的提示词明确 Image ID 与附图的对应关系，提示核对公式、电路连接、
坐标和单位；未附图的模型须依据剩余文字尝试作答，并在解析中说明图片缺失。

选择类题目若每行选项只包含图片（可带原有字母或数字编号），会按行顺序重新
标为 `A. [Image N]`、`B. [Image M]` 等，支持最多 26 个选项。
Image ID 的数字与选项字母相互独立。单选只接受一个大写字母，如 `B`；
多选按选项顺序用 `#` 分隔，如 `A#C`，不返回数字编号、图片 ID 或 URL。
不符合格式或字母超出选项范围时，按原无效答案规则升级；L2 仍无效则返回 502。
填空题不应用该字母约束。

返回给 OCS 的 `question` 保留原题干。图文混合选项的答案中的已知 Image ID
仍恢复为原始图片 URL，其他答案文字保持原样；
`analysis` 和路由日志中的原始模型答案不改写。
纯文字题保持原 Prompt 和原有行为。

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
