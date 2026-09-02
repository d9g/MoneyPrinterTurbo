"""UI 文案翻译函数的注册点。

webui/Main.py 里定义的 tr() 依赖 st.session_state["ui_language"]，而
webui/mv_panel.py 顶层的 @st.dialog(tr("...")) 装饰器在**模块导入时**就会
求值。如果 mv_panel 直接 import Main 的 tr，就会形成循环导入。

这里的做法是把「翻译函数」做成一个可延迟绑定的槽：
    1. Main.py 定义完 tr 之后，调用 set_translator(tr) 注册；
    2. 再 import mv_panel —— 此时装饰器求值时拿到的已经是真正的 tr。

这样既不需要修改上游 Main.py 里的 tr 定义，也不会让对话框标题退化成
原始 key（导入期也能正确翻译）。
"""

_translator = None


def set_translator(fn) -> None:
    """注册实际的翻译函数（由 webui/Main.py 调用一次）。"""
    global _translator
    _translator = fn


def tr(key: str) -> str:
    """翻译一个 UI 文案 key；未注册翻译函数时原样返回 key。"""
    if _translator is None:
        return key
    return _translator(key)
