"""设置页与顶栏导航测试（UI 重构：顶栏「记录/检索/更多功能」并排；设置页落地，§28/§29）。

- GET /settings：设置页渲染（通用/模型/检索/数据管理/安全/关于区块，服务端渲染生效值）；
- 模型配置/检索配置默认折叠（<details> 不带 open）；模型配置含对话整理/Ollama/联网搜索三个子块，
  联网搜索提供商固定 DeepSeek（下拉禁用），搜索模型可选（select + 自定义入口，§31）；
- 设置页手动保存流程（§30）：每项带「已修改但未保存」标注、自定义模型含确认/取消按钮、
  DEFAULT_LLM_MODEL（「（默认值）」标注用）、提示文案不再声称改完立即生效；
- 顶栏导航：「记录」「检索」「更多功能」三按钮并排一行，其余功能收进折叠菜单；
- 更名：「笔记」→「浏览笔记」、「问答」→「检索」、「回顾」→「兴趣回顾」；
- /static 静态资源带 Cache-Control: no-cache（浏览器每次重新校验，UI 改动即时生效）。
"""

from app import config


def test_settings_page_html(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    for fragment in (
        "设置",
        "关于",
        "TalkShelf — 个人知识速记工具",
        "当前版本 v",
        "每周总结用 AI 生成",
        "默认分类",
        "模型配置",
        "检索配置",
        "数据管理",
        "修改登录密码",
        'id="settings-form"',
        'id="weekly-llm"',  # 默认开启（服务端渲染 checked）
        # 模型配置三个子块 + 提供商下拉
        "对话/整理模型",
        "Ollama 模型（本地 Embedding）",
        "联网搜索模型",
        'id="llm-provider"',
        'id="llm-model"',
        'id="llm-model-custom"',
        'id="llm-model-custom-wrap"',
        'id="llm-custom-ok"',  # 自定义模型「确认」按钮
        'id="llm-custom-cancel"',  # 自定义模型「取消」按钮
        "__custom__",  # 可选模型下拉含「自定义模型…」入口
        'id="embed-model"',
        'id="search-provider"',
        'id="search-model"',
        'id="search-model-custom"',  # 联网搜索模型可选：自定义模型输入
        'id="search-model-custom-wrap"',
        'id="search-custom-ok"',  # 联网搜索模型「确认」按钮
        'id="search-custom-cancel"',  # 联网搜索模型「取消」按钮
        'id="search-model-list-msg"',
        'class="set-dirty"',  # 「已修改但未保存」标注
        'DEFAULT_LLM_MODEL = "deepseek-chat"',  # .env 默认模型（下拉「（默认值）」标注用）
        "修改后需点击「保存设置」才生效",  # 手动保存提示（不再声称立即生效）
    ):
        assert fragment in resp.text, f"设置页缺少: {fragment}"
    assert "改完立即生效" not in resp.text, "设置页不应再声称改完立即生效"
    assert "暂不支持修改" not in resp.text, "设置页不应再出现「暂不支持修改」（搜索模型已可选）"
    # 模型配置/检索配置默认折叠：<details> 不带 open 属性
    for tag in (
        '<details class="section" id="model-cfg">',
        '<details class="section" id="retrieval-cfg">',
    ):
        assert tag in resp.text, f"缺少折叠分区: {tag}"
    assert 'id="model-cfg" open' not in resp.text, "模型配置应默认折叠"
    assert 'id="retrieval-cfg" open' not in resp.text, "检索配置应默认折叠"
    # 联网搜索：提供商固定 DeepSeek（select 禁用），模型可选（select 不带 disabled）
    assert '<select id="search-provider" disabled>' in resp.text
    assert '<select id="search-model">' in resp.text
    # 登录未启用时密码区是提示而非表单
    assert "配置 AUTH_PASSWORD 后即可在此修改密码" in resp.text
    assert 'id="old-password"' not in resp.text
    # 默认分类下拉包含全部体系分类
    for cat in ("技术", "工作", "学习", "生活", "健康", "财务", "灵感", "其他"):
        assert f'value="{cat}"' in resp.text, f"默认分类下拉缺少: {cat}"


def test_settings_page_provider_options(client):
    """提供商下拉包含注册表全部选项，且当前生效提供商被选中（限定在 llm-provider 内检查，
    避免与联网搜索只读下拉里的 deepseek 混淆）。"""
    import re

    def llm_provider_html(html):
        m = re.search(r'id="llm-provider">(.*?)</select>', html, re.DOTALL)
        assert m, "设置页缺少 llm-provider 下拉"
        return m.group(1)

    sel = llm_provider_html(client.get("/settings").text)
    for pid in ("deepseek", "openai", "openrouter", "moonshot", "zhipu", "qwen", "siliconflow"):
        assert f'value="{pid}"' in sel, f"提供商下拉缺少: {pid}"
    assert 'value="deepseek" selected' in sel  # 默认 deepseek
    # 切换提供商后刷新页面，下拉选中新值
    client.put("/api/settings", json={"llm_provider": "moonshot"})
    sel = llm_provider_html(client.get("/settings").text)
    assert 'value="moonshot" selected' in sel
    assert 'value="deepseek" selected' not in sel


def test_settings_page_renders_effective_values(client, conn):
    """设置页服务端渲染当前生效值（DB 覆盖后刷新可见）。"""
    resp = client.put("/api/settings", json={"llm_model": "deepseek-reasoner", "weekly_llm": False})
    assert resp.status_code == 200
    html = client.get("/settings").text
    assert 'value="deepseek-reasoner"' in html
    # 每周总结开关已关闭：checkbox 不带 checked
    import re

    assert not re.search(r'id="weekly-llm"[^>]*checked', html)


def test_search_model_selectable(client):
    """联网搜索模型可选（§31）：PUT search_model 生效并渲染为下拉选中项；
    提供商仍固定 DeepSeek（下拉禁用仅一项）。"""
    import re

    # 默认：search-model 下拉渲染当前生效模型为选中项
    html = client.get("/settings").text
    m = re.search(r'id="search-model">(.*?)</select>', html, re.DOTALL)
    assert m, "设置页缺少 search-model 下拉"
    assert f'value="{config.SEARCH_MODEL}" selected' in m.group(1)
    # 保存新搜索模型后刷新：下拉选中新值，且 select 不禁用（模型可选）
    resp = client.put("/api/settings", json={"search_model": "deepseek-v4-flash"})
    assert resp.status_code == 200
    html = client.get("/settings").text
    m = re.search(r'id="search-model">(.*?)</select>', html, re.DOTALL)
    assert m, "设置页缺少 search-model 下拉"
    sel = m.group(1)
    assert 'value="deepseek-v4-flash" selected' in sel
    assert "disabled" not in sel
    # 提供商仍固定 DeepSeek：select 禁用、仅一项
    assert '<select id="search-provider" disabled>' in html
    # 自定义入口：下拉含「自定义模型…」
    assert "自定义模型…" in sel


def test_topbar_nav_restructure(client):
    """顶栏只留「记录」「检索」，其余功能收进「更多功能」折叠（默认收起）。"""
    resp = client.get("/")
    assert resp.status_code == 200
    # 新顶栏：两个主按钮 + 折叠菜单（含全部功能入口）
    for fragment in (
        ">记录<",
        ">检索<",
        ">更多功能<",
        ">浏览笔记<",
        ">兴趣回顾<",
        ">统计<",
        ">设置<",
        'href="/ask"',
        'href="/settings"',
        'id="more-drop" hidden',  # 折叠菜单默认收起
    ):
        assert fragment in resp.text, f"顶栏缺少: {fragment}"
    # 旧导航标签已消失（独立导航位被折叠菜单取代）
    assert ">笔记</a>" not in resp.text
    assert ">问答</a>" not in resp.text
    # 旧命名已更名
    assert "浏览全部笔记" not in resp.text
    assert ">回顾<" not in resp.text


def test_static_assets_revalidated(client):
    """静态资源必须带 no-cache：StaticFiles 默认无 Cache-Control，浏览器启发式缓存
    旧 app.js/style.css 会导致「更多功能」无反应、样式不生效（§26.4 根因）。"""
    for path in ("/static/app.js", "/static/style.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        cc = resp.headers.get("cache-control", "").lower()
        assert "no-cache" in cc, f"{path} 缺少 Cache-Control: no-cache（实际: {cc or '无'}）"
