# CosyVoice 内网合成服务

## 安装

在仓库根目录运行。建议使用 Python 3.10 或 3.11 的独立环境。

```bash
git submodule update --init --recursive
python -m venv .venv
source .venv/bin/activate
```

macOS 安装服务依赖后可使用 CPU 推理。

```bash
python -m pip install -r requirements-service.txt
```

Ubuntu 26 + RTX 5080 先装支持 Blackwell 和 CUDA 12.8 的 NVIDIA 驱动，再在环境中安装 CUDA 版 PyTorch。Torch 和 ONNX Runtime 需要匹配 CUDA 与 cuDNN。服务使用独立的依赖文件，上游的训练依赖继续使用根目录的 `requirements.txt`。

```bash
nvidia-smi
python -m pip install torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements-service.txt
python -c 'import torch, onnxruntime as ort; print(torch.cuda.get_device_name(0), torch.version.cuda, ort.get_available_providers())'
```

下载官方模型文件。已有完整模型目录时，设置 `COSYVOICE_MODEL_DIR` 指向该目录即可复用。

```bash
python -c "from modelscope import snapshot_download; snapshot_download('FunAudioLLM/Fun-CosyVoice3-0.5B-2512', local_dir='pretrained_models/Fun-CosyVoice3-0.5B-2512')"
```

服务和本地脚本都加载该目录中的 `llm.rl.pt`、`flow.pt`、`hift.pt` 和配套文件。启动日志会打印实际使用的权重、推理设备和音源编码器的运行设备。

## 音源库

在 `static/audio_prompt/voices.xlsx` 的第一张表中维护音源。第一行为表头。示例中的文本应替换成录音实际说出的内容。

| 文本 | 路径 | 语言 |
| --- | --- | --- |
| 今天我做了两组深蹲。 | zh/prompt-01.wav | 中文 |
| I did two sets of squats today. | en/prompt-01.wav | 英文 |

路径相对于 `static/audio_prompt/`，也作为 `voice_id`。语言可填 `zh`、`en`、`中文` 或 `英文`。参考音频采样率至少为 16 kHz，时长不超过 30 秒。服务和脚本启动时按表格顺序预计算音源特征。更新音源表后重启服务即可生效。

上传的 `prompt_audio` 和 `prompt_text` 优先，其次用 `voice_id`。未指定音源时，单条从目标语言的音源随机选取，批量按表格行序循环。没有可用本地音源时，可以上传参考音频和对应文本。合成文本需用目标语言填写。

## 启动与内网下载

本机直接启动并由 FastAPI 返回文件。

```bash
python -m uvicorn cosyvoice.service.api:app --host 127.0.0.1 --port 8000
```

在生产机上将 [cosyvoice.conf](../deploy/nginx/cosyvoice.conf) 放入 Nginx 的站点配置目录。将示例域名和允许访问的网段替换为实际内网配置。确保服务进程可写批量目录，Nginx 可读取 ZIP 及所在目录。配置中的 `alias` 与 `COSYVOICE_BATCH_DIR` 应指向同一目录。

```bash
export COSYVOICE_BATCH_DIR=/var/lib/cosyvoice/batches
export COSYVOICE_NGINX_DOWNLOADS=1
export COSYVOICE_REQUIRE_CUDA=1
python -m uvicorn cosyvoice.service.api:app --host 127.0.0.1 --port 8000 --workers 1
```

在生产机运行 `sudo nginx -t` 并重新加载 Nginx。调用方使用 `http://tts.intra.example/api/v1/batches/{task_id}/download`，API 核对任务状态后由 Nginx 发送文件。音源和任务输入文件只供服务进程使用。

服务使用一个模型实例和一个批量队列。任务状态和 ZIP 保存在磁盘上。服务重启后会重新执行排队中和未完成的任务，磁盘上的 ZIP 仍可下载。

## 调用示例

`language` 支持 `zh` 和 `en`。以下示例默认直接调用本机服务，部署后将 `BASE` 换成 Nginx 的内网地址。

```bash
BASE=http://127.0.0.1:8000
curl -fsS "$BASE/api/v1/voices"

curl -fsS "$BASE/api/v1/synthesize" \
  -F 'language=zh' -F 'text=今天做了两组深蹲。' \
  -o audio.wav

curl -fsS "$BASE/api/v1/synthesize" \
  -F 'language=en' -F 'text=I did two sets of squats.' \
  -F 'prompt_audio=@reference.wav' -F 'prompt_text=录音中的实际文本' \
  -o audio-en.wav

curl -fsS "$BASE/api/v1/batches" -F 'language=zh' -F 'table=@sentences.xlsx'
```

批量支持 `.xlsx` 和逗号分隔的 UTF-8 CSV。每张工作表或 CSV 的第一行须包含 `文本` 或 `测试语句`，也可提供 `音频编号`。Excel 按工作表和行顺序读取，API 的 ZIP 使用平铺文件名。编号缺失时按整个请求的有效行位置生成六位编号。需要保留显式编号的前导零时，请将该单元格设为文本。

提交成功返回 HTTP 202、`task_id` 和 `status_url`。在 `status_url` 查询 `queued`、`running`、`succeeded` 或 `failed`。任务成功且 ZIP 可用时，响应包含 `download_url`。两个 URL 都是相对于服务地址的路径。

```bash
TASK_ID=替换为提交返回的任务ID
curl -fsS "$BASE/api/v1/batches/$TASK_ID"
curl -fsS "$BASE/api/v1/batches/$TASK_ID/download" -o batch.zip
```

调用方确认 ZIP 已下载并保存后，用同一个 URL 发起 `DELETE`。

```bash
curl -fsS -X DELETE "$BASE/api/v1/batches/$TASK_ID/download"
```

删除和重复删除均返回 HTTP 204。任务状态仍为 `succeeded`，查询结果省略 `download_url`，旧下载地址返回 404。尚未成功完成的任务返回 409，未知任务返回 404。ZIP 保留到调用方删除或运维清理。

## 从本地 Excel 输出 WAV 和标注

编辑 [synthesize_excel.py](../synthesize_excel.py) 顶部的 `EXCEL_PATH`、`LANGUAGE`、`OUTPUT_DIR` 和 `SHEETS`。例如将 `SHEETS` 设为 `["常规用例", "动作修改"]`。留空时按工作簿顺序处理所有工作表。

```bash
python synthesize_excel.py
```

脚本读取每张表的 `测试语句` 列。每张工作表从 `000001.wav` 开始计数，同语言音源在本次运行中持续轮换。音频和 UTF-8 标注分别生成在 `audio/<工作表>/000001.wav` 和 `labels/<工作表>/000001.txt`，标注保留原单元格文本。WAV 为双声道 PCM，采样率取自模型配置。
