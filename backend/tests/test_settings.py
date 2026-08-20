"""设置页与顶栏导航测试（UI 重构：顶栏「记录/检索/更多功能」并排；设置页落地，§28）。

- GET /settings：设置页渲染（通用/模型/检索/数据管理/安全/关于区块，服务端渲染生效值）；
- 顶栏导航：「记录」「检索」「更多功能」三按钮并排一行，其余功能收进折叠菜单；
- 更名：「笔记」→「浏览笔记」、「问答」→「检索」、「回顾」→「兴趣回顾」；
- /static 静态资源带 Cache-Control: no-cache（浏览器每次重新校验，UI 改动即时生效）。
"""


def test_settings_page_html(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    for fragment in (
        "设置",
        "关于",
        "note-brain — 个人知识速记工具",
        "当前版本 v",
        "每周总结用 AI 生成",
        "默认分类",
        "模型配置",
        "检索配置",
        "数据管理",
        "修改登录密码",
        'id="settings-form"',
        'id="weekly-llm"',  # 默认开启（服务端渲染 checked）
    ):
        assert fragment in resp.text, f"设置页缺少: {fragment}"
    # 登录未启用时密码区是提示而非表单
    assert "配置 AUTH_PASSWORD 后即可在此修改密码" in resp.text
    assert 'id="old-password"' not in resp.text
    # 默认分类下拉包含全部体系分类
    for cat in ("技术", "工作", "学习", "生活", "健康", "财务", "灵感", "其他"):
        assert f'value="{cat}"' in resp.text, f"默认分类下拉缺少: {cat}"


def test_settings_page_renders_effective_values(client, conn):
    """设置页服务端渲染当前生效值（DB 覆盖后刷新可见）。"""
    resp = client.put("/api/settings", json={"llm_model": "deepseek-reasoner", "weekly_llm": False})
    assert resp.status_code == 200
    html = client.get("/settings").text
    assert 'value="deepseek-reasoner"' in html
    # 每周总结开关已关闭：checkbox 不带 checked
    import re

    assert not re.search(r'id="weekly-llm"[^>]*checked', html)


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
