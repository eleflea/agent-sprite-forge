# 第三方 OpenAI / xAI 接口兼容性测试

测试日期：2026-11-12（Australia/Sydney）。测试使用项目的 `uv` 虚拟环境和 `.env.local` 中的两个提供商地址；密钥没有写入报告、日志或仓库。

## 结论

这两个地址都可以在本项目中工作，但它们不是“完全等同于官方响应”的实现：

| 路由 | 地址 | 模型 | 结果 |
|---|---|---|---|
| OpenAI Images | `https://api.klong.lat/v1/images/generations` | `gpt-image-2.6-sunburst` | 成功，HTTP 200；返回 `data[0].url`，不是 `b64_json`；URL 下载后是有效 PNG |
| xAI Video | `https://qa.dashun9527.com/videos/generations` | `grok-imagine-video-1.5` | 成功提交、轮询和下载；返回 `request_id`，完成结果的 `video.url` 是相对路径；下载需要 Bearer 鉴权 |

Klong 文档明确说明 Images 接口兼容 OpenAI，`/v1/images/generations` 和 `/v1/images/edits` 可以返回 `data[].url` 或 `data[].b64_json`。实际的 `gpt-image-2.6-sunburst` 请求返回 URL。Klong 文档还说明 `background` 不提供透明背景控制，因此该模型在能力表中会拒绝 `--transparent`。

xAI 官方文档记录的标准流程是 `POST /v1/videos/generations`、轮询 `GET /v1/videos/{request_id}`，然后及时下载临时 URL；`grok-imagine-video-1.5` 支持 1–15 秒、480p/720p/1080p（参考图流程最高 720p）和 `generate_audio: false`。第三方地址接受相同的 JSON 字段，但把完成媒体地址改成了相对路径，所以适配器会把它解析到配置的 API 主机，并为该同主机下载发送 Bearer。

OpenAI 官方模型页也确认 `gpt-image-2.6-sunburst` 提供 Image generation 和 Image edit 端点；第三方 Klong 的模型列表实际列出了该模型。模型的价格和能力限制仍以第三方账户/分组为准，项目不会替它虚构价格；本次 OpenAI 2.6 结果在账本中标记为 `price unknown`。

## 实际调用记录

- OpenAI：`1024x1024`、`n=1`、PNG、无参考图；响应约 20 秒，返回 1254×1254 PNG。项目完整命令成功，`generated.png` 已写入 `outputs/provider-compat-2026-11-12/asf-openai/`。
- xAI：`grok-imagine-video-1.5`、1 秒、480p、`generate_audio=false`、使用 `src/forest-shrine-base.png`；提交后异步完成，提供商报告实际费用 `$0.09`（`0.08` 输出秒 + `0.01` 输入图）。项目完整命令先保留了中断 job，再用 `resume` 完成，没有重复 POST；`generated.mp4` 已写入 `outputs/provider-compat-2026-11-12/asf-xai/`。
- xAI 生成的 MP4 下载响应为有效 `ftyp` 容器；项目按现有流程保留原始下载并发布游戏用的无音频文件。该次第三方响应实际没有音轨，因此 `removed=0`。
- GIF 专项：对 `gpt-image-2.6-sunburst` 发送一次 `output_format=gif`，提示绿色史莱姆四帧待机弹跳。HTTP 200；Klong 返回 URL，下载后确认是 GIF89a，`623×623`、4 帧、每帧 180 ms、`loop=0`（无限循环）。文件位于 `outputs/provider-compat-2026-11-12/gpt26-gif-test/generated.gif`。输出使用洋红色背景而非透明 alpha，且包含投影/装饰飞溅，进入 Godot 前应做抠色和裁切。
- 复杂 GIF 专项：对同一模型发送一只橙色狐狸的 6 帧拔剑挥砍循环。HTTP 200；下载结果是 GIF89a，`414×621`、6 帧、每帧 180 ms、`loop=0`，接触表显示角色轮廓、围巾、武器和挥砍弧线在各帧保持一致。文件位于 `outputs/provider-compat-2026-11-12/gpt26-complex-gif/generated.gif`，接触表位于同目录的 `contact-sheet.png`。

## 已做的工程修改

1. `OPENAI_API_BASE_URL` 和 `XAI_API_BASE_URL` 会覆盖各自官方默认 API base；job receipt 会记录主机和完整 endpoint。命令行 `--base-url` 的显式确认规则仍保留。
2. OpenAI 适配器同时接受 `data[].b64_json` 和 HTTPS `data[].url`，并立即下载 URL。
3. xAI 适配器支持相对 `video.url`，按 job 的 API base 解析；同主机下载携带 Bearer，外部临时 URL 不携带密钥。
4. 增加了 URL 响应、环境 base 覆盖、相对视频 URL 和鉴权下载的离线回归测试。

## 重跑方式

```bash
UV_CACHE_DIR=/tmp/uv-cache uv venv --allow-existing .venv
UV_CACHE_DIR=/tmp/uv-cache uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt -r requirements-provider-compat.txt

set -a; . ./.env.local; set +a
.venv/bin/python -m pytest -q tests/test_media_providers_openai.py tests/test_media_providers_xai.py tests/test_generate2dmedia.py

# 先查看计划（不发送请求）
.venv/bin/python skills/generate2dmedia/scripts/generate_media.py image \
  --provider openai --model gpt-image-2.6-sunburst \
  --prompt-file outputs/provider-compat-2026-11-12/prompts/slime.txt \
  --size 1024x1024 --out-dir outputs/compat-openai --project-dir .

# 运行完整兼容性矩阵（会产生多次付费请求；每个 case 只提交一次）
uv run --env-file .env.local --no-project --python .venv/bin/python \
  tools/provider_compat.py --execute
```

执行付费请求时再追加 `--execute`，视频中断后用 `generate_media.py resume --job <job.json>`，不要重新提交同一个视频任务。

## 参考文档

- [Klong GPT Image 文档](https://docs.klong.lat/docs/image/gpt-image-2)
- [OpenAI GPT-Image-2.6 Sunburst 模型页](https://developers.openai.com/api/docs/models/gpt-image-2.6-sunburst)
- [xAI Video Generation](https://docs.x.ai/developers/model-capabilities/video/generation)
- [xAI Reference-to-Video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)
- [xAI Image Generation](https://docs.x.ai/developers/model-capabilities/images/generation)
