"""设置页与顶栏导航测试（UI 重构：顶栏「记录/检索/更多功能」并排，设置页占位）。

- GET /settings：占位页渲染（关于/版本信息），与其它页面一样受登录保护；
- 顶栏导航：「记录」「检索」「更多功能」三按钮并排一行，其余功能收进折叠菜单；
- 更名：「笔记」→「浏览笔记」、「问答」→「检索」、「回顾」→「兴趣回顾」；
- /static 静态资源带 Cache-Control: no-cache（浏览器每次重新校验，UI 改动即时生效）。
"""


def test_settings_page_html(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    for fragment in ("设置", "关于", "正在规划中", "note-brain — 个人知识速记工具", "当前版本 v"):
        assert fragment in resp.text, f"设置页缺少: {fragment}"


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
