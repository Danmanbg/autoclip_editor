# models/

Downloaded automatically - nothing here is committed.

| Folder | What | Size | Licence | How it gets here |
| --- | --- | --- | --- | --- |
| `tiny/` `base/` `small/` ... | [faster-whisper](https://huggingface.co/Systran) speech models | 75 MB - 1.5 GB | MIT | the first time a feature needs one |
| `llm/` | [Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) (Q4_K_M) for the AI title ideas | 1.1 GB | Apache-2.0 | `py -3 run.py --get-ai-model` (optional) |

Downloads use parallel range requests and are checked against the published
SHA-256 before they are used.
