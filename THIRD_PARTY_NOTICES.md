# Third-party notices

This project reuses or is informed by the following third-party works. It does
**not** bundle any of their source code as-is; references below are for
attribution and transparency.

## DeepSeek Harness (MIT)

The `web_search` / `web_fetch` functionality in `backend/app/web_search.py` and
`backend/app/fetch.py` follows the design and output conventions of the
[DeepSeek Harness](https://github.com/deepseek-ai/harness) web-search and
web-fetch tools (the `web_search_20250305` server tool over the Anthropic-compatible
Messages API, the structured `web_search_tool_result` parsing, the
`Fetched <url> (HTTP <n>)` presentation header, and the `- [title](url)` source
format). DeepSeek Harness is MIT Licensed:

```
MIT License
Copyright (c) 2026 DeepSeek
```

## Second-brain reference project (AGPL-3.0) — idea reference only

The local Ollama embedding / vectorization *approach* in
`backend/app/embedding.py` is inspired by the "second brain" project
[`同类产品`](https://github.com/同类产品/同类产品), which is licensed under
**AGPL-3.0**. No code from 同类产品 is copied into this project; the design is
an independent reimplementation (different endpoint, model, storage, and
library choices). This notice does not impose AGPL obligations on this project.
