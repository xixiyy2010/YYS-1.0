# plugins/__init__.py
"""
plugins —— 业务层根目录

每个子文件夹代表一个游戏。游戏自带的 config.json 声明：
    - name           显示名（UI 上展示用）
    - boss           老板类的 import 路径
    - need           需要的工具清单
    - tasks          任务清单（UI 上列按钮用）

加新游戏 = 新文件夹 + 新 config.json，主架构代码不改。
"""