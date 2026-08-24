"""检索回归测试集：种子笔记数据（设计文档 §12 M2 / §15.3 #1）。

用途：scripts/eval_retrieval.py 把本数据写入临时库并跑真实检索评测；
防改代码时检索悄悄退化。评测集采用「问题 + 期望命中笔记」标注格式，
评测问题见 rag_eval_set.json。

本文件只定义笔记数据；评测逻辑在 scripts/eval_retrieval.py。
"""

from __future__ import annotations

# 每条：(kind, category, title, summary, content, raw, source_url)
# content 为整理稿正文（Tier 1 检索主体）；raw 为用户原话（保证永远可检索）。
SEED_NOTES: list[tuple[str, str, str, str, str, str, str | None]] = [
    (
        "note",
        "技术",
        "nginx 上传大文件限制",
        "nginx client_max_body_size 默认 1M，上传超过 1M 的文件被 413 拒绝，需显式调大。",
        "nginx 的 client_max_body_size 默认 1M，上传大文件会直接 413。\n解决：http/server/location 块里加 client_max_body_size 100m，然后 reload。\n注意：反向代理场景还要检查后端（如 uwsgi/php）的上传限制。",
        "今天发现 nginx 上传大文件报 413，查了下是 client_max_body_size 默认 1M 的锅",
        None,
    ),
    (
        "note",
        "技术",
        "frp 隧道必须开 TLS",
        "frp 隧道默认明文，VPS 到家里的流量会被看光，两端都要开 transport.tls.enable。",
        "frp 的 frps/frpc 默认不加密隧道流量。\n安全要求：两端配置文件都加 transport.tls.enable = true。\n控制端口 7000 也要配 token 认证。",
        "frp 配置：transport tls enable 必须开，否则密码在公网裸奔",
        None,
    ),
    (
        "note",
        "技术",
        "bge-m3 embedding 模型",
        "bge-m3 是智源的多语言 embedding 模型，1024 维，中文效果好，本地 Ollama 免费跑。",
        "bge-m3（BAAI）多语言 embedding 模型：\n- 1024 维向量\n- 中文/英文/多语言混合检索效果好\n- 本地 Ollama 可跑，个人笔记检索免费且隐私好\n- 内存占用约 1~2GB 常驻",
        "embedding 选型：本地 Ollama bge-m3，中文语义检索靠谱",
        None,
    ),
    (
        "note",
        "技术",
        "SQLite FTS5 trigram 中文检索",
        "SQLite FTS5 用 trigram 分词才能搜中文；查询词少于 3 字符匹配不到。",
        'SQLite FTS5 默认分词器 unicode61 不切中文，一段中文是一个 token。\ntrigram 按 3 字符子串建索引，中文可用，但查询词必须 >= 3 字符。\n2 字词（如"上传"）需要 LIKE 兜底。要求 SQLite >= 3.34。',
        "sqlite fts5 trigram 中文全文检索的坑：短词搜不到",
        None,
    ),
    (
        "note",
        "技术",
        "DeepSeek 原生联网搜索",
        "DeepSeek Anthropic 兼容端点支持 web_search_20250305 工具，服务端执行搜索返回结构化结果。",
        "DeepSeek 原生联网搜索：\n- 端点 POST https://api.deepseek.com/anthropic/v1/messages\n- 声明 web_search_20250305 工具，max_uses 默认 5\n- 返回 web_search_tool_result 结构化块（url/title/page_age）\n- 摘录不可用（citations 属性实测没有），snippet 省略",
        "DeepSeek 居然有原生联网搜索，Anthropic 兼容端点，复用同一把 key",
        None,
    ),
    (
        "note",
        "技术",
        "uv 管理 Python 版本",
        "uv 可以托管安装 Python 解释器并管理项目 venv，Windows 上 PATH 的 python 可能是假存根。",
        "uv（Rust 写的 Python 包管理器）：\n- uv python install 3.14 安装托管解释器\n- 项目用 uv venv 建虚拟环境\n- Windows 上 PATH 里的 python 可能是 0 字节 Store 存根\n- 检查工具是否真实存在要先验证探测方法本身",
        "uv 托管 Python 3.14，Windows 的 python 命令是假存根要小心",
        None,
    ),
    (
        "note",
        "技术",
        "Windows 沙箱吞掉外部程序输出",
        "受限沙箱里外部 exe 无输出不一定是没装，可能是输出被静默拦截；空输出不等于 not found。",
        "排查工具是否安装的经验：\n- 沙箱模式下外部 exe（where/git/python）可能无输出，没有错误标记\n- 先跑已知能工作的命令验证探测本身\n- 0 字节的 WindowsApps 文件是 Store 别名不是真程序\n- 注册表 HKCU\\SOFTWARE\\Python\\PythonCore 能发现真实解释器",
        "沙箱吞输出，探测工具装没装要看注册表，空输出不等于没有",
        None,
    ),
    (
        "note",
        "技术",
        "Python os.mkdir 0o700 目录 ACL 陷阱",
        "Windows 上 os.mkdir(mode=0o700) 会生成连创建者都访问不了的目录，测试临时目录要用默认 mode。",
        "Windows + Python：\n- os.mkdir(p, 0o700) 的 mode 被映射成拒绝访问的 ACL\n- tempfile.mkdtemp 内部也用 0o700，踩同一个坑\n- 测试临时目录要自己建：Path.mkdir()（默认 0o777）\n- 清理要短重试 + 可见失败，ignore_errors 会静默吞掉删除失败",
        "pytest 临时目录权限坑：os.mkdir 0o700 在 Windows 生成废目录",
        None,
    ),
    (
        "note",
        "工作",
        "周报模板",
        "周报三段式：本周完成 / 遇到的问题与解决 / 下周计划，数据先行结论殿后。",
        "个人周报模板：\n1. 本周完成（列事实，带数据）\n2. 遇到的问题与解决（含卡点）\n3. 下周计划（按优先级）\n语气：结论先行，不写流水账。",
        "周报怎么写：模板存一份，每周往里填",
        None,
    ),
    (
        "note",
        "工作",
        "会议纪要要点",
        "会议纪要记四件事：决策、行动项（谁/何时）、遗留问题、下次议程。",
        "会议纪要只记四类内容：\n- 决策及理由\n- 行动项：责任人 + 截止时间\n- 遗留问题\n- 下次会议议程\n不记流水账。",
        "开会纪要：决策、行动项带责任人、遗留问题",
        None,
    ),
    (
        "note",
        "学习",
        "《原子习惯》核心观点",
        "习惯养成：忘记目标专注系统；环境设计比意志力可靠；2 分钟规则降低启动门槛。",
        "《原子习惯》James Clear：\n- 人会跌落到系统的水平（You do not rise to the level of your goals, you fall to the level of your systems）\n- 改变身份认同：我不是要读书，我是读者\n- 环境设计：想多喝水就把水杯放桌上\n- 2 分钟规则：把习惯缩到 2 分钟内先启动",
        "原子习惯：立 flag 没用，改系统改环境才有用",
        None,
    ),
    (
        "note",
        "学习",
        "费曼学习法",
        "费曼学习法四步：学概念 → 用大白话讲给外行 → 卡壳处回炉 → 简化类比。",
        "费曼学习法：\n1. 选概念，读完\n2. 假装讲给 12 岁小孩，用大白话写出来\n3. 讲不清楚的地方就是没懂，回炉重读\n4. 用类比简化，去掉术语",
        "学东西最快的办法：费曼学习法，讲不出来就是没懂",
        None,
    ),
    (
        "note",
        "学习",
        "Anki 间隔重复",
        "Anki 用间隔重复对抗遗忘曲线，卡片要原子化，新卡每天 20 张以内。",
        "Anki 使用要点：\n- 间隔重复（SM-2 算法）对抗遗忘曲线\n- 卡片原子化：一张卡一个问题\n- 新卡上限 20 张/天，避免堆积\n- 每天复习优先于添加新卡",
        "anki 间隔重复背单词：卡片要小，每天 20 张新卡够了",
        None,
    ),
    (
        "note",
        "生活",
        "搬家 checklist",
        "搬家清单：水电燃气过户、宽带迁移、快递地址变更、门锁换芯、大件搬运预约。",
        "搬家 checklist：\n- 水电燃气过户/结清\n- 宽带迁移（提前 7 天预约）\n- 快递默认地址修改\n- 换门锁芯\n- 大件（冰箱洗衣机）提前量尺寸预约搬运\n- 宠物/植物单独安排",
        "下月搬家，先列个 checklist 免得漏事",
        None,
    ),
    (
        "note",
        "生活",
        "红烧肉做法",
        "红烧肉：五花肉焯水 → 炒糖色 → 加料酒生抽老抽 → 小火炖 1 小时 → 大火收汁。",
        "红烧肉（家常版）：\n1. 五花肉切块冷水下锅焯水\n2. 小火炒糖色（冰糖）\n3. 下肉裹色，加料酒、生抽、老抽、姜片\n4. 加热水没过肉，小火炖 1 小时\n5. 大火收汁\n关键：糖色别炒糊，炖够时间才软烂。",
        "今晚做红烧肉：焯水、炒糖色、炖一小时",
        None,
    ),
    (
        "note",
        "健康",
        "久坐提醒方案",
        "每小时起身 5 分钟：番茄钟 + 站立办公 + 定时提醒喝水。",
        "久坐缓解：\n- 番茄钟 45/15：每 45 分钟起身 5 分钟\n- 升降桌：坐着 1 小时换站立 30 分钟\n- 喝水提醒每小时一次（顺便起来接水）\n- 屏幕抬高到视线水平，避免低头",
        "久坐腰疼，试试番茄钟每小时起来动一动",
        None,
    ),
    (
        "note",
        "健康",
        "跑步入门计划",
        "跑步入门：Couch to 5K 计划，跑走结合，每周 3 次，心率控制在能说话的程度。",
        "跑步入门（Couch to 5K）：\n- 第 1 周：跑 1 分钟走 2 分钟 × 8 组\n- 每周 3 次，隔天跑\n- 心率：能边跑边说话\n- 跑鞋买大半码\n- 8 周后能连续跑 30 分钟",
        "想开始跑步：C25K 计划入门，跑走结合不受伤",
        None,
    ),
    (
        "note",
        "财务",
        "公积金提取条件",
        "租房提取公积金：无房证明 + 租房合同，每年可提一次，线上办理。",
        "公积金租房提取：\n- 条件：名下无房 + 真实租房\n- 材料：无房证明、租房合同\n- 频次：每年一次\n- 渠道：当地公积金 App 线上办，钱 3 个工作日内到账",
        "公积金租房提取流程查了：线上办，一年一次",
        None,
    ),
    (
        "note",
        "财务",
        "基金定投纪律",
        "定投纪律：固定比例、低估多买高估少买、不预测短期涨跌、每季度再平衡。",
        "基金定投纪律：\n- 固定金额/固定频率（如每月发薪日）\n- 估值低多买，估值高少买\n- 不看短期涨跌，不追热点\n- 每季度做一次再平衡\n- 止盈不止损（指数基金）",
        "定投纪律记下来：低估多买，高估少买，季度再平衡",
        None,
    ),
    (
        "note",
        "灵感",
        "个人知识库产品点子",
        "记录零负担 + AI 整理 + 对话式找回，本地优先，移动端为主。",
        "产品点子：个人知识速记工具\n- 记录零负担：口语直接说，AI 整理\n- 找回用对话：问一句就能找到\n- 本地优先：数据在自己机器上\n- 移动端为主：刷手机时随手记\n- 每周回顾兴趣清单：去做/留着/放弃",
        "想到一个点子：记录零负担、查找用对话的个人知识库",
        None,
    ),
    (
        "note",
        "灵感",
        "极简任务管理法",
        "任务管理极简方案：纸笔 + 每日三件事，不装任何 App。",
        "极简任务管理：\n- 每天只列 3 件最重要的事（MIT）\n- 纸笔记录，不装 App\n- 完成就打勾，没完成不补记\n- 周日晚复盘本周 MIT 完成率",
        "灵感：任务管理回归纸笔，每天三件事够了",
        None,
    ),
    (
        "note",
        "其他",
        "垃圾分类速查",
        "上海垃圾分类速查：厨余垃圾（湿垃圾）扔棕色桶，可回收物扔蓝色桶，有害垃圾红色，其他黑色。",
        "上海垃圾分类速查：\n- 湿垃圾（厨余）：剩饭剩菜、果皮 → 棕色桶\n- 可回收物：纸箱、塑料瓶、易拉罐 → 蓝色桶\n- 有害垃圾：电池、灯管、过期药 → 红色桶\n- 干垃圾（其他）：纸巾、烟蒂 → 黑色桶",
        "垃圾分类总是记混，整理个速查表",
        None,
    ),
    (
        "note",
        "技术",
        "Caddy 自动 HTTPS 配置",
        "Caddy 一行 reverse_proxy 自动申请续期 HTTPS 证书，反向代理首选。",
        "Caddy 用法：\n- Caddyfile：域名 + reverse_proxy 127.0.0.1:8000 即自动 HTTPS\n- 证书自动申请/续期（Let's Encrypt）\n- 比 nginx 配证书省事太多\n- 适合个人服务反向代理",
        "caddy 反向代理自动 HTTPS，比 nginx 省心",
        None,
    ),
    (
        "note",
        "兴趣",
        "学做手冲咖啡",
        "手冲咖啡入门：粉水比 1:15、水温 92 度、闷蒸 30 秒、总时长 2 分半。",
        "手冲咖啡入门参数：\n- 粉水比 1:15（15g 粉 225g 水）\n- 水温 92℃\n- 闷蒸 30 秒\n- 三段注水，总时长 2 分 30 秒\n- 磨豆：中细研磨（白砂糖粗细）",
        "想试试手冲咖啡，先记个入门参数",
        None,
    ),
]

# 材料层种子（Tier 2 兜底场景）：剪藏笔记的抓取正文里才有、整理稿里没有的词
SEED_MATERIALS: list[tuple[str, str, str]] = [
    # (kind, url, text) —— 挂在对应笔记上（按 raw 前缀匹配）
    (
        "fetched_page",
        "https://zhidx.com/p/bge-m3",
        (
            "bge-m3 由北京智源人工智能研究院（BAAI）发布，支持 100 多种语言的检索、分类、相似度任务，"
            "采用自学习多语言表示训练，在 MTEB 多语言排行榜位居前列。"
        ),
    ),
    (
        "search_result",
        "https://www.deepseek.com/zh/news/native-search",
        "DeepSeek 官方 API 上线原生联网搜索能力，通过 Anthropic 兼容接口的 web_search_20250305 工具声明启用。",
    ),
]

# 材料归属：SEED_MATERIALS 的索引 → SEED_NOTES 的索引
MATERIAL_OWNERS: list[int] = [2, 4]  # bge-m3 笔记 / DeepSeek 搜索笔记
