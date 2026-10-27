# eleven-say

一条命令，把文字变成 Telegram 能直接发的语音（`.ogg`，opus 编码），用的是 ElevenLabs 的嗓子。

```bash
python3 eleven_say.py "[warm, close to her ear] 早安，今天也要记得喝水。" -o morning.ogg
# → 打印 morning.ogg 的路径，拿去发 Telegram 语音就行
```

它不挑是谁在调用：Codex、OpenCode、Claude Code，或者你自己在终端敲，都一样。底下跑的是 DeepSeek、GPT 还是 Claude 也没关系——只要能执行一条 shell 命令。

比直接调 API 多做的几件小事：

- **首尾垫一点静音**：Telegram 播语音时开头结尾容易被吞掉半个字，垫上就好了；
- **剪掉结尾杂音**：v3 偶尔在最后一个字之后多吐半秒怪声，自动剪掉（但"……我在。"这种很短的真结尾会留着）；
- **防复读**：模型偶尔把一句话念两遍，中文按时长判断，太长就自动重新生成（最多再生成两次，所以**一条命令最多可能扣三次额度**；还不行就保留最后一条并提示）；
- **转成 ogg/opus**：Telegram 收到就是语音条，不是文件。
- **安全落盘**：先写临时文件，成功后再替换目标（权限 600）；失败不会留下半个文件，也不会弄坏同名旧文件。

只用 Python 标准库，外加系统里的 ffmpeg。

---

## 从零开始（Linux VPS）

### 1. 注册 ElevenLabs，拿钥匙

1. 打开 <https://elevenlabs.io> 注册账号（免费档不用绑卡）。
2. 在账户设置里找到 **API Keys**（通常在 Developers 那一栏）→ 新建一把钥匙，复制那串 `sk_` 开头的字符。
   **钥匙等于你的账户，别发到聊天里，别提交进任何仓库。**

免费档目前大致是：每月 10,000 credits（按字数扣），API 能用，生成的内容**只能非商业使用、需要署名 ElevenLabs**。额度和条款经常调整，**以官网定价页为准**：<https://elevenlabs.io/pricing>

### 2. 挑一个嗓子，复制 Voice ID

1. 网页里打开 **Voices** 的声音库（Voice Library），试听、挑一个喜欢的，加到自己的嗓子列表里。
   免费档没法通过 API 浏览声音库，所以这一步在网页上做。
2. 在自己的嗓子列表里点开它，复制 **Voice ID**（一串字母数字）。

（网页按钮的名字和位置时常改版，找不到就搜官网帮助里的 "voice ID"。）

想要独一无二的嗓子，可以用 **Voice Design**（用文字描述生成一个嗓子），免费档有几个保存名额；声音克隆是付费档的功能。

### 3. 装依赖

```bash
sudo apt update && sudo apt install -y ffmpeg python3
git clone https://github.com/<owner>/eleven-say.git
cd eleven-say
```

### 4. 填配置

```bash
cp .env.example .env
nano .env        # 填 ELEVENLABS_API_KEY 和 ELEVENLABS_VOICE_ID
chmod 600 .env
set -a; . ./.env; set +a     # 让当前终端读到配置
```

想每次登录自动生效，把这两行加进 `~/.bashrc`：

```bash
set -a; . ~/eleven-say/.env; set +a
```

也可以不把钥匙放环境变量，而是存进一个只有自己能读的文件，再用 `ELEVENLABS_API_KEY_FILE` 指过去。

### 5. 试一句

```bash
python3 eleven_say.py "你好呀，我是你家的小机。" -o hello.ogg
```

成功会打印 `hello.ogg`。听一下，满意就可以交给你家的机用了。

---

## 调音：只是建议，自己听了算

工具默认只发「文字 + 模型 + 嗓子」，不替你预设任何语气参数——哪种最好听，每个嗓子、每只机都不一样，**自己试**。

默认模型是 `eleven_v4`，可以用 `--model` 换。先用同一句话各跑一条，听听哪个合你的耳朵：

```bash
python3 eleven_say.py "[warm, gentle] 今天辛苦啦，早点休息。" -o try-v4.ogg
python3 eleven_say.py --model eleven_v3 "[warm, gentle] 今天辛苦啦，早点休息。" -o try-v3.ogg
```

⚠️ **每生成一条都会扣额度**，试听别一口气跑几十条。

一些经验，供参考：

- **方括号标签**：v4、v3 都认写在文字最前面的英文标签，例如 `[soft, sleepy, slow]`、`[excited]`、`[whispers]`、`[quiet, measured narration]`。标签不会被念出来。可以叠几个词：情绪、音量和距离、节奏。
- **参数**：想试 stability / style / speed 这类参数，在 `.env` 里设对应的 `ELEVENLABS_*`，设了就会发出去。哪个模型认哪个参数官方会调整，以官方文档为准：<https://elevenlabs.io/docs/overview/models>。模型不认的参数可能被悄悄忽略，也可能报 HTTP 422（报了就去掉那一项）。
- **结尾杂音**：v3 偶尔在最后一个字后面多半秒怪声，所以 `ELEVENLABS_TRIM_TAIL=auto` 只在 v3 上剪；觉得剪掉了真话就设成 `off`。

### 讲长故事

一段太长时分几段念，把前后段传进去，语气能接上：

```bash
python3 eleven_say.py -f part2.txt -o part2.ogg --previous-text "$(cat part1.txt)" --next-text "$(cat part3.txt)"
```

满意的一条想原样再出一次：加 `--seed 123`（同样的 seed + 同样的文字 ≈ 同样的读法）。

---

## 给机看的说明（复制进 AGENTS.md / 系统提示）

> 你可以用语音回复。生成一条语音：
>
> `python3 /path/to/eleven-say/eleven_say.py "<要说的话>" -o /tmp/voice-<时间戳>.ogg`
>
> - 命令成功会在标准输出打印 ogg 文件路径，把这个文件当 **Telegram 语音（voice）** 发出去。
> - 想要某种语气，在文字最前面加英文方括号标签，例如 `[warm, gentle]`、`[sleepy, slow]`、`[excited]`。标签不会被念出来。
> - 写成说出口的话：短句，口语，不要 markdown、链接、列表。
> - 命令失败时退出码是 1，错误原因在标准错误里（例如缺钥匙、缺 ffmpeg、额度用完），把原因告诉主人，不要反复重试。
> - 每条语音都会消耗主人的 ElevenLabs 额度，别一口气发很多条做试验。

**Codex**：把上面这段放进项目的 `AGENTS.md`。
**OpenCode**：放进 `AGENTS.md`，或者写成一个 custom command / tool 指向同一条命令。
确认运行它的用户能读到 `.env` 里的环境变量（比如 bot 是 systemd 服务的话，在 service 里加 `EnvironmentFile=/path/to/eleven-say/.env`）。

---

## 所有配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ELEVENLABS_API_KEY` | — | 必填（或用下一项） |
| `ELEVENLABS_API_KEY_FILE` | — | 存钥匙的文件路径 |
| `ELEVENLABS_VOICE_ID` | — | 必填 |
| `ELEVENLABS_MODEL` | `eleven_v4` | 也可以命令行 `--model` |
| `ELEVENLABS_STABILITY` | 不发送 | 设了才发，0–1 |
| `ELEVENLABS_SIMILARITY` | 不发送 | 设了才发，0–1 |
| `ELEVENLABS_STYLE` | 不发送 | 设了才发，0–1 |
| `ELEVENLABS_SPEED_ZH` / `_EN` | 不发送 | 设了才发，0.7–1.2（也可 `--speed`），中文 / 英文分开 |
| `ELEVENLABS_TRIM_TAIL` | `auto` | auto = 只在 v3 上剪结尾；`on` / `off` 强制 |

## 常见问题

- **找不到 ffmpeg**：`sudo apt install ffmpeg`。
- **HTTP 401**：钥匙不对，或者没读到 `.env`（`echo $ELEVENLABS_VOICE_ID` 看看有没有值）。
- **HTTP 402 / quota**：本月额度用完了。
- **HTTP 422**：某个参数这个模型不认，去掉对应的 `ELEVENLABS_*` 再试。
- **结尾少了一个字**：设 `ELEVENLABS_TRIM_TAIL=off` 再试（v4 默认本来就不剪）。
- **出来是文件不是语音条**：发送时要用 Telegram 的 *voice* 接口（`sendVoice`），不是 `sendDocument`。

## 测试

```bash
pip install pytest
pytest -q
```

测试不联网、不需要钥匙、不花额度（API 是模拟的；音频相关的几条需要本机有 ffmpeg）。

## License

MIT
