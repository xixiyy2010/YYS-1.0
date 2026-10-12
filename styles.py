# styles.py

# 基础颜色定义
LIGHT_COLORS = {
    'bg': '#F5F0E8',
    'text': '#5C5348',
    'input_bg': '#FDFBF7',
    'input_border': '#E2D9CC',
    'tab_bg': '#E8E0D3',
    'tab_selected': '#FDFBF7',
    'tab_text': '#6A5F52',
    'btn_bg': '#EFE7DA',
    'btn_border': '#DCD2C2',
    'btn_hover': '#E8DECF',
    'btn_pressed': '#DFD3C0',
}

DARK_COLORS = {
    'bg': '#2B2723',
    'text': '#D6CCC2',
    'input_bg': '#332F2A',
    'input_border': '#4A443D',
    'tab_bg': '#3B3630',
    'tab_selected': '#423C35',
    'tab_text': '#D6CCC2',
    'btn_bg': '#423C35',
    'btn_border': '#4A443D',
    'btn_hover': '#4E463E',
    'btn_pressed': '#3A352F',
}

def _build_style(colors):
    """根据颜色字典生成完整的 QSS 样式表"""
    return f"""
QWidget {{
    background-color: {colors['bg']};
    color: {colors['text']};
}}
QPlainTextEdit {{
    background-color: {colors['input_bg']};
    color: {colors['text']};
    border: 1px solid {colors['input_border']};
    border-radius: 10px;
    padding: 4px;
}}
QLineEdit {{
    background-color: {colors['input_bg']};
    color: {colors['text']};
    border: 1px solid {colors['input_border']};
    border-radius: 8px;
    padding: 2px 6px;
}}
QLabel {{
    color: {colors['text']};
    background: transparent;
}}
QTabWidget::pane {{
    border: none;
    background: {colors['bg']};
}}
QTabBar::tab {{
    background: {colors['tab_bg']};
    color: {colors['tab_text']};
    border-radius: 8px;
    padding: 4px 10px;
    margin: 2px;
}}
QTabBar::tab:selected {{
    background: {colors['tab_selected']};
    color: {colors['tab_text']};
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QPushButton {{
    background: {colors['btn_bg']};
    color: {colors['text']};
    border: 1px solid {colors['btn_border']};
    border-radius: 10px;
    padding: 4px 10px;
}}
QPushButton:hover {{
    background: {colors['btn_hover']};
}}
QPushButton:pressed {{
    background: {colors['btn_pressed']};
}}
"""

LIGHT_STYLE = _build_style(LIGHT_COLORS)
DARK_STYLE = _build_style(DARK_COLORS)

def get_menu_style(is_dark: bool) -> str:
    if is_dark:
        return """
        QMenu {
            background-color: #2B2723;
            border: 1px solid #4A443D;
            border-radius: 6px;
            color: #D6CCC2;
        }
        QMenu::item {
            padding: 4px 8px;
            color: #D6CCC2;
        }
        QMenu::item:selected {
            background-color: #4E463E;
        }
        """
    else:
        return """
        QMenu {
            background-color: #FDFBF7;
            border: 1px solid #DCD2C2;
            border-radius: 6px;
            color: #5C5348;
        }
        QMenu::item {
            padding: 4px 8px;
            color: #5C5348;
        }
        QMenu::item:selected {
            background-color: #E8DECF;
        }
        """

def get_line_edit_style(is_dark: bool) -> str:
    if is_dark:
        return """
        QLineEdit {
            background-color: #332F2A;
            color: #D6CCC2;
            border: 1px solid #4A443D;
            border-radius: 6px;
            padding: 2px 6px;
        }
        """
    else:
        return """
        QLineEdit {
            background-color: #FDFBF7;
            color: #5C5348;
            border: 1px solid #DCD2C2;
            border-radius: 6px;
            padding: 2px 6px;
        }
        """